"""Load a locally verified cl100k vocabulary with no network fallback."""

import base64
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol, cast

from market_intelligence.fomc.embedding_provider import MAX_INPUT_TOKENS, token_count
from market_intelligence.fomc.vector_models import EmbeddingError, sha256

TOKENIZER = "cl100k_base"
PACKAGE_VERSION = "0.14.0"
VOCABULARY_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
TOKENIZER_REVISION = f"tiktoken-{PACKAGE_VERSION}-sha256-{VOCABULARY_SHA256}"
# Ordinary-token pattern from tiktoken 0.14.0, MIT, tiktoken_ext/openai_public.py.
_PATTERN = (
    r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+"
    r"| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
)


class OrdinaryEncoding(Protocol):
    def encode_ordinary(self, text: str) -> list[int]: ...
    def decode_bytes(self, tokens: list[int]) -> bytes: ...


def load_pilot_tokenizer(path: Path) -> OrdinaryEncoding:
    if path.is_symlink():
        raise EmbeddingError("invalid_tokenizer_vocabulary")
    try:
        with path.open("rb") as handle:
            source = handle.read(2 * 1024 * 1024 + 1)
    except OSError:
        raise EmbeddingError("invalid_tokenizer_vocabulary") from None
    if sha256(source) != VOCABULARY_SHA256:
        raise EmbeddingError("invalid_tokenizer_vocabulary")
    try:
        if version("tiktoken") != PACKAGE_VERSION:
            raise EmbeddingError("unapproved_tokenizer_version")
        module = import_module("tiktoken")
    except ImportError, PackageNotFoundError:
        raise EmbeddingError("unapproved_tokenizer_version") from None
    entries = (line.split() for line in source.splitlines())
    return cast(
        OrdinaryEncoding,
        module.Encoding(
            name=TOKENIZER,
            pat_str=_PATTERN,
            mergeable_ranks={
                base64.b64decode(code, validate=True): int(rank) for code, rank in entries
            },
            special_tokens={},
        ),
    )


def encode_reviewed_text(text: str, encoding: OrdinaryEncoding) -> tuple[int, ...]:
    raw = text.encode("utf-8")
    if not 1 <= len(raw) <= 4 * MAX_INPUT_TOKENS:
        raise EmbeddingError("invalid_embedding_input")
    tokens = encoding.encode_ordinary(text)
    token_count([tokens])
    if len(tokens) > len(raw) or encoding.decode_bytes(tokens) != raw:
        raise EmbeddingError("embedding_token_roundtrip_mismatch")
    return tuple(tokens)
