from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from cbcl_platform.models import RuntimeMode
from cbcl_platform.runtime import build_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cbcl-platform",
        description="CB/CL Polymarket trading stack.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("start-live", "run-replay", "run-backtest"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        if command == "start-live":
            subparser.add_argument(
                "--duration-seconds",
                type=float,
                default=0.0,
                help="If set, run the live node for a bounded duration before exiting.",
            )
            subparser.add_argument(
                "--with-dashboard",
                action="store_true",
                help="Serve the dashboard from the same Nautilus process.",
            )

    paper_parser = subparsers.add_parser("start-paper")
    paper_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    paper_parser.add_argument(
        "--duration-seconds",
        type=float,
        default=0.0,
        help="If set, run the paper bot for a bounded duration before exiting.",
    )
    paper_parser.add_argument(
        "--with-dashboard",
        action="store_true",
        help="Serve the dashboard from the same Nautilus process.",
    )

    dashboard_parser = subparsers.add_parser("start-dashboard")
    dashboard_parser.add_argument(
        "--json", action="store_true", help="Print the dashboard snapshot and exit."
    )
    dashboard_parser.add_argument(
        "--duration-seconds",
        type=float,
        default=0.0,
        help="If set, serve the dashboard for a bounded duration before exiting.",
    )
    return parser


def _print_runtime_summary(mode: RuntimeMode, *, as_json: bool) -> int:
    runtime = build_runtime(mode)
    summary = runtime.summary()
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"{mode.value} runtime ready")
    print(f"environment={summary['environment']} markets={','.join(summary['markets'])}")
    print(
        f"strategy={summary['strategy_name']} threshold={summary['threshold']:.4f} "
        f"window_5m={summary['max_minutes_to_close_5m']:.1f}m "
        f"window_15m={summary['max_minutes_to_close_15m']:.1f}m",
    )
    print(
        f"hard_cap={summary['hard_cap']:.2f} max_drift={summary['max_price_drift']:.2f} "
        f"kelly={summary['kelly_enabled']} realistic_paper={summary['realistic_paper_enabled']}",
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "start-live":
        if args.json:
            return _print_runtime_summary(RuntimeMode.LIVE, as_json=True)
        from cbcl_platform.nautilus.node import run_trading_node

        print(
            "live runtime starting "
            f"(state={build_runtime(RuntimeMode.LIVE).config.runtime_state_path} "
            f"duration={args.duration_seconds or 'infinite'}s "
            f"dashboard={'embedded' if args.with_dashboard else 'separate'})"
        )
        return asyncio.run(
            run_trading_node(
                mode=RuntimeMode.LIVE,
                duration_seconds=float(args.duration_seconds),
                with_dashboard=bool(args.with_dashboard),
            ),
        )
    if args.command == "start-paper":
        if args.json:
            return _print_runtime_summary(RuntimeMode.PAPER, as_json=True)
        from cbcl_platform.nautilus.node import run_trading_node

        runtime = build_runtime(RuntimeMode.PAPER)
        print(
            "paper runtime starting "
            f"(state={runtime.config.runtime_state_path} "
            f"duration={args.duration_seconds or 'infinite'}s "
            f"dashboard={'embedded' if args.with_dashboard else 'separate'})"
        )
        return asyncio.run(
            run_trading_node(
                mode=RuntimeMode.PAPER,
                duration_seconds=float(args.duration_seconds),
                with_dashboard=bool(args.with_dashboard),
            ),
        )
    if args.command == "run-replay":
        from cbcl_platform.replay import replay_summary

        runtime = build_runtime(RuntimeMode.REPLAY)
        payload = replay_summary(runtime)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(payload["summary"])
        print(f"path={payload['path']} events={payload['event_count']}")
        return 0
    if args.command == "run-backtest":
        return _print_runtime_summary(RuntimeMode.BACKTEST, as_json=bool(args.json))
    if args.command == "start-dashboard":
        from cbcl_platform import dashboard as serve_dashboard
        from cbcl_platform.state_store import RuntimeStateStore

        runtime = build_runtime(RuntimeMode.DASHBOARD)
        store = RuntimeStateStore(runtime.config.runtime_state_path)
        if args.json:
            payload = serve_dashboard.bootstrap_payload(runtime=runtime, active_state=store.read())
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(
            "dashboard serving on "
            f"http://{runtime.config.dashboard.host}:{runtime.config.dashboard.port} "
            f"(duration={args.duration_seconds or 'infinite'}s)"
        )
        serve_dashboard.serve_dashboard(
            runtime=runtime,
            host=runtime.config.dashboard.host,
            port=runtime.config.dashboard.port,
            duration_seconds=float(args.duration_seconds),
        )
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


def console_entrypoint() -> int:
    return main()


def start_live_cli() -> int:
    return main(["start-live", *sys.argv[1:]])


def start_paper_cli() -> int:
    return main(["start-paper", *sys.argv[1:]])


def start_dashboard_cli() -> int:
    return main(["start-dashboard", *sys.argv[1:]])


def run_replay_cli() -> int:
    return main(["run-replay", *sys.argv[1:]])


def run_backtest_cli() -> int:
    return main(["run-backtest", *sys.argv[1:]])
