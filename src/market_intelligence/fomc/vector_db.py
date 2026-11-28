"""Shared database and profile checks for FOMC vector operations."""

import os
from typing import Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.db.fomc_vector_schema import VECTOR_REVISION
from market_intelligence.fomc.isolation import require_fomc_database
from market_intelligence.fomc.vector_models import (
    PASSAGE_INPUT_RECIPE,
    VECTOR_COORDINATE_FORMAT,
    VECTOR_DISTANCE_METRIC,
    EmbeddingError,
    EmbeddingProfile,
    valid_fingerprint,
)

VectorRole = Literal["admin", "ingest", "reader"]


def require_vector_target(engine: Engine, role: VectorRole) -> None:
    url = engine.url
    test = url.database == "market_intelligence_test"
    prefix = "m3_test" if test else "m3"
    if url.username != f"{prefix}_{role}" or (test and os.environ.get("RUN_VECTOR_TESTS") != "1"):
        raise EmbeddingError("vector_unavailable")
    try:
        require_fomc_database(url.set(username=f"{prefix}_ingest"))
    except ValueError:
        raise EmbeddingError("vector_unavailable") from None


def check_vector_connection(conn: Connection, engine: Engine) -> None:
    identity = conn.execute(sa.text("SELECT current_database(), current_user")).one()
    if tuple(identity) != (engine.url.database, engine.url.username):
        raise EmbeddingError("vector_unavailable")
    revision = conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalars()
    if revision.all() != [VECTOR_REVISION]:
        raise EmbeddingError("vector_unavailable")


def load_profile(conn: Connection, profile_id: str) -> EmbeddingProfile:
    if not valid_fingerprint(profile_id):
        raise EmbeddingError("invalid_profile")
    row = conn.execute(
        sa.text(
            """
            SELECT provider, model, dimensions, tokenizer, tokenizer_revision, epoch_id,
                   config_sha256
            FROM public.fomc_embedding_profiles
            WHERE profile_id = :profile
            """
        ),
        {"profile": profile_id},
    ).one_or_none()
    if row is None:
        raise EmbeddingError("unknown_profile")
    profile = EmbeddingProfile(
        row.provider, row.model, row.dimensions, row.tokenizer, row.tokenizer_revision, row.epoch_id
    )
    if profile.profile_id != profile_id or profile.config_sha256 != row.config_sha256:
        raise EmbeddingError("vector_unavailable")
    return profile


def register_embedding_profile(
    engine: Engine, profile: EmbeddingProfile, *, must_create: bool = False
) -> str:
    """Register one immutable configuration explicitly with the isolated administrator."""
    require_vector_target(engine, "admin")
    profile_id = profile.profile_id
    try:
        with engine.begin() as conn:
            check_vector_connection(conn, engine)
            inserted = conn.execute(
                sa.text(
                    """
                    INSERT INTO public.fomc_embedding_profiles
                        (profile_id, config_sha256, epoch_id, provider, model, dimensions,
                         tokenizer, tokenizer_revision, input_recipe, coordinate_format,
                         distance_metric)
                    VALUES (:profile, :config, :epoch, :provider, :model, :dimensions,
                            :tokenizer, :tokenizer_revision, :recipe, :coordinates, :metric)
                    ON CONFLICT (profile_id) DO NOTHING
                    RETURNING profile_id
                    """
                ),
                dict(
                    profile=profile_id,
                    config=profile.config_sha256,
                    epoch=profile.epoch_id,
                    provider=profile.provider,
                    model=profile.model,
                    dimensions=profile.dimensions,
                    tokenizer=profile.tokenizer,
                    tokenizer_revision=profile.tokenizer_revision,
                    recipe=PASSAGE_INPUT_RECIPE,
                    coordinates=VECTOR_COORDINATE_FORMAT,
                    metric=VECTOR_DISTANCE_METRIC,
                ),
            ).scalar_one_or_none()
            if must_create and inserted is None:
                raise EmbeddingError("embedding_profile_already_exists")
            if load_profile(conn, profile_id) != profile:
                raise EmbeddingError("vector_unavailable")
    except sa.exc.DBAPIError:
        raise EmbeddingError("vector_unavailable") from None
    return profile_id
