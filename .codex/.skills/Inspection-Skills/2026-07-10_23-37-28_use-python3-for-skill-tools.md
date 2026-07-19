# Use `python3` for local Skill tooling

- Recorded at: 2026-07-10T23:37:28+08:00
- Scope: Codex Skill creation and validation commands in the current IS-Bench environment
- Trigger: failure and repeat-risk

## Context and symptom

Initializing the project Skills with `python .../init_skill.py` failed immediately with `/bin/bash: python：未找到命令`.

## Root cause

Verified: this environment does not expose a `python` executable on `PATH`, while the same Skill initialization command succeeds with `python3`.

## Resolution or current status

Run Python-based Codex Skill helper scripts with `python3` in this project environment.

## Reusable prevention and checks

- Use `python3 <script>` rather than assuming `python` exists.
- Treat a zero exit code and the helper's `[OK]` output as initialization success, then run `quick_validate.py` with `python3` as a separate check.

## Relevant locations

- `/home/lzy/.codex/skills/.system/skill-creator/scripts/init_skill.py`
- `/home/lzy/.codex/skills/.system/skill-creator/scripts/quick_validate.py`
