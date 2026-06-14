from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from itsdangerous import URLSafeSerializer

from vchat.settings import config as app_config

from .config import IdleWsProfileConfig
from .profiles import run_idle_ws_profile
from .reporters import render_report_text, write_json_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m extra.loadtest.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    idle = subparsers.add_parser(
        "idle-ws",
        help="Open and hold a large number of idle chat websocket connections.",
    )
    idle.add_argument("--base-url", default="http://127.0.0.1:9080")
    idle.add_argument("--payload")
    idle.add_argument("--user-id")
    idle.add_argument("--chat-id")
    idle.add_argument("--secret-key")
    idle.add_argument("--target-concurrency", type=int, default=1000)
    idle.add_argument("--ramp-per-second", type=float, default=100.0)
    idle.add_argument("--hold-seconds", type=float, default=600.0)
    idle.add_argument("--connect-timeout-seconds", type=float, default=10.0)
    idle.add_argument("--heartbeat-seconds", type=float, default=30.0)
    idle.add_argument("--receive-poll-seconds", type=float, default=1.0)
    idle.add_argument("--report-json-path")
    idle.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification for wss:// targets.",
    )
    return parser


def _resolve_payload(args: argparse.Namespace) -> str:
    if args.payload:
        return args.payload
    if not args.user_id or not args.chat_id:
        raise SystemExit(
            "either --payload or both --user-id and --chat-id are required"
        )
    secret_key = args.secret_key or app_config["secret_key"]
    serializer = URLSafeSerializer(secret_key)
    return serializer.dumps([args.user_id, args.chat_id], salt="vchat")


async def _run_idle_ws(args: argparse.Namespace) -> int:
    payload = _resolve_payload(args)
    cfg = IdleWsProfileConfig(
        base_url=args.base_url,
        target_concurrency=args.target_concurrency,
        ramp_per_second=args.ramp_per_second,
        hold_seconds=args.hold_seconds,
        connect_timeout_seconds=args.connect_timeout_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        payload=payload,
        report_json_path=(
            Path(args.report_json_path) if args.report_json_path else None
        ),
        verify_ssl=not args.insecure,
        receive_poll_seconds=args.receive_poll_seconds,
    )
    report = await run_idle_ws_profile(cfg)
    print(render_report_text(report))
    if cfg.report_json_path is not None:
        write_json_report(cfg.report_json_path, report)
        print(f"json_report: {cfg.report_json_path}")
    return 0 if report.total_connected == report.target_concurrency else 1


async def _async_main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "idle-ws":
        return await _run_idle_ws(args)
    raise SystemExit(f"unknown command: {args.command}")


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
