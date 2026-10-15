"""Operate only the explicit isolated development resources from a linked worktree."""

import argparse
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

POSTGRES_IMAGE = (
    "postgres:18.6-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650"
)
PROJECT = "ai-market-intelligence-m3"
PASSWORD_KEYS = {f"M3_{role}_PASSWORD" for role in ("ADMIN", "INGEST", "READ")}
PORT_KEYS = {"M3_API_PORT", "M3_POSTGRES_PORT"}


class IsolationError(ValueError):
    """A controlled refusal; messages never include configuration values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IsolationError(message)


def object_value(value: object) -> dict[str, Any]:
    require(isinstance(value, dict), "Invalid Compose object.")
    assert isinstance(value, dict)
    return value


def project_name(testing: bool) -> str:
    return PROJECT + ("-tests" if testing else "")


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


def compose_environment(environment: Mapping[str, str]) -> dict[str, str]:
    # Shell values outrank env files. Remove application and Compose overrides entirely.
    prefixes = ("COMPOSE_", "M3_", "POSTGRES_", "OPENAI_", "AGENT_", "SCHEDULER_", "API_")
    return {
        key: value for key, value in environment.items() if not key.upper().startswith(prefixes)
    }


def compose_prefix(root: Path, testing: bool) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "--env-file",
        str(env_path(root, testing)),
        "-p",
        project_name(testing),
        "-f",
        str(root / ("compose.m3.test.yaml" if testing else "compose.m3.yaml")),
    ]


def expected_environment(service: str, values: Mapping[str, str], testing: bool) -> dict[str, str]:
    database = "market_intelligence_test" if testing else "market_intelligence_m3"
    role_prefix = "m3_test" if testing else "m3"
    if service in {"db", "db-test"}:
        return {
            "POSTGRES_DB": database,
            "POSTGRES_USER": role_prefix + "_admin",
            "POSTGRES_PASSWORD": values["M3_ADMIN_PASSWORD"],
        }
    result = {
        "POSTGRES_HOST": "db-test" if testing else "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": database,
    }
    roles = (
        ("ADMIN", "INGEST", "READ")
        if service in {"tests", "migrate"}
        else ("INGEST",)
        if service in {"ingest", "scheduler"}
        else ("READ",)
    )
    for role in roles:
        result[f"POSTGRES_{role}_USER"] = role_prefix + (
            "_reader" if role == "READ" else "_" + role.lower()
        )
        result[f"POSTGRES_{role}_PASSWORD"] = values[f"M3_{role}_PASSWORD"]
    if service == "api":
        result["AGENT_ENABLED"] = "false"
    if service == "scheduler":
        result.update(SCHEDULER_ENABLED="false", SCHEDULER_JOBS="")
    if service == "tests":
        result["RUN_POSTGRES_TESTS"] = "1"
    return result


def validate_model(
    model: dict[str, Any], root: Path, testing: bool, values: Mapping[str, str]
) -> None:
    """Check the fully resolved model before allowing any Docker mutation."""
    name = project_name(testing)
    require(model.get("name") == name, "Wrong Compose project.")
    require(
        set(model) <= {"name", "services", "networks", "volumes"}, "Unexpected Compose resources."
    )
    networks = object_value(model.get("networks"))
    require(set(networks) == {"default"}, "Unexpected network.")
    network = object_value(networks["default"])
    require(
        network.get("name") == name + "_default"
        and set(network) <= {"name", "ipam"}
        and network.get("ipam", {}) == {},
        "Wrong network.",
    )
    volumes = {} if testing else {"postgres_data": {"name": PROJECT + "_postgres_data"}}
    require(model.get("volumes", {}) == volumes, "Wrong persistent volume.")
    services = object_value(model.get("services"))
    expected_services = (
        {"db-test", "tests"}
        if testing
        else {"db", "api", "migrate", "check", "ingest", "scheduler"}
    )
    require(set(services) == expected_services, "Unexpected services.")
    commands = {
        "api": ["serve", "--host", "0.0.0.0", "--port", "8000"],
        "migrate": ["init-db"],
        "check": ["check-db"],
        "ingest": ["ingest"],
        "scheduler": ["schedule", "run"],
    }
    for service, raw in services.items():
        item = object_value(raw)
        is_database = service in {"db", "db-test"}
        image = (
            POSTGRES_IMAGE
            if is_database
            else "ai-market-intelligence:m3" + ("-tests" if testing else "")
        )
        require(item.get("image") == image, "Wrong image tag or database digest.")
        require(item.get("restart") == "no", "Automatic restart is not enabled for M3.")
        require(item.get("networks") == {"default": None}, "Service has an unexpected network.")
        require(
            item.get("environment") == expected_environment(service, values, testing),
            "Wrong service credentials, target or enabled flags.",
        )
        require(item.get("command") == commands.get(service), "Unexpected service command.")
        forbidden = {
            "container_name",
            "network_mode",
            "pid",
            "ipc",
            "privileged",
            "cap_add",
            "devices",
            "volumes_from",
            "env_file",
            "secrets",
            "configs",
            "post_start",
            "pre_stop",
            "develop",
            "provider",
            "use_api_socket",
            "external_links",
            "links",
            "extra_hosts",
        }
        require(not forbidden.intersection(item), "Unsafe service override.")
        require(item.get("entrypoint") is None, "Unexpected service entrypoint.")
        build = item.get("build")
        if service in {"migrate", "tests"}:
            expected_build = {
                "context": str(root),
                "dockerfile": "Dockerfile",
                "target": "development" if testing else "runtime",
            }
            require(build == expected_build, "Build context or target escaped the worktree.")
        else:
            require(build is None, "Unexpected build target.")
        ports = []
        if service in {"api", "db"}:
            ports = [
                {
                    "mode": "ingress",
                    "host_ip": "127.0.0.1",
                    "target": 8000 if service == "api" else 5432,
                    "published": values["M3_API_PORT" if service == "api" else "M3_POSTGRES_PORT"],
                    "protocol": "tcp",
                }
            ]
        require(item.get("ports", []) == ports, "Ports are not the isolated localhost targets.")
        mounts = (
            []
            if service != "db"
            else [
                {
                    "type": "volume",
                    "source": "postgres_data",
                    "target": "/var/lib/postgresql",
                    "volume": {},
                }
            ]
        )
        require(item.get("volumes", []) == mounts, "Unexpected mount or demo volume.")
        require(
            item.get("tmpfs", []) == (["/var/lib/postgresql"] if service == "db-test" else []),
            "Wrong test storage.",
        )


def run(root: Path, testing: bool, action: str) -> int:
    require(action == "validate", "Only validation is available at this checkpoint.")
    check_worktree(root)
    values = read_settings(root, testing)
    environment = compose_environment(os.environ)
    prefix = compose_prefix(root, testing)
    rendered = subprocess.run(
        [*prefix, "--profile", "*", "config", "--format", "json"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    require(rendered.returncode == 0, "Compose configuration could not be resolved.")
    validate_model(object_value(json.loads(rendered.stdout)), root, testing, values)
    print(
        json.dumps(
            {
                "project": project_name(testing),
                "mode": "tests" if testing else "development",
                "validated": True,
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="Use the independent test project.")
    parser.add_argument("action", choices=("init", "validate"))
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if arguments.action == "init":
            initialize(root, arguments.test)
            return 0
        return run(root, arguments.test, arguments.action)
    except IsolationError as error:
        print(f"Refused: {error}")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
    ):
        print("Isolated setup failed; inspect local files/services without printing secrets.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
