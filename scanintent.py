"""ScanIntent CLI: initialize local CK, collect one window, or view stored sessions."""

import argparse
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from ck_reader import DEFAULT_ENV
from clickhouse import Client
from collector import collect
from source import Source
from store import Store


ROOT = Path(__file__).resolve().parent


def parse_time(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("Start time must include a timezone, for example +08:00")
    return int(stamp.timestamp() * 1000)


def initialize_local():
    path = ROOT / ".env"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {"status": "existing", "message": "Existing .env preserved"}
    with os.fdopen(descriptor, "w") as file:
        file.write("SCANINTENT_CLICKHOUSE_URL=http://127.0.0.1:18124\n"
                   "SCANINTENT_CLICKHOUSE_DATABASE=scanintent\n"
                   "SCANINTENT_CLICKHOUSE_USER=scanintent\n"
                   "SCANINTENT_CLICKHOUSE_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    return {"status": "created", "message": "Local target configuration created with mode 0600"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-local")
    commands.add_parser("init-db")
    doctor = commands.add_parser("doctor-source")
    doctor.add_argument("--source-env", type=Path, default=DEFAULT_ENV)
    command = commands.add_parser("collect", help="Process exactly one 30-minute window, then exit")
    command.add_argument("--project-id", required=True)
    command.add_argument("--source-env", type=Path, default=DEFAULT_ENV)
    command.add_argument("--start", help="ISO time with UTC offset; default is source now minus 30 minutes")
    command.add_argument("--resume", action="store_true", help="Read one next window from the stored checkpoint")
    command.add_argument("--alias", default="")
    command.add_argument("--trace-id", action="append", help="Restrict this test run to specified trace IDs")
    command.add_argument("--batch-size", type=int, default=20)
    command.add_argument("--max-traces", type=int, help="Stop after this many traces for testing; an incomplete window never advances progress")
    serve = commands.add_parser("serve", help="Read stored conversations; never starts collection")
    serve.add_argument("--project-id", required=True)
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        if args.command == "init-local":
            result = initialize_local()
        elif args.command == "doctor-source":
            timings = {}
            result = Source(Client.source(args.source_env)).clock(timings)
            result["timings"] = timings
        else:
            store = Store(Client.target())
            if args.command == "serve":
                from web import serve
                serve(store, args.project_id, args.port)
                return
            with (ROOT / ".collection.lock").open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Another collection or initialization is already running") from None
                store.initialize()
                if args.command == "init-db":
                    result = {"status": "initialized"}
                else:
                    result = collect(Source(Client.source(args.source_env)), store, args.project_id,
                                     start_ms=parse_time(args.start) if args.start else None,
                                     resume=args.resume, alias=args.alias, trace_ids=args.trace_id,
                                     batch_size=args.batch_size, max_traces=args.max_traces)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        # Errors from transport and collection are sanitized at the boundary.
        parser.exit(1, type(exc).__name__ + ": " + str(exc) + "\n")


if __name__ == "__main__":
    main()
