import pytest
from sqlalchemy import URL

from market_intelligence.fomc.isolation import require_fomc_database


@pytest.mark.parametrize(
    "db,host,port,user",
    [
        ("market_intelligence", "db", 5432, "m3_ingest"),
        ("market_intelligence_m3", "db", 5432, "ingest"),
        ("market_intelligence_m3", "127.0.0.1", 55432, "m3_ingest"),
        ("market_intelligence_m3", "remote", 5432, "m3_ingest"),
        ("market_intelligence_test", "db-test", 5432, "test_ingest"),
    ],
)
def test_refuse_demo_unreviewed_and_unguarded_targets(
    db: str, host: str, port: int, user: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUN_POSTGRES_TESTS", raising=False)
    with pytest.raises(ValueError, match="isolated M3"):
        require_fomc_database(
            URL.create("postgresql", username=user, host=host, port=port, database=db)
        )


def test_isolated_targets_and_explicit_test_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    require_fomc_database(
        URL.create(
            "postgresql",
            username="m3_ingest",
            host="db",
            port=5432,
            database="market_intelligence_m3",
        )
    )
    require_fomc_database(
        URL.create(
            "postgresql",
            username="m3_reader",
            host="127.0.0.1",
            port=55433,
            database="market_intelligence_m3",
        )
    )
    monkeypatch.setenv("RUN_POSTGRES_TESTS", "1")
    require_fomc_database(
        URL.create(
            "postgresql",
            username="test_ingest",
            host="db-test",
            port=5432,
            database="market_intelligence_test",
        )
    )
