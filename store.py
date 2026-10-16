"""Versioned, complete conversation snapshots; no cross-table transaction needed."""

import json
import time
from pathlib import Path

from clickhouse import json_text


SUMMARY = """project_id, trace_id, aliases, title, first_ms, last_ms, event_ms,
    observation_count, message_count, warnings, collected_ms"""


class Store:
    def __init__(self, client):
        self.client = client

    def initialize(self):
        for statement in Path(__file__).with_name("schema.sql").read_text().split(";"):
            if statement.strip():
                self.client.execute(statement)

    def save(self, conversation):
        self.save_many([conversation])

    def save_many(self, conversations):
        rows = []
        for conversation in conversations:
            row = dict(conversation)
            row["messages_json"] = json_text(row.pop("messages"))
            row["collected_ms"] = time.time_ns() // 1000000
            row["version"] = time.time_ns()
            rows.append(row)
        if rows:
            self.client.insert("conversations", rows)

    def save_run(self, run):
        row = dict(run, version=time.time_ns())
        self.client.insert("collection_runs", [row])

    def checkpoint(self, project, scope):
        rows = list(self.client.rows("""SELECT max(window_end_ms) AS checkpoint
            FROM collection_runs FINAL WHERE project_id={project:String}
            AND scope={scope:String} AND status='success'""",
            {"project": project, "scope": scope}))
        return int(rows[0]["checkpoint"]) if rows and rows[0]["checkpoint"] else None

    def list(self, project, *, alias="", before_ms=None, before_trace="", limit=50):
        params = {"project": project, "limit": max(1, min(int(limit), 100))}
        scope = "project_id={project:String}"
        if alias:
            scope += " AND has(aliases, {alias:String})"
            params["alias"] = alias
        if before_ms is not None:
            scope += " AND (last_ms, trace_id) < ({before:Int64}, {trace:String})"
            params.update(before=int(before_ms), trace=before_trace)
        rows = list(self.client.rows("SELECT " + SUMMARY + " FROM conversations FINAL WHERE " +
                    scope + " ORDER BY last_ms DESC, trace_id DESC LIMIT {limit:UInt32}", params))
        return rows

    def get(self, project, trace):
        rows = list(self.client.rows("SELECT " + SUMMARY + """, messages_json, observation_ids
            FROM conversations FINAL WHERE project_id={project:String}
            AND trace_id={trace:String} LIMIT 1""", {"project": project, "trace": trace}))
        if not rows:
            return None
        row = rows[0]
        row["messages"] = json.loads(row.pop("messages_json"))
        return row

    def browse(self, project, *, alias="", query="", start=None, end=None,
               warning=False, order="newest", page=1, size=20):
        page, size = max(1, int(page)), max(1, min(int(size), 100))
        params = {"project": project}
        scope = "project_id={project:String}"
        if alias:
            scope += " AND has(aliases, {alias:String})"
            params["alias"] = alias
        if query:
            scope += " AND (positionCaseInsensitiveUTF8(title, {query:String}) > 0 OR positionCaseInsensitiveUTF8(trace_id, {query:String}) > 0)"
            params["query"] = query
        for value, name, operator in ((start, "start", ">="), (end, "end", "<")):
            if value is not None:
                scope += f" AND last_ms {operator} {{{name}:Int64}}"
                params[name] = int(value)
        if start is not None and end is not None and int(start) >= int(end):
            raise ValueError("Invalid date range")
        if warning:
            scope += " AND notEmpty(warnings)"
        total = int(next(iter(self.client.rows(
            "SELECT count() AS total FROM conversations FINAL WHERE " + scope, params)))["total"])
        page = min(page, max(1, (total + size - 1) // size))
        params.update(limit=size, offset=(page - 1) * size)
        direction = "ASC" if order == "oldest" else "DESC"
        rows = list(self.client.rows("SELECT " + SUMMARY + " FROM conversations FINAL WHERE " +
                    scope + f" ORDER BY last_ms {direction}, trace_id {direction}" +
                    " LIMIT {limit:UInt32} OFFSET {offset:UInt64}", params))
        return dict(conversations=rows, total=total, page=page, size=size)

    def aliases(self, project):
        return [row["alias"] for row in self.client.rows("""SELECT DISTINCT arrayJoin(aliases) AS alias
            FROM conversations FINAL WHERE project_id={project:String} ORDER BY alias""", {"project": project})]

    def runs(self, project):
        return list(self.client.rows("""SELECT * EXCEPT version FROM collection_runs FINAL
            WHERE project_id={project:String} ORDER BY started_ms DESC LIMIT 20""",
            {"project": project}))
