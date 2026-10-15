"""Operate only the explicit isolated development resources from a linked worktree."""

import argparse
import os
import re
from collections.abc import Sequence
from pathlib import Path
from secrets import token_urlsafe

PASSWORD_KEYS = {f"M3_{role}_PASSWORD" for role in ("ADMIN", "INGEST", "READ")}
PORT_KEYS = {"M3_API_PORT", "M3_POSTGRES_PORT"}


class IsolationError(ValueError):
    """A controlled refusal; messages never include configuration values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IsolationError(message)


def env_path(root: Path, testing: bool) -> Path:
    return root / (".env.m3.test" if testing else ".env.m3")


def check_worktree(root: Path) -> None:
    require((root / ".git").is_file(), "Use a separate linked Git worktree, not the demo clone.")
    require(
        all((root / name).is_file() for name in ("Dockerfile", "compose.m3.yaml")),
        "The isolated development files are missing.",
    )


def initialize(root: Path, testing: bool) -> None:
    check_worktree(root)
    values = {key: token_urlsafe(32) for key in sorted(PASSWORD_KEYS)}
    if not testing:
        values.update(M3_API_PORT="8001", M3_POSTGRES_PORT="55433")
    # Exclusive creation also refuses an existing symlink; never overwrite configuration.
    descriptor = os.open(env_path(root, testing), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("# Isolated local credentials; never commit or copy demo settings.\n")
        output.writelines(f"{key}={value}\n" for key, value in values.items())
    print("Created isolated configuration; no secrets printed and no services started.")


def read_settings(root: Path, testing: bool) -> dict[str, str]:
    path = env_path(root, testing)
    require(not path.is_symlink(), "Refusing a symlinked configuration file.")
    require(path.stat().st_size <= 8192, "Configuration file is too large.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(bool(separator) and key not in values, "Malformed or duplicate configuration key.")
        values[key] = value
    require(
        set(values) == PASSWORD_KEYS | (set() if testing else PORT_KEYS),
        "Only isolated password/port settings are allowed.",
    )
    require(
        all(re.fullmatch(r"[A-Za-z0-9_-]{40,128}", values[key]) for key in PASSWORD_KEYS),
        "Use newly generated isolated passwords.",
    )
    require(len({values[key] for key in PASSWORD_KEYS}) == 3, "Passwords must be distinct.")
    if not testing:
        ports = [values[key] for key in sorted(PORT_KEYS)]
        require(
            all(p.isascii() and p.isdecimal() and str(int(p)) == p for p in ports),
            "Ports must be canonical integers.",
        )
        numbers = {int(p) for p in ports}
        require(
            len(numbers) == 2
            and all(1024 <= p <= 65535 and p not in {8000, 55432} for p in numbers),
            "Ports must be distinct, unprivileged and separate from demo ports.",
        )
    return values


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Use independent test credentials.")
    parser.add_argument("action", choices=("init",))
    arguments = parser.parse_args(argv)
    try:
        initialize(Path(__file__).resolve().parents[1], arguments.test)
        return 0
    except IsolationError as error:
        print(f"Refused: {error}")
    except OSError, UnicodeError:
        print("Isolated setup failed; inspect local files without printing secrets.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
