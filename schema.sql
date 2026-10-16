CREATE TABLE IF NOT EXISTS conversations
(
    project_id String,
    trace_id String,
    aliases Array(String),
    title String,
    first_ms Int64,
    last_ms Int64,
    event_ms Int64,
    observation_count UInt32,
    message_count UInt32,
    warnings Array(String),
    messages_json String CODEC(ZSTD(3)),
    observation_ids Array(String),
    collected_ms Int64,
    version UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (project_id, trace_id);

CREATE TABLE IF NOT EXISTS collection_runs
(
    project_id String,
    scope String,
    run_id String,
    window_start_ms Int64,
    window_end_ms Int64,
    started_ms Int64,
    finished_ms Int64,
    status LowCardinality(String),
    trace_count UInt32,
    observation_count UInt64,
    timings_json String,
    source_clock_json String,
    error String,
    version UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (project_id, scope, run_id);
