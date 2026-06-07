from __future__ import annotations

import contextlib
import gzip
import json
import shutil
import tempfile
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, RichLog, Static


def _scan_archives(history_dir: Path) -> list[dict]:
    """Scan ~/.cursor-agent-history/ for archived runs."""
    if not history_dir.is_dir():
        return []
    runs = []
    for d in sorted(history_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        summary_path = d / "summary.json"
        meta = {}
        summary = {}
        if meta_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                meta = json.loads(meta_path.read_text())
        if summary_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                summary = json.loads(summary_path.read_text())
        if not meta and not summary:
            continue

        agents = summary.get("agents", {})
        ok = sum(1 for a in agents.values() if a.get("state") == "DONE")
        err = sum(1 for a in agents.values() if a.get("state") in ("ERROR", "DIED"))
        cost = summary.get("totals", {}).get("cost", 0)

        runs.append({
            "dir": d.name,
            "path": str(d),
            "date": meta.get("timestamp", d.name[:16]).replace("T", " ")[:16],
            "tag": meta.get("tag", ""),
            "repo": Path(meta.get("repo", "")).name if meta.get("repo") else "",
            "agent_count": meta.get("agent_count", len(agents)),
            "ok": ok,
            "err": err,
            "cost": cost,
        })
    return runs


class HistoryApp(App):
    CSS = """
    Screen { background: #1a1b26; color: #c0caf5; }
    #header { dock: top; height: 3; background: #1f2335; border-bottom: solid #3b4261; padding: 0 2; }
    #header-title { padding: 1 0; color: #7aa2f7; text-style: bold; }
    #run-table { height: 1fr; background: #1a1b26; }
    #run-table > .datatable--header { background: #1f2335; color: #565f89; text-style: bold; }
    #run-table > .datatable--cursor { background: #292e42; color: #c0caf5; }
    #detail-pane { height: 12; background: #1f2335; border-top: solid #3b4261; padding: 1 2; overflow-y: auto; }
    #footer { dock: bottom; height: 1; background: #1f2335; border-top: solid #3b4261; padding: 0 1; color: #414868; }
    """
    TITLE = "agent-tui history"

    BINDINGS = [  # noqa: RUF012
        Binding("q", "quit", "Quit", priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "open_run", "Open"),
        Binding("o", "open_run", "Open", show=False),
    ]

    def __init__(self, history_dir: str):
        super().__init__()
        self._history_dir = Path(history_dir)
        self._runs: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Static("[#7aa2f7 bold]agent-tui history[/]", id="header-title")
        yield DataTable(id="run-table", cursor_type="row", zebra_stripes=True)
        yield RichLog(id="detail-pane", markup=True, wrap=True)
        yield Static(
            " [#7aa2f7 bold]q[/] quit  "
            "[#7aa2f7 bold]j/k[/] nav  "
            "[#7aa2f7 bold]enter[/] open run in TUI",
            id="footer",
        )

    def on_mount(self) -> None:
        self._runs = _scan_archives(self._history_dir)
        table = self.query_one("#run-table", DataTable)
        table.add_column("Date", key="date", width=18)
        table.add_column("Tag", key="tag", width=16)
        table.add_column("Agents", key="agents", width=7)
        table.add_column("OK", key="ok", width=4)
        table.add_column("Err", key="err", width=4)
        table.add_column("Cost", key="cost", width=10)
        table.add_column("Repo", key="repo")

        if not self._runs:
            panel = self.query_one("#detail-pane", RichLog)
            panel.write("[#565f89]No archived runs found in " + str(self._history_dir) + "[/]")
            panel.write("[#565f89]Archive runs with: bash archive.sh ./logs --tag <label>[/]")
            return

        for run in self._runs:
            cost_str = f"${run['cost']:.2f}" if run["cost"] else "-"
            table.add_row(
                run["date"],
                run["tag"] or "[#565f89](none)[/]",
                str(run["agent_count"]),
                str(run["ok"]),
                str(run["err"]),
                cost_str,
                run["repo"],
                key=run["dir"],
            )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not event.row_key or not event.row_key.value:
            return
        key = str(event.row_key.value)
        run = next((r for r in self._runs if r["dir"] == key), None)
        if not run:
            return
        panel = self.query_one("#detail-pane", RichLog)
        panel.clear()
        panel.write(f"[#7aa2f7 bold]{run['dir']}[/]")
        panel.write(f"  [#565f89]repo[/] {run['repo'] or '—'}  [#565f89]tag[/] {run['tag'] or '—'}")
        panel.write(f"  [#565f89]agents[/] {run['agent_count']}  [#9ece6a]{run['ok']} ok[/]  [#f7768e]{run['err']} err[/]  [#565f89]cost[/] ${run['cost']:.2f}")

        # Show per-agent breakdown from summary.json
        summary_path = Path(run["path"]) / "summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text())
                agents = summary.get("agents", {})
                if agents:
                    panel.write(f"[#3b4261]{'─' * 60}[/]")
                    for name, info in agents.items():
                        state = info.get("state", "?")
                        dur = info.get("duration_s", 0)
                        tok = info.get("tokens", {})
                        state_color = "#9ece6a" if state == "DONE" else "#f7768e"
                        panel.write(
                            f"  [{state_color}]{state:>5}[/]  {name:<20}  "
                            f"[#565f89]{dur}s  {tok.get('in', 0):,}in {tok.get('out', 0):,}out[/]"
                        )
            except (json.JSONDecodeError, OSError):
                pass

    def action_cursor_down(self) -> None:
        self.query_one("#run-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#run-table", DataTable).action_cursor_up()

    def action_open_run(self) -> None:
        table = self.query_one("#run-table", DataTable)
        if not table.row_count:
            return
        row_idx = table.cursor_row
        if row_idx >= len(self._runs):
            return
        run = self._runs[row_idx]
        archive_path = Path(run["path"])
        agents_dir = archive_path / "agents"
        if not agents_dir.is_dir():
            self.notify("No agent logs in this archive", severity="warning")
            return

        # Decompress .jsonl.gz files to a temp directory, then launch TUI
        tmp = Path(tempfile.mkdtemp(prefix="agent-tui-history-"))
        try:
            # Copy manifest if present
            manifest = archive_path / "manifest.tsv"
            if manifest.exists():
                shutil.copy2(manifest, tmp / "manifest.tsv")

            # Decompress .jsonl.gz files
            for gz in agents_dir.glob("*.jsonl.gz"):
                name = gz.stem  # removes .gz, keeps .jsonl
                with gzip.open(gz, "rb") as fin:
                    (tmp / name).write_bytes(fin.read())

            # Copy .err files
            for err in agents_dir.glob("*.err"):
                shutil.copy2(err, tmp / err.name)

        except OSError as e:
            self.notify(f"Failed to decompress: {e}", severity="error")
            return

        # Exit this app and launch the main TUI on the temp dir
        self.exit(result=str(tmp))
