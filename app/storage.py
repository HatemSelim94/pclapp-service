"""Cloud files on a local volume.

Layout under ``DATA_DIR``::

    clouds/<cloud-uuid>.<fmt>     final files, path stored in clouds.path relative to DATA_DIR
    tmp/<random>.part             uploads in flight; anything left here is garbage

Paths are built from the cloud's UUID and a validated format, never from user input, so
there is nothing to sanitise and no way to escape ``DATA_DIR``.

The filesystem is not transactional. The order used by the upload endpoint is:
write to ``tmp/`` → ``rename`` into ``clouds/`` → insert the row and commit → on commit
failure, delete the file. A crash between rename and commit leaves an orphan file with no
row; ``cleanup`` sweeps those. A row without a file cannot happen.
"""

from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from app.config import Settings

CHUNK = 1024 * 1024


class UploadTooLargeError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"Upload exceeds {limit} bytes.")
        self.limit = limit


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.data_dir
        self.max_bytes = settings.max_upload_bytes
        self.allowed_formats = settings.allowed_formats

    # --- paths -------------------------------------------------------------------------

    def rel_path(self, cloud_id: UUID, fmt: str) -> str:
        if fmt not in self.allowed_formats:
            raise ValueError(f"unsupported format {fmt!r}")
        return f"clouds/{cloud_id}.{fmt}"

    def abs_path(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"path escapes DATA_DIR: {rel!r}")
        return path

    def ensure_dirs(self) -> None:
        (self.root / "clouds").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)

    # --- writing -----------------------------------------------------------------------

    def write_temp(self, stream: BinaryIO) -> tuple[Path, int]:
        """Stream ``stream`` into ``tmp/``, enforcing the size cap. Returns (path, size)."""
        self.ensure_dirs()
        tmp = self.root / "tmp" / f"{secrets.token_hex(16)}.part"
        size = 0
        try:
            with tmp.open("wb") as out:
                while chunk := stream.read(CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadTooLargeError(self.max_bytes)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return tmp, size

    def finalize(self, tmp: Path, rel: str) -> Path:
        """Move a temp file to its final place. Atomic on the same filesystem."""
        final = self.abs_path(rel)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        return final

    def write_file(self, src: Path, rel: str) -> Path:
        """Copy a file produced elsewhere (e.g. by a job runner) into place."""
        final = self.abs_path(rel)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, final)
        return final

    # --- reading / deleting --------------------------------------------------------------

    def open(self, rel: str) -> BinaryIO:
        return self.abs_path(rel).open("rb")

    def exists(self, rel: str) -> bool:
        return self.abs_path(rel).is_file()

    def delete(self, rel: str) -> None:
        self.abs_path(rel).unlink(missing_ok=True)

    def iter_cloud_files(self) -> Iterator[str]:
        """Relative paths of every file under ``clouds/`` (for the cleanup command)."""
        base = self.root / "clouds"
        if not base.is_dir():
            return
        for p in sorted(base.iterdir()):
            if p.is_file():
                yield f"clouds/{p.name}"


_storage: Storage | None = None


def get_storage() -> Storage:
    """FastAPI dependency; one instance per process."""
    global _storage  # noqa: PLW0603
    if _storage is None:
        from app.config import get_settings

        _storage = Storage(get_settings())
    return _storage
