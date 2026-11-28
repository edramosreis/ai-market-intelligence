"""Single-attempt OpenAI embeddings for the isolated, manual FOMC pilot."""

import json
from collections.abc import Sequence
from time import monotonic
from types import TracebackType
from typing import Literal, Self
from uuid import UUID

import httpx2 as httpx
from openai import APIError, APIStatusError, OpenAI
from pydantic import SecretStr

from market_intelligence.fomc.vector_models import (
    MAX_BATCH_INPUTS,
    MAX_REQUEST_TOKENS,
    EmbeddingError,
    EmbeddingOutput,
    EmbeddingProfile,
    EmbeddingReceipt,
    EmbeddingReceiptEvidence,
)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
MAX_INPUT_TOKENS = 8192
MAX_ORDINARY_TOKEN = 100255
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_BASE_URL = "https://api.openai.com/v1"

DispatchFailure = Literal["provider_rejected", "dispatch_uncertain", "response_invalid"]


class EmbeddingDispatchError(EmbeddingError):
    def __init__(self, code: DispatchFailure, evidence: EmbeddingReceiptEvidence) -> None:
        super().__init__(code)
        self.code = code
        self.evidence = evidence


def token_count(inputs: Sequence[Sequence[int]]) -> int:
    if not 1 <= len(inputs) <= MAX_BATCH_INPUTS or any(
        not 1 <= len(item) <= MAX_INPUT_TOKENS
        or any(type(token) is not int or not 0 <= token <= MAX_ORDINARY_TOKEN for token in item)
        for item in inputs
    ):
        raise EmbeddingError("invalid_embedding_input")
    count = sum(map(len, inputs))
    if count > MAX_REQUEST_TOKENS:
        raise EmbeddingError("invalid_embedding_input")
    return count


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_response_key")
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("invalid_response_number")


def _safe_evidence(
    request_id: object, model: object = None, prompt: object = None, total: object = None
) -> EmbeddingReceiptEvidence:
    try:
        request_id = EmbeddingReceiptEvidence(
            request_id=request_id if isinstance(request_id, str) else None
        ).request_id
    except EmbeddingError:
        request_id = None
    try:
        model = EmbeddingReceiptEvidence(model=model if isinstance(model, str) else None).model
    except EmbeddingError:
        model = None
    try:
        return EmbeddingReceiptEvidence(
            request_id=request_id,
            model=model,
            prompt_tokens=prompt if type(prompt) is int else None,
            total_tokens=total if type(total) is int else None,
        )
    except EmbeddingError:
        return EmbeddingReceiptEvidence(request_id=request_id, model=model)


def _parse(
    body: bytes, request_id: str | None, profile: EmbeddingProfile, count: int, expected_tokens: int
) -> EmbeddingReceipt:
    evidence = _safe_evidence(request_id)
    try:
        parsed = json.loads(body, object_pairs_hook=_strict_pairs, parse_constant=_invalid_constant)
        if not isinstance(parsed, dict) or parsed.get("object") != "list":
            raise ValueError
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            raise ValueError
        evidence = _safe_evidence(
            request_id, parsed.get("model"), usage.get("prompt_tokens"), usage.get("total_tokens")
        )
        data = parsed.get("data")
        prompt, total = evidence.prompt_tokens, evidence.total_tokens
        if (
            evidence.request_id is None
            or evidence.model != profile.model
            or prompt is None
            or total is None
            or prompt != expected_tokens
            or total != prompt
            or not isinstance(data, list)
            or len(data) != count
        ):
            raise ValueError
        outputs = []
        for item in data:
            if not isinstance(item, dict) or item.get("object") != "embedding":
                raise ValueError
            index, values = item.get("index"), item.get("embedding")
            if type(index) is not int or not isinstance(values, list):
                raise ValueError
            outputs.append(EmbeddingOutput(index, values))
        receipt = EmbeddingReceipt(evidence.request_id, evidence.model, prompt, total, outputs)
        receipt.vectors(profile, count)
        return receipt
    except ValueError, TypeError, RecursionError:
        code: DispatchFailure = (
            "response_invalid" if evidence.total_tokens is not None else "dispatch_uncertain"
        )
        raise EmbeddingDispatchError(code, evidence) from None


class OpenAIEmbeddingProvider:
    def __init__(self, key: SecretStr, *, transport: httpx.BaseTransport | None = None) -> None:
        secret = key.get_secret_value()
        if not secret or secret.strip() != secret:
            raise EmbeddingError("embedding_key_unavailable")
        timeout = httpx.Timeout(20.0, connect=5.0)
        self._client = OpenAI(
            api_key=secret,
            base_url=_BASE_URL,
            timeout=timeout,
            max_retries=0,
            http_client=httpx.Client(
                timeout=timeout, transport=transport, follow_redirects=False, trust_env=False
            ),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.close()

    def embed(
        self, profile: EmbeddingProfile, inputs: Sequence[Sequence[int]], request_id: UUID
    ) -> EmbeddingReceipt:
        if (profile.provider, profile.model, profile.dimensions) != (
            "openai",
            EMBEDDING_MODEL,
            EMBEDDING_DIMENSIONS,
        ):
            raise EmbeddingError("unapproved_embedding_model")
        expected = token_count(inputs)
        provider_id = None
        deadline = monotonic() + 30.0
        try:
            with self._client.embeddings.with_streaming_response.create(
                model=EMBEDDING_MODEL,
                dimensions=EMBEDDING_DIMENSIONS,
                encoding_format="float",
                input=[list(item) for item in inputs],
                extra_headers={"X-Client-Request-Id": str(request_id)},
            ) as response:
                provider_id = response.headers.get("x-request-id")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=32 * 1024):
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES or monotonic() >= deadline:
                        raise EmbeddingDispatchError(
                            "dispatch_uncertain", _safe_evidence(provider_id)
                        )
                    body.extend(chunk)
                return _parse(bytes(body), provider_id, profile, len(inputs), expected)
        except APIStatusError as error:
            code: DispatchFailure = (
                "provider_rejected" if 400 <= error.status_code < 500 else "dispatch_uncertain"
            )
            raise EmbeddingDispatchError(code, _safe_evidence(error.request_id)) from None
        except APIError, httpx.HTTPError, OSError:
            raise EmbeddingDispatchError(
                "dispatch_uncertain", _safe_evidence(provider_id)
            ) from None
