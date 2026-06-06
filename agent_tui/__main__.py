import argparse
from pathlib import Path


def _detect_scripts_dir() -> str | None:
    candidate = Path.home() / ".claude" / "skills" / "orchestrating-cursor-agents"
    return str(candidate) if candidate.is_dir() else None


def cmd_tui(args):
    from agent_tui.app import CursorTUI
    from agent_tui.config import AppConfig, ensure_config

    if args.config:
        cfg = AppConfig.load(Path(args.config))
    else:
        ensure_config()
        cfg = AppConfig.load()

    if args.logs_dir != "./logs":
        from agent_tui.config import MachineConfig

        cfg.machines = [MachineConfig(name="local", type="local", logs_dir=args.logs_dir)]

    scripts = args.scripts_dir or cfg.scripts_dir or _detect_scripts_dir()

    app = CursorTUI(
        config=cfg,
        scripts_dir=scripts,
        refresh_interval=args.refresh or cfg.display.refresh,
    )
    app.run()


def cmd_serve(args):
    from agent_tui.server import run_server

    projects = None
    if args.project:
        projects = {}
        for p in args.project:
            if "=" not in p:
                print(f"error: --project must be NAME=PATH, got '{p}'")
                return
            name, path = p.split("=", 1)
            projects[name] = path

    run_server(
        logs_dir=args.logs_dir,
        host=args.host,
        port=args.port,
        stall_secs=args.stall_secs,
        projects=projects,
        scripts_dir=args.scripts_dir,
    )


def cmd_init(args):
    from agent_tui.config import ensure_config

    path = ensure_config()
    print(f"Config written to {path}")
    print("Edit it to add remote machines, token rates, etc.")


def main():
    parser = argparse.ArgumentParser(description="Live TUI dashboard for cursor-agent orchestration")
    sub = parser.add_subparsers(dest="command")

    # Default: TUI mode (also works without subcommand)
    tui_p = sub.add_parser("watch", help="Launch the TUI dashboard (default)")
    tui_p.add_argument("logs_dir", nargs="?", default="./logs")
    tui_p.add_argument("--scripts-dir", default=None)
    tui_p.add_argument("--refresh", type=float, default=None)
    tui_p.add_argument("--config", default=None, help="Path to config.toml")

    # Server mode
    srv_p = sub.add_parser("serve", help="Start HTTP log server (deploy on VPS)")
    srv_p.add_argument("logs_dir", nargs="?", default=None, help="Single project logs dir (legacy)")
    srv_p.add_argument(
        "--project",
        action="append",
        metavar="NAME=PATH",
        help="Add a project (repeatable): --project backend=/home/deploy/backend/logs",
    )
    srv_p.add_argument("--host", default="0.0.0.0")
    srv_p.add_argument("--port", type=int, default=7400)
    srv_p.add_argument("--stall-secs", type=int, default=60)
    srv_p.add_argument("--scripts-dir", default=None)

    # Init config
    sub.add_parser("init", help="Create default config at ~/.config/agent-tui/config.toml")

    args = parser.parse_args()

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "watch":
        cmd_tui(args)
    else:
        # No subcommand: treat positional arg as logs_dir, launch TUI
        parser2 = argparse.ArgumentParser()
        parser2.add_argument("logs_dir", nargs="?", default="./logs")
        parser2.add_argument("--scripts-dir", default=None)
        parser2.add_argument("--refresh", type=float, default=None)
        parser2.add_argument("--config", default=None)
        args2 = parser2.parse_args()
        args2.command = "watch"
        cmd_tui(args2)


if __name__ == "__main__":
    main()
