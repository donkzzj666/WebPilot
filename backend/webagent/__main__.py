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
    parser = argparse.ArgumentParser(description="WebAgent M1-01 startup")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("api", help="Run the loopback API")
    api.add_argument("--port", type=port_number, default=os.getenv("WEBAGENT_API_PORT", "8000"))
    worker = commands.add_parser("worker", help="Run the independent idle Worker")
    worker.add_argument("--once", action="store_true", help="Initialize, report readiness, and exit")
    commands.add_parser("doctor", help="Check linked SQLite and runtime capabilities")
    args = parser.parse_args()
    if args.command == "api":
        import uvicorn
        uvicorn.run("webagent.api:create_app", factory=True, host="127.0.0.1",
                    port=args.port, access_log=False)
    elif args.command == "worker":
        from .worker import run_worker
        asyncio.run(run_worker(Settings.from_env(), once=args.once))
    else:
        from .runtime import check_runtime
        print(json.dumps(check_runtime(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

