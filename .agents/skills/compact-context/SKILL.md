---
name: compact-context
description: Manually monitor active context remaining in an IS-Bench Codex session, using the latest rollout token snapshot rather than cumulative usage or account quota. Use only when the user explicitly invokes `$compact-context` or asks to enable context monitoring for the current session; create or update a continuation-ready summary under `.codex/.compact_context/` when the session reaches the compact or handoff risk bands.
---

# Compact Context

Manage one Codex session's active context after the user explicitly enables this Skill. Do not run it automatically at session startup. The configured GPT-5.6 Sol catalog has a 1,050,000-token maximum and a 95% effective runtime window, normally 997,500 tokens.

## Enable and check the current session

1. Treat an explicit invocation as enabling monitoring for the current session only. Without explicit user activation, stop and continue the task without context monitoring.
2. Run this command after activation:

   ```bash
   python3 .agents/skills/compact-context/scripts/session_context_status.py \
     --watch-remaining-pct 40 \
     --compact-remaining-pct 20 \
     --handoff-remaining-pct 10
   ```

3. While enabled, run it again before a large phase, after unusually large tool output has entered a later model request, after runtime compaction, or before finishing a long-running turn. This Skill is not a background daemon.
4. Use `last_token_usage.total_tokens` and the rollout's `model_context_window` through the script. Treat `total_token_usage.total_tokens` as cumulative diagnostic data only. Never use cumulative usage, rate limits, account allowance, or daily quota to decide whether to compact.
5. Treat the snapshot as the previous model boundary. It does not include tool output produced afterward until another model request consumes that output.
6. If the script returns `status=unavailable`, report the missing measurement instead of guessing and retry after a later model boundary.

Use `--thread-id <full-id>` to inspect a named session or `--rollout <path>` to inspect a specific JSONL file. With no override, inspect only the rollout selected by `CODEX_THREAD_ID`.

## Act on the risk band

- `remaining > 40%` (`healthy`): continue the current task normally.
- `20% < remaining <= 40%` (`watch`): continue the same task, but recheck before a large phase and refresh an existing summary after substantial tool output or progress.
- `10% <= remaining <= 20%` (`compact`): create or update the continuation summary immediately. For the same task, suggest `/compact`.
- `remaining < 10%` (`handoff`): do not begin a large phase. Create or update the summary immediately and prepare to continue in a new session.

The script always calculates against the runtime window in the rollout. When an old session still reports `353400`, expect `runtime_catalog_mismatch=true` and use that smaller window for protection. A newly calibrated session should report a runtime window near `997500`.

Task boundaries override percentage heuristics:

- When the current task is complete or the new goal is independent, suggest a new session regardless of remaining percentage.
- When exploring an alternative implementation route while preserving the current route, suggest `/fork`.
- Keep the same session for the same coherent task when the risk band allows it.
- Only suggest `/compact`, `/fork`, or a new session. The user must execute those UI operations.

## Create or update the summary

When `summary_due=true`, use `latest_same_risk_summary` from the script:

- Update that file in place when it belongs to the same full session ID and risk band.
- Otherwise create `/home/lzy/code/IS-Bench/.codex/.compact_context/` and write a new Markdown file named exactly:

  ```text
  {task_description}_ {session_id_first_4}_{timestamp}.md
  ```

Use a short filesystem-safe task description and Asia/Shanghai time formatted as `YYYY-MM-DD_HH-MM-SS`. Preserve the literal space after the first underscore. If the full session ID is unavailable, do not invent one; report that summary creation is blocked.

Start the file with fields that make identity and coverage explicit:

```text
- Full session ID: `{complete UUID}`
- Session prefix: `{first four characters}`
- Snapshot timestamp: `{rollout token snapshot time}`
- Coverage through: `{latest state included in this summary}`
- Runtime context window: `{tokens}`
- Used context: `{tokens} ({percent}%)`
- Remaining context: `{tokens} ({percent}%)`
- Context risk band: `{compact|handoff}`
- Task status: `{in_progress|blocked|complete}`
- Repository: `/home/lzy/code/IS-Bench`
- Task description: `{short description}`
```

Include the current goal and newest exact requirements, critical constraints and decisions, progress and changed files, commands and validation results, unresolved failures and dead ends, and an ordered next-step list. Preserve exact paths, identifiers, arguments, and errors when they matter. Omit secrets, credentials, redundant chatter, obsolete reasoning, and unrelated history.

End the file with this exact final line and place nothing after it:

```text
【请仔细阅读以上内容，并反复确认你已经理解了现在的进度，和接下来你要做的事之后，再开始执行】
```

## Resume after compaction

Run the status script and use `latest_matching_summary`, which is resolved by the full session ID stored in each file body. Never choose a summary using only the common four-character filename prefix. Read the matched summary, verify cheap claims against the live workspace and Git diff, then continue from its next-step list.
