---
name: add-inspection-skill
description: Manually invoked workflow that captures reusable IS-Bench knowledge in a concise Markdown record under .codex/.skills/Inspection-Skills. Use only when the user explicitly invokes $add-inspection-skill; do not invoke it automatically after failures, difficult tasks, or before final responses.
---

# Add Inspection Skill

When explicitly invoked, turn material task experience into a concise, evidence-based inspection record for future sessions.

## Decide whether to record

Record an entry when at least one condition holds:

1. A task command, test, runtime, validation, or implementation failed in a way future work may encounter.
2. The work exposed a mistake or misleading assumption that could recur.
3. The problem was difficult, required prolonged diagnosis, or needed multiple attempts before resolution.
4. The result revealed another durable rule, warning, decision boundary, or verification technique worth preserving.

Do not record harmless transient noise, secrets, raw large logs, or a duplicate of an existing lesson. If an apparent failure contains no reusable information, explicitly conclude that no entry is warranted.

## Check existing records

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Read all `*.md` files directly under `<repo-root>/.codex/.skills/Inspection-Skills` before writing.
3. Search for the same symptom, root cause, and prevention rule.
4. Update the most relevant existing record when the new evidence clarifies the same lesson; otherwise create a new record. Never overwrite unrelated content.

## Write the record

Create a Markdown file named `YYYY-MM-DD_HH-MM-SS_<short-slug>.md` in `<repo-root>/.codex/.skills/Inspection-Skills` using the local timezone. Use `apply_patch` for the edit. Keep the record focused and include only applicable sections:

```markdown
# <Concise lesson title>

- Recorded at: <ISO-8601 timestamp with timezone>
- Scope: <affected module, workflow, or environment>
- Trigger: <failure | repeat-risk | difficult-problem | other>

## Context and symptom

<What was being attempted and the observable evidence.>

## Root cause

<Verified cause, or clearly label the explanation as inferred.>

## Resolution or current status

<What fixed it, or what remains unresolved and the next useful check.>

## Reusable prevention and checks

- <Concrete action for future sessions.>
- <Validation that distinguishes success from a false positive.>

## Relevant locations

- `<file, config key, command, or artifact>`
```

## Quality gate

Before finishing:

1. Separate verified facts from inference.
2. Include enough evidence to recognize the same issue without copying bulky logs.
3. Record failed approaches only when they explain what not to repeat.
4. Prefer actionable prevention and validation over a narrative timeline.
5. Confirm the new file is inside the inspection directory and is readable by `inspection-skill`.
