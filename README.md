# agent-tui

Live TUI dashboard for monitoring and managing cursor-agent fan-outs across
multiple projects and machines.

Watch agents run, see their progress in real time, and merge/retry/discard from
one pane — whether the agents are on your laptop or a remote VPS.

## Install

```bash
# Development
cd agent-tui
uv venv && uv pip install -e ".[dev]"

# Global CLI (run from anywhere)
uv tool install ~/Projects/agent-tui

# Optional: WebSocket streaming for remote sources
uv pip install "agent-tui[ws]"
```

## Quick start

```bash
# Watch agents in a local logs directory
agent-tui /path/to/logs

# Or use the config file for multi-project/multi-machine setups
agent-tui init          # creates ~/.config/agent-tui/config.toml
agent-tui watch         # launches with config

# Browse past archived runs
agent-tui history
```

## Four modes

### `agent-tui watch` (default)

The TUI dashboard. Shows all agents from all configured sources with live
auto-refresh.

```bash
agent-tui /path/to/logs              # single dir, no config needed
agent-tui watch                       # uses ~/.config/agent-tui/config.toml
agent-tui watch --config ./my.toml    # custom config
agent-tui watch --refresh 5           # refresh every 5s (default: 2)
```

### `agent-tui serve`

HTTP + WebSocket log server. Deploy on a VPS so the TUI on your laptop can
monitor remote agents and trigger actions.

```bash
# Single project
agent-tui serve /path/to/logs --port 7400

# Multiple projects (one server, many dirs)
agent-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --project worker=/home/deploy/cf-worker/logs \
  --port 7400

# With action support (merge/retry/discard/clean from remote TUI)
agent-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --scripts-dir ~/.claude/skills/orchestrating-cursor-agents
```

**Server endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/projects` | List all projects with agent counts |
| GET | `/projects/<name>/agents` | All agents in a project |
| GET | `/projects/<name>/agent/<id>` | Single agent detail + events |
| GET | `/projects/<name>/agent/<id>/tail?lines=N` | Last N events (live tailing) |
| GET | `/projects/<name>/agent/<id>/diff?stat=1` | Worktree diff |
| POST | `/projects/<name>/action/merge/<agent>` | Merge an agent's worktree |
| POST | `/projects/<name>/action/retry/<agent>` | Retry a failed agent |
| POST | `/projects/<name>/action/discard/<agent>` | Discard an agent's worktree |
| POST | `/projects/<name>/action/clean/<agent>` | Clean terminal worktrees |
| GET | `/health` | Liveness check |
| WS | `ws://host:7401` | Push-based state updates (optional, requires `websockets`) |

### `agent-tui history`

Browse past archived fan-out runs. Reads from `~/.cursor-agent-history/`
(populated by `archive.sh`).

```bash
agent-tui history                                 # browse all archived runs
agent-tui history --history-dir /custom/path      # custom archive location
```

Select a run and press `Enter` to open its logs in the main TUI (read-only).

### `agent-tui init`

Creates `~/.config/agent-tui/config.toml` with an annotated example config.

## Features

### Dashboard

The main table shows all agents with state indicators, heartbeat, current
action, tool count, cost/tokens, and a detail column.

- **Tokyo Night** color theme
- **Multi-machine** support — monitor local + remote agents in one view
- **Auto-refresh** every 2s (configurable)

### Detail panel (`Enter`)

Three viewing modes for the selected agent:

- **Events** (default) — recent stream-json events with timestamps, tool calls,
  and assistant messages. Shows RESULT.json verdict when available.
- **Live** (`l`) — tails the JSONL log file on every refresh, showing the most
  recent 40 events. `[LIVE]` indicator in the header.
- **Diff** (`d`) — syntax-highlighted git diff of the agent's worktree vs HEAD.
  Green adds, red removes, cyan hunk headers.

### Progress bar

Running agents show an elapsed-time progress bar in both the detail panel
(20-char `█░`) and the table (8-char `▓░` inline). Based on wall-clock time
against a 5-minute baseline — caps at 100% but the agent continues running.

### Wave sidebar (`w`)

Auto-detects wave patterns from agent names and groups them:
- `w1-schema`, `w2-api` → grouped under `w1`, `w2`
- `review-correctness`, `fix-src-api` → grouped under `review`, `fix`
- Shows per-wave progress (`w1 ● 3/3`, `w2 ◐ 1/2`)
- Falls back to machine grouping when no wave patterns are detected

### Filtering and sorting

- `/` opens a filter bar — type a substring to filter by name
- `:error`, `:done`, `:run`, `:stall` filter by state
- `Escape` clears the filter
- `s` cycles sort order: name → state → cost → duration → heartbeat
- Header shows `filter: N/M` when active; token totals are unaffected

### Command palette (`?`)

Searchable modal listing all keybindings with descriptions. Type to filter
commands by key or description.

### Actions

All actions work on both local and remote agents:

