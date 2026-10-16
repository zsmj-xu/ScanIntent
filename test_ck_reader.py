import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from ck_reader import _connection, _query, read_observations


class ReaderTests(unittest.TestCase):
    def read(self, **kwargs):
        return read_observations("project", "2026-10-14T16:00:00+08:00",
                                 "2026-10-14T16:10:00+08:00", **kwargs)

    @patch("ck_reader._connection", return_value=("http://localhost:8123/", "langfuse", "Basic test"))
    @patch("ck_reader._query")
    def test_grouping_and_project_time_filters(self, query, connection):
        rows = [dict(trace_id=t, name="n", metadata={"key": "value"}, input=None, output="text")
                for t in ("a", "a", "b")]
        query.return_value = rows
        result = self.read()
        self.assertEqual(result, {"a": rows[:2], "b": rows[2:]})
        for call in query.call_args_list:
            sql, params = call.args[1:]
            self.assertIn("project_id = {project:String}", sql)
            self.assertIn("start_time <", sql)
            self.assertIn("toDate(start_time)", sql)
            self.assertIn("observations FINAL", sql)
            self.assertEqual(params["start"], "2026-10-14 08:00:00.000000")
        sql, params = query.call_args.args[1:]
        self.assertNotIn("LIMIT", sql)
        self.assertNotIn("limit", params)
        self.assertEqual(query.call_count, 1)

    @patch("ck_reader._connection")
    @patch("ck_reader._query")
    def test_empty_and_more_than_previous_limits(self, query, connection):
        query.return_value = []
        self.assertEqual(self.read(), {})
        self.assertEqual(query.call_count, 1)
        query.return_value = [dict(trace_id=str(index // 2), name="n", metadata={}, input=None, output=None)
                              for index in range(2000)]
        result = self.read()
        self.assertEqual(len(result), 1000)
        self.assertEqual(sum(map(len, result.values())), 2000)

    @patch("ck_reader._connection")
    def test_validation_before_connection(self, connection):
        for project, start, end in (
                ("", "2026-10-14T16:00:00+08:00", "2026-10-14T16:10:00+08:00"),
                ("p", "2026-10-14T16:00:00", "2026-10-14T16:10:00+08:00"),
                ("p", "2026-10-14T16:10:00+08:00", "2026-10-14T16:00:00+08:00")):
            with self.assertRaises(ValueError):
                read_observations(project, start, end)
        connection.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    def test_source_config_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / ".env"
            file.write_text("CLICKHOUSE_HOST=legacy\nSOURCE_CLICKHOUSE_HOST=source\n"
                            "SOURCE_CLICKHOUSE_USER=reader\nSOURCE_CLICKHOUSE_PASSWORD=secret\n"
                            "AUTOMODE_CLICKHOUSE_HOST=runtime\n")
            self.assertEqual(_connection(file)[0], "http://source:8123/")
            with patch.dict("os.environ", {"SOURCE_CLICKHOUSE_HOST": "https://override:8443"}):
                self.assertEqual(_connection(file)[0], "https://override:8443/")

    @patch("ck_reader.urlopen")
    def test_transport_readonly_parameters_without_limits(self, open_url):
        response = open_url.return_value.__enter__.return_value
        response.read.return_value = b'{"trace_id":"a"}\n'
        self.assertEqual(_query(("http://localhost/", "langfuse", "Basic secret"),
                                "SELECT {project:String}", {"project": "quote'\n\\"}),
                         [{"trace_id": "a"}])
        request = open_url.call_args.args[0]
        settings = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(settings["readonly"], ["1"])
        self.assertEqual(settings["param_project"], ["quote'\\n\\\\"])
        self.assertNotIn("secret", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Basic secret")
        for key in ("max_execution_time", "max_rows_to_read", "max_result_bytes", "max_result_rows"):
            self.assertEqual(settings[key], ["0"])
        self.assertIsNone(open_url.call_args.kwargs["timeout"])
        response.read.assert_called_once_with()


    @patch("ck_reader.perf_counter", side_effect=range(30))
    @patch("ck_reader._connection", return_value=("http://localhost/", "langfuse", "Basic test"))
    @patch("ck_reader.urlopen")
    def test_stage_timings_preserve_result(self, open_url, connection, clock):
        row = b'{"trace_id":"a","name":"n","metadata":{},"input":null,"output":"text"}'
        open_url.return_value.__enter__.return_value.read.return_value = row
        timings = {"old_run": 999}
        result = self.read(timings=timings)
        self.assertEqual(result["a"][0]["output"], "text")
        self.assertEqual(timings, {"config_ms": 1000,
                                  "observations_query_ms": 1000, "field_processing_ms": 1000,
                                  "grouping_ms": 1000, "total_ms": 9000})

    @patch("ck_reader.urlopen")
    def test_server_exception_does_not_become_data_or_leak_details(self, open_url):
        open_url.return_value.__enter__.return_value.read.return_value = (
            b'{"exception":"Code: 159. private SQL and conversation details"}')
        with self.assertRaisesRegex(RuntimeError, r"^ClickHouse query failed \(code 159\)$"):
            _query(("http://localhost/", "langfuse", "Basic secret"), "SELECT 1", {})

    @patch("ck_reader.perf_counter", side_effect=range(30))
    @patch("ck_reader._connection", return_value=("http://localhost/", "langfuse", "Basic test"))
    @patch("ck_reader.urlopen")
    def test_empty_and_failed_queries_keep_timings(self, open_url, connection, clock):
        open_url.return_value.__enter__.return_value.read.return_value = b""
        timings = {}
        self.assertEqual(self.read(timings=timings), {})
        self.assertEqual(timings["observations_query_ms"], 1000)
        self.assertEqual(timings["grouping_ms"], 1000)
        self.assertEqual(timings["total_ms"], 9000)
        open_url.side_effect = URLError("private connection details")
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            self.read(timings=timings)
        self.assertEqual(timings["observations_query_ms"], 1000)
        self.assertEqual(timings["field_processing_ms"], 0)
        self.assertEqual(timings["total_ms"], 5000)


if __name__ == "__main__":
    unittest.main()
