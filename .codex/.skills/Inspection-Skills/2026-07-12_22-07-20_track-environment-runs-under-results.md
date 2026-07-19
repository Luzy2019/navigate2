# Track environment runs under `results/`

- Recorded at: 2026-07-12T22:07:20+08:00
- Scope: IS-Bench environment tests and lifelong safe-memory benchmark runs
- Trigger: repeat-risk

## Context and symptom

Environment-test artifacts had accumulated across `results/`, `logs/`, and `outputs/`, with ad hoc directory names and no local description of what each run tested. This made it difficult to associate console output, reports, observations, and videos with one experiment.

## Root cause

Verified: the safe-memory single-run CLI could timestamp an output root, but its directory name did not include the scene or test purpose, and it did not create a README or capture stdout and stderr beside the artifacts.

## Resolution or current status

Future `safe_memory_benchmark_once` timestamped runs use `results/<task>___<scene>/<timestamp>_<purpose>/`. The run root contains `README.md` and `console.log`; benchmark artifacts remain below it. Existing historical `logs/` and `outputs/` are retained because other records may reference them.

## Reusable prevention and checks

- Give each environment test a short `--run-purpose` label and keep its artifacts under `results/`.
- For every environment run, request and verify both `video.mp4` and `topdown.mp4`, whether the task succeeds or fails.
- Keep `README.md`, `console.log`, `report.json`, observations, and videos within the same timestamped run tree.
- Do not move or delete historical artifact directories merely to enforce the convention for future runs.
- Validate both MP4 files with a decoder; file existence alone is not sufficient.

## Relevant locations

- `og_ego_prim/cli/safe_memory_benchmark_once.py`
- `results/<task>___<scene>/<timestamp>_<purpose>/README.md`
- `results/<task>___<scene>/<timestamp>_<purpose>/console.log`
- `--timestamp-work-dir --run-purpose <label> --save-video --save-topdown-video`
