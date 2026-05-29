"""
Storage abstraction so the pipeline runs identically on a local disk (desktop dev)
and on an ephemeral cloud host backed by Dropbox (the deploy).

Two backends behind one interface:

  • LocalStorage   — plain filesystem, current behaviour. Paths are OS-native.
  • DropboxStorage — Dropbox Business *team* API. Paths are team-space paths
                     ("/THENEWFACE/02_PROJECTS/..."), forward-slashed, rooted at the
                     team namespace. Auth is durable (refresh token + app key/secret),
                     acting as the team admin who authorized the app.

Calling code never touches `pathlib` for storage paths directly — it uses the path
helpers here (join/name/stem/parent/relpath) so the same code produces correct
OS paths locally and correct Dropbox paths on the cloud.

API clients (Anthropic/Gemini) and PIL still want a real local file. `materialize(path)`
bridges that: it returns the path unchanged for the local backend, and downloads to a
cached temp file for Dropbox. Source renders are immutable, so the cache is keyed by path.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from .config import Config


@runtime_checkable
class Storage(Protocol):
    # ── path helpers (pure string, backend-appropriate separators) ──
    def join(self, *parts: str) -> str: ...
    def name(self, path: str) -> str: ...
    def stem(self, path: str) -> str: ...
    def parent(self, path: str) -> str: ...
    def relpath(self, path: str, start: str) -> str: ...

    # ── io ──
    def exists(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
    def list_subdirs(self, path: str) -> list[str]: ...   # full paths, sorted
    def list_files(self, path: str) -> list[str]: ...     # full paths, sorted
    def read_bytes(self, path: str) -> bytes: ...
    def write_bytes(self, path: str, data: bytes) -> None: ...
    def read_text(self, path: str) -> str: ...
    def write_text(self, path: str, text: str) -> None: ...
    def makedirs(self, path: str) -> None: ...
    def delete(self, path: str) -> bool: ...

    # ── bridge to a real local file for API clients / PIL ──
    def materialize(self, path: str) -> str: ...


# ───────────────────────────── Local ─────────────────────────────

class LocalStorage:
    """Filesystem backend — paths are OS-native strings."""

    backend = "local"

    def join(self, *parts: str) -> str:
        return str(Path(parts[0]).joinpath(*parts[1:]))

    def name(self, path: str) -> str:
        return Path(path).name

    def stem(self, path: str) -> str:
        return Path(path).stem

    def parent(self, path: str) -> str:
        return str(Path(path).parent)

    def relpath(self, path: str, start: str) -> str:
        return str(Path(path).relative_to(start))

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def is_dir(self, path: str) -> bool:
        return Path(path).is_dir()

    def list_subdirs(self, path: str) -> list[str]:
        p = Path(path)
        if not p.is_dir():
            return []
        return sorted(str(c) for c in p.iterdir() if c.is_dir())

    def list_files(self, path: str) -> list[str]:
        p = Path(path)
        if not p.is_dir():
            return []
        return sorted(str(c) for c in p.iterdir() if c.is_file())

    def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def write_bytes(self, path: str, data: bytes) -> None:
        """Atomic tmp+rename with retries — a local output folder is often a Dropbox/
        OneDrive sync target that grabs a brief read-lock the moment a temp file appears
        (WinError 5/32). Retry with backoff, then fall back to a direct write."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        last_exc: Optional[BaseException] = None
        for attempt in range(5):
            fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(p.parent))
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, p)
                return
            except OSError as e:
                last_exc = e
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
                time.sleep(0.1 * (2 ** attempt))
            except Exception:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
                raise
        p.write_bytes(data)  # last resort: direct, non-atomic write
        if last_exc is not None:
            import sys
            print(f"[storage] atomic write retried, fell back to direct write: {last_exc}", file=sys.stderr)

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write_text(self, path: str, text: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def makedirs(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def delete(self, path: str) -> bool:
        p = Path(path)
        try:
            if p.exists():
                p.unlink()
            return True
        except OSError:
            return False

    def materialize(self, path: str) -> str:
        return path


# ───────────────────────────── Dropbox ─────────────────────────────

class DropboxStorage:
    """
    Dropbox Business team backend. Paths are team-space paths ("/THENEWFACE/...").

    The client is built lazily and reused. We authenticate as the team admin who
    authorized the app (team_token_get_authenticated_admin) and re-scope to the team
    namespace so team-space folders are reachable with ordinary "/..." paths.
    """

    backend = "dropbox"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._dbx = None
        self._lock = threading.Lock()
        self._cache_dir = Path(tempfile.gettempdir()) / "ai_bucherer_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # -- client --
    def _client(self):
        if self._dbx is not None:
            return self._dbx
        with self._lock:
            if self._dbx is not None:
                return self._dbx
            import dropbox
            dbxt = dropbox.DropboxTeam(
                oauth2_refresh_token=self.cfg.dropbox_refresh_token,
                app_key=self.cfg.dropbox_app_key,
                app_secret=self.cfg.dropbox_app_secret,
            )
            admin_id = dbxt.team_token_get_authenticated_admin().admin_profile.team_member_id
            ns = self.cfg.dropbox_root_namespace
            client = dbxt.as_admin(admin_id)
            if ns:
                client = client.with_path_root(dropbox.common.PathRoot.root(ns))
            self._dbx = client
            return self._dbx

    # -- path helpers (always posix; Dropbox paths are forward-slashed, rooted at "/") --
    @staticmethod
    def _norm(path: str) -> str:
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        # Dropbox rejects a trailing slash (except for the root "")
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return path

    def join(self, *parts: str) -> str:
        first = self._norm(parts[0])
        rest = [p.replace("\\", "/").strip("/") for p in parts[1:] if p]
        return self._norm(posixpath.join(first, *rest)) if rest else first

    def name(self, path: str) -> str:
        return posixpath.basename(self._norm(path))

    def stem(self, path: str) -> str:
        return posixpath.splitext(self.name(path))[0]

    def parent(self, path: str) -> str:
        return self._norm(posixpath.dirname(self._norm(path)))

    def relpath(self, path: str, start: str) -> str:
        return posixpath.relpath(self._norm(path), self._norm(start))

    # -- io --
    def exists(self, path: str) -> bool:
        import dropbox
        try:
            self._client().files_get_metadata(self._norm(path))
            return True
        except dropbox.exceptions.ApiError:
            return False

    def is_dir(self, path: str) -> bool:
        import dropbox
        from dropbox.files import FolderMetadata
        try:
            md = self._client().files_get_metadata(self._norm(path))
            return isinstance(md, FolderMetadata)
        except dropbox.exceptions.ApiError:
            return False

    def _list(self, path: str, want_dirs: bool) -> list[str]:
        from dropbox.files import FolderMetadata, FileMetadata
        base = self._norm(path)
        dbx = self._client()
        out: list[str] = []
        try:
            res = dbx.files_list_folder(base)
        except Exception:
            return []
        while True:
            for e in res.entries:
                is_folder = isinstance(e, FolderMetadata)
                if (want_dirs and is_folder) or (not want_dirs and isinstance(e, FileMetadata)):
                    out.append(self.join(base, e.name))
            if not res.has_more:
                break
            res = dbx.files_list_folder_continue(res.cursor)
        return sorted(out)

    def list_subdirs(self, path: str) -> list[str]:
        return self._list(path, want_dirs=True)

    def list_files(self, path: str) -> list[str]:
        return self._list(path, want_dirs=False)

    def read_bytes(self, path: str) -> bytes:
        _md, resp = self._client().files_download(self._norm(path))
        return resp.content

    def write_bytes(self, path: str, data: bytes) -> None:
        from dropbox.files import WriteMode
        norm = self._norm(path)
        self._client().files_upload(data, norm, mode=WriteMode("overwrite"), mute=True)
        # Bust any stale materialize cache for this path so a re-written file
        # (e.g. a re-graded output) is re-downloaded on next display.
        try:
            self._cache_path(norm).unlink(missing_ok=True)
        except OSError:
            pass

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8")

    def write_text(self, path: str, text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))

    def makedirs(self, path: str) -> None:
        # Dropbox auto-creates parents on upload; create_folder is only needed for empty dirs.
        import dropbox
        try:
            self._client().files_create_folder_v2(self._norm(path))
        except dropbox.exceptions.ApiError:
            pass  # already exists (path/conflict/folder) — fine

    def delete(self, path: str) -> bool:
        import dropbox
        try:
            self._client().files_delete_v2(self._norm(path))
            return True
        except dropbox.exceptions.ApiError:
            return False

    def _cache_path(self, norm: str) -> Path:
        key = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
        ext = posixpath.splitext(norm)[1] or ".bin"
        return self._cache_dir / f"{key}{ext}"

    def materialize(self, path: str) -> str:
        """Download to a path-keyed temp file. The cache is invalidated by write_bytes,
        so a re-written remote file is re-fetched on next access."""
        norm = self._norm(path)
        local = self._cache_path(norm)
        if not local.exists():
            local.write_bytes(self.read_bytes(norm))
        return str(local)


# ───────────────────────────── factory ─────────────────────────────

_STORAGE: dict[str, Storage] = {}
_STORAGE_LOCK = threading.Lock()


def get_storage(cfg: Config) -> Storage:
    """Return the configured backend (cached per-process by backend name)."""
    backend = cfg.effective_storage_backend
    with _STORAGE_LOCK:
        if backend not in _STORAGE:
            _STORAGE[backend] = DropboxStorage(cfg) if backend == "dropbox" else LocalStorage()
        return _STORAGE[backend]
