"""FOMC writes are restricted to M3; deterministic tests retain their existing guard."""

import os

from sqlalchemy import URL


def require_fomc_database(url: URL) -> None:
    testing = (
        os.environ.get("RUN_POSTGRES_TESTS") == "1"
        and url.host == "db-test"
        and url.database == "market_intelligence_test"
        and url.port == 5432
    )
    runtime = (
        url.database == "market_intelligence_m3"
        and url.username in {"m3_ingest", "m3_reader"}
        and (
            (url.host == "db" and url.port == 5432)
            or (url.host in {"127.0.0.1", "localhost"} and url.port not in {None, 5432, 55432})
        )
    )
    if not (testing or runtime):
        raise ValueError("FOMC operations require the isolated M3 database")
