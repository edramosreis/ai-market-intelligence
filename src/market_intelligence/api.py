"""HTTP transport for shared market queries; no ingestion or model calls."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, cast

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import ApiSettings, DatabaseRole, DatabaseSettings
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


def get_queries(request: Request) -> MarketQueries:
    return cast(MarketQueries, request.app.state.market_queries)


Queries = Annotated[MarketQueries, Depends(get_queries)]
MarketId = Annotated[int, Path(ge=1)]


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
        try:
            yield
        finally:
            if owned:
                active_engine.dispose()

    app = FastAPI(title="AI Market Intelligence", version="0.1.0", lifespan=lifespan)

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

    return app
