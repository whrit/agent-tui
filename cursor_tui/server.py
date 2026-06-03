from __future__ import annotations

import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cursor_tui.state import AgentState, scan_agents

# Route patterns
_RE_PROJECT_AGENTS = re.compile(r"^/projects/([^/]+)/agents$")
_RE_PROJECT_AGENT = re.compile(r"^/projects/([^/]+)/agent/([^/]+)$")
_RE_PROJECT_ACTION = re.compile(r"^/projects/([^/]+)/action/(merge|retry|discard)/([^/]+)$")
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
        if m := _RE_PROJECT_AGENTS.match(self.path):
            self._serve_agents(m.group(1))
            return
        if m := _RE_PROJECT_AGENT.match(self.path):
            self._serve_agent_detail(m.group(1), m.group(2))
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
            return

        self._json(
            404,
            {
                "error": "not found",
                "endpoints": [
                    "GET  /projects",
                    "GET  /projects/<name>/agents",
                    "GET  /projects/<name>/agent/<id>",
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
        agents = scan_agents(logs_dir, self.stall_secs)
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

    # ── Helpers ───────────────────────────────────────────────────────────

    def _json(self, code: int, data: dict):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
    print(f"cursor-tui server on {host}:{port}")
    print(f"  projects: {proj_list}")
    print(f"  scripts:  {LogHandler.scripts_dir or '(none — actions disabled)'}")
    print("  GET  /projects                              — list all projects")
    print("  GET  /projects/<name>/agents                — agents in a project")
    print("  GET  /projects/<name>/agent/<id>            — agent detail")
    print("  POST /projects/<name>/action/merge/<agent>  — merge agent")
    print("  POST /projects/<name>/action/retry/<agent>  — retry agent")
    print("  POST /projects/<name>/action/discard/<agent>— discard agent")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()
