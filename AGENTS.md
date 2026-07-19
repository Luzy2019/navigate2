# IS-Bench Codex instructions

## Optional context compaction skill

- Do not invoke `compact-context` automatically or at session startup.
- Invoke it only when the user explicitly requests `$compact-context` or asks to enable context monitoring for the current session.
- When explicitly invoked, open and follow `.agents/skills/compact-context/SKILL.md`.

## Optional inspection skills

- Do not invoke `inspection-skill` or `add-inspection-skill` automatically.
- Invoke either Skill only when the user explicitly requests it by name, such as `$inspection-skill` or `$add-inspection-skill`.
- When explicitly invoked, open and follow the corresponding `SKILL.md` under `.agents/skills/`.

## Scoped file search (always active)

- Always apply `.agents/skills/scoped-file-search/SKILL.md` before running any `find`, `grep -r`, `fd`, `rg`, `locate`, or `tree` command.
- Never use `/`, `/home`, `/home/lzy`, `~`, or `$HOME` as a search root. The only allowed search root is the current project directory `/home/lzy/code/IS-Bench`.
- If a file may live outside the project, ask the user for the exact path instead of scanning the home directory.

## Subagent lifecycle

- When subagents are used, close each completed subagent thread as soon as its result has been incorporated into the parent thread.
- After every delegation batch and before the parent agent's final response, inspect the subagent list and close all threads in `Done`.
- Preserve any evidence needed for the final response in the parent thread before closing a subagent; do not keep completed threads open solely as history.
