"""Thin wrapper around :mod:`gplaydl` — the Google Play protocol layer.

This module isolates Packrat from gplaydl's internals. It exposes a small,
stable surface (authenticate / search / details / download) returning plain
dataclasses, and transparently refreshes an expired anonymous token once.

Everything here talks to live Google servers. Only *free* apps can be
downloaded anonymously; paid apps will raise :class:`StoreError`.
"""

from __future__ import annotations

import asyncio
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

from gplaydl import api as _api
from gplaydl import auth as _auth
from gplaydl.protobuf import ProtoDecoder as _ProtoDecoder

# Google Play image types (DocV2 field 10, sub-field 1).
_IMG_SCREENSHOT = 1
_IMG_ICON = 4

# Progress callback: (downloaded_bytes, total_bytes, current_label).
# total_bytes may be 0 until a server reports Content-Length.
ProgressCallback = Callable[[int, int, str], None]

_CHUNK = 64 * 1024
_MAX_CONCURRENT = 4

# Re-export gplaydl's error types under our own namespace so callers depend on
# packrat, not gplaydl.
PlayAPIError = _api.PlayAPIError
AuthExpiredError = _api.AuthExpiredError


class StoreError(RuntimeError):
    """Raised when a Play Store operation cannot be completed."""


@dataclass
class AppInfo:
    """Lightweight details about an app on Google Play."""

    package: str
    title: str
    developer: str
    version_string: str
    version_code: int
    rating: Optional[str] = None
    downloads: Optional[str] = None
    play_url: str = ""
    icon_url: str = ""
    screenshots: list[str] = field(default_factory=list)

    @classmethod
    def _from_gplaydl(cls, d: "_api.AppDetails") -> "AppInfo":
        return cls(
            package=d.package,
            title=d.title,
            developer=d.developer,
            version_string=d.version_string,
            version_code=d.version_code,
            rating=d.rating,
            downloads=d.downloads,
            play_url=d.play_url,
        )


def _extract_images(raw: bytes) -> tuple[str, list[str]]:
    """Pull (icon_url, [screenshot_urls]) out of a raw details protobuf.

    Images live in DocV2 (path 1->2->4) as repeated field 10, each an Image
    message with sub-field 1 = imageType and sub-field 5 = url. Best-effort:
    returns empties if gplaydl's internal layout ever changes.
    """
    try:
        doc = _api._navigate(raw, 1, 2, 4)
        icon = ""
        shots: list[str] = []
        for blob in _api._all_bytes(doc, 10):
            fields = _ProtoDecoder(blob).read_all_ordered()
            kind = _api._first_int(fields, 1)
            url = _api._first_string(fields, 5)
            if not url:
                continue
            if kind == _IMG_ICON and not icon:
                icon = url
            elif kind == _IMG_SCREENSHOT:
                shots.append(url)
        return icon, shots
    except Exception:
        return "", []


@dataclass
class _Spec:
    """Internal: everything needed to fetch one file."""

    url: str
    dest: Path
    label: str
    cookies: list[dict] = field(default_factory=list)
    gzipped: bool = False


@dataclass
class DownloadedFile:
    path: Path
    label: str
    size: int = 0


@dataclass
class DownloadResult:
    """Outcome of a download: the base APK plus any splits / extra files."""

    package: str
    version_code: int
    version_string: str
    title: str
    sha1: str = ""
    base: Optional[DownloadedFile] = None
    splits: list[DownloadedFile] = field(default_factory=list)
    extras: list[DownloadedFile] = field(default_factory=list)

    @property
    def all_files(self) -> list[DownloadedFile]:
        files: list[DownloadedFile] = []
        if self.base:
            files.append(self.base)
        files.extend(self.splits)
        files.extend(self.extras)
        return files

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.all_files)


async def _fetch_one(
    spec: _Spec,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    state: dict,
    on_progress: Optional[ProgressCallback],
) -> None:
    """Stream a single file to disk, updating shared progress state."""
    async with sem:
        headers: dict[str, str] = {}
        if spec.cookies:
            headers["Cookie"] = "; ".join(
                f"{c['name']}={c['value']}" for c in spec.cookies
            )
        decomp = zlib.decompressobj(zlib.MAX_WBITS | 16) if spec.gzipped else None

        async with client.stream("GET", spec.url, headers=headers) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            state["total"] += total
            spec.dest.parent.mkdir(parents=True, exist_ok=True)
            last_emit = 0
            with open(spec.dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=_CHUNK):
                    fh.write(decomp.decompress(chunk) if decomp else chunk)
                    state["done"] += len(chunk)
                    # Throttle UI updates to ~every 256 KB.
                    if on_progress and state["done"] - last_emit >= 256 * 1024:
                        last_emit = state["done"]
                        on_progress(state["done"], state["total"], spec.label)
                if decomp:
                    tail = decomp.flush()
                    if tail:
                        fh.write(tail)
        if on_progress:
            on_progress(state["done"], state["total"], spec.label)


async def _run(specs: list[_Spec], on_progress: Optional[ProgressCallback]) -> None:
    sem = asyncio.Semaphore(_MAX_CONCURRENT)
    state = {"done": 0, "total": 0}
    timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        await asyncio.gather(
            *[_fetch_one(s, client, sem, state, on_progress) for s in specs]
        )


