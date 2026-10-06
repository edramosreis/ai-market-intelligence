"""HTTP transport for shared reader queries and the explicitly enabled agent."""

import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated, cast

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import OpenAI
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from market_intelligence.agent.models import (
    AgentQuestion,
    AgentResult,
    AgentUnavailableError,
    LimitationCode,
)
from market_intelligence.agent.runner import AgentRunner
from market_intelligence.agent.tools import MarketTools
from market_intelligence.config import AgentSettings, ApiSettings, DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.ingestion.models import parse_instant, utc_now
from market_intelligence.queries.models import (
    CandlePage,
    Latest,
    Market,
    QueryValidationError,
    Summary,
    UnknownMarketError,
)
from market_intelligence.queries.service import MarketQueries
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.query_models import TreasuryCurvePage, TreasuryCurveResult


class AgentBodyLimit:
    """Bound question bytes before JSON parsing, including streamed request bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] != "/v1/agent/query"
            or scope["method"] != "POST"
        ):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > 65536:
                await JSONResponse(
                    status_code=413, content={"detail": "Agent request body exceeds 64 KiB"}
                )(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)


def get_queries(request: Request) -> MarketQueries:
    return cast(MarketQueries, request.app.state.market_queries)


Queries = Annotated[MarketQueries, Depends(get_queries)]
MarketId = Annotated[int, Path(ge=1)]


def get_treasury_queries(request: Request) -> TreasuryQueries:
    return cast(TreasuryQueries, request.app.state.treasury_queries)


TreasuryReads = Annotated[TreasuryQueries, Depends(get_treasury_queries)]


def treasury_date(value: str) -> date:
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise QueryValidationError("Use Treasury source dates as YYYY-MM-DD") from None


def get_agent(request: Request) -> AgentRunner:
    return cast(AgentRunner, request.app.state.agent_runner)


Agent = Annotated[AgentRunner, Depends(get_agent)]


def window_instant(value: str) -> datetime:
    try:
        return parse_instant(value)
    except ValueError as error:
        raise QueryValidationError(
            "Use an ISO date (UTC) or timestamp with an explicit UTC offset"
        ) from error


def create_app(
    *,
    engine: Engine | None = None,
    database_settings: DatabaseSettings | None = None,
    api_settings: ApiSettings | None = None,
    agent_settings: AgentSettings | None = None,
    model_client: OpenAI | None = None,
    now: Callable[[], datetime] = utc_now,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = engine is None
        active_engine = engine
        if active_engine is None:
            settings = database_settings or DatabaseSettings()  # type: ignore[call-arg]
            active_engine = create_db_engine(settings, DatabaseRole.READ)
        app.state.market_queries = MarketQueries(active_engine, api_settings, now)
        app.state.treasury_queries = TreasuryQueries(active_engine, api_settings, now)
        active_client = model_client
        owns_client = False
        try:
            agent_config = agent_settings or AgentSettings()
            if active_client is None and agent_config.configured:
                assert agent_config.api_key is not None
                # Never enable SDK payload/header logging in this application.
                logging.getLogger("openai").setLevel(logging.WARNING)
                active_client = OpenAI(
                    api_key=agent_config.api_key.get_secret_value(),
                    base_url="https://api.openai.com/v1",
                    timeout=agent_config.deadline_seconds,
                    max_retries=0,
                )
                owns_client = True
            app.state.agent_runner = AgentRunner(
                active_client, MarketTools(app.state.market_queries), agent_config, now=now
            )
            yield
        finally:
            if owns_client and active_client is not None:
                active_client.close()
            if owned:
                active_engine.dispose()

    app = FastAPI(title="AI Market Intelligence", version="0.1.0", lifespan=lifespan)
    app.add_middleware(AgentBodyLimit)

    @app.exception_handler(AgentUnavailableError)
    async def agent_unavailable(request: Request, error: AgentUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(error)})

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, error: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @app.exception_handler(UnknownMarketError)
    async def unknown_market(request: Request, error: UnknownMarketError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Unknown market"})

    @app.exception_handler(QueryValidationError)
    async def invalid_query(request: Request, error: QueryValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(RequestValidationError)
    async def invalid_transport(request: Request, error: RequestValidationError) -> JSONResponse:
        if request.url.path == "/v1/agent/query":
            return JSONResponse(status_code=422, content={"detail": "Invalid agent question"})
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid request parameters",
                "errors": [
                    {"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()
                ],
            },
        )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready(queries: Queries) -> dict[str, str]:
        if not queries.ready():
            raise HTTPException(status_code=503, detail="Schema not ready")
        return {"status": "ready"}

    @app.get("/v1/markets", response_model=list[Market])
    def list_markets(queries: Queries) -> list[Market]:
        return queries.list_markets()

    @app.get("/v1/markets/{market_id}/latest", response_model=Latest)
    def latest(market_id: MarketId, queries: Queries) -> Latest:
        return queries.latest(market_id)

    @app.get("/v1/markets/{market_id}/summary", response_model=Summary)
    def summary(market_id: MarketId, start: str, end: str, queries: Queries) -> Summary:
        return queries.summary(market_id, window_instant(start), window_instant(end))

    @app.get("/v1/markets/{market_id}/candles", response_model=CandlePage)
    def candle_page(
        market_id: MarketId,
        start: str,
        end: str,
        queries: Queries,
        interval_seconds: int = 300,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> CandlePage:
        return queries.candle_page(
            market_id, window_instant(start), window_instant(end), interval_seconds, limit, cursor
        )

    @app.get("/v1/treasury/curve", response_model=TreasuryCurveResult)
    def treasury_curve(observed_on: str, queries: TreasuryReads) -> TreasuryCurveResult:
        return queries.curve(treasury_date(observed_on))

    @app.get("/v1/treasury/curves", response_model=TreasuryCurvePage)
    def treasury_curves(
        start: str,
        end: str,
        queries: TreasuryReads,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> TreasuryCurvePage:
        return queries.curve_page(treasury_date(start), treasury_date(end), limit, cursor)

    @app.post("/v1/agent/query", response_model=AgentResult)
    def agent_query(question: AgentQuestion, runner: Agent, response: Response) -> AgentResult:
        result = runner.run(question.question)
        if any(
            code in (LimitationCode.MODEL_UNAVAILABLE, LimitationCode.DATABASE_UNAVAILABLE)
            for code in result.limitations
        ):
            response.status_code = 503
        return result

    return app
