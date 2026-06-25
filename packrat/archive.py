"""The local APK archive — Packrat's reason for existing.

An *archive* is just a directory with a JSON index (``packrat.json``) and an
``apps/`` tree::

    <archive>/
        packrat.json
        apps/
            org.videolan.vlc/
                13070009/
                    org.videolan.vlc-13070009.apk
                    ...

The index lets Packrat answer "what do I already have?" and "is there a newer
version upstream?" without re-scanning files, while the on-disk layout keeps
every downloaded version side by side so you can roll back at any time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from packrat import __version__
from packrat.store import DownloadResult

INDEX_NAME = "packrat.json"
INDEX_SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class StoredVersion:
    version_code: int
    version_string: str
    downloaded_at: str
    sha1: str
    files: list[dict]

    @property
    def total_size(self) -> int:
        return sum(int(f.get("size", 0)) for f in self.files)


@dataclass
class StoredApp:
    package: str
    title: str
    developer: str
    versions: dict[int, StoredVersion]

    @property
    def latest_code(self) -> int:
        return max(self.versions) if self.versions else 0

    @property
    def latest(self) -> Optional[StoredVersion]:
        return self.versions.get(self.latest_code) if self.versions else None


class ArchiveError(RuntimeError):
    """Raised for archive-level problems (missing/corrupt index, etc.)."""


class Archive:
    """A versioned, on-disk collection of downloaded APKs."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self._data: dict = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_NAME

    @property
    def apps_dir(self) -> Path:
        return self.root / "apps"

    def exists(self) -> bool:
        return self.index_path.is_file()

    @classmethod
    def create(cls, root: Path) -> "Archive":
        """Initialise a new archive at ``root`` (must not already exist)."""
        arc = cls(root)
        if arc.exists():
            raise ArchiveError(f"An archive already exists at {arc.root}")
        arc.root.mkdir(parents=True, exist_ok=True)
        arc.apps_dir.mkdir(parents=True, exist_ok=True)
        arc._data = {
            "schema": INDEX_SCHEMA,
            "created": _now(),
            "packrat": __version__,
            "apps": {},
        }
        arc.save()
        return arc

    @classmethod
    def open(cls, root: Path) -> "Archive":
        """Open an existing archive."""
        arc = cls(root)
        if not arc.exists():
            raise ArchiveError(
                f"No archive found at {arc.root}. Run 'packrat init' there first."
            )
        try:
            arc._data = json.loads(arc.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ArchiveError(f"Could not read archive index: {exc}") from exc
        arc._data.setdefault("apps", {})
        return arc

    def save(self) -> None:
        # Write atomically so an interrupted save can't corrupt the index.
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    # ── layout ────────────────────────────────────────────────────────────────

    def dir_for(self, package: str, version_code: int) -> Path:
        return self.apps_dir / package / str(version_code)

    # ── reads ─────────────────────────────────────────────────────────────────

    def _app_raw(self, package: str) -> Optional[dict]:
        return self._data.get("apps", {}).get(package)

    def has(self, package: str, version_code: int) -> bool:
        app = self._app_raw(package)
        return bool(app and str(version_code) in app.get("versions", {}))

    def get_app(self, package: str) -> Optional[StoredApp]:
        raw = self._app_raw(package)
        if not raw:
            return None
        versions = {
            int(vc): StoredVersion(
                version_code=int(vc),
                version_string=v.get("version_string", ""),
                downloaded_at=v.get("downloaded_at", ""),
                sha1=v.get("sha1", ""),
                files=v.get("files", []),
            )
            for vc, v in raw.get("versions", {}).items()
        }
        return StoredApp(
            package=package,
            title=raw.get("title", package),
            developer=raw.get("developer", ""),
            versions=versions,
        )

    def apps(self) -> list[StoredApp]:
        out = [self.get_app(p) for p in sorted(self._data.get("apps", {}))]
        return [a for a in out if a is not None]

    def latest_local_code(self, package: str) -> int:
        app = self.get_app(package)
        return app.latest_code if app else 0

    # ── writes ────────────────────────────────────────────────────────────────

    def record(self, result: DownloadResult, developer: str = "") -> None:
        """Add a completed :class:`DownloadResult` to the index and save."""
        apps = self._data.setdefault("apps", {})
        app = apps.setdefault(
            result.package,
            {"title": result.title, "developer": developer, "versions": {}},
        )
        # Refresh metadata that may have changed since the last download.
        app["title"] = result.title or app.get("title", result.package)
        if developer:
            app["developer"] = developer

        files = [
            {"name": f.label, "size": f.size, "path": str(f.path.relative_to(self.root))}
            for f in result.all_files
        ]
        app["versions"][str(result.version_code)] = {
            "version_code": result.version_code,
            "version_string": result.version_string,
            "downloaded_at": _now(),
            "sha1": result.sha1,
            "files": files,
        }
        self.save()
