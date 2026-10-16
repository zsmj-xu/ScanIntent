"""Build an offline review case. This command never writes to ClickHouse."""

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from reconstruction import dialogue_messages, reconstruct


def write_private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)


def prepare_case(project, trace, raw_file, output_dir):
    raw_file, output_dir = Path(raw_file).resolve(), Path(output_dir).resolve()
    raw = json.loads(raw_file.read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("Review input must be a nonempty observation array")
    if any(row.get("project_id", project) != project or row.get("trace_id", trace) != trace for row in raw):
        raise ValueError("Review input contains a different project or trace")
    candidate = reconstruct(project, trace, raw)
    dialogue = dialogue_messages(candidate["messages"])
    # A new directory makes reviewed artifacts immutable across subsequent iterations.
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_private_json(output_dir / "candidate.json", candidate)
    write_private_json(output_dir / "dialogue.json", dialogue)
    manifest = dict(project_id=project, trace_id=trace, raw_file=str(raw_file),
                    raw_sha256=hashlib.sha256(raw_file.read_bytes()).hexdigest(),
                    reconstruction_sha256=hashlib.sha256(Path(__file__).with_name("reconstruction.py").read_bytes()).hexdigest(),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    observation_count=len(raw), message_count=len(dialogue),
                    internal_event_counts=dict(Counter(e["kind"] for e in candidate["messages"])),
                    source_has_error_levels=all("level" in row for row in raw),
                    status="pending_independent_review")
    manifest["artifact_sha256"] = {name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
                                   for name in ("candidate.json", "dialogue.json")}
    write_private_json(output_dir / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--raw-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare_case(args.project_id, args.trace_id, args.raw_file, args.output_dir)
    print(json.dumps({k: manifest[k] for k in ("trace_id", "observation_count", "message_count", "status")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
