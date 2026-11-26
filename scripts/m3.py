"""Operate only the explicit isolated development resources from a linked worktree."""

import argparse
import json
import os
import re
import socket
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import UUID

POSTGRES_IMAGE = (
    "postgres:18.6-bookworm@sha256:3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650"
)
VECTOR_TEST_IMAGE = "ai-market-intelligence-postgres:m3-vector-test"
VECTOR_RUNTIME_IMAGE = "ai-market-intelligence-postgres:m3-vector"
VECTOR_TOOLS_IMAGE = "ai-market-intelligence:m3-vector-tools"
VECTOR_RUNTIME_LABEL = "org.market-intelligence.m3.vector-runtime"
PROJECT = "ai-market-intelligence-m3"
VECTOR_DB_ACTIONS = {"db", "migrate", "check"}
VECTOR_RUNTIME_ACTIONS = {"validate", "build", *VECTOR_DB_ACTIONS}
PASSWORD_KEYS = {f"M3_{role}_PASSWORD" for role in ("ADMIN", "INGEST", "READ")}
PORT_KEYS = {"M3_API_PORT", "M3_POSTGRES_PORT"}
FOMC_BATCHES = ("2024-apr-may-dec", "2024-jan-mar", "2024-jun-jul")


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


def compose_prefix(
    root: Path, testing: bool, *, vectors: bool = False, vector_runtime: bool = False
) -> list[str]:
    require(not vectors or testing, "Vector builds require the isolated test project.")
    require(
        not vector_runtime or not testing, "Vector runtime requires the M3 development project."
    )
    prefix = [
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
    if vectors:
        prefix.extend(["-f", str(root / "compose.m3.vector.test.yaml")])
    if vector_runtime:
        prefix.extend(["-f", str(root / "compose.m3.vector.yaml")])
    return prefix


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
        if service in {"ingest", "process", "scheduler"}
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
    model: dict[str, Any],
    root: Path,
    testing: bool,
    values: Mapping[str, str],
    *,
    vectors: bool = False,
    vector_runtime: bool = False,
    database_image_id: str | None = None,
) -> None:
    """Check the fully resolved model before allowing any Docker mutation."""
    require(not vectors or testing, "Vector builds require the isolated test project.")
    require(
        not vector_runtime or not testing, "Vector runtime requires the M3 development project."
    )
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
    volumes = (
        {}
        if testing
        else {
            "postgres_data": {"name": PROJECT + "_postgres_data"},
            "fomc_artifacts": {"name": PROJECT + "_fomc_artifacts"},
        }
    )
    require(model.get("volumes", {}) == volumes, "Wrong persistent volume.")
    services = object_value(model.get("services"))
    expected_services = (
        {"db-test", "tests"}
        if testing
        else {"db", "api", "migrate", "check", "ingest", "process", "scheduler"}
    )
    require(set(services) == expected_services, "Unexpected services.")
    commands = {
        "api": ["serve", "--host", "0.0.0.0", "--port", "8000"],
        "migrate": ["init-db"],
        "check": ["check-db"],
        "ingest": ["ingest"],
        "process": ["process-fomc", "--pilot"],
        "scheduler": ["schedule", "run"],
    }
    if vector_runtime:
        for service in ("migrate", "check"):
            commands[service].append("--fomc-vectors")
    for service, raw in services.items():
        item = object_value(raw)
        is_database = service in {"db", "db-test"}
        if is_database:
            image = VECTOR_TEST_IMAGE if vectors else POSTGRES_IMAGE
        else:
            image = "ai-market-intelligence:m3" + ("-tests" if testing else "")
        if vector_runtime and service == "db":
            image = database_image_id or VECTOR_RUNTIME_IMAGE
        if vector_runtime and service in {"migrate", "check"}:
            image = VECTOR_TOOLS_IMAGE
        require(item.get("image") == image, "Wrong image tag or database digest.")
        require(item.get("restart") == "no", "Automatic restart is not enabled for M3.")
        if vectors and service == "db-test":
            require(item.get("pull_policy") == "never", "Vector tests require a local build.")
        if vector_runtime:
            local_image = service in {"db", "migrate", "check"}
            require(
                item.get("pull_policy") == ("never" if local_image else None),
                "Vector runtime requires the reviewed local images.",
            )
            profiles = (
                ["scheduler"]
                if service == "scheduler"
                else (["tools"] if service in {"migrate", "check", "ingest", "process"} else [])
            )
            require(item.get("profiles", []) == profiles, "Unexpected runtime service profile.")
            labels = {VECTOR_RUNTIME_LABEL: "v1"} if service == "db" else {}
            require(item.get("labels", {}) == labels, "Unexpected runtime service labels.")
        require(item.get("networks") == {"default": None}, "Service has an unexpected network.")
        environment = expected_environment(service, values, testing)
        if vectors and service == "tests":
            environment["RUN_VECTOR_TESTS"] = "1"
        require(
            item.get("environment") == environment,
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
        elif (vectors and service == "db-test") or (vector_runtime and service == "db"):
            require(
                build
                == {
                    "context": str(root / "docker" / "m3-vector-postgres"),
                    "dockerfile": "Dockerfile",
                    "target": "vector",
                },
                "Wrong vector database build context.",
            )
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
        mounts: list[dict[str, object]] = (
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
        if service in {"ingest", "process"}:
            mounts = [
                {
                    "type": "volume",
                    "source": "fomc_artifacts",
                    "target": "/app/data/fomc",
                    "volume": {},
                }
            ]
        if service == "process":
            mounts[0]["read_only"] = True
        require(item.get("volumes", []) == mounts, "Unexpected mount or demo volume.")
        require(
            item.get("tmpfs", []) == (["/var/lib/postgresql"] if service == "db-test" else []),
            "Wrong test storage.",
        )


def check_ports(values: Mapping[str, str], service: str) -> None:
    for key in ("M3_POSTGRES_PORT" if service == "db" else "M3_API_PORT",):
        with socket.socket() as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", int(values[key])))
            except OSError:
                raise IsolationError("An isolated localhost port is unavailable.") from None


def docker_output(command: list[str], root: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=30
    )
    require(result.returncode == 0, "Could not verify local Docker resources.")
    return result.stdout.strip()


def inspect_one(
    kind: str, reference: str, root: Path, environment: dict[str, str]
) -> dict[str, Any]:
    data = json.loads(docker_output(["docker", kind, "inspect", reference], root, environment))
    require(isinstance(data, list) and len(data) == 1, "Unexpected Docker inspection result.")
    return object_value(data[0])


def existing_database(
    prefix: list[str], root: Path, environment: dict[str, str]
) -> dict[str, Any] | None:
    container = docker_output([*prefix, "ps", "--all", "--quiet", "db"], root, environment)
    if not container:
        return None
    require(
        re.fullmatch(r"[0-9a-f]{12,64}", container) is not None,
        "Expected at most one M3 database container.",
    )
    data = inspect_one("container", container, root, environment)
    labels = object_value(object_value(data.get("Config")).get("Labels"))
    require(
        labels.get("com.docker.compose.project") == PROJECT
        and labels.get("com.docker.compose.service") == "db",
        "Unexpected database container.",
    )
    return data


def is_vector_database(container: dict[str, Any]) -> bool:
    config = object_value(container.get("Config"))
    return (
        VECTOR_RUNTIME_LABEL in object_value(config.get("Labels"))
        or config.get("Image") == VECTOR_RUNTIME_IMAGE
    )


def require_local_docker(root: Path, environment: dict[str, str]) -> None:
    require(
        not any(environment.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT")),
        "Vector runtime requires the selected local Docker context.",
    )
    endpoint = docker_output(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        root,
        environment,
    )
    require(
        endpoint.startswith(("unix:///", "npipe:////./pipe/")),
        "Vector runtime requires local Docker.",
    )


def prepare_vector_action(
    action: str,
    prefix: list[str],
    root: Path,
    environment: dict[str, str],
    values: Mapping[str, str],
    image_id: str,
) -> list[str]:
    image = inspect_one("image", VECTOR_RUNTIME_IMAGE, root, environment)
    require(image.get("Id") == image_id, "The vector database image differs from the reviewed ID.")
    volume = inspect_one("volume", PROJECT + "_postgres_data", root, environment)
    labels = object_value(volume.get("Labels"))
    require(
        volume.get("Name") == PROJECT + "_postgres_data"
        and volume.get("Driver") == "local"
        and volume.get("Scope") == "local"
        and not volume.get("Options")
        and labels.get("com.docker.compose.project") == PROJECT
        and labels.get("com.docker.compose.volume") == "postgres_data",
        "The existing M3 PostgreSQL volume could not be verified.",
    )
    container = existing_database(prefix, root, environment)
    require(container is not None, "Review the missing M3 database container before proceeding.")
    assert container is not None
    config = object_value(container.get("Config"))
    configured = {}
    for entry in config.get("Env", []):
        key, separator, value = entry.partition("=")
        if key.startswith("POSTGRES_"):
            require(bool(separator) and key not in configured, "Unexpected database environment.")
            configured[key] = value
    require(
        configured == expected_environment("db", values, False),
        "The existing database has unexpected M3 settings.",
    )
    host = object_value(container.get("HostConfig"))
    require(
        host.get("NetworkMode") == PROJECT + "_default"
        and host.get("PortBindings")
        == {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": values["M3_POSTGRES_PORT"]}]},
        "The existing database has unexpected network settings.",
    )
    mounts = container.get("Mounts", [])
    require(
        isinstance(mounts, list) and len(mounts) == 1,
        "The existing M3 database has an unexpected mount.",
    )
    mount = object_value(mounts[0])
    require(
        mount.get("Type") == "volume"
        and mount.get("Name") == PROJECT + "_postgres_data"
        and mount.get("Destination") == "/var/lib/postgresql"
        and mount.get("RW") is True,
        "The existing M3 database has an unexpected mount.",
    )
    vector = is_vector_database(container)
    if vector:
        require(container.get("Image") == image_id, "The M3 container uses another vector image.")
    else:
        require(
            config.get("Image") == POSTGRES_IMAGE, "Unexpected image on the existing M3 database."
        )
    state = object_value(container.get("State"))
    if action != "check":
        running = docker_output(
            [*prefix, "--profile", "*", "ps", "--status", "running", "--services"],
            root,
            environment,
        )
        require(set(running.splitlines()) <= {"db"}, "Stop other M3 services before this step.")
    if action == "db":
        require(
            state.get("Status") in {"exited", "created"}
            or (vector and state.get("Status") == "running"),
            "Stop the stock M3 database before changing its image.",
        )
        command = ["up", "-d", "--wait", "--no-deps", "--no-build", "--pull", "never"]
        if state.get("Status") == "running":
            command.append("--no-recreate")
        else:
            check_ports(values, "db")
        return [*command, "db"]
    require(
        vector
        and state.get("Status") == "running"
        and object_value(state.get("Health", {})).get("Status") == "healthy",
        "Start the reviewed vector database before running the migration or readiness check.",
    )
    inspect_one("image", VECTOR_TOOLS_IMAGE, root, environment)
    return ["run", "--rm", "--no-deps", "--pull", "never", action]


def run(
    root: Path,
    testing: bool,
    action: str,
    run_id: UUID | None = None,
    *,
    batch: str | None = None,
    vectors: bool = False,
    vector_runtime: bool = False,
    database_image_id: str | None = None,
) -> int:
    require(
        not vectors or (testing and action in {"validate", "build", "quality", "test", "down"}),
        "Vector mode is available only for isolated test operations.",
    )
    require(
        not vector_runtime or (not testing and action in VECTOR_RUNTIME_ACTIONS),
        "Vector runtime is available only for isolated M3 runtime operations.",
    )
    require(
        (database_image_id is not None) == (vector_runtime and action in VECTOR_DB_ACTIONS),
        "Use the reviewed database image ID for vector database operations.",
    )
    if database_image_id is not None:
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", database_image_id) is not None,
            "Use a full SHA-256 Docker image ID.",
        )
    require(
        not testing or not action.startswith("fomc-"),
        "FOMC runtime commands cannot use test resources.",
    )
    require(
        (action == "fomc-reconcile") == (run_id is not None),
        "Supply a run ID only for explicit FOMC reconciliation.",
    )
    require(
        batch is None or (batch in FOMC_BATCHES and action in {"fomc-ingest", "fomc-process"}),
        "Supply a reviewed batch only for FOMC acquisition or processing.",
    )
    check_worktree(root)
    values = read_settings(root, testing)
    environment = compose_environment(os.environ)
    if database_image_id is not None:
        environment["M3_VECTOR_DATABASE_IMAGE"] = database_image_id
    prefix = compose_prefix(root, testing, vectors=vectors, vector_runtime=vector_runtime)
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
    validate_model(
        object_value(json.loads(rendered.stdout)),
        root,
        testing,
        values,
        vectors=vectors,
        vector_runtime=vector_runtime,
        database_image_id=database_image_id,
    )
    print(
        json.dumps(
            {
                "project": project_name(testing),
                "mode": "tests"
                if testing
                else "vector-runtime"
                if vector_runtime
                else "development",
                "validated": True,
            }
        )
    )
    if action == "validate":
        return 0
    if vector_runtime:
        require_local_docker(root, environment)
        if database_image_id is None:
            arguments = ["build", "db", "migrate"]
        else:
            arguments = prepare_vector_action(
                action, prefix, root, environment, values, database_image_id
            )
        return subprocess.run(
            [*prefix, *arguments], cwd=root, env=environment, check=False, timeout=1800
        ).returncode
    if not testing and action != "build":
        database = existing_database(prefix, root, environment)
        require(
            database is None or not is_vector_database(database),
            "The M3 database uses the vector runtime. Review before using a stock command.",
        )
        if action in {"db", "api"}:
            check_ports(values, action)
    operations: list[list[str]]
    if action == "quality":
        operations = [
            ["run", "--rm", "--no-deps", "tests", *command]
            for command in (
                ["ruff", "format", "--check", "src", "migrations", "tests", "scripts"],
                ["ruff", "check", "src", "migrations", "tests", "scripts"],
                ["mypy"],
            )
        ]
    else:
        selector = ["--pilot"] if batch is None else ["--batch", batch]
        process = ["run", "--rm", "--no-deps", "process"]
        if batch is not None:
            process.extend(["process-fomc", *selector])
        operations = [
            {
                "build": ["build", "db-test", "tests"]
                if vectors
                else ["build", "tests" if testing else "migrate"],
                "test": ["up", "--abort-on-container-exit", "--exit-code-from", "tests"],
                "down": ["down"],
                "db": ["up", "-d", "--wait", "db"],
                "api": ["up", "-d", "--wait", "api"],
                "migrate": ["run", "--rm", "migrate"],
                "check": ["run", "--rm", "check"],
                "fomc-ingest": ["run", "--rm", "ingest", "ingest-fomc", *selector],
                "fomc-status": ["run", "--rm", "check", "ingest-fomc", "--status"],
                "fomc-process": process,
                "fomc-process-status": [
                    "run",
                    "--rm",
                    "--no-deps",
                    "check",
                    "process-fomc",
                    "--status",
                ],
                "fomc-reconcile": [
                    "run",
                    "--rm",
                    "ingest",
                    "ingest-fomc",
                    "--reconcile-run",
                    str(run_id),
                ],
            }[action]
        ]
    for arguments in operations:
        result = subprocess.run(
            [*prefix, *arguments], cwd=root, env=environment, check=False, timeout=1800
        )
        if result.returncode:
            return result.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test", action="store_true", help="Use the independent tmpfs test project."
    )
    parser.add_argument(
        "--vectors", action="store_true", help="Use the pinned pgvector image in --test mode."
    )
    parser.add_argument(
        "--vector-runtime", action="store_true", help="Use the reviewed M3 vector runtime overlay."
    )
    parser.add_argument(
        "--database-image-id", help="Reviewed Docker image ID for vector runtime DB actions."
    )
    parser.add_argument(
        "action",
        choices=(
            "init",
            "validate",
            "build",
            "quality",
            "test",
            "down",
            "db",
            "migrate",
            "api",
            "check",
            "fomc-ingest",
            "fomc-status",
            "fomc-reconcile",
            "fomc-process",
            "fomc-process-status",
        ),
    )
    parser.add_argument("--run-id", type=UUID, help="An inspected interrupted FOMC run")
    parser.add_argument("--batch", choices=FOMC_BATCHES, help="A reviewed 2024 FOMC batch")
    arguments = parser.parse_args(argv)
    if arguments.vector_runtime and (
        arguments.test or arguments.action not in VECTOR_RUNTIME_ACTIONS
    ):
        parser.error("--vector-runtime is available only for isolated M3 runtime operations.")
    if (arguments.database_image_id is not None) != (
        arguments.vector_runtime and arguments.action in VECTOR_DB_ACTIONS
    ):
        parser.error("--database-image-id is required for vector runtime db, migrate and check.")
    if arguments.vectors and (
        not arguments.test
        or arguments.action not in {"validate", "build", "quality", "test", "down"}
    ):
        parser.error("--vectors is available only for isolated test operations.")
    if arguments.action in {"quality", "test", "down"} and not arguments.test:
        parser.error("This action requires --test; runtime cleanup is deliberately not exposed.")
    if arguments.action in {"db", "migrate", "api", "check"} and arguments.test:
        parser.error("This action is for the isolated development project.")
    if (arguments.action == "fomc-reconcile") != (arguments.run_id is not None):
        parser.error("Supply --run-id only with fomc-reconcile.")
    if arguments.batch is not None and arguments.action not in {"fomc-ingest", "fomc-process"}:
        parser.error("Supply --batch only with fomc-ingest or fomc-process.")
    if arguments.test and arguments.action.startswith("fomc-"):
        parser.error("FOMC runtime commands require the isolated development project.")
    root = Path(__file__).resolve().parents[1]
    try:
        if arguments.action == "init":
            initialize(root, arguments.test)
            return 0
        if arguments.run_id is not None:
            return run(root, arguments.test, arguments.action, arguments.run_id)
        if arguments.batch is not None:
            return run(root, arguments.test, arguments.action, batch=arguments.batch)
        if arguments.vectors:
            return run(root, arguments.test, arguments.action, vectors=True)
        if arguments.vector_runtime:
            return run(
                root,
                False,
                arguments.action,
                vector_runtime=True,
                database_image_id=arguments.database_image_id,
            )
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
