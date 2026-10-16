"""One 30-minute collection window per invocation. There is no polling loop."""

import hashlib
import time
import uuid
from itertools import groupby, islice

from clickhouse import json_text, measure
from reconstruction import reconstruct


WINDOW_MS = 30 * 60 * 1000
STAGES = ("clock_ms", "discovery_ms", "lookup_ms", "history_ms", "parse_ms", "ordering_ms", "reconstruct_ms",
          "persist_ms", "checkpoint_ms", "run_log_ms", "total_ms")


def collect(source, store, project, *, start_ms=None, resume=False, alias="", trace_ids=None, batch_size=20, max_traces=None):
    if not project or not project.strip():
        raise ValueError("project_id is required")
    if batch_size < 1 or batch_size > 100:
        raise ValueError("batch_size must be between 1 and 100")
    if max_traces is not None and max_traces < 1:
        raise ValueError("max_traces must be positive")
    timings = dict.fromkeys(STAGES, 0.0)
    began = time.perf_counter()
    local_before = time.time_ns() // 1000000
    clock = source.clock(timings)
    local_after = time.time_ns() // 1000000
    server_ms = int(clock["server_ms"])
    clock["local_midpoint_ms"] = (local_before + local_after) // 2
    clock["clock_offset_ms"] = server_ms - clock["local_midpoint_ms"]
    clock["roundtrip_ms"] = local_after - local_before
    clock["window_field"] = "observations.event_ts"
    scope = hashlib.sha256(json_text(dict(alias=alias, traces=sorted(trace_ids or []),
                                         field="event_ts", format_version=1)).encode()).hexdigest()[:24]
    if resume:
        with measure(timings, "checkpoint_ms"):
            checkpoint = store.checkpoint(project, scope)
        if checkpoint is not None:
            start_ms = checkpoint
    if start_ms is None:
        start_ms = server_ms - WINDOW_MS
    end_ms = start_ms + WINDOW_MS
    if end_ms > server_ms:
        raise ValueError("The complete 30-minute window must end before source server time")
    run = dict(project_id=project, scope=scope, run_id=str(uuid.uuid4()),
               window_start_ms=start_ms, window_end_ms=end_ms, started_ms=local_before,
               finished_ms=0, status="running", trace_count=0, observation_count=0,
               timings_json="{}", source_clock_json=json_text(clock), error="")
    with measure(timings, "run_log_ms"):
        store.save_run(run)
    failure = None
    candidates = None
    try:
        candidates = iter(source.discover(project, start_ms, end_ms, timings,
                                          alias=alias, trace_ids=trace_ids))
        while True:
            size = batch_size if max_traces is None else min(batch_size, max_traces - run["trace_count"])
            if size == 0:
                run["status"] = "partial" if next(candidates, None) is not None else "success"
                break
            batch = [r["trace_id"] for r in islice(candidates, size)]
            if not batch:
                break
            rows = source.observations(project, batch, timings)
            returned = set()
            completed = []
            try:
                for trace, records in groupby(rows, key=lambda row: row["trace_id"]):
                    # Materialize only one trace. Query timings stay out of reconstruction timing.
                    records = list(records)
                    with measure(timings, "reconstruct_ms"):
                        conversation = reconstruct(project, trace, records)
                    completed.append(conversation)
                    returned.add(trace)
            finally:
                close = getattr(rows, "close", None)
                if close:
                    close()
            if returned != set(batch):
                raise RuntimeError("Some discovered traces disappeared before full-history reading; retry window")
            with measure(timings, "persist_ms"):
                store.save_many(completed)
            run["trace_count"] += len(completed)
            run["observation_count"] += sum(row["observation_count"] for row in completed)
            timings["total_ms"] = (time.perf_counter() - began) * 1000
            run["timings_json"] = json_text({k: round(v, 3) for k, v in timings.items()})
            with measure(timings, "run_log_ms"):
                store.save_run(run)
        if run["status"] == "running":
            run["status"] = "success"
    except (Exception, KeyboardInterrupt) as exc:
        run["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        # Avoid storing exception strings that might contain payloads or SQL parameters.
        run["error"] = type(exc).__name__
        failure = exc
    finally:
        close = getattr(candidates, "close", None)
        if close:
            close()
        run["finished_ms"] = time.time_ns() // 1000000
        timings["total_ms"] = (time.perf_counter() - began) * 1000
        run["timings_json"] = json_text({k: round(v, 3) for k, v in timings.items()})
        # A successful final run row is itself the checkpoint. Never publish it early.
        store.save_run(run)
    if failure:
        raise RuntimeError("Collection " + run["status"] + "; window not advanced (" + run["error"] + ")") from failure
    return run
