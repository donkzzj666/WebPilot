import argparse
import asyncio
import json
import os

from .config import Settings, disable_external_tracing


def port_number(value: str) -> int:
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def main() -> None:
    disable_external_tracing()
    parser = argparse.ArgumentParser(description="WebAgent startup and business migrations")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("api", help="Run the loopback API")
    api.add_argument("--port", type=port_number, default=os.getenv("WEBAGENT_API_PORT", "8000"))
    worker = commands.add_parser("worker", help="Run the independent idle Worker")
    worker.add_argument("--once", action="store_true", help="Initialize, report readiness, and exit")
    commands.add_parser("doctor", help="Check linked SQLite and runtime capabilities")
    migration = commands.add_parser("migrate", help="Apply numbered business database migrations")
    migration.add_argument("--target", type=int, help="Optional known target version; no downgrades")
    args = parser.parse_args()
    diagnostic_logger = None
    if args.command in ('api', 'worker'):
        from .observability.standard import install_safe_standard_logging
        diagnostic_logger = install_safe_standard_logging(Settings.from_env().data_dir, args.command)
    try:
        execute(args)
    except Exception as error:
        if diagnostic_logger is None:
            raise
        from .observability.logging import safe_error_class
        diagnostic_logger.emit('service_failed', service=args.command, error_class=safe_error_class(error))
        print(json.dumps({'event': 'service_failed', 'service': args.command,
                          'error_class': safe_error_class(error)}), flush=True)
        raise SystemExit(1) from None


def execute(args):
    if args.command == "api":
        import uvicorn
        os.environ["WEBAGENT_API_PORT"] = str(args.port)
        uvicorn.run("webagent.api:create_app", factory=True, host="127.0.0.1",
                    port=args.port, access_log=False, log_config=None)
    elif args.command == "worker":
        from .worker import run_worker
        asyncio.run(run_worker(Settings.from_env(), once=args.once))
    elif args.command == "migrate":
        from .db import migrate
        from .runtime import check_runtime
        check_runtime()
        print(json.dumps(migrate(Settings.from_env().business_db, target=args.target)))
    else:
        from .runtime import check_runtime
        print(json.dumps(check_runtime(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
