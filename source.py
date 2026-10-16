"""Discover traces in an event-time window, then read their complete histories."""

from clickhouse import array_parameter, measure


class Source:
    def __init__(self, client):
        self.client = client

    def clock(self, timings):
        return next(self.client.rows("""SELECT timezone() AS timezone,
            toUnixTimestamp64Milli(now64(3, 'UTC')) AS server_ms""",
            timings=timings, stage="clock_ms"))

    def discover(self, project, start_ms, end_ms, timings, *, alias="", trace_ids=None):
        # Do not constrain start_time: a delayed record can belong to an old partition.
        # event_ts has no source index; correctness currently requires a project scan.
        params = {"project": project, "start": start_ms, "end": end_ms}
        extra = ""
        if alias:
            extra += " AND metadata['user_api_key_alias'] = {alias:String}"
            params["alias"] = alias
        if trace_ids:
            extra += " AND trace_id IN {traces:Array(String)}"
            params["traces"] = array_parameter(trace_ids)
        return self.client.rows("""SELECT DISTINCT trace_id FROM observations FINAL
            WHERE project_id = {project:String} AND is_deleted = 0 AND trace_id != ''
            AND event_ts >= fromUnixTimestamp64Milli({start:Int64}, 'UTC')
            AND event_ts < fromUnixTimestamp64Milli({end:Int64}, 'UTC')""" + extra +
            " ORDER BY trace_id", params, timings=timings, stage="discovery_ms")

    def observations(self, project, traces, timings):
        # First scan narrow columns for a superset of IDs, including older versions.
        # Then use every primary-key dimension to avoid reading unrelated large bodies.
        references = {}
        params = {"project": project, "traces": array_parameter(traces)}
        for row in self.client.rows("""SELECT trace_id, id, type, toDate(start_time) AS day
            FROM observations WHERE project_id={project:String}
            AND trace_id IN {traces:Array(String)}""", params,
            timings=timings, stage="lookup_ms"):
            references.setdefault((row["day"], row["type"]), set()).add(row["id"])
        records = []
        for (day, kind), ids in sorted(references.items()):
            ids = sorted(ids)
            for offset in range(0, len(ids), 200):
                scoped = dict(params, day=day, kind=kind,
                              ids=array_parameter(ids[offset:offset + 200]))
                records.extend(self.client.rows("""SELECT project_id, trace_id, id AS observation_id,
                    toUnixTimestamp64Milli(start_time) AS start_ms,
                    toUnixTimestamp64Milli(event_ts) AS event_ms,
                    toUnixTimestamp64Milli(updated_at) AS updated_ms,
                    name, level, status_message, metadata['user_api_key_alias'] AS alias, input, output
                    FROM observations FINAL
                    PREWHERE project_id={project:String} AND type={kind:String}
                    AND toDate(start_time)={day:Date} AND id IN {ids:Array(String)}
                    WHERE trace_id IN {traces:Array(String)} AND is_deleted=0""", scoped,
                    timings=timings, stage="history_ms"))
        # Batch requests avoid a network round trip for every short conversation.
        with measure(timings, "ordering_ms"):
            records.sort(key=lambda r: (r["trace_id"], int(r["start_ms"]), r["observation_id"]))
        yield from records
