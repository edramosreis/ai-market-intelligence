"""Verify isolated setup refuses shared configuration and resources."""

import copy
import importlib.util
import json
import secrets
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def launcher() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "m3.py"
    spec = importlib.util.spec_from_file_location("m3_setup", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def linked_root(tmp_path: Path) -> Path:
    (tmp_path / ".git").write_text("gitdir: unused-synthetic-worktree-metadata\n")
    (tmp_path / "Dockerfile").touch()
    (tmp_path / "compose.m3.yaml").touch()
    return tmp_path


def configuration(root: Path, testing: bool) -> dict[str, str]:
    values = {
        f"M3_{role}_PASSWORD": secrets.token_urlsafe(32) for role in ("ADMIN", "INGEST", "READ")
    }
    if not testing:
        values.update(M3_API_PORT="8001", M3_POSTGRES_PORT="55433")
    path = root / (".env.m3.test" if testing else ".env.m3")
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    return values


def resolved_model(root: Path, values: dict[str, str], testing: bool) -> dict[str, Any]:
    """Small synthetic resolved Compose model; no Docker or local credentials needed."""
    name = "ai-market-intelligence-m3" + ("-tests" if testing else "")
    image = (
        "postgres:18.6-bookworm@sha256:"
        "3725f4e2499eef5134592b3b4ab79a543ed7f8e533b05b5b637af926630f6650"
    )
    db_name = "market_intelligence_test" if testing else "market_intelligence_m3"
    role = "m3_test" if testing else "m3"
    base = {
        "POSTGRES_HOST": "db-test" if testing else "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": db_name,
    }
    reader = {
        "POSTGRES_READ_USER": role + "_reader",
        "POSTGRES_READ_PASSWORD": values["M3_READ_PASSWORD"],
    }
    writer = {
        "POSTGRES_INGEST_USER": role + "_ingest",
        "POSTGRES_INGEST_PASSWORD": values["M3_INGEST_PASSWORD"],
    }
    admin = {
        "POSTGRES_ADMIN_USER": role + "_admin",
        "POSTGRES_ADMIN_PASSWORD": values["M3_ADMIN_PASSWORD"],
    }
    environments = {
        "db-test" if testing else "db": {
            "POSTGRES_DB": db_name,
            "POSTGRES_USER": role + "_admin",
            "POSTGRES_PASSWORD": values["M3_ADMIN_PASSWORD"],
        },
    }
    if testing:
        environments["tests"] = base | admin | writer | reader | {"RUN_POSTGRES_TESTS": "1"}
    else:
        environments.update(
            {
                "api": base | reader | {"AGENT_ENABLED": "false"},
                "check": base | reader,
                "migrate": base | admin | writer | reader,
                "ingest": base | writer,
                "scheduler": base | writer | {"SCHEDULER_ENABLED": "false", "SCHEDULER_JOBS": ""},
            }
        )
    model: dict[str, Any] = {
        "name": name,
        "networks": {"default": {"name": name + "_default", "ipam": {}}},
        "services": {},
    }
    if not testing:
        model["volumes"] = {"postgres_data": {"name": name + "_postgres_data"}}
    commands = {
        "api": ["serve", "--host", "0.0.0.0", "--port", "8000"],
        "migrate": ["init-db"],
        "check": ["check-db"],
        "ingest": ["ingest"],
        "scheduler": ["schedule", "run"],
    }
    for service, environment in environments.items():
        item: dict[str, Any] = {
            "image": image
            if service.startswith("db")
            else "ai-market-intelligence:m3" + ("-tests" if testing else ""),
            "restart": "no",
            "networks": {"default": None},
            "environment": environment,
            "entrypoint": None,
        }
        if service in commands:
            item["command"] = commands[service]
        if service in {"migrate", "tests"}:
            item["build"] = {
                "context": str(root),
                "dockerfile": "Dockerfile",
                "target": "development" if testing else "runtime",
            }
        if service in {"db", "api"}:
            item["ports"] = [
                {
                    "mode": "ingress",
                    "host_ip": "127.0.0.1",
                    "target": 5432 if service == "db" else 8000,
                    "published": "55433" if service == "db" else "8001",
                    "protocol": "tcp",
                }
            ]
        if service == "db":
            item["volumes"] = [
                {
                    "type": "volume",
                    "source": "postgres_data",
                    "target": "/var/lib/postgresql",
                    "volume": {},
                }
            ]
        if service == "db-test":
            item["tmpfs"] = ["/var/lib/postgresql"]
        model["services"][service] = item
    return model


def test_initializer_refuses_primary_clone(launcher: ModuleType, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(launcher.IsolationError, match="linked"):
        launcher.initialize(tmp_path, False)
    assert not (tmp_path / ".env.m3").exists()


def test_initializer_creates_independent_files_without_overwrite(
    launcher: ModuleType, linked_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = secrets.token_urlsafe(24)
    (linked_root / ".env").write_text(sentinel)
    launcher.initialize(linked_root, False)
    launcher.initialize(linked_root, True)
    first = launcher.read_settings(linked_root, False)
    second = launcher.read_settings(linked_root, True)
    keys = ("M3_ADMIN_PASSWORD", "M3_INGEST_PASSWORD", "M3_READ_PASSWORD")
    assert len({first[k] for k in keys} | {second[k] for k in keys}) == 6
    before = (linked_root / ".env.m3").read_bytes()
    with pytest.raises(FileExistsError):
        launcher.initialize(linked_root, False)
    assert (linked_root / ".env.m3").read_bytes() == before
    assert (linked_root / ".env").read_text() == sentinel
    output = capsys.readouterr().out
    assert all(first[k] not in output and second[k] not in output for k in keys)


@pytest.mark.parametrize(
    "mutation",
    ["duplicate", "foreign", "password", "same_password", "demo_port", "same_port", "port_syntax"],
)
def test_invalid_configuration_refused(
    launcher: ModuleType, linked_root: Path, mutation: str
) -> None:
    values = configuration(linked_root, False)
    if mutation == "foreign":
        values["OPENAI_API_KEY"] = "synthetic-unused-key"
    elif mutation == "password":
        values["M3_ADMIN_PASSWORD"] = "REPLACE_WITH_PASSWORD"
    elif mutation == "same_password":
        values["M3_INGEST_PASSWORD"] = values["M3_ADMIN_PASSWORD"]
    elif mutation == "demo_port":
        values["M3_API_PORT"] = "8000"
    elif mutation == "same_port":
        values["M3_API_PORT"] = values["M3_POSTGRES_PORT"]
    elif mutation == "port_syntax":
        values["M3_API_PORT"] = "+8001"
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    if mutation == "duplicate":
        text += "M3_API_PORT=8001\n"
    (linked_root / ".env.m3").write_text(text)
    with pytest.raises(launcher.IsolationError):
        launcher.read_settings(linked_root, False)


def test_ambient_overrides_removed(launcher: ModuleType) -> None:
    dirty = dict.fromkeys(
        (
            "COMPOSE_FILE",
            "COMPOSE_PROFILES",
            "M3_ADMIN_PASSWORD",
            "POSTGRES_HOST",
            "AGENT_ENABLED",
            "OPENAI_API_KEY",
            "SCHEDULER_ENABLED",
            "API_PORT",
            "postgres_db",
        ),
        "untrusted",
    )
    assert launcher.compose_environment(dirty | {"PATH": "docker-bin"}) == {"PATH": "docker-bin"}


@pytest.mark.parametrize("testing", [False, True])
def test_safe_model(launcher: ModuleType, linked_root: Path, testing: bool) -> None:
    values = configuration(linked_root, testing)
    launcher.validate_model(
        resolved_model(linked_root, values, testing), linked_root, testing, values
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "project",
        "network",
        "volume",
        "image",
        "database",
        "password",
        "model_key",
        "agent",
        "scheduler",
        "port",
        "public_port",
        "mount",
        "build",
        "command",
        "restart",
        "socket",
        "extra_service",
    ],
)
def test_unsafe_runtime_model_refused(
    launcher: ModuleType, linked_root: Path, mutation: str
) -> None:
    values = configuration(linked_root, False)
    model = resolved_model(linked_root, values, False)
    services = model["services"]
    if mutation == "project":
        model["name"] = "ai-market-intelligence"
    elif mutation == "network":
        model["networks"]["default"] = {"name": "ai-market-intelligence_default", "external": True}
    elif mutation == "volume":
        model["volumes"]["postgres_data"]["name"] = "ai-market-intelligence_postgres_data"
    elif mutation == "image":
        services["migrate"]["image"] = "ai-market-intelligence:local"
    elif mutation == "database":
        services["migrate"]["environment"]["POSTGRES_DB"] = "market_intelligence"
    elif mutation == "password":
        services["migrate"]["environment"]["POSTGRES_ADMIN_PASSWORD"] = secrets.token_urlsafe(32)
    elif mutation == "model_key":
        services["api"]["environment"]["OPENAI_API_KEY"] = "synthetic-unused-key"
    elif mutation == "agent":
        services["api"]["environment"]["AGENT_ENABLED"] = "true"
    elif mutation == "scheduler":
        services["scheduler"]["environment"]["SCHEDULER_ENABLED"] = "true"
    elif mutation == "port":
        services["api"]["ports"][0]["published"] = "8000"
    elif mutation == "public_port":
        services["db"]["ports"][0]["host_ip"] = "0.0.0.0"
    elif mutation == "mount":
        services["api"]["volumes"] = [
            {"type": "bind", "source": "/demo/.env", "target": "/app/.env"}
        ]
    elif mutation == "build":
        services["migrate"]["build"]["context"] = str(linked_root.parent)
    elif mutation == "command":
        services["api"]["command"] = ["ingest", "--refresh"]
    elif mutation == "restart":
        services["db"]["restart"] = "unless-stopped"
    elif mutation == "socket":
        services["api"]["use_api_socket"] = True
    else:
        services["demo"] = copy.deepcopy(services["api"])
    with pytest.raises(launcher.IsolationError):
        launcher.validate_model(model, linked_root, False, values)


@pytest.mark.parametrize("mutation", ["host", "db", "port", "storage", "volume", "image"])
def test_test_boundary_preserved(launcher: ModuleType, linked_root: Path, mutation: str) -> None:
    values = configuration(linked_root, True)
    model = resolved_model(linked_root, values, True)
    if mutation == "host":
        model["services"]["tests"]["environment"]["POSTGRES_HOST"] = "db"
    elif mutation == "db":
        model["services"]["tests"]["environment"]["POSTGRES_DB"] = "market_intelligence_m3"
    elif mutation == "port":
        model["services"]["db-test"]["ports"] = [{"published": "55432"}]
    elif mutation == "storage":
        model["services"]["db-test"].pop("tmpfs")
    elif mutation == "volume":
        model["services"]["db-test"]["volumes"] = [
            {"source": "demo", "target": "/var/lib/postgresql"}
        ]
    else:
        model["services"]["tests"]["image"] = "ai-market-intelligence-tests-tests"
    with pytest.raises(launcher.IsolationError):
        launcher.validate_model(model, linked_root, True, values)


def test_validation_output_is_redacted_and_paths_explicit(
    launcher: ModuleType,
    linked_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = configuration(linked_root, True)
    model = resolved_model(linked_root, values, True)
    execute = MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps(model), ""))
    monkeypatch.setattr(launcher.subprocess, "run", execute)
    monkeypatch.setenv("COMPOSE_FILE", "other-compose.yaml")
    assert launcher.run(linked_root, True, "validate") == 0
    command = execute.call_args.args[0]
    assert str(linked_root / "compose.m3.test.yaml") in command
    assert str(linked_root / ".env.m3.test") in command
    assert "ai-market-intelligence-m3-tests" in command
    assert "COMPOSE_FILE" not in execute.call_args.kwargs["env"]
    output = capsys.readouterr().out
    assert all(value not in output for value in values.values())
