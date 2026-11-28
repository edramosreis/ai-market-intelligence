import json
from dataclasses import replace
from uuid import UUID

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from market_intelligence.fomc.embedding_provider import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingDispatchError,
    OpenAIEmbeddingProvider,
)
from market_intelligence.fomc.vector_models import EmbeddingError, EmbeddingProfile

PROFILE = EmbeddingProfile(
    "openai", EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, "cl100k_base", "test", UUID(int=1)
)
REQUEST_ID = UUID(int=2)
COORDINATES = [1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))]


def response(indices: list[int], *, prompt: int = 3, total: int = 3) -> dict[str, object]:
    return {
        "object": "list",
        "model": EMBEDDING_MODEL,
        "data": [
            {"object": "embedding", "index": index, "embedding": COORDINATES} for index in indices
        ],
        "usage": {"prompt_tokens": prompt, "total_tokens": total},
    }


def test_sdk_sends_exact_tokens_once_to_reviewed_endpoint() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response([1, 0]), headers={"x-request-id": "req_fixed"})

    with OpenAIEmbeddingProvider(SecretStr("test"), transport=httpx.MockTransport(respond)) as sdk:
        receipt = sdk.embed(PROFILE, [[14], [13, 16]], REQUEST_ID)

    [request] = requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/embeddings"
    assert request.headers["x-client-request-id"] == str(REQUEST_ID)
    assert json.loads(request.content) == {
        "model": EMBEDDING_MODEL,
        "dimensions": 1536,
        "encoding_format": "float",
        "input": [[14], [13, 16]],
    }
    assert receipt.request_id == "req_fixed"
    assert set(receipt.vectors(PROFILE, 2)) == {0, 1}


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "wrong_usage", "wrong_model", "zero"])
def test_sdk_rejects_invalid_receipts_but_retains_known_usage(mutation: str) -> None:
    payload = response([0, 0] if mutation == "duplicate" else [0, 1])
    if mutation == "missing":
        payload = response([0])
    elif mutation == "wrong_usage":
        payload["usage"] = {"prompt_tokens": 4, "total_tokens": 4}
    elif mutation == "wrong_model":
        payload["model"] = "different-model"
    elif mutation == "zero":
        payload["data"] = [
            {"object": "embedding", "index": 0, "embedding": [0.0] * EMBEDDING_DIMENSIONS},
            {"object": "embedding", "index": 1, "embedding": COORDINATES},
        ]
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=payload, headers={"x-request-id": "req_retained"})

    with (
        OpenAIEmbeddingProvider(SecretStr("test"), transport=httpx.MockTransport(respond)) as sdk,
        pytest.raises(EmbeddingDispatchError, match="response_invalid") as raised,
    ):
        sdk.embed(PROFILE, [[14], [13, 16]], REQUEST_ID)
    assert calls == ["/v1/embeddings"]
    assert raised.value.evidence.request_id == "req_retained"
    assert raised.value.evidence.total_tokens == (4 if mutation == "wrong_usage" else 3)


@pytest.mark.parametrize("status", [429, 500, None])
def test_provider_failures_never_retry(status: int | None) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if status is None:
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(status, headers={"x-request-id": "req_failure"})

    with (
        OpenAIEmbeddingProvider(SecretStr("test"), transport=httpx.MockTransport(respond)) as sdk,
        pytest.raises(EmbeddingDispatchError) as raised,
    ):
        sdk.embed(PROFILE, [[14]], REQUEST_ID)
    assert calls == ["/v1/embeddings"]
    assert raised.value.code == ("provider_rejected" if status == 429 else "dispatch_uncertain")


def test_unapproved_model_or_special_token_send_nothing() -> None:
    def forbidden(_: httpx.Request) -> httpx.Response:
        pytest.fail("The validation failure reached the transport")

    with OpenAIEmbeddingProvider(
        SecretStr("test"), transport=httpx.MockTransport(forbidden)
    ) as sdk:
        with pytest.raises(EmbeddingError, match="unapproved_embedding_model"):
            sdk.embed(replace(PROFILE, dimensions=32), [[0]], REQUEST_ID)
        with pytest.raises(EmbeddingError, match="invalid_embedding_input"):
            sdk.embed(PROFILE, [[100257]], REQUEST_ID)
