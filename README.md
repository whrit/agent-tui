# cursor-tui

Live TUI dashboard for monitoring and managing cursor-agent fan-outs across
multiple projects and machines.

Watch agents run, see their progress in real time, and merge/retry/discard from
one pane — whether the agents are on your laptop or a remote VPS.

## Install

```bash
cd cursor-tui
uv venv && uv pip install -e ".[dev]"
source .venv/bin/activate
```

## Quick start

```bash
# Watch agents in a local logs directory
cursor-tui /path/to/logs

# Or use the config file for multi-project/multi-machine setups
cursor-tui init          # creates ~/.config/cursor-tui/config.toml
cursor-tui watch         # launches with config
```

## Three modes

### `cursor-tui watch` (default)

The TUI dashboard. Shows all agents from all configured sources with live
auto-refresh.

```bash
cursor-tui /path/to/logs              # single dir, no config needed
cursor-tui watch                       # uses ~/.config/cursor-tui/config.toml
cursor-tui watch --config ./my.toml    # custom config
cursor-tui watch --refresh 5           # refresh every 5s (default: 2)
```

### `cursor-tui serve`

HTTP log server. Deploy on a VPS so the TUI on your laptop can monitor remote
agents and trigger actions.

```bash
# Single project
cursor-tui serve /path/to/logs --port 7400

# Multiple projects (one server, many dirs)
cursor-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --project worker=/home/deploy/cf-worker/logs \
  --port 7400

# With action support (merge/retry/discard from remote TUI)
cursor-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --scripts-dir ~/.claude/skills/orchestrating-cursor-agents
```

**Server endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/projects` | List all projects with agent counts |
| GET | `/projects/<name>/agents` | All agents in a project |
| GET | `/projects/<name>/agent/<id>` | Single agent detail + events |
| POST | `/projects/<name>/action/merge/<agent>` | Merge an agent's worktree |
| POST | `/projects/<name>/action/retry/<agent>` | Retry a failed agent |
| POST | `/projects/<name>/action/discard/<agent>` | Discard an agent's worktree |
| GET | `/health` | Liveness check |
| GET | `/agents` | Legacy: first project's agents |

### `cursor-tui init`

Creates `~/.config/cursor-tui/config.toml` with an annotated example config.

## Configuration

```toml
# ~/.config/cursor-tui/config.toml

[rates]
input = 1.25      # $ per million input tokens
output = 10.0     # $ per million output tokens
cache = 0.125     # $ per million cache-read tokens

# ── Local projects ──────────────────────────────────────
[machines.backend]
type = "local"
logs_dir = "/Users/you/Projects/backend-v1/logs"
repo = "/Users/you/Projects/backend-v1"     # git root for script cwd

[machines.worker]
type = "local"
logs_dir = "/Users/you/Projects/cf-worker/logs"
repo = "/Users/you/Projects/cf-worker"

# ── Remote projects (via HTTP server on VPS) ────────────
[machines.vps-backend]
type = "http"
url = "http://your-vps:7400"
project = "backend"      # matches --project name on the server

[machines.vps-worker]
type = "http"
url = "http://your-vps:7400"
project = "worker"

[display]
refresh = 2.0        # seconds between refreshes
stall_secs = 60      # heartbeat age that counts as a stall

[scripts]
# dir = "~/.claude/skills/orchestrating-cursor-agents"  # auto-detected
```

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `k` | Navigate agents (vim-style) |
| `Enter` | Toggle detail panel |
| `w` | Toggle wave/agent sidebar |
| `Tab` | Cycle focus between panels |
| `r` | Retry selected agent |
| `m` | Merge selected agent (with confirmation) |
| `d` | Discard selected agent (with confirmation) |
| `p` | Force refresh |
| `q` | Quit |

Actions work on both local and remote agents. Remote actions are sent as HTTP
POSTs to the server.

## VPS deployment

### Prerequisites

On your VPS, you need:
1. Python 3.11+
2. The `cursor-tui` package installed
3. The `orchestrating-cursor-agents` skill scripts (at `~/.claude/skills/orchestrating-cursor-agents/`)
4. `jq` installed (`apt install jq`)

### Setup

```bash
# On VPS
git clone <this-repo> ~/cursor-tui
cd ~/cursor-tui && uv venv && uv pip install -e .

# Start the server (use systemd/screen/tmux to keep it running)
cursor-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --project worker=/home/deploy/cf-worker/logs \
  --port 7400

# On your laptop — add to config
cat >> ~/.config/cursor-tui/config.toml << 'EOF'

[machines.vps-backend]
type = "http"
url = "http://your-vps:7400"
project = "backend"

[machines.vps-worker]
type = "http"
url = "http://your-vps:7400"
project = "worker"
EOF

# Launch
cursor-tui watch
```

### Systemd service (optional)

```ini
# /etc/systemd/system/cursor-tui.service
[Unit]
Description=cursor-tui log server
After=network.target

[Service]
User=deploy
ExecStart=/home/deploy/cursor-tui/.venv/bin/cursor-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --project worker=/home/deploy/cf-worker/logs \
  --port 7400
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cursor-tui
```

## Architecture

```
┌─ Laptop ──────────────────────────┐     ┌─ VPS ────────────────────────────┐
│                                    │     │                                   │
│  Claude Code → cursor-agents       │     │  Claude Code → cursor-agents      │
│    ↓ writes logs/                  │     │    ↓ writes logs/                 │
│                                    │     │                                   │
│  cursor-tui (TUI)                  │     │  cursor-tui serve                 │
│    LocalSource → reads local logs  │     │    /projects/backend/agents       │
│    HttpSource  → GET agents   ────────→  │    /projects/worker/agents        │
│    HttpSource  → POST action  ────────→  │    /projects/*/action/* → scripts │
│                                    │     │                                   │
└────────────────────────────────────┘     └───────────────────────────────────┘
```

### Modules

| File | Role |
|------|------|
| `state.py` | Agent state parser — reads JSONL logs, derives state/heartbeat/action/tokens |
| `sources.py` | `LogSource` protocol — `LocalSource` (filesystem) and `HttpSource` (server). Both support `scan()` and `run_action()` |
| `server.py` | Multi-project HTTP server with action endpoints |
| `config.py` | TOML config loader — machines, rates, display settings |
| `app.py` | Textual TUI — table, detail panel, wave sidebar, confirmations |
| `app.tcss` | Textual CSS — Tokyo Night theme, three-pane layout |
| `__main__.py` | CLI entry — `watch`, `serve`, `init` subcommands |

## Related: orchestrating-cursor-agents skill

The TUI is designed to work with the `orchestrating-cursor-agents` skill at
`~/.claude/skills/orchestrating-cursor-agents/`. The skill provides:

- **fan-out-stream.sh** — launch parallel agents with streamed logs
- **wait-all.sh** — block until all agents finish, return summary JSON
- **status.sh** — CLI dashboard (the TUI replaces this for interactive use)
- **merge.sh** — commit worktrees + merge/discard
- **retry.sh** — auto-resume failed agents
- **wave-orchestration.md** — multi-wave pattern with review gates

The TUI calls these scripts for local actions (merge/retry/discard), and the
server calls them for remote actions.

## Development

```bash
source .venv/bin/activate
ruff check cursor_tui/     # lint
ruff format cursor_tui/    # format
pyright cursor_tui/        # type check
pytest                     # tests
```
