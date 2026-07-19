---
name: scoped-file-search
description: Prevent runaway file-system scans that freeze the desktop and spike iowait. Use this skill before running any find, grep -r, fd, rg, locate, or tree command—or any script that may recursively walk directories—to ensure the search is scoped to the current project directory and cannot scan the home directory or filesystem root. Trigger this skill whenever Codex needs to search for files by name, extension, or content, or when a command may traverse directories recursively.
---

# Scoped File Search

Unbounded recursive file searches (`find /home`, `grep -r /`, `fd . ~`) are the single most common cause of desktop freezes in this environment. A single `find /home/lzy` process can saturate disk IO for over 30 minutes, pushing iowait to 17–24% and blocking 12+ processes in D state, which makes input methods and all GUI applications stutter or hang.

This skill enforces a hard scope: **never scan outside the current project directory.**

## Hard rules

1. **Never use `/`, `/home`, `/home/lzy`, `~`, or `$HOME` as a search root.** The only allowed search root is the current project directory (default: `/home/lzy/code/IS-Bench`).
2. **Never run bare `find` without a path argument.** `find` with no path defaults to `.`, which is still dangerous if the current working directory is accidentally the home directory.
3. **Always pass an explicit, project-relative path** as the first argument to `find`, `fd`, `grep -r`, `rg`, or `tree`:
   ```bash
   # ✅ Correct — scoped to project
   find /home/lzy/code/IS-Bench -name "*.py" -path "*mason_jar*"
   fd "mason_jar" /home/lzy/code/IS-Bench
   rg "pattern" /home/lzy/code/IS-Bench

   # ❌ Forbidden — unbounded
   find /home/lzy -name "*.py"
   find ~ -type d -path "*/objects/mason_jar"
   grep -r "pattern" /
   ```
4. **Add `-maxdepth` when the project tree is deep.** This project contains `data/`, `results/`, `bddl/bddl/activity_definitions/`, and other large subtrees. Prefer `-maxdepth 4` unless a deeper search is explicitly justified.
5. **Prefer faster tools over `find`:**
   - `fd` — faster, respects `.gitignore` by default, parallelized.
   - `rg` (ripgrep) — for content search, far faster than `grep -r`.
   - `locate` — for known file names, uses a pre-built index (run `updatedb` if stale).
6. **Exclude heavy directories** when they are not relevant:
   ```bash
   find /home/lzy/code/IS-Bench -name "*.py" \
     -not -path "*/results/*" \
     -not -path "*/.git/*" \
     -not -path "*/data/bddl/*"
   ```
   Or with `fd`:
   ```bash
   fd -E results -E .git -E "data/bddl" "pattern" /home/lzy/code/IS-Bench
   ```

## Pre-flight checklist

Before running any recursive search command, verify:

1. **Path is explicit and project-scoped.** Confirm the first argument starts with `/home/lzy/code/IS-Bench` or `.` (only when CWD is confirmed to be the project root).
2. **No bare home or root paths.** Grep the command string for `~/`, `$HOME`, `/home/lzy` (without the project suffix), or a lone `/`.
3. **Maxdepth is set** if the search does not intentionally need to traverse the entire tree.
4. **Heavy subtrees are excluded** when the target is source code (`og_ego_prim/`, `scripts/`, `entrypoints/`, `api/`) rather than data or results.

## Incident reference

On 2026-07-16, two `find` processes ran for ~27 minutes scanning `/home/lzy`:

```
find /home/lzy -path *objects/mason_jar/gqtsam* -o -path *objects/quiche/fvhqcu*
find /home/lzy -type d -path */objects/mason_jar
```

Impact:
- iowait spiked to 17–24%.
- 12–16 processes stuck in D (uninterruptible IO) state.
- Desktop GUI and input method became nearly unusable.
- The processes were only stopped by `kill -9`.

Root cause: the commands searched the entire home directory, which includes `anaconda3/` (hundreds of thousands of files), Docker overlay layers, and other large data stores.

Lesson: **always scope file searches to the current project directory.** If a file truly may live outside the project, ask the user for the exact path rather than scanning the home directory.

## Quick reference

| Task | Recommended command |
|------|-------------------|
| Find file by name | `fd "name" /home/lzy/code/IS-Bench` |
| Find file by glob | `find /home/lzy/code/IS-Bench -maxdepth 4 -name "*.py"` |
| Search file contents | `rg "pattern" /home/lzy/code/IS-Bench` |
| List directory tree | `tree -L 3 -I 'results|.git|data/bddl' /home/lzy/code/IS-Bench` |
| Find file anywhere (last resort) | `locate "filename"` — uses index, no live traversal |
