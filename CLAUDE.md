# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

agents-tui is a live TUI dashboard for monitoring cursor-agent fan-outs. It reads JSONL log files produced by `cursor-agent` processes, derives agent state (running, done, error, stalled, died), and displays them in a Textual-based terminal UI. It also runs an HTTP server mode for remote monitoring from a laptop to a VPS.

## Commands

```bash
# Setup
uv venv && uv pip install -e ".[dev]"
source .venv/bin/activate

# Dev checks
ruff check agent_tui/          # lint
ruff format agent_tui/         # format
pyright agent_tui/             # type check
pytest                          # tests (testpaths = tests/)

# Run the TUI
agents-tui /path/to/logs        # single dir, no config
agents-tui watch                 # uses ~/.config/agents-tui/config.toml
agents-tui serve --project name=/path/to/logs --port 7400
```

## Architecture

Three modes share one data pipeline: **state parsing → source abstraction → display**.

**Data pipeline:**
- `state.py` — Parses `{name}.jsonl` + `{name}.pid` + `{name}.err` files from a logs directory into `AgentState` dataclasses. Derives state from a combination of JSONL events (result event = DONE/ERROR), PID liveness checks (`os.kill(pid, 0)`), and heartbeat staleness. Token costs are calculated from the `result` event's `usage` field.
- `sources.py` — `LogSource` protocol with two implementations: `LocalSource` (calls `scan_agents` from state.py, runs shell scripts for actions) and `HttpSource` (fetches agent data via HTTP from a remote `agents-tui serve` instance). Actions (merge/retry/discard) delegate to bash scripts from the `orchestrating-cursor-agents` skill.
- `config.py` — TOML config at `~/.config/agents-tui/config.toml`. `AppConfig` holds machines (local/http), token rates, display settings. `ensure_config()` writes the example config on first run.

**Display:**
- `app.py` — Textual `App` subclass. Three-pane layout: DataTable (agent list), RichLog detail panel, RichLog wave sidebar. Refreshes on a timer via `set_interval`. Actions run in a background thread via `@work(thread=True)`. Confirmation dialogs use `ModalScreen`.
- `app.tcss` — Tokyo Night color theme. CSS variables at top, layout uses `dock`, `1fr`, and `.collapsed` class toggling.
- `__main__.py` — argparse CLI with `watch`/`serve`/`init` subcommands. No subcommand = `watch`.

**Agent state machine:** An agent is `run` if its PID is alive and heartbeat is fresh, `STALL` if PID alive but heartbeat exceeds `stall_secs`, `DIED` if PID is dead with no result event, `DONE`/`ERROR` if a `result` event exists in the JSONL. `NO_LOG` if no JSONL file found.

**Script integration:** Actions call shell scripts (`merge.sh`, `retry.sh`) from `~/.claude/skills/orchestrating-cursor-agents/`. The server and local sources both resolve this path via auto-detection or explicit `--scripts-dir`.

## Conventions

- Python 3.11+ (uses `tomllib`, `X | Y` union syntax)
- Ruff for linting and formatting (line-length 120, double quotes)
- Pyright in standard mode for type checking
- No runtime dependencies beyond `textual`; `asyncssh` is optional (`[remote]` extra, SSH source not yet implemented)
- All HTTP done with `urllib.request` (no requests/httpx dependency)
- Server uses `http.server.HTTPServer` (stdlib), not aiohttp/flask
- Markup strings in app.py use Textual/Rich `[color]...[/]` syntax with hex colors from the Tokyo Night palette
