#!/usr/bin/env python3
"""Report active context usage for one Codex session rollout."""

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple


CATALOG_CONTEXT_WINDOW = 1_050_000
CATALOG_EFFECTIVE_CONTEXT_WINDOW = 997_500
DEFAULT_WATCH_REMAINING_PCT = 40.0
DEFAULT_COMPACT_REMAINING_PCT = 20.0
DEFAULT_HANDOFF_REMAINING_PCT = 10.0

SESSION_ID_RE = re.compile(
    r"(?im)^\s*-?\s*Full session ID:\s*`?"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"`?\s*$"
)
JSON_SESSION_ID_RE = re.compile(
    r'"(?:session_id|thread_id)"\s*:\s*"'
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r'"',
    re.IGNORECASE,
)
RISK_BAND_RE = re.compile(
    r"(?im)^\s*-?\s*(?:Context )?Risk band:\s*`?(healthy|watch|compact|handoff)`?\s*$"
)


class ContextStatusError(ValueError):
    """Raised when rollout data cannot provide a trustworthy measurement."""


@dataclass(frozen=True)
class Thresholds:
    watch: float = DEFAULT_WATCH_REMAINING_PCT
    compact: float = DEFAULT_COMPACT_REMAINING_PCT
    handoff: float = DEFAULT_HANDOFF_REMAINING_PCT

    def validate(self) -> None:
        if not (0.0 < self.handoff < self.compact < self.watch < 100.0):
            raise ContextStatusError(
                "remaining-percent thresholds must satisfy "
                "0 < handoff < compact < watch < 100"
            )


def reverse_lines(path: Path, block_size: int = 65536) -> Iterable[bytes]:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        remainder = b""
        while position > 0:
            size = min(block_size, position)
            position -= size
            stream.seek(position)
            parts = (stream.read(size) + remainder).split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line
        if remainder:
            yield remainder


