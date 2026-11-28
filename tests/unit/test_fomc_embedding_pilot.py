from pathlib import Path

import pytest

from market_intelligence.fomc.embedding_pilot import PilotRateLimit
from market_intelligence.fomc.embedding_tokenizer import encode_reviewed_text, load_pilot_tokenizer
from market_intelligence.fomc.vector_models import EmbeddingError


class BytesEncoding:
    def encode_ordinary(self, text: str) -> list[int]:
        return list(text.encode("utf-8"))

    def decode_bytes(self, tokens: list[int]) -> bytes:
        return bytes(tokens)


def test_local_tokenization_verifies_unicode_roundtrip_and_vocabulary(tmp_path: Path) -> None:
    assert encode_reviewed_text("¼", BytesEncoding()) == (194, 188)
    vocabulary = tmp_path / "encoding"
    vocabulary.write_bytes(b"corrupt")
    with pytest.raises(EmbeddingError, match="invalid_tokenizer_vocabulary"):
        load_pilot_tokenizer(vocabulary)

    class WrongDecode(BytesEncoding):
        def decode_bytes(self, tokens: list[int]) -> bytes:
            return b"wrong"

    with pytest.raises(EmbeddingError, match="embedding_token_roundtrip_mismatch"):
        encode_reviewed_text("¼", WrongDecode())


def test_rolling_window_waits_before_exceeding_approved_token_rate() -> None:
    clock = [0.0]
    waits = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    limiter = PilotRateLimit(2, 5, now=lambda: clock[0], wait=wait)
    limiter(3)
    limiter(2)
    limiter(1)
    assert waits == [60.0]
    with pytest.raises(EmbeddingError, match="pilot_batch_exceeds_rate_limit"):
        limiter(6)
