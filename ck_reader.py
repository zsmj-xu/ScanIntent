"""Small, read-only Langfuse observation reader using the project's source config."""

import argparse
import base64
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_ENV = Path(__file__).resolve().parent / ".env"


@contextmanager
def _measure(timings, key):
    started = perf_counter()
    try:
        yield
    finally:
        if timings is not None:
            timings[key] = timings.get(key, 0.0) + (perf_counter() - started) * 1000


def _connection(env_file):
    values = {}
    if env_file is not None:
        for line in Path(env_file).read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ")
            if key.startswith(("SOURCE_CLICKHOUSE_", "CLICKHOUSE_")):
                values[key] = value.strip().strip("\"'")
    values.update(os.environ)

    def read(name, default=""):
        return values.get("SOURCE_CLICKHOUSE_" + name,
                          values.get("CLICKHOUSE_" + name, default))

    secure = read("SECURE", "0").lower() in ("1", "true", "yes")
    host = read("HOST")
    parsed = urlsplit(host if "://" in host else ("https" if secure else "http") + "://" + host)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Invalid SOURCE_CLICKHOUSE_HOST")
    database = read("DATABASE", "langfuse")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise ValueError("Invalid ClickHouse database name")
    user, password = read("USER"), read("PASSWORD")
    if not user or not password:
        raise ValueError("Source ClickHouse user and password are required")
    port = parsed.port or int(read("PORT", "8123"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid ClickHouse port")
    hostname = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
    url = f"{parsed.scheme}://{hostname}:{port}/"
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return url, database, auth


def _query(connection, sql, parameters, *, timings=None, timing_key="query_ms"):
    url, database, auth = connection
    settings = {
        "database": database, "readonly": 1, "wait_end_of_query": 1,
        "max_execution_time": 0, "max_rows_to_read": 0,
        "max_result_bytes": 0, "max_result_rows": 0,
    }
    # HTTP parameter values use ClickHouse's escaped string format.
    settings.update({"param_" + key: str(value).replace("\\", "\\\\")
                     .replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                     for key, value in parameters.items()})
    request = Request(url + "?" + urlencode(settings),
                      data=(sql + "\nFORMAT JSONEachRow").encode(), method="POST",
                      headers={"Authorization": auth, "Content-Type": "text/plain; charset=utf-8"})
    try:
        with _measure(timings, timing_key):
            with urlopen(request, timeout=None) as response:
                body = response.read()
    except HTTPError as exc:
        # Error bodies may contain SQL values or conversation data.
        raise RuntimeError(f"ClickHouse HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("ClickHouse connection failed or timed out") from None
    try:
        with _measure(timings, "field_processing_ms"):
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
    except ValueError:
        raise RuntimeError("ClickHouse returned an invalid or incomplete result") from None
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("ClickHouse returned a non-object row")
        if "exception" in row:
            code = re.search(r"\bCode:\s*(\d+)", str(row["exception"]))
            suffix = f" (code {code.group(1)})" if code else ""
            raise RuntimeError("ClickHouse query failed" + suffix)
    return rows


def read_observations(project_id, start, end, *, env_file=DEFAULT_ENV, timings=None):
    """Return {trace_id: [observations]} within [start, end), using start_time.

    Times must include a UTC offset. All matching observations are read.
    Input/output and metadata values retain their original database representation.
    Pass a dict as timings to receive per-stage milliseconds, including on failure.
    """
    if timings is not None:
        timings.clear()
        timings.update(dict.fromkeys(("config_ms", "observations_query_ms",
                                      "field_processing_ms", "grouping_ms", "total_ms"), 0.0))
    with _measure(timings, "total_ms"):
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id is required")
        times = [datetime.fromisoformat(value) if isinstance(value, str) else value
                 for value in (start, end)]
        if any(not isinstance(t, datetime) or t.utcoffset() is None for t in times):
            raise ValueError("start and end must include a timezone, e.g. +08:00")
        if times[0] >= times[1]:
            raise ValueError("start must be earlier than end")
        parameters = dict(project=project_id,
                          start=times[0].astimezone(timezone.utc).isoformat(sep=" ", timespec="microseconds")[:-6],
                          end=times[1].astimezone(timezone.utc).isoformat(sep=" ", timespec="microseconds")[:-6])
        # Date predicates match the source's project/type/date sorting key.
        scope = """project_id = {project:String}
            AND toDate(start_time) >= toDate(toDateTime64({start:String}, 6, 'UTC'), timezone())
            AND toDate(start_time) <= toDate(toDateTime64({end:String}, 6, 'UTC'), timezone())
            AND start_time >= toDateTime64({start:String}, 6, 'UTC')
            AND start_time < toDateTime64({end:String}, 6, 'UTC')
            AND is_deleted = 0 AND trace_id != ''"""
        with _measure(timings, "config_ms"):
            connection = _connection(env_file)
        rows = _query(connection, f"""SELECT trace_id, name, metadata, input, output
            FROM observations FINAL WHERE {scope}
            ORDER BY trace_id, start_time, id""", parameters,
                      timings=timings, timing_key="observations_query_ms")
        with _measure(timings, "grouping_ms"):
            groups = {}
            for row in rows:
                if not {"trace_id", "name", "metadata", "input", "output"} <= row.keys():
                    raise RuntimeError("ClickHouse response is missing observation fields")
                groups.setdefault(row["trace_id"], []).append(row)
        return groups


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--start", required=True, help="ISO datetime with timezone")
    parser.add_argument("--end", required=True, help="Exclusive ISO datetime with timezone")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = parser.parse_args()
    timings = {}
    try:
        result = read_observations(args.project_id, args.start, args.end,
                                   env_file=args.env_file, timings=timings)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")
    finally:
        print("Timings (ms): " + json.dumps({key: round(value, 3) for key, value in timings.items()}),
              file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
