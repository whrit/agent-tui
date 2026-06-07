from __future__ import annotations

import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent_tui.state import AgentState, scan_agents

# Route patterns
_RE_PROJECT_AGENTS = re.compile(r"^/projects/([^/]+)/agents$")
_RE_PROJECT_AGENT = re.compile(r"^/projects/([^/]+)/agent/([^/]+)$")
_RE_PROJECT_ACTION = re.compile(r"^/projects/([^/]+)/action/(merge|retry|discard|clean)/([^/]+)$")
_RE_PROJECT_AGENT_TAIL = re.compile(r"^/projects/([^/]+)/agent/([^/]+)/tail$")
_RE_PROJECT_AGENT_DIFF = re.compile(r"^/projects/([^/]+)/agent/([^/]+)/diff$")
# Legacy single-project routes (backward compat)
_RE_LEGACY_AGENTS = re.compile(r"^/agents$")
_RE_LEGACY_AGENT = re.compile(r"^/agent/([^/]+)$")


class LogHandler(BaseHTTPRequestHandler):
    projects: dict[str, Path] = {}  # noqa: RUF012
    scripts_dir: Path | None = None
    stall_secs: int = 60

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "projects": list(self.projects.keys())})
            return
        if self.path == "/projects":
            self._serve_project_list()
            return

        # Multi-project routes
        if m := _RE_PROJECT_AGENTS.match(self.path.split("?")[0]):
            self._serve_agents(m.group(1))
            return
        if m := _RE_PROJECT_AGENT.match(self.path):
            self._serve_agent_detail(m.group(1), m.group(2))
            return
        if m := _RE_PROJECT_AGENT_TAIL.match(self.path.split("?")[0]):
            self._serve_agent_tail(m.group(1), m.group(2))
            return
        if m := _RE_PROJECT_AGENT_DIFF.match(self.path.split("?")[0]):
            self._serve_agent_diff(m.group(1), m.group(2))
            return

        # Legacy single-project routes (first project)
        if _RE_LEGACY_AGENTS.match(self.path):
            first = next(iter(self.projects), None)
            if first:
                self._serve_agents(first)
            else:
                self._json(404, {"error": "no projects configured"})
            return
        if m := _RE_LEGACY_AGENT.match(self.path):
            first = next(iter(self.projects), None)
            if first:
                self._serve_agent_detail(first, m.group(1))
            else:
                self._json(404, {"error": "no projects configured"})
            return

        self._json(
            404,
            {
                "error": "not found",
                "endpoints": [
                    "GET  /projects",
                    "GET  /projects/<name>/agents",
                    "GET  /projects/<name>/agent/<id>",
                    "GET  /projects/<name>/agent/<id>/tail?lines=N",
                    "GET  /projects/<name>/agent/<id>/diff?stat=1",
                    "POST /projects/<name>/action/{merge|retry|discard}/<agent>",
                    "GET  /health",
                ],
            },
        )

    def do_POST(self):
        if m := _RE_PROJECT_ACTION.match(self.path):
            self._handle_action(m.group(1), m.group(2), m.group(3))
            return
        self._json(404, {"error": "not found"})

    # ── Handlers ──────────────────────────────────────────────────────────

    def _serve_project_list(self):
        projects = []
        for name, logs_dir in self.projects.items():
            agents = scan_agents(logs_dir, self.stall_secs)
            projects.append(
                {
                    "name": name,
                    "logs_dir": str(logs_dir),
                    "agent_count": len(agents),
                    "terminal": sum(1 for a in agents if a.state in ("DONE", "ERROR", "DIED")),
                }
            )
        self._json(200, {"projects": projects})

    def _serve_agents(self, project: str):
        logs_dir = self.projects.get(project)
        if not logs_dir:
            self._json(404, {"error": f"project '{project}' not found", "available": list(self.projects.keys())})
            return
        stall_secs = self.stall_secs
        if "?" in self.path:
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            try:
                stall_secs = int(qs.get("stall_secs", [str(self.stall_secs)])[0])
            except (ValueError, IndexError):
                stall_secs = self.stall_secs
        agents = scan_agents(logs_dir, stall_secs)
        self._json(
            200,
            {
                "project": project,
                "agents": [_agent_to_dict(a) for a in agents],
                "count": len(agents),
                "terminal": sum(1 for a in agents if a.state in ("DONE", "ERROR", "DIED")),
            },
        )

    def _serve_agent_detail(self, project: str, name: str):
        logs_dir = self.projects.get(project)
        if not logs_dir:
            self._json(404, {"error": f"project '{project}' not found"})
            return
        agents = scan_agents(logs_dir, self.stall_secs)
        agent = next((a for a in agents if a.name == name), None)
        if not agent:
            self._json(404, {"error": f"agent '{name}' not found in project '{project}'"})
            return
        self._json(200, _agent_to_dict(agent))

    def _handle_action(self, project: str, action: str, agent_name: str):
        logs_dir = self.projects.get(project)
        if not logs_dir:
            self._json(404, {"error": f"project '{project}' not found"})
            return
        if not self.scripts_dir:
            self._json(500, {"error": "no scripts_dir configured on server"})
            return

        script_map = {
            "merge": ("merge.sh", [str(logs_dir), "merge", agent_name]),
            "discard": ("merge.sh", [str(logs_dir), "discard", agent_name]),
            "retry": ("retry.sh", [str(logs_dir)]),
            "clean": ("clean.sh", [str(logs_dir)]),
        }
        script_name, args = script_map[action]
        script_path = self.scripts_dir / script_name
        if not script_path.exists():
            self._json(500, {"error": f"script not found: {script_path}"})
            return

        try:
            result = subprocess.run(
                ["bash", str(script_path), *args],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(logs_dir.parent),
            )
            self._json(
                200 if result.returncode == 0 else 500,
                {
                    "action": action,
                    "agent": agent_name,
                    "project": project,
                    "ok": result.returncode == 0,
                    "output": (result.stdout.strip() or result.stderr.strip())[:500],
                },
            )
        except subprocess.TimeoutExpired:
            self._json(504, {"error": f"{action} timed out"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _serve_agent_tail(self, project: str, name: str):
        logs_dir = self.projects.get(project)
        if not logs_dir:
            self._json(404, {"error": f"project '{project}' not found"})
            return
        # Parse ?lines=N from query string
        lines = 50
        if "?" in self.path:
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            try:
                lines = int(qs.get("lines", ["50"])[0])
            except (ValueError, IndexError):
                lines = 50
        log_path = logs_dir / f"{name}.jsonl"
        if not log_path.exists():
            self._json(404, {"error": f"agent '{name}' log not found"})
            return
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
            self._json(200, {"agent": name, "events": events, "count": len(events)})
        except OSError as e:
            self._json(500, {"error": str(e)})

    def _serve_agent_diff(self, project: str, name: str):
        logs_dir = self.projects.get(project)
        if not logs_dir:
            self._json(404, {"error": f"project '{project}' not found"})
            return
        repo = logs_dir.parent
        wt = Path.home() / ".cursor" / "worktrees" / repo.name / name
        if not wt.is_dir():
            self._text(404, f"worktree not found: {wt}")
            return
        stat_only = False
        if "?" in self.path:
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            stat_only = qs.get("stat", ["0"])[0] == "1"
        cmd = ["git", "-C", str(wt), "diff"]
        if stat_only:
            cmd.append("--stat")
        cmd.append("HEAD")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self._text(200, result.stdout or "(no changes)")
        except subprocess.TimeoutExpired:
            self._text(504, "diff timed out")
        except Exception as e:
            self._text(500, str(e))

    # ── Helpers ───────────────────────────────────────────────────────────

    def _json(self, code: int, data: dict):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def _agent_to_dict(a: AgentState) -> dict:
    return {
        "name": a.name,
        "state": a.state,
        "heartbeat_s": a.heartbeat_s,
        "action": a.action,
        "tool_count": a.tool_count,
        "detail": a.detail,
        "session_id": a.session_id,
        "duration_s": a.duration_s,
        "wall_clock_s": a.wall_clock_s,
        "is_error": a.is_error,
        "err_tail": a.err_tail,
        "tokens": {
            "input": a.tokens.input,
            "output": a.tokens.output,
            "cache": a.tokens.cache,
        },
        "recent_events": a.recent_events[-20:],
        "result_json": a.result_json,
    }


def run_server(
    logs_dir: str | None = None,
    host: str = "0.0.0.0",
    port: int = 7400,
    stall_secs: int = 60,
    projects: dict[str, str] | None = None,
    scripts_dir: str | None = None,
):
    if projects:
        LogHandler.projects = {n: Path(p) for n, p in projects.items()}
    elif logs_dir:
        LogHandler.projects = {"default": Path(logs_dir)}
    else:
        print("error: provide --logs-dir or --project args")
        return

    LogHandler.stall_secs = stall_secs
    if scripts_dir:
        LogHandler.scripts_dir = Path(scripts_dir)
    else:
        candidate = Path.home() / ".claude" / "skills" / "orchestrating-cursor-agents"
        if candidate.is_dir():
            LogHandler.scripts_dir = candidate

    server = HTTPServer((host, port), LogHandler)
    proj_list = ", ".join(f"{n}={p}" for n, p in LogHandler.projects.items())
    print(f"agent-tui server on {host}:{port}")
    print(f"  projects: {proj_list}")
    print(f"  scripts:  {LogHandler.scripts_dir or '(none — actions disabled)'}")
    print("  GET  /projects                              — list all projects")
    print("  GET  /projects/<name>/agents                — agents in a project")
    print("  GET  /projects/<name>/agent/<id>            — agent detail")
    print("  POST /projects/<name>/action/merge/<agent>  — merge agent")
    print("  POST /projects/<name>/action/retry/<agent>  — retry agent")
    print("  POST /projects/<name>/action/discard/<agent>— discard agent")
    print("  GET  /projects/<name>/agent/<id>/tail    — last N events (live)")
    print("  GET  /projects/<name>/agent/<id>/diff    — worktree diff")
    # Optional WebSocket server
    ws_port = port + 1
    ws_actual = _start_ws_server(LogHandler.projects, stall_secs, host, ws_port)
    if ws_actual:
        print(f"  WebSocket on {host}:{ws_actual} (push interval: 2s)")
    else:
        print("  WebSocket: disabled (install 'websockets' package to enable)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


def _start_ws_server(
    projects: dict[str, Path],
    stall_secs: int,
    host: str,
    port: int,
    interval: float = 2.0,
):
    """Start a WebSocket server that pushes agent state snapshots."""
    try:
        import asyncio

        import websockets  # type: ignore[import-unresolved]
    except ImportError:
        return None

    connected: set = set()

    async def handler(ws):
        connected.add(ws)
        try:
            # Send initial full snapshot
            snapshot = _build_snapshot(projects, stall_secs)
            await ws.send(json.dumps({"type": "snapshot", **snapshot}))
            # Keep connection alive — pushes happen from the broadcast loop
            async for _msg in ws:
                pass  # clients don't send anything, but this keeps the connection open
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            connected.discard(ws)

    async def broadcast_loop():
        prev_hash = ""
        while True:
            await asyncio.sleep(interval)
            snapshot = _build_snapshot(projects, stall_secs)
            payload = json.dumps({"type": "update", **snapshot})
            cur_hash = payload
            if cur_hash != prev_hash and connected:
                prev_hash = cur_hash
                dead = set()
                for ws in connected:
                    try:
                        await ws.send(payload)
                    except Exception:
                        dead.add(ws)
                connected.difference_update(dead)

    async def main():
        async with websockets.serve(handler, host, port):
            await broadcast_loop()

    def run():
        asyncio.run(main())

    import threading
    t = threading.Thread(target=run, daemon=True, name="ws-server")
    t.start()
    return port


def _build_snapshot(projects: dict[str, Path], stall_secs: int) -> dict:
    """Build a snapshot of all agents across all projects."""
    all_agents = {}
    for proj_name, logs_dir in projects.items():
        agents = scan_agents(logs_dir, stall_secs)
        all_agents[proj_name] = [_agent_to_dict(a) for a in agents]
    return {"projects": all_agents}
