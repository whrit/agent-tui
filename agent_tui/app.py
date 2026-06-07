from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, RichLog, Static

from agent_tui import __version__
from agent_tui.config import AppConfig
from agent_tui.sources import LogSource, create_source, scan_all
from agent_tui.state import AgentState, TokenUsage, fmt_cost, fmt_spend

# ── State indicators ─────────────────────────────────────────────────────────
# Dot + color for each state. Scannable at a glance.

STATE_INDICATOR: dict[str, tuple[str, str]] = {
    "DONE": ("●", "#9ece6a"),
    "ERROR": ("✗", "#f7768e"),
    "DIED": ("✗", "#f7768e"),
    "STALL": ("◐", "#e0af68"),
    "run": ("◐", "#7dcfff"),
    "OFFLINE": ("○", "#bb9af7"),
    "unknown": ("○", "#565f89"),
    "NO_LOG": ("○", "#565f89"),
    "READ_ERR": ("○", "#565f89"),
}


def _fmt_hb(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return ">1h"


# ── Confirm dialog ───────────────────────────────────────────────────────────


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [  # noqa: RUF012
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes [y]", id="btn-yes", variant="error")
                yield Button("No [n]", id="btn-no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── Command palette ───────────────────────────────────────────────────────────


class CommandPalette(ModalScreen[str | None]):
    BINDINGS = [  # noqa: RUF012
        Binding("escape", "dismiss_palette", "Close"),
    ]

    _COMMAND_LIST = [  # noqa: RUF012
        ("j / k", "Navigate agents up/down"),
        ("Enter", "Toggle detail panel"),
        ("l", "Toggle live log tailing"),
        ("d", "Toggle diff preview"),
        ("w", "Toggle wave sidebar"),
        ("/ + text", "Filter agents by name"),
        (":state", "Filter by state (:error, :done, :run, :stall)"),
        ("Escape", "Clear filter / close palette"),
        ("s", "Cycle sort order (name → state → cost → duration → heartbeat)"),
        ("r", "Retry selected agent"),
        ("m", "Merge selected agent"),
        ("x", "Discard selected agent"),
        ("c", "Clean all terminal worktrees"),
        ("M", "Merge all DONE agents (bulk)"),
        ("n", "Show notification history"),
        ("p", "Force refresh"),
        ("Tab", "Cycle focus between panels"),
        ("?", "Show this command palette"),
        ("q", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Static("[#7aa2f7 bold]Commands[/]  [#565f89]press ? or Escape to close[/]", id="palette-title")
            yield Input(placeholder="search commands...", id="palette-search")
            yield RichLog(id="palette-list", markup=True, wrap=True)

    def on_mount(self) -> None:
        self._render_commands("")
        self.query_one("#palette-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-search":
            self._render_commands(event.value)

    def _render_commands(self, query: str) -> None:
        panel = self.query_one("#palette-list", RichLog)
        panel.clear()
        q = query.lower()
        for key, desc in self._COMMAND_LIST:
            if q and q not in key.lower() and q not in desc.lower():
                continue
            panel.write(f"  [#7aa2f7 bold]{key:>14}[/]  [#c0caf5]{desc}[/]")

    def action_dismiss_palette(self) -> None:
        self.dismiss(None)


# ── Main app ─────────────────────────────────────────────────────────────────


class CursorTUI(App):
    CSS_PATH = "app.tcss"
    TITLE = "agent-tui"

    BINDINGS = [  # noqa: RUF012
        Binding("q", "quit", "Quit", priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("r", "retry", "Retry"),
        Binding("m", "merge", "Merge"),
        Binding("x", "discard", "Discard"),
        Binding("c", "clean_all", "Clean"),
        Binding("enter", "toggle_detail", "Detail"),
        Binding("w", "toggle_waves", "Waves"),
        Binding("p", "refresh_now", "Refresh"),
        Binding("l", "toggle_live", "Live"),
        Binding("d", "show_diff", "Diff"),
        Binding("slash", "toggle_filter", "Filter", show=False),
        Binding("s", "cycle_sort", "Sort"),
        Binding("M", "merge_all_ok", "Merge All", show=False),
        Binding("n", "show_notifications", "Notifs", show=False),
        Binding("escape", "clear_filter", "Clear", show=False, priority=True),
        Binding("question_mark", "show_palette", "Help"),
        Binding("tab", "focus_next", "Focus", show=False),
        Binding("shift+tab", "focus_previous", "Focus", show=False),
    ]

    selected_agent: reactive[str | None] = reactive(None)

    def __init__(
        self,
        config: AppConfig | None = None,
        scripts_dir: str | None = None,
        refresh_interval: float = 2.0,
        logs_dir: str | None = None,
    ):
        super().__init__()
        self._cfg = config or AppConfig()
        self.theme = self._cfg.display.theme
        self.refresh_interval = refresh_interval
        self._agents: list[AgentState] = []
        self._all_agents: list[AgentState] = []
        self._detail_visible = True
        self._waves_visible = False
        self._detail_mode = "events"  # "events" | "diff" | "live"
        self._filter_text = ""
        self._filter_visible = False
        self._sort_key = "name"
        self._sort_keys_cycle = ["name", "state", "cost", "duration", "heartbeat"]
        self._sort_index = 0
        self._sources: list[LogSource] = []
        self._multi_machine = False
        self._prev_states: dict[str, str] = {}
        self._notification_log: list[tuple[str, str, str]] = []
        self._table_keys: set[str] = set()

        if logs_dir and not config:
            from agent_tui.config import MachineConfig

            self._cfg.machines = [MachineConfig(name="local", type="local", logs_dir=logs_dir)]

        _sd = Path(scripts_dir) if scripts_dir else None
        for mc in self._cfg.machines:
            self._sources.append(create_source(mc, scripts_dir=_sd))
        self._multi_machine = len(self._sources) > 1

    # ── Layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # Header
        with Horizontal(id="header"):
            yield Static("◈ agent-tui", id="header-title")
            yield Static("", id="header-stats")

        # Status strip
        yield Static("", id="status-strip")

        # Filter bar (hidden by default)
        yield Input(placeholder="filter: type name or :state (esc to clear)", id="filter-bar", classes="collapsed")

        # Body: table + optional wave sidebar
        with Horizontal(id="body"):
            with Vertical(id="table-pane"):
                yield DataTable(id="agent-table", cursor_type="row", zebra_stripes=True)
            yield RichLog(id="wave-pane", classes="collapsed", markup=True, wrap=True)

        # Detail panel (starts visible)
        yield RichLog(id="detail-pane", markup=True, wrap=True)

        # Footer — essentials only; ? for full list
        yield Static(
            " [#7aa2f7 bold]j/k[/] nav  "
            "[#7aa2f7 bold]enter[/] detail  "
            "[#7aa2f7 bold]r[/] retry  "
            "[#7aa2f7 bold]m[/] merge  "
            "[#7aa2f7 bold]/[/] filter  "
            "[#7aa2f7 bold]?[/] all keys",
            id="footer",
        )

    def on_mount(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        if self._multi_machine:
            table.add_column("Machine", key="machine")
        table.add_column("", key="dot", width=2)
        table.add_column("Name", key="name")
        table.add_column("State", key="state", width=8)
        table.add_column("Heartbt", key="hb", width=8)
        table.add_column("Action", key="action")
        table.add_column("Tools", key="tools", width=5)
        table.add_column("Spend", key="spend", width=12)
        table.add_column("Detail", key="detail")
        wave_pane = self.query_one("#wave-pane", RichLog)
        wave_pane.styles.width = self._cfg.display.wave_sidebar_width
        self._refresh_data()
        self.set_interval(self.refresh_interval, self._refresh_data)
        self.set_interval(1.0, self._tick_running_agents)

    # ── Data refresh ──────────────────────────────────────────────────────

    def _refresh_data(self) -> None:
        self._scan_in_background()

    @work(thread=True, exclusive=True)
    def _scan_in_background(self) -> None:
        all_agents = scan_all(self._sources, self._cfg.display.stall_secs)
        self.call_from_thread(self._apply_scan_results, all_agents)

    def _apply_scan_results(self, all_agents: list[AgentState]) -> None:
        self._check_state_transitions(all_agents)
        self._all_agents = all_agents
        self._agents = self._apply_filter(all_agents)
        self._agents = self._apply_sort(self._agents)
        self._update_table()
        self._update_header()
        self._update_status_strip()
        if self._detail_visible and self.selected_agent:
            if self._detail_mode == "live":
                self._render_live_log(self.selected_agent)
            elif self._detail_mode in ("diff", "notifications"):
                pass
            else:
                self._render_detail(self.selected_agent)
        if self._waves_visible:
            self._render_waves()

    def _apply_filter(self, agents: list[AgentState]) -> list[AgentState]:
        if not self._filter_text:
            return agents
        ft = self._filter_text.strip().lower()
        if ft.startswith(":"):
            state_filter = ft[1:]
            return [a for a in agents if a.state.lower() == state_filter]
        return [a for a in agents if ft in a.name.lower()]

    def _apply_sort(self, agents: list[AgentState]) -> list[AgentState]:
        r = self._cfg.rates
        key_map = {
            "name": lambda a: a.name.lower(),
            "state": lambda a: (0 if a.state == "run" else 1 if a.state == "STALL" else 2 if a.state == "ERROR" else 3 if a.state == "DIED" else 4),
            "cost": lambda a: -a.tokens.cost(r.input, r.output, r.cache),
            "duration": lambda a: -(a.duration_s or a.wall_clock_s),
            "heartbeat": lambda a: -a.heartbeat_s,
        }
        fn = key_map.get(self._sort_key, key_map["name"])
        return sorted(agents, key=fn)

    def _build_row_cells(self, a: AgentState) -> dict[str, str]:
        r = self._cfg.rates
        dot, color = STATE_INDICATOR.get(a.state, ("○", "#565f89"))
        spend = fmt_spend(a, r.input, r.output, r.cache)
        detail_text = a.detail[:48]
        if a.state in ("run", "STALL"):
            pct = min(a.wall_clock_s / 300, 1.0)
            bar = "▓" * int(pct * 8) + "░" * (8 - int(pct * 8))
            detail_text = f"{bar} {a.detail[:36]}"
        cells: dict[str, str] = {}
        if self._multi_machine:
            cells["machine"] = f"[#565f89]{a.machine}[/]"
        cells.update({
            "dot": f"[{color}]{dot}[/]",
            "name": a.name,
            "state": f"[{color}]{a.state}[/]",
            "hb": _fmt_hb(a.heartbeat_s),
            "action": a.action[:30],
            "tools": str(a.tool_count),
            "spend": spend,
            "detail": detail_text,
        })
        return cells

    def _update_table(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        new_keys: set[str] = set()

        for a in self._agents:
            key = f"{a.machine}:{a.name}" if self._multi_machine else a.name
            new_keys.add(key)
            cells = self._build_row_cells(a)

            if key in self._table_keys:
                for col, val in cells.items():
                    with contextlib.suppress(Exception):
                        table.update_cell(key, col, val)
            else:
                table.add_row(*cells.values(), key=key)

        for old_key in self._table_keys - new_keys:
            with contextlib.suppress(Exception):
                table.remove_row(old_key)

        self._table_keys = new_keys

    def _update_header(self) -> None:
        src_agents = getattr(self, "_all_agents", self._agents)
        done = sum(1 for a in src_agents if a.state == "DONE")
        errors = sum(1 for a in src_agents if a.state in ("ERROR", "DIED"))
        running = sum(1 for a in src_agents if a.state in ("run", "STALL"))
        total = len(src_agents)
        filtered = len(self._agents)
        machines = " ".join(f"[#565f89]{s.machine_name}[/]" for s in self._sources)

        filter_tag = f"  [#e0af68]filter: {filtered}/{total}[/]" if self._filter_text else ""

        title = self.query_one("#header-title", Static)
        title.update(f"[#7aa2f7 bold]◈ agent-tui[/]  [#414868]v{__version__}[/]")

        stats = self.query_one("#header-stats", Static)
        stats.update(
            f"[#9ece6a]● {done} ok[/]  "
            f"[#7dcfff]◐ {running} run[/]  "
            f"[#f7768e]✗ {errors} err[/]  "
            f"[#565f89]│[/]  {total} total{filter_tag}  "
            f"[#565f89]│[/]  {machines} "
        )

    def _update_status_strip(self) -> None:
        r = self._cfg.rates
        src_agents = getattr(self, "_all_agents", self._agents)
        total_tokens = TokenUsage(
            input=sum(a.tokens.input for a in src_agents),
            output=sum(a.tokens.output for a in src_agents),
            cache=sum(a.tokens.cache for a in src_agents),
        )
        strip = self.query_one("#status-strip", Static)
        if r.input and r.output:
            cost = fmt_cost(total_tokens, r.input, r.output, r.cache)
            strip.update(
                f" [#e0af68 bold]{cost}[/] total  [#3b4261]│[/]  "
                f"[#565f89]{total_tokens.input:,} in  {total_tokens.output:,} out  {total_tokens.cache:,} cache[/]"
            )
        else:
            strip.update(
                f" [#565f89]{total_tokens.input:,} in  {total_tokens.output:,} out  {total_tokens.cache:,} cache[/]"
            )

    # ── Detail panel ──────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self.selected_agent = str(event.row_key.value)
            if self._detail_visible:
                if self._detail_mode == "live":
                    self._render_live_log(self.selected_agent)
                elif self._detail_mode == "diff":
                    self._render_diff(self.selected_agent)
                else:
                    self._render_detail(self.selected_agent)

    def _render_detail(self, key: str) -> None:
        panel = self.query_one("#detail-pane", RichLog)
        panel.clear()
        agent = self._find_agent(key)
        if not agent:
            return

        dot, color = STATE_INDICATOR.get(agent.state, ("○", "#565f89"))
        r = self._cfg.rates
        machine_tag = f"  [#565f89]{agent.machine}[/]" if self._multi_machine else ""

        # Header line
        panel.write(
            f"[{color} bold]{dot} {agent.name}[/]  "
            f"[{color}]{agent.state}[/]{machine_tag}  "
            f"[#565f89]session={agent.session_id or '—'}[/]"
        )

        # Metrics line
        parts = [f"[#565f89]heartbeat[/] {_fmt_hb(agent.heartbeat_s)}"]
        parts.append(f"[#565f89]tools[/] {agent.tool_count}")
        if agent.duration_s:
            parts.append(f"[#565f89]duration[/] {_fmt_hb(agent.duration_s)}")
        if agent.tokens.output:
            parts.append(f"[#565f89]tokens[/] {agent.tokens.input:,}in {agent.tokens.output:,}out")
            if r.input and r.output:
                parts.append(f"[#565f89]cost[/] {fmt_cost(agent.tokens, r.input, r.output, r.cache)}")
        if agent.state in ("run", "STALL"):
            elapsed = agent.wall_clock_s
            pct = min(elapsed / 300, 1.0)
            bar_width = 20
            filled = int(pct * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            parts.append(f"[#7dcfff]{bar}[/] {_fmt_hb(elapsed)}")
        panel.write("  ".join(parts))

        if agent.err_tail:
            panel.write(f"[#f7768e]stderr: {agent.err_tail}[/]")

        if agent.result_json:
            status = agent.result_json.get("status", "?")
            files = agent.result_json.get("files", [])
            notes = agent.result_json.get("notes", "")
            status_color = "#9ece6a" if status == "ok" else "#f7768e"
            panel.write(
                f"  [{status_color}]RESULT: {status}[/]  "
                f"[#565f89]{len(files)} file{'s' if len(files) != 1 else ''}[/]  "
                f"{notes[:50]}"
            )
            for f in files[:5]:
                panel.write(f"    [#565f89]{f}[/]")
            if len(files) > 5:
                panel.write(f"    [#565f89]… +{len(files) - 5} more[/]")

        panel.write(f"[#3b4261]{'─' * 72}[/]")

        for ev in agent.recent_events[-20:]:
            self._render_event(panel, ev)

    def _render_event(self, panel: RichLog, ev: dict) -> None:
        t = ev.get("type", "?")
        sub = ev.get("subtype", "")
        ts = ev.get("timestamp_ms")
        label = f"{t}/{sub}" if sub else t

        ts_str = ""
        if ts:
            ts_str = f"[#414868]{datetime.fromtimestamp(ts / 1000, tz=UTC).strftime('%H:%M:%S')}[/] "

        extra = ""
        if t == "tool_call" and sub == "started":
            tc = ev.get("tool_call", {})
            tool_key = next(iter(tc), "")
            args = tc.get(tool_key, {}).get("args", {})
            target = args.get("path") or args.get("command") or args.get("globPattern") or ""
            if "/" in target:
                target = target.rsplit("/", 1)[-1]
            short_tool = tool_key.removesuffix("ToolCall")
            extra = f" [#7aa2f7]{short_tool}[/] [#565f89]{target[:35]}[/]"
        elif t == "tool_call" and sub == "completed":
            extra = " [#565f89]done[/]"
        elif t == "assistant":
            content = ev.get("message", {}).get("content", [])
            txt = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            extra = f" [#c0caf5]{txt[:60]}[/]"
        elif t == "result":
            ok = not ev.get("is_error")
            dur = ev.get("duration_ms", 0) // 1000
            extra = f" [#9ece6a]OK[/] [#565f89]in {dur}s[/]" if ok else f" [#f7768e]FAILED[/] [#565f89]in {dur}s[/]"
        elif t == "thinking":
            if sub == "completed":
                return
            extra = " [#414868]…[/]"

        panel.write(f"  {ts_str}[#414868]{label}[/]{extra}")

    def _render_live_log(self, key: str) -> None:
        panel = self.query_one("#detail-pane", RichLog)
        panel.clear()
        agent = self._find_agent(key)
        if not agent:
            return

        src = self._source_for(agent.machine)
        if not src:
            return

        dot, color = STATE_INDICATOR.get(agent.state, ("○", "#565f89"))
        panel.write(
            f"[{color} bold]{dot} {agent.name}[/]  "
            f"[{color}]{agent.state}[/]  "
            f"[#e0af68 bold]LIVE[/]  "
            f"[#565f89]session={agent.session_id or '—'}[/]"
        )
        panel.write(f"[#3b4261]{'─' * 72}[/]")

        events = src.tail_log(agent.name, lines=40)
        for ev in events:
            self._render_event(panel, ev)

    def _render_diff(self, key: str) -> None:
        panel = self.query_one("#detail-pane", RichLog)
        panel.clear()
        agent = self._find_agent(key)
        if not agent:
            return

        src = self._source_for(agent.machine)
        if not src:
            return

        dot, color = STATE_INDICATOR.get(agent.state, ("○", "#565f89"))
        panel.write(
            f"[{color} bold]{dot} {agent.name}[/]  "
            f"[{color}]{agent.state}[/]  "
            f"[#bb9af7 bold]DIFF[/]  "
            f"[#565f89]press D to return to events[/]"
        )
        panel.write(f"[#3b4261]{'─' * 72}[/]")

        diff_text = src.get_diff(agent.name)
        if not diff_text or diff_text.strip() == "":
            panel.write("[#565f89]No uncommitted changes[/]")
            return

        for line in diff_text.splitlines():
            escaped = line.replace("[", "\\[")
            if line.startswith("+++") or line.startswith("---"):
                panel.write(f"[bold]{escaped}[/]")
            elif line.startswith("+"):
                panel.write(f"[#9ece6a]{escaped}[/]")
            elif line.startswith("-"):
                panel.write(f"[#f7768e]{escaped}[/]")
            elif line.startswith("@@"):
                panel.write(f"[#7dcfff]{escaped}[/]")
            elif line.startswith("diff "):
                panel.write(f"[bold #c0caf5]{escaped}[/]")
            else:
                panel.write(f"[#565f89]{escaped}[/]")

    # ── Wave sidebar ──────────────────────────────────────────────────────

    def _render_waves(self) -> None:
        panel = self.query_one("#wave-pane", RichLog)
        panel.clear()

        # Try wave grouping first
        waves: dict[str, list[AgentState]] = {}
        ungrouped: list[AgentState] = []
        for a in self._agents:
            if a.wave:
                waves.setdefault(a.wave, []).append(a)
            else:
                ungrouped.append(a)

        if waves:
            panel.write("[#7aa2f7 bold]Waves[/]")
            panel.write(f"[#3b4261]{'━' * 28}[/]")
            for wave_name in sorted(waves.keys()):
                agents = waves[wave_name]
                done = sum(1 for a in agents if a.state == "DONE")
                total = len(agents)
                all_done = done == total
                wave_color = "#9ece6a" if all_done else "#7dcfff"
                wave_dot = "●" if all_done else "◐"
                panel.write("")
                panel.write(
                    f"[{wave_color}]{wave_dot}[/] [#c0caf5 bold]{wave_name}[/]  "
                    f"[#565f89]{done}/{total}[/]"
                )
                panel.write(f"[#3b4261]{'─' * 28}[/]")
                for a in agents:
                    dot, color = STATE_INDICATOR.get(a.state, ("○", "#565f89"))
                    name_short = a.name
                    if a.wave and name_short.startswith(a.wave + "-"):
                        name_short = name_short[len(a.wave) + 1:]
                    name_short = name_short[:18]
                    detail = ""
                    if a.state == "run":
                        detail = f"[#414868]{a.action[:12]}[/]"
                    elif a.state in ("ERROR", "DIED"):
                        detail = f"[#414868]{a.detail[:12]}[/]"
                    elif a.state == "DONE":
                        detail = f"[#414868]{_fmt_hb(a.duration_s)}[/]"
                    panel.write(f"  [{color}]{dot}[/] {name_short}  {detail}")

            # Show ungrouped agents below waves if any
            if ungrouped:
                panel.write("")
                panel.write("[#c0caf5 bold]Other[/]")
                panel.write(f"[#3b4261]{'─' * 28}[/]")
                for a in ungrouped:
                    dot, color = STATE_INDICATOR.get(a.state, ("○", "#565f89"))
                    panel.write(f"  [{color}]{dot}[/] {a.name[:18]}")
        else:
            # Fall back to machine grouping (original behavior)
            panel.write("[#7aa2f7 bold]Agents[/]")
            panel.write(f"[#3b4261]{'━' * 28}[/]")

            machines: dict[str, list[AgentState]] = {}
            for a in self._agents:
                machines.setdefault(a.machine, []).append(a)

            for m, agents in machines.items():
                done = sum(1 for a in agents if a.state == "DONE")
                err = sum(1 for a in agents if a.state in ("ERROR", "DIED"))
                run = sum(1 for a in agents if a.state in ("run", "STALL"))
                panel.write("")
                panel.write(
                    f"[#c0caf5 bold]{m}[/]  "
                    f"[#9ece6a]{done}[/][#565f89]/[/]"
                    f"[#f7768e]{err}[/][#565f89]/[/]"
                    f"[#7dcfff]{run}[/]"
                )
                panel.write(f"[#3b4261]{'─' * 28}[/]")
                for a in agents:
                    dot, color = STATE_INDICATOR.get(a.state, ("○", "#565f89"))
                    name = a.name[:20]
                    detail = ""
                    if a.state == "run":
                        detail = f"[#414868]{a.action[:14]}[/]"
                    elif a.state in ("ERROR", "DIED"):
                        detail = f"[#414868]{a.detail[:14]}[/]"
                    elif a.state == "DONE":
                        detail = f"[#414868]{_fmt_hb(a.duration_s)}[/]"
                    panel.write(f"  [{color}]{dot}[/] {name}  {detail}")

    # ── Actions ───────────────────────────────────────────────────────────

    def _find_agent(self, key: str) -> AgentState | None:
        for a in self._agents:
            k = f"{a.machine}:{a.name}" if self._multi_machine else a.name
            if k == key:
                return a
        return None

    def _source_for(self, machine_name: str) -> LogSource | None:
        for src in self._sources:
            if src.machine_name == machine_name:
                return src
        return None

    def action_cursor_down(self) -> None:
        self.query_one("#agent-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#agent-table", DataTable).action_cursor_up()

    def action_toggle_detail(self) -> None:
        panel = self.query_one("#detail-pane", RichLog)
        self._detail_visible = not self._detail_visible
        if self._detail_visible:
            panel.remove_class("collapsed")
            if self.selected_agent:
                if self._detail_mode == "live":
                    self._render_live_log(self.selected_agent)
                elif self._detail_mode == "diff":
                    self._render_diff(self.selected_agent)
                else:
                    self._render_detail(self.selected_agent)
        else:
            panel.add_class("collapsed")

    def action_toggle_waves(self) -> None:
        panel = self.query_one("#wave-pane", RichLog)
        self._waves_visible = not self._waves_visible
        if self._waves_visible:
            panel.remove_class("collapsed")
            self._render_waves()
        else:
            panel.add_class("collapsed")

    def action_refresh_now(self) -> None:
        self._refresh_data()

    def action_toggle_live(self) -> None:
        if self._detail_mode == "live":
            self._detail_mode = "events"
            if self.selected_agent:
                self._render_detail(self.selected_agent)
        else:
            self._detail_mode = "live"
            if not self._detail_visible:
                self._detail_visible = True
                self.query_one("#detail-pane", RichLog).remove_class("collapsed")
            if self.selected_agent:
                self._render_live_log(self.selected_agent)

    def action_show_diff(self) -> None:
        if self._detail_mode == "diff":
            self._detail_mode = "events"
            if self.selected_agent:
                self._render_detail(self.selected_agent)
        else:
            self._detail_mode = "diff"
            if not self._detail_visible:
                self._detail_visible = True
                self.query_one("#detail-pane", RichLog).remove_class("collapsed")
            if self.selected_agent:
                self._render_diff(self.selected_agent)

    def action_toggle_filter(self) -> None:
        bar = self.query_one("#filter-bar", Input)
        self._filter_visible = not self._filter_visible
        if self._filter_visible:
            bar.remove_class("collapsed")
            bar.focus()
        else:
            bar.add_class("collapsed")
            self.query_one("#agent-table", DataTable).focus()

    def action_clear_filter(self) -> None:
        if self._filter_visible:
            bar = self.query_one("#filter-bar", Input)
            bar.value = ""
            self._filter_text = ""
            self._filter_visible = False
            bar.add_class("collapsed")
            self.query_one("#agent-table", DataTable).focus()
            self._refresh_data()

    def action_show_palette(self) -> None:
        self.push_screen(CommandPalette())

    def action_cycle_sort(self) -> None:
        self._sort_index = (self._sort_index + 1) % len(self._sort_keys_cycle)
        self._sort_key = self._sort_keys_cycle[self._sort_index]
        self.notify(f"Sort: {self._sort_key}", severity="information")
        self._refresh_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-bar":
            self._filter_text = event.value
            self._refresh_data()

    # ── State transitions + notifications ───────────────────────────────

    def _check_state_transitions(self, new_agents: list[AgentState]) -> None:
        for a in new_agents:
            key = f"{a.machine}:{a.name}"
            prev = self._prev_states.get(key)
            if prev and prev != a.state:
                if a.state == "DONE":
                    self._desktop_notify(f"✓ {a.name} finished", f"Duration: {_fmt_hb(a.duration_s)}")
                elif a.state in ("ERROR", "DIED"):
                    self._desktop_notify(f"✗ {a.name} failed", a.detail[:50])
            self._prev_states[key] = a.state

    def _desktop_notify(self, title: str, body: str) -> None:
        import subprocess
        import sys

        title = title.replace('"', "'").replace("\\", "")
        body = body.replace('"', "'").replace("\\", "")
        if sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "linux":
            subprocess.Popen(
                ["notify-send", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def notify(self, message: str, *, title: str = "", severity: str = "information", timeout: float = 5, markup: bool = True) -> None:
        ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
        self._notification_log.append((ts, severity, str(message)))
        if len(self._notification_log) > 50:
            self._notification_log.pop(0)
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)

    def action_show_notifications(self) -> None:
        self._detail_mode = "notifications"
        if not self._detail_visible:
            self._detail_visible = True
            self.query_one("#detail-pane", RichLog).remove_class("collapsed")
        panel = self.query_one("#detail-pane", RichLog)
        panel.clear()
        panel.write("[#7aa2f7 bold]Notification History[/]")
        panel.write(f"[#3b4261]{'─' * 72}[/]")
        for ts, sev, msg in reversed(self._notification_log):
            color = {"information": "#7dcfff", "warning": "#e0af68", "error": "#f7768e"}.get(sev, "#565f89")
            panel.write(f"  [#414868]{ts}[/]  [{color}]{msg}[/]")
        if not self._notification_log:
            panel.write("  [#565f89]No notifications yet[/]")

    def action_merge_all_ok(self) -> None:
        ok_agents = [a for a in self._agents if a.state == "DONE"]
        if not ok_agents:
            self.notify("No DONE agents to merge", severity="information")
            return
        names = ", ".join(a.name for a in ok_agents[:5])
        if len(ok_agents) > 5:
            names += f" +{len(ok_agents) - 5} more"

        def _do_merge_all(confirmed: bool | None) -> None:
            if confirmed:
                for a in ok_agents:
                    src = self._source_for(a.machine)
                    if src:
                        self._do_action(src, "merge", a.name)

        self.push_screen(ConfirmDialog(f"Merge {len(ok_agents)} DONE agent(s)?\n{names}"), _do_merge_all)

    # ── Per-second ticker ─────────────────────────────────────────────────

    def _tick_running_agents(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        for a in self._agents:
            if a.state not in ("run", "STALL"):
                continue
            a.wall_clock_s += 1
            a.heartbeat_s = max(0, a.heartbeat_s - 1)
            key = f"{a.machine}:{a.name}" if self._multi_machine else a.name
            with contextlib.suppress(Exception):
                table.update_cell(key, "hb", _fmt_hb(a.heartbeat_s))
                pct = min(a.wall_clock_s / 300, 1.0)
                bar = "▓" * int(pct * 8) + "░" * (8 - int(pct * 8))
                table.update_cell(key, "detail", f"{bar} {a.detail[:36]}")

    # ── Agent actions ─────────────────────────────────────────────────────

    def action_retry(self) -> None:
        agent = self._find_agent(self.selected_agent) if self.selected_agent else None
        if not agent:
            return
        src = self._source_for(agent.machine)
        if not src:
            return
        self._do_action(src, "retry", agent.name)

    def action_merge(self) -> None:
        if not self.selected_agent:
            return
        agent = self._find_agent(self.selected_agent)
        if not agent:
            return
        src = self._source_for(agent.machine)
        if not src:
            return

        def _do_merge(confirmed: bool | None) -> None:
            if confirmed and src:
                self._do_action(src, "merge", agent.name)

        label = f"[{agent.machine}] " if self._multi_machine else ""
        self.push_screen(ConfirmDialog(f"Merge {label}'{agent.name}'?"), _do_merge)

    def action_discard(self) -> None:
        if not self.selected_agent:
            return
        agent = self._find_agent(self.selected_agent)
        if not agent:
            return
        src = self._source_for(agent.machine)
        if not src:
            return

        def _do_discard(confirmed: bool | None) -> None:
            if confirmed and src:
                self._do_action(src, "discard", agent.name)

        label = f"[{agent.machine}] " if self._multi_machine else ""
        self.push_screen(ConfirmDialog(f"Discard {label}'{agent.name}' worktree?"), _do_discard)

    def action_clean_all(self) -> None:
        src_agents = getattr(self, "_all_agents", self._agents)
        terminal = sum(1 for a in src_agents if a.state in ("DONE", "ERROR", "DIED"))
        if terminal == 0:
            self.notify("No terminal agents to clean", severity="information")
            return

        def _do_clean(confirmed: bool | None) -> None:
            if confirmed:
                for src in self._sources:
                    self._do_action(src, "clean", "_all")

        self.push_screen(ConfirmDialog(f"Clean {terminal} terminal agent worktree(s)?"), _do_clean)

    @work(thread=True)
    def _do_action(self, source: LogSource, action: str, agent_name: str) -> None:
        try:
            ok, msg = source.run_action(action, agent_name)
            short = msg[:100] if msg else "done"
            severity = "information" if ok else "warning"
            self.call_from_thread(self.notify, f"{action}: {short}", severity=severity)
            self.call_from_thread(self._refresh_data)
        except Exception as e:
            self.call_from_thread(self.notify, f"{action}: {e}", severity="error")
