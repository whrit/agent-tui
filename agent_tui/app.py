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


def _state_markup(state: str) -> str:
    dot, color = STATE_INDICATOR.get(state, ("○", "#565f89"))
    return f"[{color}]{dot} {state}[/]"


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
        Binding("enter", "toggle_detail", "Detail"),
        Binding("w", "toggle_waves", "Waves"),
        Binding("p", "refresh_now", "Refresh"),
        Binding("l", "toggle_live", "Live"),
        Binding("d", "show_diff", "Diff"),
        Binding("slash", "toggle_filter", "Filter", show=False),
        Binding("s", "cycle_sort", "Sort"),
        Binding("escape", "clear_filter", "Clear", show=False, priority=True),
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
        self.refresh_interval = refresh_interval
        self._agents: list[AgentState] = []
        self._detail_visible = True
        self._waves_visible = False
        self._live_mode = False
        self._detail_mode = "events"  # "events" | "diff" | "live"
        self._filter_text = ""
        self._filter_visible = False
        self._sort_key = "name"
        self._sort_keys_cycle = ["name", "state", "cost", "duration", "heartbeat"]
        self._sort_index = 0
        self._sources: list[LogSource] = []
        self._multi_machine = False

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
            yield Static("agent-tui", id="header-title")
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

        # Footer
        yield Static(
            " [#7aa2f7 bold]q[/] quit  "
            "[#7aa2f7 bold]j/k[/] nav  "
            "[#7aa2f7 bold]enter[/] detail  "
            "[#7aa2f7 bold]w[/] waves  "
            "[#7aa2f7 bold]l[/] live  "
            "[#7aa2f7 bold]d[/] diff  "
            "[#7aa2f7 bold]/[/] filter  "
            "[#7aa2f7 bold]s[/] sort  "
            "[#7aa2f7 bold]r[/] retry  "
            "[#7aa2f7 bold]m[/] merge  "
            "[#7aa2f7 bold]x[/] discard  "
            "[#7aa2f7 bold]p[/] refresh",
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
        self._refresh_data()
        self.set_interval(self.refresh_interval, self._refresh_data)

    # ── Data refresh ──────────────────────────────────────────────────────

    def _refresh_data(self) -> None:
        all_agents = scan_all(self._sources, self._cfg.display.stall_secs)
        self._all_agents = all_agents
        self._agents = self._apply_filter(all_agents)
        self._agents = self._apply_sort(self._agents)
        self._update_table()
        self._update_header()
        self._update_status_strip()
        if self._detail_visible and self.selected_agent:
            if self._detail_mode == "live":
                self._render_live_log(self.selected_agent)
            elif self._detail_mode == "diff":
                pass  # diff is rendered on-demand, not on refresh
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

    def _update_table(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        r = self._cfg.rates

        prev_key: str | None = None
        if table.row_count:
            with contextlib.suppress(Exception):
                prev_key = str(table.get_row_at(table.cursor_row)[0]) if not self._multi_machine else None
                if self._multi_machine and table.cursor_row < len(self._agents):
                    a = self._agents[table.cursor_row]
                    prev_key = f"{a.machine}:{a.name}"

        table.clear()
        for a in self._agents:
            dot, color = STATE_INDICATOR.get(a.state, ("○", "#565f89"))
            spend = fmt_spend(a, r.input, r.output, r.cache)
            row: dict[str, str] = {
                "dot": f"[{color}]{dot}[/]",
                "name": a.name,
                "state": f"[{color}]{a.state}[/]",
                "hb": _fmt_hb(a.heartbeat_s),
                "action": a.action[:30],
                "tools": str(a.tool_count),
                "spend": spend,
                "detail": a.detail[:48],
            }
            if self._multi_machine:
                row["machine"] = f"[#565f89]{a.machine}[/]"
            key = f"{a.machine}:{a.name}" if self._multi_machine else a.name
            table.add_row(*row.values(), key=key)

        if prev_key:
            for i, a in enumerate(self._agents):
                k = f"{a.machine}:{a.name}" if self._multi_machine else a.name
                if k == prev_key:
                    table.move_cursor(row=i)
                    break

    def _update_header(self) -> None:
        src_agents = getattr(self, "_all_agents", self._agents)
        done = sum(1 for a in src_agents if a.state == "DONE")
        errors = sum(1 for a in src_agents if a.state in ("ERROR", "DIED"))
        running = sum(1 for a in src_agents if a.state in ("run", "STALL"))
        total = len(src_agents)
        filtered = len(self._agents)
        machines = " ".join(f"[#565f89]{s.machine_name}[/]" for s in self._sources)

        filter_tag = f"  [#e0af68]filter: {filtered}/{total}[/]" if self._filter_text else ""

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
                f" [#c0caf5 bold]{cost}[/] total  [#565f89]│[/]  "
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
        panel.write("  ".join(parts))

        if agent.err_tail:
            panel.write(f"[#f7768e]stderr: {agent.err_tail}[/]")

        panel.write(f"[#3b4261]{'─' * 72}[/]")

        # Event log
        for ev in agent.recent_events[-20:]:
            t = ev.get("type", "?")
            sub = ev.get("subtype", "")
            ts = ev.get("timestamp_ms")
            label = f"{t}/{sub}" if sub else t

            # Timestamp
            ts_str = ""
            if ts:
                ts_str = f"[#414868]{datetime.fromtimestamp(ts / 1000, tz=UTC).strftime('%H:%M:%S')}[/] "

            # Event-specific detail
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
                if ok:
                    extra = f" [#9ece6a]OK[/] [#565f89]in {dur}s[/]"
                else:
                    extra = f" [#f7768e]FAILED[/] [#565f89]in {dur}s[/]"
            elif t == "thinking":
                extra = " [#414868]…[/]"
                if sub == "completed":
                    continue

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
                extra = " [#414868]…[/]"
                if sub == "completed":
                    continue

            panel.write(f"  {ts_str}[#414868]{label}[/]{extra}")

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
                f"[#c0caf5 bold]{m}[/]  [#9ece6a]{done}[/][#565f89]/[/][#f7768e]{err}[/][#565f89]/[/][#7dcfff]{run}[/]"
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
            self._live_mode = False
            if self.selected_agent:
                self._render_detail(self.selected_agent)
        else:
            self._detail_mode = "live"
            self._live_mode = True
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
            self._live_mode = False
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

    def action_cycle_sort(self) -> None:
        self._sort_index = (self._sort_index + 1) % len(self._sort_keys_cycle)
        self._sort_key = self._sort_keys_cycle[self._sort_index]
        self.notify(f"Sort: {self._sort_key}", severity="information")
        self._refresh_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-bar":
            self._filter_text = event.value
            self._refresh_data()

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
