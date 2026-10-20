import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from market_intelligence.fomc.artifacts import ArtifactError, ArtifactStore


def test_atomic_replay_and_concurrent_placement(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    body = b"<html>synthetic original</html>"
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(store.put, [body, body]))
    assert paths[0] == paths[1]
    assert paths[0].read_bytes() == body
    assert list(tmp_path.rglob(".pending-*")) == []
    assert store.put(body) == paths[0]


def test_corrupt_existing_object_is_never_replaced(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    body = b"expected"
    target = store.path(hashlib.sha256(body).hexdigest())
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt!")
    with pytest.raises(ArtifactError):
        store.put(body)
    assert target.read_bytes() == b"corrupt!"
    assert list(tmp_path.rglob(".pending-*")) == []


def test_unsupported_atomic_placement_is_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("sensitive filesystem details")

    monkeypatch.setattr("market_intelligence.fomc.artifacts.os.link", fail)
    with pytest.raises(ArtifactError, match="^artifact_error$"):
        ArtifactStore(tmp_path).put(b"original")
    assert list(tmp_path.rglob("*html")) == []
    assert list(tmp_path.rglob(".pending-*")) == []


def test_reject_empty_oversize_and_path_injection(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    for body in [b"", b"x" * (2 * 1024 * 1024 + 1)]:
        with pytest.raises(ArtifactError):
            store.put(body)
    with pytest.raises(ArtifactError):
        store.path("../unsafe")


@pytest.mark.skipif(__import__("os").name != "posix", reason="Runtime symlink boundary uses Linux")
def test_symlink_root_and_existing_object_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ArtifactError):
        ArtifactStore(link).put(b"original")
    store = ArtifactStore(real)
    body = b"original"
    target = store.path(hashlib.sha256(body).hexdigest())
    target.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(body)
    target.symlink_to(outside)
    with pytest.raises(ArtifactError):
        store.put(body)
    assert outside.read_bytes() == body
