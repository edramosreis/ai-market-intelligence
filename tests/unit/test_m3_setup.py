"""The local launcher must refuse shared resources before any Docker mutation."""

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

from market_intelligence.fomc.catalog import ADDITIONAL_BATCHES


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
                "process": base | writer,
                "scheduler": base | writer | {"SCHEDULER_ENABLED": "false", "SCHEDULER_JOBS": ""},
            }
        )
    model: dict[str, Any] = {
        "name": name,
        "networks": {"default": {"name": name + "_default", "ipam": {}}},
        "services": {},
    }
    if not testing:
        model["volumes"] = {
            "postgres_data": {"name": name + "_postgres_data"},
            "fomc_artifacts": {"name": name + "_fomc_artifacts"},
        }
    commands = {
        "api": ["serve", "--host", "0.0.0.0", "--port", "8000"],
        "migrate": ["init-db"],
        "check": ["check-db"],
        "ingest": ["ingest"],
        "process": ["process-fomc", "--pilot"],
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
        if service in {"ingest", "process"}:
            item["volumes"] = [
                {
                    "type": "volume",
                    "source": "fomc_artifacts",
                    "target": "/app/data/fomc",
                    "volume": {},
                }
            ]
        if service == "process":
            item["volumes"][0]["read_only"] = True
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


def test_validation_precedes_docker_mutation(
    launcher: ModuleType, linked_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = configuration(linked_root, True)
    model = resolved_model(linked_root, values, True)
    model["services"]["tests"]["environment"]["POSTGRES_HOST"] = "db"
    execute = MagicMock(return_value=subprocess.CompletedProcess([], 0, json.dumps(model), ""))
    monkeypatch.setattr(launcher.subprocess, "run", execute)
    with pytest.raises(launcher.IsolationError):
        launcher.run(linked_root, True, "test")
    assert execute.call_count == 1
    assert execute.call_args.args[0][-3:] == ["config", "--format", "json"]


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


def test_occupied_port_refuses_start(launcher: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = MagicMock()
    probe.__enter__.return_value.bind.side_effect = OSError("synthetic occupied port")
    monkeypatch.setattr(launcher.socket, "socket", MagicMock(return_value=probe))
    with pytest.raises(launcher.IsolationError, match="unavailable"):
        launcher.check_ports({"M3_API_PORT": "8001"}, "api")


def test_runtime_cleanup_not_exposed(launcher: ModuleType) -> None:
    with pytest.raises(SystemExit) as error:
        launcher.main(["down"])
    assert error.value.code == 2


@pytest.mark.parametrize("mutation", ["foreign_volume", "missing_mount", "bind_mount", "api_mount"])
def test_fomc_artifact_boundary(launcher: ModuleType, linked_root: Path, mutation: str) -> None:
    values = configuration(linked_root, False)
    model = resolved_model(linked_root, values, False)
    if mutation == "foreign_volume":
        model["volumes"]["fomc_artifacts"]["name"] = "ai-market-intelligence_fomc_artifacts"
    elif mutation == "missing_mount":
        del model["services"]["ingest"]["volumes"]
    elif mutation == "bind_mount":
        model["services"]["ingest"]["volumes"][0]["type"] = "bind"
    else:
        model["services"]["api"]["volumes"] = model["services"]["ingest"]["volumes"]
    with pytest.raises(launcher.IsolationError):
        launcher.validate_model(model, linked_root, False, values)


@pytest.mark.parametrize(
    "action,service,flag",
    [("fomc-ingest", "ingest", "--pilot"), ("fomc-status", "check", "--status")],
)
def test_fomc_manual_operations_are_exact(
    launcher: ModuleType,
    linked_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    service: str,
    flag: str,
) -> None:
    values = configuration(linked_root, False)
    model = resolved_model(linked_root, values, False)
    execute = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(model), ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(launcher.subprocess, "run", execute)
    assert launcher.run(linked_root, False, action) == 0
    assert execute.call_args.args[0][-5:] == ["run", "--rm", service, "ingest-fomc", flag]


@pytest.mark.parametrize(
    "args",
    [
        ["--test", "fomc-ingest"],
        ["--test", "fomc-status"],
        ["--test", "fomc-process"],
        ["--test", "fomc-process-status"],
        ["fomc-reconcile"],
        ["validate", "--run-id", "00000000-0000-0000-0000-000000000001"],
        ["fomc-status", "--batch", "2024-apr-may-dec"],
        ["fomc-process", "--batch", "2025"],
        ["--test", "fomc-ingest", "--batch", "2024-apr-may-dec"],
    ],
)
def test_fomc_runtime_option_misuse_refused_before_docker(
    launcher: ModuleType, args: list[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        launcher.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("action", "expected", "batch"),
    [
        ("fomc-process", ["run", "--rm", "--no-deps", "process"], None),
        (
            "fomc-process-status",
            ["run", "--rm", "--no-deps", "check", "process-fomc", "--status"],
            None,
        ),
        (
            "fomc-process",
            ["run", "--rm", "--no-deps", "process", "process-fomc", "--batch", "2024-apr-may-dec"],
            "2024-apr-may-dec",
        ),
        (
            "fomc-ingest",
            ["run", "--rm", "ingest", "ingest-fomc", "--batch", "2024-jan-mar"],
            "2024-jan-mar",
        ),
    ],
)
def test_processing_commands_use_only_the_validated_service(
    launcher: ModuleType,
    linked_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected: list[str],
    batch: str | None,
) -> None:
    values = configuration(linked_root, False)
    model = resolved_model(linked_root, values, False)
    launch = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(model), ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0),
        ]
    )
    monkeypatch.setattr(launcher.subprocess, "run", launch)
    assert set(launcher.FOMC_BATCHES) == set(ADDITIONAL_BATCHES)
    assert launcher.run(linked_root, False, action, batch=batch) == 0
    assert launch.call_args.args[0][-len(expected) :] == expected


def test_processing_mount_requires_read_only(launcher: ModuleType, linked_root: Path) -> None:
    values = configuration(linked_root, False)
    model = resolved_model(linked_root, values, False)
    model["services"]["process"]["volumes"][0].pop("read_only")
    with pytest.raises(launcher.IsolationError, match="Unexpected mount"):
        launcher.validate_model(model, linked_root, False, values)


@pytest.mark.parametrize("mutation", [None, "image", "context", "port", "mount", "pull", "role"])
def test_vector_database_requires_the_exact_disposable_build(
    launcher: ModuleType, linked_root: Path, mutation: str | None
) -> None:
    values = configuration(linked_root, True)
    model = resolved_model(linked_root, values, True)
    database = model["services"]["db-test"]
    database.update(
        image="ai-market-intelligence-postgres:m3-vector-test",
        pull_policy="never",
        build=dict(
            context=str(linked_root / "docker" / "m3-vector-postgres"),
            dockerfile="Dockerfile",
            target="vector",
        ),
    )
    model["services"]["tests"]["environment"]["RUN_VECTOR_TESTS"] = "1"
    if mutation is None:
        launcher.validate_model(model, linked_root, True, values, vectors=True)
        return
    if mutation == "image":
        database["image"] = "ai-market-intelligence-postgres:m3-vector"
    elif mutation == "context":
        database["build"]["context"] = str(linked_root.parent)
    elif mutation == "port":
        database["ports"] = [{"published": "55433"}]
    elif mutation == "mount":
        database["volumes"] = [{"source": "ai-market-intelligence-m3_postgres_data"}]
    elif mutation == "pull":
        database["pull_policy"] = "always"
    else:
        database["environment"]["POSTGRES_USER"] = "m3_admin"
    with pytest.raises(launcher.IsolationError):
        launcher.validate_model(model, linked_root, True, values, vectors=True)


def test_vector_mode_cannot_select_the_persistent_database(launcher: ModuleType) -> None:
    with pytest.raises(SystemExit) as error:
        launcher.main(["--vectors", "build"])
    assert error.value.code == 2


RUNTIME_IMAGE_ID = "sha256:" + "a" * 64


def runtime_model(
    launcher: ModuleType, root: Path, values: dict[str, str], *, pin: bool = False
) -> dict[str, Any]:
    model = resolved_model(root, values, False)
    services = model["services"]
    services["db"].update(
        image=RUNTIME_IMAGE_ID if pin else launcher.VECTOR_RUNTIME_IMAGE,
        pull_policy="never",
        labels={launcher.VECTOR_RUNTIME_LABEL: "v1"},
        build={
            "context": str(root / "docker" / "m3-vector-postgres"),
            "dockerfile": "Dockerfile",
            "target": "vector",
        },
    )
    for name in ("migrate", "check", "ingest", "process"):
        services[name]["profiles"] = ["tools"]
    services["scheduler"]["profiles"] = ["scheduler"]
    for name in ("migrate", "check"):
        services[name].update(image=launcher.VECTOR_TOOLS_IMAGE, pull_policy="never")
        services[name]["command"].append("--fomc-vectors")
    return model


@pytest.mark.parametrize(
    "mutation",
    [None, "demo", "test", "image", "app_image", "volume", "role", "scheduler", "profile", "flag"],
)
def test_runtime_overlay_keeps_the_exact_project_and_service_contract(
    launcher: ModuleType, linked_root: Path, mutation: str | None
) -> None:
    values = configuration(linked_root, False)
    model = runtime_model(launcher, linked_root, values, pin=True)
    services = model["services"]
    if mutation == "demo":
        model["name"] = "ai-market-intelligence"
    elif mutation == "test":
        model["name"] = "ai-market-intelligence-m3-tests"
    elif mutation == "image":
        services["db"]["image"] = launcher.VECTOR_TEST_IMAGE
    elif mutation == "app_image":
        services["migrate"]["image"] = "ai-market-intelligence:m3"
    elif mutation == "volume":
        model["volumes"]["postgres_data"]["name"] = "ai-market-intelligence_postgres_data"
    elif mutation == "role":
        services["check"]["environment"]["POSTGRES_READ_USER"] = "m3_admin"
    elif mutation == "scheduler":
        services["scheduler"]["environment"]["SCHEDULER_ENABLED"] = "true"
    elif mutation == "profile":
        services["scheduler"]["profiles"] = []
    elif mutation == "flag":
        services["migrate"]["command"] = ["init-db"]
    if mutation is None:
        launcher.validate_model(
            model,
            linked_root,
            False,
            values,
            vector_runtime=True,
            database_image_id=RUNTIME_IMAGE_ID,
        )
    else:
        with pytest.raises(launcher.IsolationError):
            launcher.validate_model(
                model,
                linked_root,
                False,
                values,
                vector_runtime=True,
                database_image_id=RUNTIME_IMAGE_ID,
            )


def test_vector_build_only_builds_the_separate_local_images(
    launcher: ModuleType, linked_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = configuration(linked_root, False)
    model = runtime_model(launcher, linked_root, values)
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(model), ""),
            subprocess.CompletedProcess([], 0, "unix:///var/run/docker.sock", ""),
            subprocess.CompletedProcess([], 0),
        ]
    )
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setenv("M3_VECTOR_DATABASE_IMAGE", "unreviewed")
    assert launcher.run(linked_root, False, "build", vector_runtime=True) == 0
    resolved = run.call_args_list[0]
    assert str(linked_root / "compose.m3.vector.yaml") in resolved.args[0]
    assert "M3_VECTOR_DATABASE_IMAGE" not in resolved.kwargs["env"]
    assert run.call_args.args[0][-3:] == ["build", "db", "migrate"]