def find_rollout(thread_id: str, codex_home: Optional[Path] = None) -> Optional[Path]:
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    candidates = list((home / "sessions").glob(f"*/*/*/*{thread_id}*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def read_session_metadata(path: Path) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContextStatusError(f"cannot read rollout: {exc}") from exc

    with stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            current_id = payload.get("id")
            if isinstance(current_id, str) and current_id:
                metadata["thread_id"] = current_id
            for key in ("cwd", "originator", "source", "thread_source", "model_provider"):
                if key not in metadata and payload.get(key) is not None:
                    metadata[key] = payload.get(key)
            if metadata.get("thread_id"):
                break
    return metadata


def scan_rollout(path: Path) -> Dict[str, Any]:
    token_snapshot: Optional[Dict[str, Any]] = None
    token_timestamp: Optional[str] = None
    compaction_timestamp: Optional[str] = None
    skipped_empty_token_events = 0
    malformed_lines_skipped = 0

    for raw_line in reverse_lines(path):
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed_lines_skipped += 1
            continue

        payload = record.get("payload")
        payload_type = payload.get("type") if isinstance(payload, dict) else None

        if compaction_timestamp is None and (
            record.get("type") == "compacted"
            or (record.get("type") == "event_msg" and payload_type == "context_compacted")
        ):
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                compaction_timestamp = timestamp

        if token_snapshot is not None:
            continue
        if record.get("type") != "event_msg" or payload_type != "token_count":
            continue

        info = payload.get("info")
        if info is None:
            skipped_empty_token_events += 1
            continue
        if not isinstance(info, dict):
            raise ContextStatusError("latest non-empty token_count.info is not an object")
        token_snapshot = info
        timestamp = record.get("timestamp")
        token_timestamp = timestamp if isinstance(timestamp, str) else None

    if token_snapshot is None:
        raise ContextStatusError("valid token_count event not found")

    return {
        "token_info": token_snapshot,
        "snapshot_timestamp": token_timestamp,
        "last_compaction_timestamp": compaction_timestamp,
        "skipped_empty_token_events": skipped_empty_token_events,
        "malformed_lines_skipped": malformed_lines_skipped,
    }


def parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_risk(remaining_pct: float, thresholds: Thresholds) -> str:
    if remaining_pct < thresholds.handoff:
        return "handoff"
    if remaining_pct <= thresholds.compact:
        return "compact"
    if remaining_pct <= thresholds.watch:
        return "watch"
    return "healthy"


def recommended_action(risk_band: str) -> str:
    return {
        "healthy": "continue_current_task",
        "watch": "continue_and_recheck_before_large_phase",
        "compact": "write_or_update_summary_and_suggest_compact",
        "handoff": "write_or_update_summary_and_prepare_new_session",
    }[risk_band]


def extract_summary_metadata(path: Path) -> Tuple[Optional[str], Optional[str]]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    session_match = SESSION_ID_RE.search(content) or JSON_SESSION_ID_RE.search(content)
    risk_match = RISK_BAND_RE.search(content)
    session_id = session_match.group(1).lower() if session_match else None
    risk_band = risk_match.group(1).lower() if risk_match else None
    return session_id, risk_band


def matching_summaries(
    summary_dir: Path, thread_id: str, risk_band: str
) -> Tuple[List[Path], List[Path]]:
    matches: List[Path] = []
    same_risk: List[Path] = []
    if not summary_dir.is_dir():
        return matches, same_risk

    for path in summary_dir.glob("*.md"):
        summary_thread_id, summary_risk = extract_summary_metadata(path)
        if summary_thread_id != thread_id.lower():
            continue
        matches.append(path)
        if summary_risk == risk_band:
            same_risk.append(path)

    key = lambda item: item.stat().st_mtime_ns
    matches.sort(key=key)
    same_risk.sort(key=key)
    return matches, same_risk


def analyze_rollout(
    rollout: Path,
    thread_id: Optional[str] = None,
    thresholds: Optional[Thresholds] = None,
    summary_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    thresholds = thresholds or Thresholds()
    thresholds.validate()
    rollout = rollout.expanduser().resolve()
    if not rollout.is_file():
        raise ContextStatusError(f"rollout file not found: {rollout}")

    metadata = read_session_metadata(rollout)
    metadata_thread_id = metadata.get("thread_id")
    resolved_thread_id = thread_id or metadata_thread_id
    if not isinstance(resolved_thread_id, str) or not resolved_thread_id:
        raise ContextStatusError("session ID unavailable")
    if thread_id and metadata_thread_id and thread_id != metadata_thread_id:
        raise ContextStatusError(
            f"requested thread ID {thread_id} does not match rollout {metadata_thread_id}"
        )

    scanned = scan_rollout(rollout)
    info = scanned["token_info"]
    last_usage = info.get("last_token_usage")
    if not isinstance(last_usage, dict):
        raise ContextStatusError("last_token_usage unavailable")
    last_total = last_usage.get("total_tokens")
    if not isinstance(last_total, int) or isinstance(last_total, bool) or last_total < 0:
        raise ContextStatusError("last_token_usage.total_tokens unavailable")

    runtime_window = info.get("model_context_window")
    if (
        not isinstance(runtime_window, int)
        or isinstance(runtime_window, bool)
        or runtime_window <= 0
    ):
        raise ContextStatusError("model_context_window unavailable")

    cumulative_usage = info.get("total_token_usage")
    cumulative_total = (
        cumulative_usage.get("total_tokens")
        if isinstance(cumulative_usage, dict)
        else None
    )
    if not isinstance(cumulative_total, int) or isinstance(cumulative_total, bool):
        cumulative_total = None

    used_context_tokens = min(last_total, runtime_window)
    remaining_tokens = max(runtime_window - last_total, 0)
    used_context_pct = used_context_tokens / runtime_window * 100.0
    remaining_pct = remaining_tokens / runtime_window * 100.0
    risk_band = classify_risk(remaining_pct, thresholds)

    workspace = metadata.get("cwd")
    default_summary_dir = (
        Path(workspace) / ".codex" / ".compact_context"
        if isinstance(workspace, str) and workspace
        else Path.cwd() / ".codex" / ".compact_context"
    )
    resolved_summary_dir = (summary_dir or default_summary_dir).expanduser().resolve()
    summaries, same_risk_summaries = matching_summaries(
        resolved_summary_dir, resolved_thread_id, risk_band
    )
    summary_due = risk_band in {"compact", "handoff"}

    snapshot_time = parse_iso_timestamp(scanned["snapshot_timestamp"])
    compaction_time = parse_iso_timestamp(scanned["last_compaction_timestamp"])
    if compaction_time is None:
        compaction_status = "not_detected"
    elif snapshot_time is None:
        compaction_status = "timestamp_comparison_unavailable"
    elif snapshot_time >= compaction_time:
        compaction_status = "snapshot_after_compaction"
    else:
        compaction_status = "compaction_after_snapshot"

    return {
        "status": "available",
        "thread_id": resolved_thread_id,
        "session_id_first_4": resolved_thread_id[:4],
        "thread_id_source": "explicit" if thread_id else "rollout_session_meta",
        "rollout_path": str(rollout),
        "workspace_path": workspace,
        "snapshot_timestamp": scanned["snapshot_timestamp"],
        "measurement_boundary": "previous_model_response",
        "snapshot_excludes_unconsumed_tool_output": True,
        "last_token_usage_total_tokens": last_total,
        "used_context_tokens": used_context_tokens,
        "remaining_tokens": remaining_tokens,
        "used_context_pct": round(used_context_pct, 4),
        "remaining_pct": round(remaining_pct, 4),
        "cumulative_session_tokens_diagnostic": cumulative_total,
        "cumulative_tokens_used_for_decision": False,
        "model_context_window": runtime_window,
        "catalog_context_window": CATALOG_CONTEXT_WINDOW,
        "catalog_effective_context_window": CATALOG_EFFECTIVE_CONTEXT_WINDOW,
        "runtime_catalog_mismatch": runtime_window
        != CATALOG_EFFECTIVE_CONTEXT_WINDOW,
        "risk_band": risk_band,
        "summary_due": summary_due,
        "recommended_action": recommended_action(risk_band),
        "thresholds": {
            "watch_remaining_pct": thresholds.watch,
            "compact_remaining_pct": thresholds.compact,
            "handoff_remaining_pct": thresholds.handoff,
        },
        "last_compaction_timestamp": scanned["last_compaction_timestamp"],
        "recent_compaction_status": compaction_status,
        "skipped_empty_token_events": scanned["skipped_empty_token_events"],
        "malformed_lines_skipped": scanned["malformed_lines_skipped"],
        "summary_dir": str(resolved_summary_dir),
        "matching_summary_paths": [str(path) for path in summaries],
        "latest_matching_summary": str(summaries[-1]) if summaries else None,
        "latest_same_risk_summary": (
            str(same_risk_summaries[-1]) if same_risk_summaries else None
        ),
        "summary_write_mode": (
            "update"
            if summary_due and same_risk_summaries
            else "create"
            if summary_due
            else "none"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report active context usage for one Codex session rollout."
    )
    parser.add_argument(
        "--watch-remaining-pct", type=float, default=DEFAULT_WATCH_REMAINING_PCT
    )
    parser.add_argument(
        "--compact-remaining-pct", type=float, default=DEFAULT_COMPACT_REMAINING_PCT
    )
    parser.add_argument(
        "--handoff-remaining-pct", type=float, default=DEFAULT_HANDOFF_REMAINING_PCT
    )
    parser.add_argument("--thread-id")
    parser.add_argument("--rollout", type=Path)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    thresholds = Thresholds(
        watch=args.watch_remaining_pct,
        compact=args.compact_remaining_pct,
        handoff=args.handoff_remaining_pct,
    )

    requested_thread_id = args.thread_id
    rollout = args.rollout
    thread_id_source = "explicit"
    if rollout is None:
        if requested_thread_id is None:
            requested_thread_id = os.environ.get("CODEX_THREAD_ID")
            thread_id_source = "environment"
        if not requested_thread_id:
            print(
                json.dumps(
                    {"status": "unavailable", "error": "CODEX_THREAD_ID is unavailable"}
                )
            )
            return 2
        rollout = find_rollout(requested_thread_id)
        if rollout is None:
            print(
                json.dumps(
                    {
                        "status": "unavailable",
                        "thread_id": requested_thread_id,
                        "error": "rollout file not found",
                    }
                )
            )
            return 2

    try:
        result = analyze_rollout(
            rollout,
            thread_id=requested_thread_id,
            thresholds=thresholds,
        )
    except ContextStatusError as exc:
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "thread_id": requested_thread_id,
                    "rollout_path": str(rollout),
                    "error": str(exc),
                },
                ensure_ascii=True,
            )
        )
        return 2

    if requested_thread_id and thread_id_source == "environment":
        result["thread_id_source"] = "environment"
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