| Key | Action |
|-----|--------|
| `r` | Retry selected agent |
| `m` | Merge selected agent (with confirmation) |
| `x` | Discard selected agent (with confirmation) |
| `c` | Clean all terminal worktrees (with confirmation) |

### WebSocket streaming (optional)

When `websockets` is installed, the server pushes state updates over WebSocket
on port+1 (e.g., 7401). The TUI auto-detects and uses WS when available,
falling back to HTTP polling otherwise. Actions still use HTTP POST.

```bash
uv pip install websockets    # enable WS on both server and client
```

## Keybindings

| Key | Action |
|-----|--------|
| `j` / `k` | Navigate agents (vim-style) |
| `Enter` | Toggle detail panel |
| `l` | Toggle live log tailing |
| `d` | Toggle diff preview |
| `w` | Toggle wave sidebar |
| `/` | Open filter bar |
| `s` | Cycle sort order |
| `Escape` | Clear filter / close palette |
| `r` | Retry selected agent |
| `m` | Merge selected agent (with confirmation) |
| `x` | Discard selected agent (with confirmation) |
| `c` | Clean all terminal worktrees (with confirmation) |
| `p` | Force refresh |
| `?` | Command palette (searchable help) |
| `Tab` | Cycle focus between panels |
| `q` | Quit |

## Configuration

```toml
# ~/.config/agent-tui/config.toml

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

# ── Remote projects (via HTTP/WS server on VPS) ────────
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

## VPS deployment

### Prerequisites

On your VPS, you need:
1. Python 3.11+
2. The `agent-tui` package installed
3. The `orchestrating-cursor-agents` skill scripts
4. `jq` installed (`apt install jq`)
5. Optional: `websockets` package for push-based updates

### Setup

```bash
# On VPS
git clone <this-repo> ~/agent-tui
cd ~/agent-tui && uv venv && uv pip install -e ".[ws]"

# Start the server
agent-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --project worker=/home/deploy/cf-worker/logs \
  --port 7400

# On your laptop — add to config
cat >> ~/.config/agent-tui/config.toml << 'EOF'

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
agent-tui watch
```

### Systemd service (optional)

```ini
# /etc/systemd/system/agent-tui.service
[Unit]
Description=agent-tui log server
After=network.target

[Service]
User=deploy
ExecStart=/home/deploy/agent-tui/.venv/bin/agent-tui serve \
  --project backend=/home/deploy/backend-v1/logs \
  --project worker=/home/deploy/cf-worker/logs \
  --port 7400
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now agent-tui
```

## Architecture

```
┌─ Laptop ──────────────────────────┐     ┌─ VPS ────────────────────────────┐
│                                    │     │                                   │
│  Claude Code → cursor-agents       │     │  Claude Code → cursor-agents      │
│    ↓ writes logs/                  │     │    ↓ writes logs/                 │
│                                    │     │                                   │
│  agent-tui (TUI)                   │     │  agent-tui serve                  │
│    LocalSource → reads local logs  │     │    HTTP :7400 → REST API          │
│    WsSource   → receives pushes ──────→  │    WS   :7401 → state stream      │
│    HttpSource → GET/POST fallback ────→  │    /projects/*/action/* → scripts  │
│                                    │     │                                   │
└────────────────────────────────────┘     └───────────────────────────────────┘
```

### Modules

| File | Role |
|------|------|
| `state.py` | Agent state parser — reads JSONL logs, derives state/heartbeat/action/tokens/wave |
| `sources.py` | `LogSource` protocol — `LocalSource` (filesystem), `HttpSource` (polling), `WsSource` (push). All support `scan()`, `run_action()`, `tail_log()`, `get_diff()` |
| `server.py` | Multi-project HTTP server + optional WebSocket push server |
| `config.py` | TOML config loader — machines, rates, display settings |
| `app.py` | Textual TUI — table, detail panel (events/live/diff), wave sidebar, filter bar, command palette, confirmations |
| `app.tcss` | Textual CSS — Tokyo Night theme, layout |
| `history.py` | Archived runs browser — DataTable of past fan-outs with detail view |
| `__main__.py` | CLI entry — `watch`, `serve`, `init`, `history` subcommands |

## Related: orchestrating-cursor-agents skill

The TUI is designed to work with the `orchestrating-cursor-agents` skill at
`~/.claude/skills/orchestrating-cursor-agents/`. The skill provides:

**Pre-flight:** gen-brief.sh, validate-plan.sh, predict-conflicts.sh
**Launch:** fan-out-stream.sh, fan-out-dag.sh
**Monitor:** wait-all.sh, status.sh, watch-stalls.sh
**Close:** merge.sh, retry.sh, clean.sh, archive.sh, history.sh
**Review:** final-review.sh, reviewers/*.md (9 dimension profiles)

The TUI calls these scripts for local actions (merge/retry/discard/clean), and
the server calls them for remote actions.

## Development

```bash
uv venv && uv pip install -e ".[dev,ws]"
ruff check agent_tui/     # lint
ruff format agent_tui/    # format
pyright agent_tui/        # type check
pytest                     # tests
```
