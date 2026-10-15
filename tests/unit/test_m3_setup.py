"""Verify isolated setup refuses shared configuration and resources."""

import importlib.util
import secrets
from pathlib import Path
from types import ModuleType

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
