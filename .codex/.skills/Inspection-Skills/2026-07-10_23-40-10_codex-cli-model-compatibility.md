# Override an unsupported default model for local Codex validation

- Recorded at: 2026-07-10T23:40:10+08:00
- Scope: Launching nested `codex exec` validation sessions from the current IS-Bench environment
- Trigger: failure and repeat-risk

## Context and symptom

A read-only `codex exec --ephemeral` validation session failed before it could answer. The service returned: `The 'gpt-5.6-sol' model requires a newer version of Codex. Please upgrade to the latest app or CLI and try again.` The installed client reported `codex-cli 0.139.0`.

## Root cause

Verified: the installed CLI version and the configured `gpt-5.6-sol` model are incompatible. The same validation session completed successfully when the model was overridden to `gpt-5.4` for that invocation.

## Resolution or current status

For a temporary, non-mutating workaround, pass `-m gpt-5.4` to nested `codex exec` validation commands. The durable fix is to upgrade Codex before relying on `gpt-5.6-sol`; this task did not change the user's default model or upgrade the CLI.

## Reusable prevention and checks

- Check `codex --version` before using the configured default model in nested validation sessions.
- If the server reports a CLI/model compatibility error, do not diagnose it as a project Skill failure.
- Validate the workaround by requiring `turn.completed` and a zero process exit code, not merely `thread.started`.

## Relevant locations

- `codex --version`
- `codex exec --ephemeral --sandbox read-only -m gpt-5.4 ...`
