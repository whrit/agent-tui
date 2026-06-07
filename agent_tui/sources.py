from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agent_tui.state import AgentState, TokenUsage

if TYPE_CHECKING:
    from agent_tui.config import MachineConfig


class LogSource(Protocol):
    @property
    def machine_name(self) -> str: ...

    @property
    def source_type(self) -> str: ...

    def scan(self, stall_secs: int = 60) -> list[AgentState]: ...

    def run_action(self, action: str, agent_name: str) -> tuple[bool, str]:
        """Run an action (merge/retry/discard) on an agent. Returns (ok, message)."""
        ...

    def tail_log(self, agent_name: str, lines: int = 50) -> list[dict]:
        """Return the last N parsed events from an agent's JSONL log."""
        ...

    def get_diff(self, agent_name: str, stat_only: bool = False) -> str:
        """Return git diff of an agent's worktree vs its HEAD. Returns empty string if no changes."""
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
        from agent_tui.state import scan_agents

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
            "clean": ("clean.sh", [str(self._dir)]),
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

    def tail_log(self, agent_name: str, lines: int = 50) -> list[dict]:
        import json
        log_path = self._dir / f"{agent_name}.jsonl"
        if not log_path.exists():
            return []
        try:
            all_lines = log_path.read_text().strip().splitlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            events = []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return events
        except OSError:
            return []

    def get_diff(self, agent_name: str, stat_only: bool = False) -> str:
        import subprocess
        wt = Path.home() / ".cursor" / "worktrees" / self._repo.name / agent_name
        if not wt.is_dir():
            return "(worktree not found)"
        cmd = ["git", "-C", str(wt), "diff"]
        if stat_only:
            cmd.append("--stat")
        cmd.append("HEAD")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.stdout if result.stdout else ""
        except (subprocess.TimeoutExpired, Exception):
            return "(diff failed)"

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

        url = f"{self._url}/projects/{self._project}/agents?stall_secs={stall_secs}"
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

    def tail_log(self, agent_name: str, lines: int = 50) -> list[dict]:
        import urllib.error
        import urllib.request
        url = f"{self._url}/projects/{self._project}/agent/{agent_name}/tail?lines={lines}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            return data.get("events", [])
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []

    def get_diff(self, agent_name: str, stat_only: bool = False) -> str:
        import urllib.error
        import urllib.request
        url = f"{self._url}/projects/{self._project}/agent/{agent_name}/diff"
        if stat_only:
            url += "?stat=1"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read().decode()
        except (urllib.error.URLError, OSError):
            return "(remote diff unavailable)"

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
    a.result_json = d.get("result_json")
    tok = d.get("tokens", {})
    a.tokens = TokenUsage(
        input=tok.get("input", 0),
        output=tok.get("output", 0),
        cache=tok.get("cache", 0),
    )
    for ev in d.get("recent_events", []):
        a.recent_events.append(ev)
    return a


class WsSource:
    """WebSocket source — receives pushed state updates instead of polling."""

    def __init__(self, config: MachineConfig):
        self._name = config.name
        self._project = config.project or config.name
        self._ws_url = config.url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        # Increment port by 1 for WS (server convention)
        parts = self._ws_url.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            self._ws_url = f"{parts[0]}:{int(parts[1]) + 1}"
        self._agents: list[AgentState] = []
        self._connected = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._start()

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._run_ws, daemon=True, name=f"ws-{self._name}")
        self._thread.start()

    def _run_ws(self) -> None:
        try:
            import asyncio

            import websockets  # type: ignore[import-unresolved]
        except ImportError:
            return

        async def connect():
            while True:
                try:
                    async with websockets.connect(self._ws_url) as ws:
                        self._connected = True
                        async for msg in ws:
                            try:
                                data = json.loads(msg)
                                proj_agents = data.get("projects", {}).get(self._project, [])
                                agents = [_agent_from_server_dict(d) for d in proj_agents]
                                for a in agents:
                                    a.machine = self._name
                                with self._lock:
                                    self._agents = agents
                            except (json.JSONDecodeError, KeyError):
                                pass
                except Exception:
                    self._connected = False
                    import time
                    time.sleep(5)  # reconnect delay

        asyncio.run(connect())

    @property
    def machine_name(self) -> str:
        return self._name

    @property
    def source_type(self) -> str:
        return "ws"

    def scan(self, stall_secs: int = 60) -> list[AgentState]:
        with self._lock:
            if self._agents:
                return list(self._agents)
        # Not connected yet or no data — return offline sentinel
        if not self._connected:
            sentinel = AgentState(name=f"[{self._name}]", state="OFFLINE")
            sentinel.detail = f"WebSocket connecting to {self._ws_url}..."
            sentinel.machine = self._name
            return [sentinel]
        return []

    def run_action(self, action: str, agent_name: str) -> tuple[bool, str]:
        # Actions still use HTTP POST (rare operations, need confirmation)
        import urllib.error
        import urllib.request
        # Derive HTTP URL from WS URL
        http_url = self._ws_url.replace("ws://", "http://").replace("wss://", "https://")
        parts = http_url.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            http_url = f"{parts[0]}:{int(parts[1]) - 1}"
        url = f"{http_url}/projects/{self._project}/action/{action}/{agent_name}"
        try:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data.get("ok", False), data.get("output", "")[:200]
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return False, f"remote action failed: {e}"

    def tail_log(self, agent_name: str, lines: int = 50) -> list[dict]:
        # Fall back to HTTP for tail
        import urllib.error
        import urllib.request
        http_url = self._ws_url.replace("ws://", "http://").replace("wss://", "https://")
        parts = http_url.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            http_url = f"{parts[0]}:{int(parts[1]) - 1}"
        url = f"{http_url}/projects/{self._project}/agent/{agent_name}/tail?lines={lines}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
            return data.get("events", [])
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return []

    def get_diff(self, agent_name: str, stat_only: bool = False) -> str:
        import urllib.error
        import urllib.request
        http_url = self._ws_url.replace("ws://", "http://").replace("wss://", "https://")
        parts = http_url.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            http_url = f"{parts[0]}:{int(parts[1]) - 1}"
        url = f"{http_url}/projects/{self._project}/agent/{agent_name}/diff"
        if stat_only:
            url += "?stat=1"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.read().decode()
        except (urllib.error.URLError, OSError):
            return "(remote diff unavailable)"

    def close(self) -> None:
        pass


def create_source(config: MachineConfig, scripts_dir: Path | None = None) -> LogSource:
    if config.type == "http":
        try:
            import websockets  # type: ignore[import-unresolved]  # noqa: F401
            return WsSource(config)
        except ImportError:
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
