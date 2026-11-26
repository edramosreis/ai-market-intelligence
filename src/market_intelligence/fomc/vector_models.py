"""Canonical, provider-independent embedding identities and coordinates."""

import hashlib
import json
import math
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

MAX_EMBEDDING_DIMENSIONS = 16_000
MAX_RUN_INPUTS = 2_048
MAX_BATCH_INPUTS = 32
MAX_RUN_REQUESTS = 64
MAX_REQUEST_TOKENS = 300_000
PASSAGE_INPUT_RECIPE = "canonical_passage_utf8_v1"
VECTOR_COORDINATE_FORMAT = "unit_float32_be_v1"
VECTOR_DISTANCE_METRIC = "cosine"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


class EmbeddingError(ValueError):
    """Controlled failure in the model-free embedding storage path."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _fingerprint(value: object) -> str:
    return sha256(_json(value).encode("utf-8"))


def valid_fingerprint(value: str) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_tag(value: str, length: int) -> bool:
    return isinstance(value, str) and len(value) <= length and _TAG.fullmatch(value) is not None


@dataclass(frozen=True)
class EmbeddingProfile:
    provider: str
    model: str
    dimensions: int
    tokenizer: str
    tokenizer_revision: str
    epoch_id: UUID

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not _PROVIDER.fullmatch(self.provider)
            or not _valid_tag(self.model, 128)
            or type(self.dimensions) is not int
            or not 1 <= self.dimensions <= MAX_EMBEDDING_DIMENSIONS
            or not _valid_tag(self.tokenizer, 80)
            or not _valid_tag(self.tokenizer_revision, 128)
            or not isinstance(self.epoch_id, UUID)
        ):
            raise EmbeddingError("invalid_profile")

    @property
    def config_sha256(self) -> str:
        return _fingerprint(
            [
                "fomc_embedding_config_v1",
                self.provider,
                self.model,
                self.dimensions,
                self.tokenizer,
                self.tokenizer_revision,
                PASSAGE_INPUT_RECIPE,
                VECTOR_COORDINATE_FORMAT,
                VECTOR_DISTANCE_METRIC,
            ]
        )

    @property
    def profile_id(self) -> str:
        return _fingerprint(["fomc_embedding_profile_v1", self.config_sha256, str(self.epoch_id)])


@dataclass(frozen=True)
class EmbeddingInput:
    passage_id: str
    input_sha256: str


def manifest_hash(profile_id: str, inputs: Sequence[EmbeddingInput]) -> str:
    return _fingerprint(
        [
            "fomc_embedding_manifest_v1",
            profile_id,
            [[item.passage_id, item.input_sha256] for item in inputs],
        ]
    )


@dataclass(frozen=True)
class CanonicalVector:
    coordinates: tuple[float, ...]
    sha256: str

    def postgres_value(self) -> str:
        return _json(self.coordinates)


def canonical_vector(values: Sequence[float], dimensions: int) -> CanonicalVector:
    if (
        not 1 <= dimensions <= MAX_EMBEDDING_DIMENSIONS
        or len(values) != dimensions
        or any(type(value) not in (int, float) for value in values)
    ):
        raise EmbeddingError("invalid_vector")
    try:
        coordinates = tuple(float(value) for value in values)
    except OverflowError:
        raise EmbeddingError("invalid_vector") from None
    if not all(math.isfinite(coordinate) for coordinate in coordinates):
        raise EmbeddingError("invalid_vector")
    scale = max(map(abs, coordinates))
    if scale == 0:
        raise EmbeddingError("invalid_vector")
    scaled = tuple(coordinate / scale for coordinate in coordinates)
    norm = math.hypot(*scaled)
    single_precision = struct.Struct(f">{dimensions}f")
    rounded = single_precision.unpack(single_precision.pack(*(value / norm for value in scaled)))
    canonical = tuple(0.0 if value == 0.0 else value for value in rounded)
    return CanonicalVector(canonical, sha256(single_precision.pack(*canonical)))


@dataclass(frozen=True)
class EmbeddingOutput:
    position: int
    coordinates: Sequence[float]


@dataclass(frozen=True)
class EmbeddingReceiptEvidence:
    request_id: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if (self.request_id is not None and not _valid_tag(self.request_id, 256)) or (
            self.model is not None and not _valid_tag(self.model, 128)
        ):
            raise EmbeddingError("invalid_embedding_receipt")
        if self.prompt_tokens is None and self.total_tokens is None:
            return
        if (
            type(self.prompt_tokens) is not int
            or type(self.total_tokens) is not int
            or not 1 <= self.prompt_tokens <= self.total_tokens <= MAX_REQUEST_TOKENS
        ):
            raise EmbeddingError("invalid_embedding_receipt")


@dataclass(frozen=True)
class EmbeddingReceipt:
    request_id: str
    model: str
    prompt_tokens: int
    total_tokens: int
    outputs: Sequence[EmbeddingOutput]

    def vectors(self, profile: EmbeddingProfile, count: int) -> dict[int, CanonicalVector]:
        if (
            not _valid_tag(self.request_id, 256)
            or self.model != profile.model
            or type(self.prompt_tokens) is not int
            or type(self.total_tokens) is not int
            or not 1 <= self.prompt_tokens <= self.total_tokens <= MAX_REQUEST_TOKENS
            or len(self.outputs) != count
        ):
            raise EmbeddingError("invalid_embedding_response")
        vectors: dict[int, CanonicalVector] = {}
        for output in self.outputs:
            position = output.position
            if type(position) is not int or not 0 <= position < count or position in vectors:
                raise EmbeddingError("invalid_embedding_response")
            vectors[position] = canonical_vector(output.coordinates, profile.dimensions)
        return vectors
