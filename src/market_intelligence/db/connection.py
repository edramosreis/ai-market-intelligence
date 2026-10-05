from sqlalchemy import Engine, create_engine

from market_intelligence.config import DatabaseRole, DatabaseSettings


def create_db_engine(settings: DatabaseSettings, role: DatabaseRole) -> Engine:
    return create_engine(
        settings.url(role),
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=0,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c timezone=UTC -c statement_timeout=15000",
        },
    )
