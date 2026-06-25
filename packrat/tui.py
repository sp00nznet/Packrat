"""Packrat's Textual TUI — a full-screen terminal app.

Features:

* Search Google Play; browse results and your archive in side-by-side tabs.
* App-details pane with the app icon rendered inline (rich-pixels).
* Screenshots viewer (modal, paged).
* Version picker: see every version you've archived, roll back by opening its
  folder, or download a specific version code (best-effort).
* Download the selected app, or update every outdated app in the archive.

Network calls run in threaded workers so the UI never blocks; updates from
those threads go through ``call_from_thread``.
"""

from __future__ import annotations

import os
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

from packrat import __version__, images
from packrat.archive import Archive
from packrat.store import AppInfo, Store, StoreError


def _fmt(size: float) -> str:
    if not size:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── modal: screenshots viewer ──────────────────────────────────────────────────


class ScreenshotsScreen(ModalScreen):
    BINDINGS = [
        Binding("left,h,up,k", "prev", "Prev"),
        Binding("right,l,down,j,space", "next", "Next"),
        Binding("escape,q", "close", "Close"),
    ]

    CSS = """
    ScreenshotsScreen { align: center middle; }
    #shot-box {
        width: 90%; height: 90%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #shot-img { height: 1fr; content-align: center middle; }
    #shot-caption { height: 1; text-align: center; color: $text-muted; }
    """

    def __init__(self, title: str, urls: list[str]):
        super().__init__()
        self._title = title
        self.urls = urls
        self.index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="shot-box"):
            yield Static("", id="shot-img")
            yield Static("", id="shot-caption")

    def on_mount(self) -> None:
        self._show()

    def action_prev(self) -> None:
        if self.urls:
            self.index = (self.index - 1) % len(self.urls)
            self._show()

    def action_next(self) -> None:
        if self.urls:
            self.index = (self.index + 1) % len(self.urls)
            self._show()

    def action_close(self) -> None:
        self.dismiss(None)

    def _show(self) -> None:
        cap = self.query_one("#shot-caption", Static)
        if not self.urls:
            self.query_one("#shot-img", Static).update("No screenshots available.")
            return
        cap.update(
            f"{self._title}  -  screenshot {self.index + 1}/{len(self.urls)}  (<- ->, Esc)"
        )
        self.query_one("#shot-img", Static).update("Loading...")
        self._load(self.index, self.urls[self.index])

    @work(thread=True, group="shot", exclusive=True)
    def _load(self, idx: int, url: str) -> None:
        size = self.app.size
        pix = images.render(url, max_cols=size.width - 8, max_rows=size.height - 8, size_hint=512)

        def paint() -> None:
            # Ignore if the user paged away while this was loading.
            if idx != self.index:
                return
            self.query_one("#shot-img", Static).update(
                pix if pix is not None else "(could not render screenshot)"
            )

        self.app.call_from_thread(paint)


# ── modal: version picker ──────────────────────────────────────────────────────


