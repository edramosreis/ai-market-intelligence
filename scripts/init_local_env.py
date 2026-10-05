"""Generate ignored local credentials without printing them or overwriting configuration."""

from pathlib import Path
from secrets import token_urlsafe


def main() -> None:
    destination = Path(".env")
    values = {
        "POSTGRES_DB": "market_intelligence",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "55432",
        "POSTGRES_ADMIN_USER": "market_admin",
        "POSTGRES_ADMIN_PASSWORD": token_urlsafe(32),
        "POSTGRES_INGEST_USER": "market_ingest",
        "POSTGRES_INGEST_PASSWORD": token_urlsafe(32),
        "POSTGRES_READ_USER": "market_reader",
        "POSTGRES_READ_PASSWORD": token_urlsafe(32),
    }
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            output.write("# Generated local development configuration; never commit.\n")
            output.writelines(f"{name}={value}\n" for name, value in values.items())
    except FileExistsError:
        raise SystemExit(".env already exists; left unchanged.") from None
    print("Created ignored .env with local credentials. No secrets were printed.")


if __name__ == "__main__":
    main()
