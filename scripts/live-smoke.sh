#!/usr/bin/env bash
# Manual live smoke test against the REAL claude CLI on the toy task.
# Spends real tokens (a small run: <= 2 loops x 2 executors on ~24 examples).
# CI never runs this; it exists to verify the claude CLI contract end-to-end.
set -euo pipefail

workdir=$(mktemp -d /tmp/rigorloop-smoke.XXXXXX)
echo "smoke project: $workdir"

rigorloop --dir "$workdir" init

# Shrink the budget and use a fast model for the smoke run.
python3 - "$workdir" <<'EOF'
import pathlib, sys

path = pathlib.Path(sys.argv[1]) / "rigorloop.toml"
text = path.read_text()
text = text.replace("max_loops              = 12", "max_loops              = 2")
text = text.replace("executors_per_loop     = 4", "executors_per_loop     = 2")
text = text.replace('model      = "claude-sonnet-5"', 'model      = "claude-haiku-4-5-20251001"')
path.write_text(text)
EOF

rigorloop --dir "$workdir" check
rigorloop --dir "$workdir" run

run_id=$(ls "$workdir/runs" | head -1)
echo
echo "=== report ==="
cat "$workdir/runs/$run_id/final/report.md"