class VersionsScreen(ModalScreen):
    BINDINGS = [
        Binding("escape,q", "close", "Close"),
    ]

    CSS = """
    VersionsScreen { align: center middle; }
    #ver-box {
        width: 80%; height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #ver-title { height: 1; text-style: bold; }
    #ver-upstream { height: auto; color: $text-muted; margin-bottom: 1; }
    #ver-table { height: 1fr; }
    #ver-controls { height: auto; }
    #ver-vc { width: 24; }
    #ver-controls Button { margin-left: 1; }
    #ver-hint { height: 1; color: $text-muted; }
    """

    def __init__(self, archive: Archive, info: AppInfo):
        super().__init__()
        self.archive = archive
        self.info = info

    def compose(self) -> ComposeResult:
        with Vertical(id="ver-box"):
            yield Static(f"Versions of {self.info.title}", id="ver-title")
            yield Static(
                f"Upstream latest: {self.info.version_string} ({self.info.version_code})",
                id="ver-upstream",
            )
            yield DataTable(id="ver-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="ver-controls"):
                yield Input(placeholder="version code", id="ver-vc")
                yield Button("Download version", id="ver-dl", variant="success")
            yield Static(
                "Enter on a row = open that version's folder  |  Esc = close",
                id="ver-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#ver-table", DataTable)
        table.add_columns("Version code", "Version", "Downloaded", "Size", "Files")
        stored = self.archive.get_app(self.info.package)
        if stored:
            for vc in sorted(stored.versions, reverse=True):
                v = stored.versions[vc]
                table.add_row(
                    str(vc), v.version_string or "-", v.downloaded_at or "-",
                    _fmt(v.total_size), str(len(v.files)), key=str(vc),
                )
        if table.row_count == 0:
            self.query_one("#ver-upstream", Static).update(
                f"Upstream latest: {self.info.version_string} ({self.info.version_code})"
                "   -   nothing archived yet."
            )
        # Pre-fill the input with the upstream version code for convenience.
        self.query_one("#ver-vc", Input).value = str(self.info.version_code)

    def action_close(self) -> None:
        self.dismiss(None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        vc = event.row_key.value
        if not vc:
            return
        folder = self.archive.dir_for(self.info.package, int(vc))
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass  # non-Windows or no handler: just close
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ver-dl":
            self._submit_download()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit_download()

    def _submit_download(self) -> None:
        raw = self.query_one("#ver-vc", Input).value.strip()
        if not raw.isdigit():
            self.query_one("#ver-hint", Static).update("[red]Enter a numeric version code.[/red]")
            return
        self.dismiss(int(raw))


# ── main app ────────────────────────────────────────────────────────────────────


class PackratTUI(App):
    TITLE = "Packrat"
    SUB_TITLE = f"Google Play archiver v{__version__}"

    CSS = """
    #search { margin: 0 1; }
    #body { height: 1fr; }
    #tabs { width: 45%; }
    #detail {
        width: 55%;
        border: round $primary;
        margin: 0 1 0 0;
        padding: 1 2;
    }
    #detail-head { height: auto; }
    #detail-icon { width: 20; height: 10; }
    #detail-text { width: 1fr; padding-left: 1; }
    #detail-hint { height: auto; color: $text-muted; margin-top: 1; }
    #actions { height: auto; align: left middle; margin-top: 1; }
    #actions Button { margin: 0 1 0 0; }
    #progress { margin: 0 1; display: none; }
    #progress.active { display: block; }
    #status { margin: 0 1; color: $text-muted; height: 1; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("d", "download", "Download"),
        Binding("s", "screenshots", "Screenshots"),
        Binding("v", "versions", "Versions"),
        Binding("u", "update_all", "Update all"),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_folder", "Open folder"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, archive: Archive, arch: str = "arm64", dispenser: Optional[str] = None):
        super().__init__()
        self.archive = archive
        self.store = Store(arch=arch, dispenser=dispenser)
        self.current: Optional[AppInfo] = None
        self._busy = False

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="Search Google Play and press Enter...", id="search")
        with Horizontal(id="body"):
            with TabbedContent(id="tabs", initial="tab-archive"):
                with TabPane("Archive", id="tab-archive"):
                    yield DataTable(id="archive-table", cursor_type="row", zebra_stripes=True)
                with TabPane("Search", id="tab-search"):
                    yield DataTable(id="search-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="detail"):
                with Horizontal(id="detail-head"):
                    yield Static("", id="detail-icon")
                    yield Static("Select an app to see its details.", id="detail-text")
                yield Static("", id="detail-hint")
                with Horizontal(id="actions"):
                    yield Button("Download", id="btn-download", variant="success", disabled=True)
                    yield Button("Update all", id="btn-update")
                    yield Button("Refresh", id="btn-refresh")
        yield ProgressBar(id="progress", total=100, show_eta=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#archive-table", DataTable).add_columns(
            "Package", "Title", "Latest", "Size"
        )
        self.query_one("#search-table", DataTable).add_columns("Title", "Package")
        self.refresh_archive()
        self._authenticate()

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    def refresh_archive(self) -> None:
        table = self.query_one("#archive-table", DataTable)
        table.clear()
        apps = self.archive.apps()
        for a in apps:
            latest = a.latest
            size = sum(v.total_size for v in a.versions.values())
            table.add_row(
                a.package,
                a.title,
                f"{latest.version_string} ({latest.version_code})" if latest else "-",
                _fmt(size),
                key=a.package,
            )
        self._status(f"Archive: {self.archive.root}  ({len(apps)} app(s))")

    def _render_details(self, info: AppInfo) -> str:
        stored = self.archive.get_app(info.package)
        lines = [
            f"[b]{info.title}[/b]",
            f"[dim]{info.package}[/dim]",
            "",
            f"Version    : {info.version_string} ([b]{info.version_code}[/b])",
            f"Developer  : {info.developer or '-'}",
            f"Rating     : {info.rating or '-'}",
            f"Downloads  : {info.downloads or '-'}",
        ]
        if stored:
            local = stored.latest_code
            if local >= info.version_code:
                lines.append(f"\n[green]In archive[/green] (have {local}; latest available)")
            else:
                lines.append(
                    f"\n[yellow]Update available[/yellow] (have {local}, "
                    f"upstream {info.version_code})"
                )
        else:
            lines.append("\n[dim]Not in archive yet.[/dim]")
        return "\n".join(lines)

    def _update_hint(self, info: AppInfo) -> None:
        bits = []
        if info.screenshots:
            bits.append(f"[b]s[/b] screenshots ({len(info.screenshots)})")
        bits.append("[b]v[/b] versions")
        bits.append("[b]d[/b] download")
        self.query_one("#detail-hint", Static).update("   ".join(bits))

    # ── actions ─────────────────────────────────────────────────────────────────

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        self.refresh_archive()

    def action_open_folder(self) -> None:
        try:
            os.startfile(self.archive.root)  # type: ignore[attr-defined]
        except AttributeError:
            self._status(f"Archive folder: {self.archive.root}")
        except OSError as exc:
            self._status(f"Could not open folder: {exc}")

    def action_download(self) -> None:
        if self.current and not self._busy:
            self._do_download(self.current)

    def action_screenshots(self) -> None:
        if self.current and self.current.screenshots:
            self.push_screen(ScreenshotsScreen(self.current.title, self.current.screenshots))
        else:
            self._status("No screenshots for this app (select one first).")

    def action_versions(self) -> None:
        if not self.current:
            self._status("Select an app first.")
            return
        target = self.current

        def after(vc: Optional[int]) -> None:
            if vc and not self._busy:
                self._do_download(target, version_code=vc)

        self.push_screen(VersionsScreen(self.archive, target), after)

    def action_update_all(self) -> None:
        if not self._busy:
            self._do_update_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self._do_search(query)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        package = event.row_key.value
        if package:
            self._load_details(package)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-download":
            self.action_download()
        elif event.button.id == "btn-update":
            self.action_update_all()
        elif event.button.id == "btn-refresh":
            self.refresh_archive()

    # ── workers ──────────────────────────────────────────────────────────────────

    @work(thread=True, group="auth", exclusive=True)
    def _authenticate(self) -> None:
        self.call_from_thread(self._status, "Authenticating anonymously...")
        try:
            self.store.authenticate()
        except StoreError as exc:
            self.call_from_thread(self._status, f"Auth failed: {exc}")
            return
        self.call_from_thread(self._status, "Ready. Type a query and press Enter to search.")

    @work(thread=True, group="search", exclusive=True)
    def _do_search(self, query: str) -> None:
        self.call_from_thread(self._status, f"Searching for '{query}'...")
        try:
            results = self.store.search(query, limit=25)
        except StoreError as exc:
            self.call_from_thread(self._status, f"Search failed: {exc}")
            return

        def fill() -> None:
            table = self.query_one("#search-table", DataTable)
            table.clear()
            for item in results:
                pkg = item.get("package", "")
                if pkg:
                    table.add_row(item.get("title", ""), pkg, key=pkg)
            self.query_one("#tabs", TabbedContent).active = "tab-search"
            if results:
                table.focus()
            self._status(f"{len(results)} result(s) for '{query}'.")

        self.call_from_thread(fill)

    @work(thread=True, group="details", exclusive=True)
    def _load_details(self, package: str) -> None:
        self.call_from_thread(self._status, f"Loading details for {package}...")
        try:
            info = self.store.details(package)
        except StoreError as exc:
            self.call_from_thread(self._status, f"Could not load {package}: {exc}")
            return
        self.current = info

        def show() -> None:
            self.query_one("#detail-text", Static).update(self._render_details(info))
            self.query_one("#detail-icon", Static).update("")
            self._update_hint(info)
            self.query_one("#btn-download", Button).disabled = False
            self._status(f"{info.title} - {info.version_string}")

        self.call_from_thread(show)
        if info.icon_url:
            self._load_icon(package, info.icon_url)

    @work(thread=True, group="icon", exclusive=True)
    def _load_icon(self, package: str, url: str) -> None:
        pix = images.render(url, max_cols=20, max_rows=10, size_hint=128)

        def paint() -> None:
            # Skip if the selection changed while the icon was loading.
            if not self.current or self.current.package != package:
                return
            if pix is not None:
                self.query_one("#detail-icon", Static).update(pix)

        self.call_from_thread(paint)

    @work(thread=True, group="download", exclusive=False)
    def _do_download(self, info: AppInfo, version_code: Optional[int] = None) -> None:
        self._busy = True
        pb = self.query_one("#progress", ProgressBar)
        vc = version_code or info.version_code

        def start() -> None:
            self.query_one("#btn-download", Button).disabled = True
            pb.add_class("active")
            pb.update(total=100, progress=0)
            self._status(f"Downloading {info.title} ({vc})...")

        self.call_from_thread(start)

        def on_progress(done: int, total: int, label: str) -> None:
            self.call_from_thread(pb.update, total=(total or 100), progress=done)

        dest = self.archive.dir_for(info.package, vc)
        try:
            result = self.store.download(
                info.package, dest_dir=dest, version_code=vc, on_progress=on_progress
            )
            self.archive.record(result, developer=info.developer)
        except StoreError as exc:
            self._busy = False
            self.call_from_thread(self._finish, info, None, str(exc))
            return

        self._busy = False
        self.call_from_thread(self._finish, info, result, None)

    @work(thread=True, group="download", exclusive=False)
    def _do_update_all(self) -> None:
        self._busy = True
        pb = self.query_one("#progress", ProgressBar)

        def start() -> None:
            pb.add_class("active")
            pb.update(total=100, progress=0)
            self._status("Checking archive for updates...")

        self.call_from_thread(start)

        def on_progress(done: int, total: int, label: str) -> None:
            self.call_from_thread(pb.update, total=(total or 100), progress=done)

        apps = self.archive.apps()
        updated = 0
        for i, a in enumerate(apps, 1):
            try:
                info = self.store.details(a.package)
            except StoreError:
                continue
            if info.version_code <= a.latest_code:
                continue
            self.call_from_thread(
                self._status,
                f"({i}/{len(apps)}) Updating {info.title} -> {info.version_code}...",
            )
            dest = self.archive.dir_for(a.package, info.version_code)
            try:
                result = self.store.download(
                    a.package, dest_dir=dest, version_code=info.version_code,
                    on_progress=on_progress,
                )
                self.archive.record(result, developer=info.developer)
                updated += 1
            except StoreError:
                continue

        self._busy = False
        self.call_from_thread(self._finish_update_all, updated)

    # ── completion handlers (main thread) ────────────────────────────────────────

    def _finish(self, info: AppInfo, result, error: Optional[str]) -> None:
        pb = self.query_one("#progress", ProgressBar)
        pb.remove_class("active")
        self.query_one("#btn-download", Button).disabled = False
        if error:
            self._status(f"Download failed: {error}")
            return
        self.refresh_archive()
        if self.current and self.current.package == info.package:
            self.query_one("#detail-text", Static).update(self._render_details(self.current))
        n = len(result.all_files)
        self._status(f"Downloaded {info.title} - {n} file(s), {_fmt(result.total_size)}.")

    def _finish_update_all(self, updated: int) -> None:
        pb = self.query_one("#progress", ProgressBar)
        pb.remove_class("active")
        self.refresh_archive()
        if updated:
            self._status(f"Updated {updated} app(s).")
        else:
            self._status("Everything is already up to date.")


def run(archive: Archive, arch: str = "arm64", dispenser: Optional[str] = None) -> None:
    PackratTUI(archive, arch=arch, dispenser=dispenser).run()
