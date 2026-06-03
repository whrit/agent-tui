from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cursor_tui.state import AgentState, TokenUsage

if TYPE_CHECKING:
    from cursor_tui.config import MachineConfig


class LogSource(Protocol):
    @property
    def machine_name(self) -> str: ...

    @property
    def source_type(self) -> str: ...

    def scan(self, stall_secs: int = 60) -> list[AgentState]: ...

    def run_action(self, action: str, agent_name: str) -> tuple[bool, str]:
        """Run an action (merge/retry/discard) on an agent. Returns (ok, message)."""
        ...

    def close(self) -> None: ...


class LocalSource:
    def __init__(self, config: MachineConfig, scripts_dir: Path | None = None):
        self._name = config.name
        self._dir = Path(config.logs_dir)
        self._repo = Path(config.repo) if config.repo else self._dir.parent
        self._scripts_dir = scripts_dir or (Path.home() / ".claude" / "skills" / "orchestrating-cursor-agents")

    @property
    def machine_name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "local"

    def scan(self, stall_secs: int = 60) -> list[AgentState]:
        from cursor_tui.state import scan_agents

        agents = scan_agents(self._dir, stall_secs)
        for a in agents:
            a.machine = self._name
        return agents

    def run_action(self, action: str, agent_name: str) -> tuple[bool, str]:
        import subprocess

        scripts_dir = self._scripts_dir
        script_map = {
            "merge": ("merge.sh", [str(self._dir), "merge", agent_name]),
            "discard": ("merge.sh", [str(self._dir), "discard", agent_name]),
            "retry": ("retry.sh", [str(self._dir)]),
        }
        if action not in script_map:
            return False, f"unknown action: {action}"
        script_name, args = script_map[action]
        script_path = scripts_dir / script_name
        if not script_path.exists():
            return False, f"script not found: {script_path}"
        try:
            result = subprocess.run(
                ["bash", str(script_path), *args],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._repo),
            )
            output = result.stdout.strip() or result.stderr.strip()
            return result.returncode == 0, output[:200]
        except subprocess.TimeoutExpired:
            return False, f"{action} timed out"
        except Exception as e:
            return False, str(e)

    def close(self) -> None:
        pass


class HttpSource:
    def __init__(self, config: MachineConfig):
        self._name = config.name
        self._url = config.url.rstrip("/")
        self._project = config.project or config.name
        self._last_error: str = ""

    @property
    def machine_name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "http"

    def scan(self, stall_secs: int = 60) -> list[AgentState]:
        import urllib.error
        import urllib.request

        url = f"{self._url}/projects/{self._project}/agents"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self._last_error = str(e)
            sentinel = AgentState(name=f"[{self._name}]", state="OFFLINE")
            sentinel.detail = f"Cannot reach {self._url}: {self._last_error[:50]}"
            sentinel.machine = self._name
            return [sentinel]

        agents = []
        for raw in data.get("agents", []):
            a = _agent_from_server_dict(raw)
            a.machine = self._name
            agents.append(a)
        return agents

    def run_action(self, action: str, agent_name: str) -> tuple[bool, str]:
        import urllib.error
        import urllib.request

        url = f"{self._url}/projects/{self._project}/action/{action}/{agent_name}"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data.get("ok", False), data.get("output", "")[:200]
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return False, f"remote action failed: {e}"

    def close(self) -> None:
        pass


def _agent_from_server_dict(d: dict) -> AgentState:
    a = AgentState(name=d.get("name", "?"))
    a.state = d.get("state", "unknown")
    a.heartbeat_s = d.get("heartbeat_s", 0)
    a.action = d.get("action", "")
    a.tool_count = d.get("tool_count", 0)
    a.detail = d.get("detail", "")
    a.session_id = d.get("session_id", "")
    a.duration_s = d.get("duration_s", 0)
    a.wall_clock_s = d.get("wall_clock_s", 0)
    a.is_error = d.get("is_error", False)
    a.err_tail = d.get("err_tail", "")
    tok = d.get("tokens", {})
    a.tokens = TokenUsage(
        input=tok.get("input", 0),
        output=tok.get("output", 0),
        cache=tok.get("cache", 0),
    )
    for ev in d.get("recent_events", []):
        a.recent_events.append(ev)
    return a


def create_source(config: MachineConfig, scripts_dir: Path | None = None) -> LogSource:
    if config.type == "http":
        return HttpSource(config)
    if config.type == "ssh":
        import warnings

        warnings.warn(f"SSH source '{config.name}' not implemented — falling back to local", stacklevel=2)
    return LocalSource(config, scripts_dir=scripts_dir)


def scan_all(sources: list[LogSource], stall_secs: int = 60) -> list[AgentState]:
    all_agents: list[AgentState] = []
    for src in sources:
        all_agents.extend(src.scan(stall_secs))
    return all_agents
