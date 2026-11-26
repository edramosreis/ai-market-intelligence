import hashlib
import struct
from dataclasses import replace
from uuid import UUID

import pytest

from market_intelligence.fomc.vector_models import (
    EmbeddingError,
    EmbeddingInput,
    EmbeddingProfile,
    canonical_vector,
    manifest_hash,
)


def test_profile_recipe_and_epoch_have_reproducible_identities() -> None:
    profile = EmbeddingProfile("synthetic", "test-model", 3, "test", "one", UUID(int=1))
    config = (
        b'["fomc_embedding_config_v1","synthetic","test-model",3,"test","one",'
        b'"canonical_passage_utf8_v1","unit_float32_be_v1","cosine"]'
    )
    assert profile.config_sha256 == hashlib.sha256(config).hexdigest()
    later = replace(profile, epoch_id=UUID(int=2))
    assert later.config_sha256 == profile.config_sha256
    assert later.profile_id != profile.profile_id
    passage = EmbeddingInput("a" * 64, "b" * 64)
    changed = replace(passage, input_sha256="c" * 64)
    assert manifest_hash(profile.profile_id, [passage]) != manifest_hash(
        profile.profile_id, [changed]
    )


def test_extreme_vectors_normalize_to_big_endian_float32_with_canonical_zero() -> None:
    values = canonical_vector([1.7e308, 1.7e308, -5e-324], 3)
    assert values.coordinates == pytest.approx([0.70710678, 0.70710678, 0.0])
    assert values.sha256 == hashlib.sha256(struct.pack(">fff", 2**-0.5, 2**-0.5, 0.0)).hexdigest()
    assert canonical_vector([-0.0, 5e-324], 2).coordinates == (0.0, 1.0)


@pytest.mark.parametrize(
    "values",
    [[1.0], [0, 0], [True, 1], [float("nan"), 1], [float("inf"), 1], [10**1000, 1]],
    ids=["wrong-dimensions", "zero", "bool", "nan", "infinite", "overflow"],
)
def test_invalid_coordinates_are_rejected(values: list[float]) -> None:
    with pytest.raises(EmbeddingError, match="^invalid_vector$"):
        canonical_vector(values, 2)
