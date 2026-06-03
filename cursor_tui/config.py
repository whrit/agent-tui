from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "cursor-tui"
CONFIG_FILE = CONFIG_DIR / "config.toml"

EXAMPLE_CONFIG = """\
# cursor-tui configuration
# Monitors cursor-agent fan-outs across multiple projects and machines.

[rates]
# Per-million-token rates for cost display. Set to 0 to show raw tokens instead.
input = 1.25
output = 10.0
cache = 0.125

# ── Local projects (this machine) ────────────────────────────────────────

[machines.backend]
type = "local"
logs_dir = "./logs"
repo = "."                    # git repo root (for cwd when running scripts)

# [machines.worker]
# type = "local"
# logs_dir = "/Users/you/Projects/cf-worker/logs"
# repo = "/Users/you/Projects/cf-worker"

# ── Remote projects (VPS via HTTP server) ────────────────────────────────
# Start the server on the VPS:
#   cursor-tui serve --project backend=/home/deploy/backend/logs \\
#                     --project worker=/home/deploy/worker/logs

# [machines.vps-backend]
# type = "http"
# url = "http://your-vps:7400"
# project = "backend"         # matches --project name on the server

# [machines.vps-worker]
# type = "http"
# url = "http://your-vps:7400"
# project = "worker"          # same server, different project

[display]
refresh = 2.0          # seconds between refreshes
stall_secs = 60        # heartbeat age (s) that counts as a stall

[scripts]
# Path to the orchestrating-cursor-agents skill scripts.
# Auto-detected from ~/.claude/skills/ if omitted.
# dir = "~/.claude/skills/orchestrating-cursor-agents"
"""


@dataclass
class MachineConfig:
    name: str
    type: str = "local"  # "local", "ssh", "http"
    logs_dir: str = "./logs"
    repo: str = ""  # git repo root (for cwd when running scripts)
    project: str = ""  # project name on HTTP server (for multi-project routing)
    host: str = ""
    user: str = ""
    port: int = 22
    key: str = ""
    url: str = ""


@dataclass
class RatesConfig:
    input: float = 0.0
    output: float = 0.0
    cache: float = 0.0


@dataclass
class DisplayConfig:
    refresh: float = 2.0
    stall_secs: int = 60


@dataclass
class AppConfig:
    machines: list[MachineConfig] = field(default_factory=list)
    rates: RatesConfig = field(default_factory=RatesConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    scripts_dir: str | None = None

    @staticmethod
    def load(path: Path | None = None) -> AppConfig:
        path = path or CONFIG_FILE
        if not path.exists():
            return AppConfig._defaults()
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except Exception:
            return AppConfig._defaults()
        return AppConfig._from_dict(raw)

    @staticmethod
    def _defaults() -> AppConfig:
        return AppConfig(
            machines=[MachineConfig(name="local", type="local", logs_dir="./logs")],
        )

    @staticmethod
    def _from_dict(raw: dict) -> AppConfig:
        cfg = AppConfig()

        if "rates" in raw:
            r = raw["rates"]
            cfg.rates = RatesConfig(
                input=float(r.get("input", 0)),
                output=float(r.get("output", 0)),
                cache=float(r.get("cache", 0)),
            )

        if "display" in raw:
            d = raw["display"]
            cfg.display = DisplayConfig(
                refresh=float(d.get("refresh", 2.0)),
                stall_secs=int(d.get("stall_secs", 60)),
            )

        if "scripts" in raw and "dir" in raw["scripts"]:
            cfg.scripts_dir = str(Path(raw["scripts"]["dir"]).expanduser())

        machines_raw = raw.get("machines", {})
        for name, m in machines_raw.items():
            cfg.machines.append(
                MachineConfig(
                    name=name,
                    type=m.get("type", "local"),
                    logs_dir=m.get("logs_dir", "./logs"),
                    repo=m.get("repo", ""),
                    project=m.get("project", name),
                    host=m.get("host", ""),
                    user=m.get("user", ""),
                    port=int(m.get("port", 22)),
                    key=m.get("key", ""),
                    url=m.get("url", ""),
                )
            )

        if not cfg.machines:
            cfg.machines = [MachineConfig(name="local")]

        return cfg


def ensure_config() -> Path:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(EXAMPLE_CONFIG)
    return CONFIG_FILE