@pytest.mark.parametrize(
    "args",
    [
        ["--test", "--vector-runtime", "validate"],
        ["--vector-runtime", "api"],
        ["--vector-runtime", "fomc-ingest"],
        ["--vector-runtime", "down"],
        ["--vector-runtime", "migrate"],
        ["check", "--database-image-id", RUNTIME_IMAGE_ID],
    ],
)
def test_vector_runtime_rejects_inapplicable_commands(
    launcher: ModuleType, args: list[str]
) -> None:
    with pytest.raises(SystemExit) as result:
        launcher.main(args)
    assert result.value.code == 2


def test_stock_launcher_refuses_a_vector_database(
    launcher: ModuleType, linked_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = configuration(linked_root, False)
    run = MagicMock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(resolved_model(linked_root, values, False)), ""
        )
    )
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(
        launcher,
        "existing_database",
        MagicMock(return_value={"Config": {"Labels": {launcher.VECTOR_RUNTIME_LABEL: "v1"}}}),
    )
    with pytest.raises(launcher.IsolationError, match="uses the vector runtime"):
        launcher.run(linked_root, False, "db")
    run.assert_called_once()


@pytest.mark.parametrize(
    ("action", "mutation", "expected"),
    [
        ("db", None, ["up", "-d", "--wait", "--no-deps", "--no-build", "--pull", "never", "db"]),
        ("db", "stock_running", None),
        ("db", "unexpected_running_service", None),
        ("check", None, ["run", "--rm", "--no-deps", "--pull", "never", "check"]),
        ("migrate", None, ["run", "--rm", "--no-deps", "--pull", "never", "migrate"]),
        ("migrate", "wrong_image_id", None),
        ("migrate", "wrong_volume", None),
        ("migrate", "wrong_mount", None),
        ("migrate", "wrong_dns", None),
        ("migrate", "wrong_role", None),
        ("migrate", "stock_running", None),
        ("migrate", "not_healthy", None),
    ],
)
def test_vector_operations_verify_the_existing_target_and_never_start_dependencies(
    launcher: ModuleType,
    linked_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    mutation: str | None,
    expected: list[str] | None,
) -> None:
    values = configuration(linked_root, False)
    vector = action != "db" and mutation != "stock_running"
    project = launcher.PROJECT
    database: dict[str, Any] = {
        "Image": RUNTIME_IMAGE_ID,
        "Config": {
            "Image": RUNTIME_IMAGE_ID if vector else launcher.POSTGRES_IMAGE,
            "Labels": {launcher.VECTOR_RUNTIME_LABEL: "v1"} if vector else {},
            "Env": [
                f"{key}={value}"
                for key, value in launcher.expected_environment("db", values, False).items()
            ],
        },
        "HostConfig": {
            "NetworkMode": project + "_default",
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55433"}]},
        },
        "State": {
            "Status": "running" if vector or mutation == "stock_running" else "exited",
            "Health": {"Status": "starting" if mutation == "not_healthy" else "healthy"},
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": project + "_postgres_data",
                "Destination": "/var/lib/postgresql",
                "RW": True,
            },
        ],
    }
    volume: dict[str, Any] = {
        "Name": project + "_postgres_data",
        "Driver": "local",
        "Scope": "local",
        "Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.volume": "postgres_data",
        },
    }
    if mutation == "wrong_volume":
        volume["Driver"] = "external"
    elif mutation == "wrong_mount":
        database["Mounts"][0]["RW"] = False
    elif mutation == "wrong_dns":
        database["HostConfig"]["NetworkMode"] = "host"
    elif mutation == "wrong_role":
        database["Config"]["Env"].append("POSTGRES_USER=market_admin")
    inspect = MagicMock(
        side_effect=lambda kind, *_: (
            volume
            if kind == "volume"
            else {"Id": "unexpected" if mutation == "wrong_image_id" else RUNTIME_IMAGE_ID}
        )
    )
    monkeypatch.setattr(launcher, "inspect_one", inspect)
    monkeypatch.setattr(launcher, "existing_database", MagicMock(return_value=database))
    monkeypatch.setattr(
        launcher,
        "docker_output",
        MagicMock(return_value="api\ndb" if mutation == "unexpected_running_service" else "db"),
    )
    monkeypatch.setattr(launcher, "check_ports", MagicMock())
    if expected is None:
        with pytest.raises(launcher.IsolationError):
            launcher.prepare_vector_action(
                action, ["docker", "compose"], linked_root, {}, values, RUNTIME_IMAGE_ID
            )
    else:
        assert (
            launcher.prepare_vector_action(
                action, ["docker", "compose"], linked_root, {}, values, RUNTIME_IMAGE_ID
            )
            == expected
        )
