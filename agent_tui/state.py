from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache: int = 0

    def cost(self, rate_in: float = 0, rate_out: float = 0, rate_cache: float = 0) -> float:
        return (self.input * rate_in + self.output * rate_out + self.cache * rate_cache) / 1_000_000


@dataclass
class AgentState:
    name: str
    state: str = "unknown"
    heartbeat_s: int = 0
    action: str = ""
    tool_count: int = 0
    detail: str = ""
    session_id: str = ""
    duration_s: int = 0
    tokens: TokenUsage = field(default_factory=TokenUsage)
    wall_clock_s: int = 0
    is_error: bool = False
    last_tool_type: str = ""
    last_tool_subtype: str = ""
    recent_events: list[dict] = field(default_factory=list)
    err_tail: str = ""
    machine: str = "local"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid(logs_dir: Path, name: str) -> int | None:
    pidf = logs_dir / f"{name}.pid"
    if not pidf.exists():
        return None
    try:
        return int(pidf.read_text().strip())
    except (ValueError, OSError):
        return None


def _extract_tool_action(event: dict) -> str:
    tc = event.get("tool_call", {})
    if not tc:
        return ""
    tool_key = next(iter(tc), "")
    short = tool_key.removesuffix("ToolCall")
    args = tc.get(tool_key, {}).get("args", {})
    target = (
        args.get("relativePath")
        or args.get("path")
        or args.get("targetDirectory")
        or args.get("filePath")
        or args.get("command")
        or args.get("globPattern")
        or ""
    )
    if "/" in target:
        target = target.rsplit("/", 1)[-1]
    return f"{short} {target[:20]}"


def _extract_assistant_text(event: dict) -> str:
    content = event.get("message", {}).get("content", [])
    parts = [c.get("text", "") for c in content if isinstance(c, dict)]
    return " ".join(parts).replace("\n", " ").strip()[:80]


def parse_agent(logs_dir: Path, name: str, stall_secs: int = 60, max_recent: int = 30) -> AgentState:
    agent = AgentState(name=name)
    log_path = logs_dir / f"{name}.jsonl"
    if not log_path.exists():
        agent.state = "NO_LOG"
        return agent

    now = time.time()
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        mtime = now
    agent.heartbeat_s = int(now - mtime)

    pid = _read_pid(logs_dir, name)
    alive = pid is not None and _pid_alive(pid)

    pidf = logs_dir / f"{name}.pid"
    if pidf.exists():
        with contextlib.suppress(OSError):
            agent.wall_clock_s = int(now - pidf.stat().st_mtime)

    result_event = None
    last_tool_started = None
    last_assistant = None
    tool_count = 0

    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if len(agent.recent_events) < max_recent:
                    agent.recent_events.append(ev)
                else:
                    agent.recent_events.pop(0)
                    agent.recent_events.append(ev)

                ev_type = ev.get("type", "")

                if ev_type == "system" and ev.get("subtype") == "init":
                    agent.session_id = ev.get("session_id", "")

                elif ev_type == "tool_call":
                    if ev.get("subtype") == "started":
                        tool_count += 1
                        last_tool_started = ev
                    agent.last_tool_type = ev_type
                    agent.last_tool_subtype = ev.get("subtype", "")

                elif ev_type == "assistant":
                    last_assistant = ev

                elif ev_type == "result":
                    result_event = ev
    except OSError:
        agent.state = "READ_ERR"
        return agent

    agent.tool_count = tool_count

    if last_tool_started:
        agent.action = _extract_tool_action(last_tool_started)
    if last_assistant:
        agent.detail = _extract_assistant_text(last_assistant)

    if result_event:
        agent.is_error = result_event.get("is_error", False)
        agent.state = "ERROR" if agent.is_error else "DONE"
        agent.duration_s = int(result_event.get("duration_ms", 0) / 1000)
        agent.action = f"finished in {_fmt_duration(agent.duration_s)}"
        result_text = result_event.get("result", "")
        if result_text:
            agent.detail = str(result_text).replace("\n", " ")[:80]
        usage = result_event.get("usage", {})
        agent.tokens = TokenUsage(
            input=usage.get("inputTokens", 0),
            output=usage.get("outputTokens", 0),
            cache=usage.get("cacheReadTokens", 0),
        )
    elif alive:
        agent.state = "STALL" if agent.heartbeat_s > stall_secs else "run"
    else:
        agent.state = "DIED"
        err_path = logs_dir / f"{name}.err"
        if err_path.exists():
            try:
                lines = err_path.read_text().strip().splitlines()
                agent.err_tail = lines[-1][:80] if lines else ""
            except OSError:
                pass
        if not agent.detail and agent.err_tail:
            agent.detail = agent.err_tail

    return agent


def scan_agents(logs_dir: str | Path, stall_secs: int = 60) -> list[AgentState]:
    d = Path(logs_dir)
    if not d.is_dir():
        return []
    names = sorted({f.stem for f in d.glob("*.jsonl")})
    return [parse_agent(d, name, stall_secs) for name in names]


def _fmt_duration(s: int) -> str:
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def fmt_tokens(t: TokenUsage) -> str:
    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    return f"{_k(t.output)}o/{_k(t.input)}i"


def fmt_cost(t: TokenUsage, rate_in: float, rate_out: float, rate_cache: float = 0) -> str:
    return f"${t.cost(rate_in, rate_out, rate_cache):.2f}"


def fmt_spend(agent: AgentState, rate_in: float = 0, rate_out: float = 0, rate_cache: float = 0) -> str:
    if agent.state in ("DONE", "ERROR"):
        if rate_in and rate_out:
            return fmt_cost(agent.tokens, rate_in, rate_out, rate_cache)
        return fmt_tokens(agent.tokens)
    if agent.state == "run" or agent.state == "STALL":
        return _fmt_duration(agent.wall_clock_s)
    return "-"
