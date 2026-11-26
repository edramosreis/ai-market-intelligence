"""Bounded FOMC search and exact citation inspection over HTTP."""

import re
from datetime import date
from typing import Annotated, cast

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.fomc.citations import FomcQueries
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.retrieval import SearchRequest
from market_intelligence.fomc.retrieval_models import (
    MAX_RESPONSE_BYTES,
    PassageDetail,
    RetrievalError,
    SearchResult,
    bound_search_result,
)

_NO_STORE = {"Cache-Control": "no-store"}
_SEARCH_PARAMETERS = frozenset(("q", "limit", "kind", "meeting_end", "published_on"))
_DATE_LABEL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INTEGER = re.compile(r"(?:0|[1-9][0-9]{0,3})")

router = APIRouter(prefix="/v1/fomc/passages", tags=["FOMC passages"])


def _encode(model: BaseModel) -> bytes:
    return model.model_dump_json().encode("utf-8")


def _search_body(result: SearchResult) -> bytes:
    return _encode(bound_search_result(result))


def _detail_body(detail: PassageDetail) -> bytes:
    payload = _encode(detail)
    if len(payload) <= MAX_RESPONSE_BYTES:
        return payload
    for count in range(len(detail.spans) - 1, 0, -1):
        prefix = detail.spans[:count]
        payload = _encode(
            detail.model_copy(
                update=dict(
                    spans=prefix,
                    next_span_start=prefix[-1].span_ordinal + 1,
                )
            )
        )
        if len(payload) <= MAX_RESPONSE_BYTES:
            return payload
    raise RetrievalError("evidence_unavailable", 503)


def _parameters(request: Request, allowed: frozenset[str]) -> None:
    keys = [name for name, _ in request.query_params.multi_items()]
    if not allowed.issuperset(keys) or len(set(keys)) != len(keys):
        raise RetrievalError("invalid_parameters", 422)


def _date_label(text: str | None) -> date | None:
    if text is None:
        return None
    if _DATE_LABEL.fullmatch(text) is not None:
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise RetrievalError("invalid_parameters", 422)


def _number(value: str, minimum: int, maximum: int) -> int:
    if _INTEGER.fullmatch(value):
        integer = int(value)
        if minimum <= integer <= maximum:
            return integer
    raise RetrievalError("invalid_parameters", 422)


def _kind(value: str | None) -> DocumentKind | None:
    try:
        return DocumentKind(value) if value is not None else None
    except ValueError:
        raise RetrievalError("invalid_parameters", 422) from None


def _reader(request: Request) -> FomcQueries:
    return cast(FomcQueries, request.app.state.fomc_queries)


def _json(payload: bytes) -> Response:
    return Response(payload, media_type="application/json", headers=_NO_STORE)


def _error(error: RetrievalError) -> JSONResponse:
    return JSONResponse(
        content={"detail": error.code},
        status_code=error.status_code,
        headers=_NO_STORE,
    )


@router.get("/search", response_model=SearchResult)
def search_passages(
    request: Request,
    q: Annotated[str, Query(description="English keywords, up to 500 Unicode characters")] = "",
    limit: Annotated[str, Query(description="Maximum passages, from 1 to 5")] = "5",
    kind: Annotated[str | None, Query(description="statement or minutes")] = None,
    meeting_end: Annotated[str | None, Query(description="Meeting end date, YYYY-MM-DD")] = None,
    published_on: Annotated[str | None, Query(description="Publication date, YYYY-MM-DD")] = None,
) -> Response:
    try:
        _parameters(request, _SEARCH_PARAMETERS)
        if "q" not in request.query_params:
            raise RetrievalError("invalid_parameters", 422)
        query = SearchRequest(
            q=q,
            limit=_number(limit, 1, 5),
            kind=_kind(kind),
            meeting_end=_date_label(meeting_end),
            published_on=_date_label(published_on),
        )
        return _json(_search_body(_reader(request).search(query)))
    except RetrievalError as error:
        return _error(error)
    except SQLAlchemyError:
        return _error(RetrievalError("retrieval_unavailable", 503))


@router.get("/{passage_id}", response_model=PassageDetail)
def inspect_passage(
    request: Request,
    passage_id: str,
    span_start: Annotated[str, Query(description="Zero-based offset from the previous page")] = "0",
) -> Response:
    try:
        _parameters(request, frozenset(("span_start",)))
        return _json(
            _detail_body(
                _reader(request).passage(
                    passage_id,
                    span_start=_number(span_start, 0, 1999),
                )
            )
        )
    except RetrievalError as error:
        return _error(error)
    except SQLAlchemyError:
        return _error(RetrievalError("retrieval_unavailable", 503))