def _download_files(specs: list[_Spec], on_progress: Optional[ProgressCallback]) -> None:
    """Download every spec in parallel, with no console output of its own."""
    asyncio.run(_run(specs, on_progress))


class Store:
    """A connection to Google Play using an anonymous, dispenser-issued token."""

    def __init__(self, arch: str = "arm64", dispenser: Optional[str] = None):
        self.arch = arch
        self.dispenser = dispenser
        self._auth: Optional[dict] = None

    # ── auth ────────────────────────────────────────────────────────────────

    def authenticate(self, force: bool = False) -> dict:
        """Acquire (or reuse) an anonymous auth token."""
        self._auth = _auth.ensure_auth(
            arch=self.arch, dispenser_url=self.dispenser, force_refresh=force
        )
        if not self._auth:
            raise StoreError(
                "Could not obtain an anonymous auth token — the dispenser "
                "rejected every device profile. Try again later."
            )
        return self._auth

    @property
    def auth(self) -> dict:
        if self._auth is None:
            return self.authenticate()
        return self._auth

    def _retrying(self, fn: Callable[[dict], object]):
        """Run ``fn(auth)``; if the token expired, refresh once and retry."""
        try:
            return fn(self.auth)
        except AuthExpiredError:
            return fn(self.authenticate(force=True))
        except PlayAPIError as exc:
            raise StoreError(str(exc)) from exc

    # ── queries ───────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Return a list of ``{"package", "title"}`` results."""
        return list(self._retrying(lambda a: _api.search_apps(query, a, limit=limit)))

    def details(self, package: str) -> AppInfo:
        """Return :class:`AppInfo` for ``package`` (incl. icon/screenshot URLs).

        Fetches the details protobuf once and parses both the standard fields
        and the image URLs from it (no extra network round-trip). Falls back to
        gplaydl's public parser if its internals ever change.
        """
        try:
            raw = self._retrying(lambda a: _api._fetch_details_raw(package, a))
            parsed = _api._parse_details_proto(raw)
            if parsed.docid:
                icon, shots = _extract_images(raw)
                return AppInfo(
                    package=parsed.docid or package,
                    title=parsed.title,
                    developer=parsed.creator,
                    version_string=parsed.version_string,
                    version_code=parsed.version_code,
                    rating=parsed.rating,
                    downloads=parsed.downloads,
                    play_url=f"https://play.google.com/store/apps/details?id={package}",
                    icon_url=icon,
                    screenshots=shots,
                )
        except AttributeError:
            pass  # gplaydl internals moved — fall back to the public API.
        d = self._retrying(lambda a: _api.get_details(package, a))
        return AppInfo._from_gplaydl(d)

    # ── download ──────────────────────────────────────────────────────────────

    def download(
        self,
        package: str,
        dest_dir: Path,
        version_code: Optional[int] = None,
        with_splits: bool = True,
        with_extras: bool = True,
        on_progress: Optional[ProgressCallback] = None,
    ) -> DownloadResult:
        """Download ``package`` (optionally a specific version) into ``dest_dir``.

        Returns a :class:`DownloadResult` describing every file written.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        info = self.details(package)
        vc = version_code or info.version_code

        # Register the free "purchase", then fetch signed delivery URLs.
        def _deliver(a: dict):
            _api.purchase(package, vc, a)
            return _api.get_delivery(package, vc, a)

        delivery = self._retrying(_deliver)

        base_name = f"{package}-{vc}.apk"
        base_path = dest_dir / base_name
        specs: list[_Spec] = [
            _Spec(
                url=delivery.download_url,
                dest=base_path,
                cookies=delivery.cookies,
                label=base_name,
            )
        ]

        split_names: list[tuple[str, Path]] = []
        if with_splits and delivery.splits:
            for split in delivery.splits:
                name = f"{package}-{vc}-{split.name}.apk"
                p = dest_dir / name
                specs.append(_Spec(url=split.url, dest=p, label=name))
                split_names.append((name, p))

        extra_names: list[tuple[str, Path]] = []
        if with_extras and delivery.additional_files:
            for af in delivery.additional_files:
                if af.is_asset_pack:
                    name = f"{package}-{vc}-{af.type_label}{af.extension}"
                else:
                    name = f"{af.type_label}.{af.version_code}.{package}{af.extension}"
                p = dest_dir / name
                specs.append(
                    _Spec(
                        url=af.url, dest=p, cookies=af.cookies,
                        label=name, gzipped=af.gzipped,
                    )
                )
                extra_names.append((name, p))

        _download_files(specs, on_progress)

        def _file(name: str, p: Path) -> DownloadedFile:
            size = p.stat().st_size if p.exists() else 0
            return DownloadedFile(path=p, label=name, size=size)

        return DownloadResult(
            package=package,
            version_code=vc,
            version_string=info.version_string,
            title=info.title,
            sha1=getattr(delivery, "sha1", "") or "",
            base=_file(base_name, base_path),
            splits=[_file(n, p) for n, p in split_names],
            extras=[_file(n, p) for n, p in extra_names],
        )
