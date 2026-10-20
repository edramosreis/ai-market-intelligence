"""Bounded content-addressed originals, published atomically without replacing objects."""

import hashlib
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from market_intelligence.fomc.client import MAX_RESPONSE_BYTES


class ArtifactError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("artifact_error")


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def _directory(self, directory: Path) -> None:
        # Walk existing components before making anything, including the supplied root.
        for part in reversed((directory, *directory.parents)):
            if part.is_symlink() or (part.exists() and not part.is_dir()):
                raise ArtifactError()
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, digest: str) -> Path:
        if re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise ArtifactError()
        return self.root / "sha256" / digest[:2] / (digest + ".html")

    def verify(self, digest: str, size: int) -> Path:
        target = self.path(digest)
        try:
            for part in (target, *target.parents):
                if part.is_symlink():
                    raise ArtifactError()
            if not stat.S_ISREG(target.lstat().st_mode) or target.stat().st_size != size:
                raise ArtifactError()
            with target.open("rb") as handle:
                content = handle.read(MAX_RESPONSE_BYTES + 1)
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise ArtifactError()
        except OSError:
            raise ArtifactError() from None
        return target

    def put(self, body: bytes) -> Path:
        if not 1 <= len(body) <= MAX_RESPONSE_BYTES:
            raise ArtifactError()
        digest = hashlib.sha256(body).hexdigest()
        target = self.path(digest)
        temporary: str | None = None
        try:
            self._directory(target.parent)
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".pending-", delete=False
            ) as handle:
                temporary = handle.name
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            if hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != digest:
                raise ArtifactError()
            with suppress(FileExistsError):
                os.link(temporary, target)
            # Durability of the directory entry is supported on the target Linux runtime.
            if os.name == "posix":
                fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return self.verify(digest, len(body))
        except OSError:
            raise ArtifactError() from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
