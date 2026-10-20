import httpx
import pytest

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import AcquisitionError
from market_intelligence.fomc.models import CALENDAR_URL
from tests.fomc_fakes import calendar_body
from tests.unit.test_fomc_acquisition import Clock, acquisition, response


def test_calendar_shares_document_budget_and_counters() -> None:
    urls: list[str] = []
    body = calendar_body()

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return response(body=body)

    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = acquisition(http, clock, max_requests=2)
        calendar = client.fetch_calendar()
        client.fetch(PILOT_DOCUMENTS[0].document_id)
        with pytest.raises(AcquisitionError, match="request_limit"):
            client.fetch(PILOT_DOCUMENTS[1].document_id)
    assert urls == [CALENDAR_URL, PILOT_DOCUMENTS[0].url]
    assert calendar.body == body and "Synthetic" not in repr(calendar)
    assert client.requests_used == 2
    assert client.wire_bytes == client.entity_bytes == 2 * len(body)
    assert client.bytes_used == client.wire_bytes + client.entity_bytes
    assert clock.sleeps == [1]


def test_calendar_rejection_halts_command() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: response(429))) as http:
        client = acquisition(http, Clock())
        with pytest.raises(AcquisitionError, match="source_rejected"):
            client.fetch_calendar()
        with pytest.raises(AcquisitionError, match="client_halted"):
            client.fetch(PILOT_DOCUMENTS[0].document_id)
    assert client.requests_used == 1
