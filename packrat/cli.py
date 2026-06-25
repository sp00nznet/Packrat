"""Packrat command-line interface.

Commands:

* ``packrat init [PATH]``        - create a new archive
* ``packrat search QUERY``       - search Google Play
* ``packrat info PACKAGE``       - show app details from Google Play
* ``packrat get PACKAGE``        - download an app into the archive
* ``packrat list``               - list what the archive contains
* ``packrat outdated``           - show archived apps that have a newer upstream version
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# On legacy Windows consoles (cp1252) the Unicode glyphs emitted by rich
# progress bars can raise UnicodeEncodeError. Degrade gracefully instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from packrat import __version__
from packrat.archive import Archive, ArchiveError
from packrat.store import Store, StoreError

console = Console()
err = Console(stderr=True)

app = typer.Typer(
    name="packrat",
    help="Google Play desktop client - download and archive Android APKs.",
    add_completion=False,
    no_args_is_help=True,
)

# Options shared by commands that talk to Google Play.
ArchOpt = typer.Option("arm64", "--arch", "-a", help="Device architecture: arm64 or armv7.")
DispenserOpt = typer.Option(None, "--dispenser", "-d", help="Custom anonymous-token dispenser URL.")
ArchiveOpt = typer.Option(Path("."), "--archive", "-A", help="Path to the archive.")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"Packrat [bold]{__version__}[/bold]")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Packrat - download and archive Android APKs from Google Play."""


# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt(size: float) -> str:
    if not size:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _store(arch: str, dispenser: Optional[str]) -> Store:
    store = Store(arch=arch, dispenser=dispenser)
    try:
        with console.status("Authenticating anonymously..."):
            store.authenticate()
    except StoreError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    return store


def _open_archive(path: Path) -> Archive:
    try:
        return Archive.open(path)
    except ArchiveError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


# ── ui ────────────────────────────────────────────────────────────────────────


@app.command()
def ui(
    archive: Path = ArchiveOpt,
    arch: str = ArchOpt,
    dispenser: Optional[str] = DispenserOpt,
) -> None:
    """Launch the full-screen terminal UI."""
    arc = Archive(archive)
    if not arc.exists():
        try:
            arc = Archive.create(archive)
        except ArchiveError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    else:
        arc = _open_archive(archive)

    # Imported lazily so the rest of the CLI works without textual installed.
    from packrat.tui import run as run_tui

    run_tui(arc, arch=arch, dispenser=dispenser)


# ── init ──────────────────────────────────────────────────────────────────────


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Where to create the archive."),
) -> None:
    """Create a new (empty) archive."""
    try:
        arc = Archive.create(path)
    except ArchiveError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(
        Panel.fit(
            f"[green bold]Archive created[/green bold]\n{arc.root}",
            title="Packrat",
        )
    )
    console.print("[dim]Download an app into it with:[/dim] packrat get <package>")


# ── search ────────────────────────────────────────────────────────────────────


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results."),
    arch: str = ArchOpt,
    dispenser: Optional[str] = DispenserOpt,
) -> None:
    """Search for apps on Google Play."""
    store = _store(arch, dispenser)
    try:
        with console.status(f"Searching for [bold]{query}[/bold]..."):
            results = store.search(query, limit=limit)
    except StoreError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if not results:
        console.print("[yellow]No results.[/yellow]")
        raise typer.Exit()

    table = Table(title=f'Results for "{query}"')
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold")
    table.add_column("Package")
    for i, item in enumerate(results, 1):
        table.add_row(str(i), item.get("title", ""), item.get("package", ""))
    console.print(table)


# ── info ──────────────────────────────────────────────────────────────────────


@app.command()
def info(
    package: str = typer.Argument(..., help="Package name, e.g. org.videolan.vlc."),
    arch: str = ArchOpt,
    dispenser: Optional[str] = DispenserOpt,
) -> None:
    """Show details for an app on Google Play."""
    store = _store(arch, dispenser)
    try:
        with console.status(f"Fetching details for [bold]{package}[/bold]..."):
            d = store.details(package)
    except StoreError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    table = Table(title=d.title or package, show_header=False, title_style="bold")
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Package", d.package)
    table.add_row("Version", f"{d.version_string} ({d.version_code})")
    table.add_row("Developer", d.developer or "-")
    table.add_row("Rating", d.rating or "-")
    table.add_row("Downloads", d.downloads or "-")
    table.add_row("Play Store", d.play_url)
    console.print(table)


