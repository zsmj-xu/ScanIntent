"""HTTP transport for ScanIntent. Credentials never enter URLs or diagnostics."""

import base64
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from ck_reader import DEFAULT_ENV, _connection


@contextmanager
def measure(timings, key):
    started = perf_counter()
    try:
        yield
    finally:
        timings[key] = timings.get(key, 0) + (perf_counter() - started) * 1000


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def array_parameter(values):
    return "[" + ",".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
                           for v in values) + "]"


class Client:
    def __init__(self, connection, *, readonly, timeout=120):
        self.url, self.database, self.auth = connection
        self.readonly = readonly
        self.timeout = timeout

    @classmethod
    def source(cls, env_file=DEFAULT_ENV):
        return cls(_connection(env_file), readonly=True)

    @classmethod
    def target(cls, env_file=Path(__file__).with_name(".env")):
        values = {}
        if env_file and Path(env_file).exists():
            for line in Path(env_file).read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
        values.update(os.environ)
        def read(key, default=""):
            return values.get("SCANINTENT_CLICKHOUSE_" + key, default)
        parsed = urlsplit(read("URL", "http://127.0.0.1:18124"))
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Invalid target ClickHouse URL")
        database = read("DATABASE", "scanintent")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("Invalid target database name")
        user, password = read("USER", "scanintent"), read("PASSWORD")
        if not user or not password:
            raise ValueError("Target ClickHouse credentials are required; run init-local first")
        auth = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
        return cls((parsed.geturl().rstrip("/") + "/", database, auth), readonly=False)

    def _request(self, sql, parameters, payload=None):
        settings = {"database": self.database, "wait_end_of_query": 1,
                    "max_execution_time": self.timeout}
        if self.readonly:
            settings["readonly"] = 1
        settings.update({"param_" + key: str(value).replace("\\", "\\\\")
                         .replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                         for key, value in parameters.items()})
        body = sql.encode()
        if payload is not None:
            body += b"\n" + payload
        return Request(self.url + "?" + urlencode(settings), data=body, method="POST",
                       headers={"Authorization": self.auth,
                                "Content-Type": "text/plain; charset=utf-8"})

    def rows(self, sql, parameters=None, *, timings=None, stage="query_ms"):
        """Stream JSON rows; network and parse timings exclude consumer processing."""
        timings = timings if timings is not None else {}
        request = self._request(sql + "\nFORMAT JSONEachRow", parameters or {})
        try:
            with measure(timings, stage):
                response = urlopen(request, timeout=self.timeout)
            with response:
                while True:
                    with measure(timings, stage):
                        line = response.readline()
                    if not line:
                        break
                    with measure(timings, "parse_ms"):
                        try:
                            row = json.loads(line)
                        except ValueError:
                            raise RuntimeError("ClickHouse returned incomplete JSON") from None
                        if not isinstance(row, dict) or "exception" in row:
                            raise RuntimeError("ClickHouse query failed or returned invalid rows")
                    yield row
        except HTTPError as exc:
            raise RuntimeError("ClickHouse HTTP " + str(exc.code)) from None
        except (URLError, OSError, TimeoutError):
            raise RuntimeError("ClickHouse connection failed or timed out") from None

    def execute(self, sql, *, payload=None):
        if self.readonly:
            raise ValueError("Source connection is read-only")
        try:
            with urlopen(self._request(sql, {}, payload), timeout=self.timeout) as response:
                response.read()
        except HTTPError as exc:
            raise RuntimeError("ClickHouse HTTP " + str(exc.code)) from None
        except (URLError, OSError, TimeoutError):
            raise RuntimeError("ClickHouse connection failed or timed out") from None

    def insert(self, table, rows):
        if table not in ("conversations", "collection_runs"):
            raise ValueError("Unexpected target table")
        self.execute("INSERT INTO " + table + " FORMAT JSONEachRow",
                     payload=("\n".join(json_text(row) for row in rows) + "\n").encode())
