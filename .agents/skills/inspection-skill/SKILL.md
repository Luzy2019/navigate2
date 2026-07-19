---
name: inspection-skill
description: Manually invoked workflow that reads IS-Bench inspection records under .codex/.skills/Inspection-Skills and applies relevant guidance to the current task. Use only when the user explicitly invokes $inspection-skill; do not invoke it automatically at session or task startup.
---

# Inspection Skill

When explicitly invoked, load the repository's accumulated inspection requirements before continuing task-specific work.

## Load the inspection records

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Enumerate every `*.md` file directly under `<repo-root>/.codex/.skills/Inspection-Skills` in stable lexical order.
3. Read every enumerated file completely. Do not rely on remembered or previously summarized versions.
4. If the directory is missing, contains no Markdown files, or any record cannot be read, report the problem before continuing.

## Apply the records

1. Identify the rules, known failure modes, validation requirements, and cautions relevant to the current request.
2. Treat the records as repository guidance, subject to system, developer, and explicit current-user instructions.
3. Apply relevant requirements during planning, implementation, diagnosis, and validation. Do not mechanically apply unrelated fixes.
4. Inspect current code and artifacts before adding compatibility code or patches; verify that the recorded failure mode still applies to the live checkout.
5. Keep this workflow read-only. Use `add-inspection-skill` when a new reusable lesson must be recorded.

## Continue the task

After loading and filtering the records, begin the user's task. Mention only the constraints that materially affect the work; do not reproduce the entire inspection library unless asked.