# ── get ───────────────────────────────────────────────────────────────────────


@app.command()
def get(
    package: str = typer.Argument(..., help="Package name to download."),
    version: Optional[int] = typer.Option(None, "--version", "-v", help="Specific version code."),
    archive: Path = ArchiveOpt,
    arch: str = ArchOpt,
    dispenser: Optional[str] = DispenserOpt,
    no_splits: bool = typer.Option(False, "--no-splits", help="Skip split APKs."),
    no_extras: bool = typer.Option(False, "--no-extras", help="Skip OBB / asset-pack files."),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download even if already archived."),
) -> None:
    """Download an app from Google Play into the archive."""
    arc = _open_archive(archive)
    store = _store(arch, dispenser)

    try:
        with console.status(f"Fetching details for [bold]{package}[/bold]..."):
            details = store.details(package)
        vc = version or details.version_code

        if arc.has(package, vc) and not force:
            console.print(
                f"[yellow]{package} {details.version_string} ({vc}) is already in the "
                f"archive.[/yellow] Use [bold]--force[/bold] to re-download."
            )
            raise typer.Exit()

        console.print(
            Panel.fit(
                f"[bold]{details.title}[/bold]\n{details.version_string}  (vc {vc})",
                title=package,
            )
        )
        dest = arc.dir_for(package, vc)
        with console.status("Acquiring app and downloading..."):
            result = store.download(
                package,
                dest_dir=dest,
                version_code=vc,
                with_splits=not no_splits,
                with_extras=not no_extras,
            )
    except StoreError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    arc.record(result, developer=details.developer)

    table = Table(title="Downloaded", show_header=True)
    table.add_column("File", style="bold")
    table.add_column("Size", justify="right")
    for f in result.all_files:
        table.add_row(f.label, _fmt(f.size))
    console.print(table)
    console.print(
        f"\n[green bold]Done[/green bold] - {len(result.all_files)} file(s), "
        f"{_fmt(result.total_size)} -> [dim]{dest}[/dim]"
    )
    if result.splits:
        console.print(
            "[dim]Tip: install split APKs with[/dim] adb install-multiple *.apk"
        )


# ── list ──────────────────────────────────────────────────────────────────────


@app.command("list")
def list_cmd(
    archive: Path = ArchiveOpt,
) -> None:
    """List the apps stored in the archive."""
    arc = _open_archive(archive)
    apps = arc.apps()
    if not apps:
        console.print("[yellow]The archive is empty.[/yellow] Add one with: packrat get <package>")
        raise typer.Exit()

    table = Table(title=f"Archive: {arc.root}")
    table.add_column("Package", style="bold")
    table.add_column("Title")
    table.add_column("Versions", justify="right")
    table.add_column("Latest")
    table.add_column("Size", justify="right")
    total = 0
    for a in apps:
        size = sum(v.total_size for v in a.versions.values())
        total += size
        latest = a.latest
        table.add_row(
            a.package,
            a.title,
            str(len(a.versions)),
            f"{latest.version_string} ({latest.version_code})" if latest else "-",
            _fmt(size),
        )
    console.print(table)
    console.print(f"[dim]{len(apps)} app(s), {_fmt(total)} total[/dim]")


# ── outdated ──────────────────────────────────────────────────────────────────


@app.command()
def outdated(
    archive: Path = ArchiveOpt,
    arch: str = ArchOpt,
    dispenser: Optional[str] = DispenserOpt,
) -> None:
    """Show archived apps that have a newer version on Google Play."""
    arc = _open_archive(archive)
    apps = arc.apps()
    if not apps:
        console.print("[yellow]The archive is empty.[/yellow]")
        raise typer.Exit()

    store = _store(arch, dispenser)
    table = Table(title="Updates available")
    table.add_column("Package", style="bold")
    table.add_column("Local")
    table.add_column("Upstream", style="green")
    found = 0
    with console.status("Checking for updates..."):
        for a in apps:
            try:
                d = store.details(a.package)
            except StoreError:
                continue
            if d.version_code > a.latest_code:
                found += 1
                local = a.latest
                table.add_row(
                    a.package,
                    f"{local.version_string} ({local.version_code})" if local else "-",
                    f"{d.version_string} ({d.version_code})",
                )

    if not found:
        console.print("[green]Everything is up to date.[/green]")
        raise typer.Exit()
    console.print(table)
    console.print("[dim]Update one with:[/dim] packrat get <package>")


if __name__ == "__main__":
    app()
