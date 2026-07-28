"""Run one deterministic frame-14 prompt-ablation branch to completion.

The script deliberately constructs ``PersistentPhysicalSession`` directly
instead of using the Unix-socket service.  One invocation owns exactly one
fresh simulator process and one fixed branch directory, making the six formal
runs independent and avoiding socket-path-length coupling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK = "lifelong_crossroom__beechwood__jar_seal_status_after_canning_v3"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs/manual_sessions/jar_seal_status_v3/ablation"
DEFAULT_BASELINE = (
    PROJECT_ROOT
    / "outputs/manual_sessions/jar_seal_status_v3/011/checkpoint/frame_000014.pt"
)
DEFAULT_ANNOTATION = (
    PROJECT_ROOT
    / "outputs/manual_sessions/jar_seal_status_v3/009/annotations/frame_000014.json"
)
DEFAULT_CONFIG = PROJECT_ROOT / "entrypoints/configs/eval_safe_memory_jar_seal.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-setting", required=True, choices=("v0", "v1", "v2"))
    parser.add_argument("--replicate", required=True, choices=("r1", "r2"))
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--baseline-checkpoint", default=str(DEFAULT_BASELINE))
    parser.add_argument("--annotation", default=str(DEFAULT_ANNOTATION))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--planner-work-dir", default=str(PROJECT_ROOT / "results"))
    parser.add_argument("--max-actions", type=int, default=72)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Recover only this existing branch from its latest checkpoint.",
    )
    return parser


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _branch_dir(args: argparse.Namespace) -> Path:
    return Path(args.output_root).resolve() / args.prompt_setting / args.replicate


def _prepare_branch_dir(path: Path, *, resume: bool) -> None:
    if path.exists() and any(path.iterdir()) and not resume:
        raise FileExistsError(
            "refusing to mix a formal run with existing artifacts: "
            f"{path}; choose the other fixed replicate or use --resume"
        )
    path.mkdir(parents=True, exist_ok=True)


def _result_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    raise TypeError(f"unexpected lifelong result type: {type(value)!r}")


def _verify_completed_branch(session: Any, args: argparse.Namespace, session_dir: Path) -> Dict[str, Any]:
    state = session.session
    if state.get("status") != "completed":
        raise RuntimeError(f"session did not reach completed: {state.get('status')!r}")
    completion = state.get("completion") or {}
    results = [_result_dict(value) for value in session.lifelong_evaluator.results]
    if len(results) != 3 or not all(bool(result.get("safe_success")) for result in results):
        raise RuntimeError(f"lifelong evaluator did not accept all subtasks: {results!r}")
    subtask_ids = {int(record.get("subtask_index", -1)) for record in state["completed_actions"]}
    if subtask_ids != {1, 2, 3}:
        raise RuntimeError(f"completed actions do not cover all subtasks: {sorted(subtask_ids)}")
    if not bool(completion.get("all_safe_success")):
        raise RuntimeError(f"invalid completion payload: {completion!r}")
    if state.get("prompt_setting") != args.prompt_setting:
        raise RuntimeError("session prompt setting drifted")
    if not bool(state.get("risk_predictor_disabled")):
        raise RuntimeError("session did not retain risk_predictor=None")

    prompt_path = session_dir / "prompt.txt"
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
    required_prompt_fields = (
        f"prompt_setting: {args.prompt_setting}",
        "===== MODEL CALL =====",
        "exact_prompt:",
        "raw_output:",
    )
    if not prompt_text or any(field not in prompt_text for field in required_prompt_fields):
        raise RuntimeError(f"prompt log is missing real model-call evidence: {prompt_path}")

    llm_paths = sorted(session_dir.glob("llm_*.txt"))
    risk_logs = []
    for path in llm_paths:
        payload = _read_json(path)
        risk = payload.get("risk_predictor") or {}
        if risk.get("raw_prompt") is not None or risk.get("raw_response") is not None:
            risk_logs.append(str(path))
    if risk_logs:
        raise RuntimeError(f"risk predictor was called despite None: {risk_logs}")

    frame_index = int((state.get("current_frame") or {}).get("frame_index", -1))
    final_checkpoint = session_dir / "checkpoint" / f"frame_{frame_index:06d}.pt"
    if not final_checkpoint.is_file():
        raise RuntimeError(f"final immutable checkpoint is missing: {final_checkpoint}")
    return {
        "status": "completed",
        "completed_action_count": len(state["completed_actions"]),
        "final_frame_index": frame_index,
        "final_checkpoint": str(final_checkpoint),
        "prompt_log": str(prompt_path),
        "llm_log_count": len(llm_paths),
        "subtask_results": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_actions < 1:
        raise ValueError("--max-actions must be positive")
    session_dir = _branch_dir(args)
    _prepare_branch_dir(session_dir, resume=args.resume)
    baseline = Path(args.baseline_checkpoint).resolve()
    annotation = Path(args.annotation).resolve()
    config = Path(args.config).resolve()
    if not baseline.is_file():
        raise FileNotFoundError(f"baseline checkpoint does not exist: {baseline}")
    if not annotation.is_file():
        raise FileNotFoundError(f"annotation does not exist: {annotation}")
    if not config.is_file():
        raise FileNotFoundError(f"config does not exist: {config}")
    baseline_manifest = baseline.with_suffix(".json")
    baseline_info = _read_json(baseline_manifest) if baseline_manifest.is_file() else {}
    if int(baseline_info.get("frame_index", 14)) != 14:
        raise ValueError(f"baseline is not frame 14: {baseline_manifest}")

    manifest_path = session_dir / "run_manifest.json"
    manifest: Dict[str, Any] = {
        "schema_version": "isbench.manual_prompt_ablation.v1",
        "task": args.task,
        "prompt_setting": args.prompt_setting,
        "replicate": args.replicate,
        "session_dir": str(session_dir),
        "baseline_checkpoint": str(baseline),
        "baseline_sha256": _sha256(baseline),
        "baseline_frame_index": int(baseline_info.get("frame_index", 14)),
        "baseline_completed_action_count": int(
            baseline_info.get("completed_action_count", 14)
        ),
        "annotation": str(annotation),
        "config": str(config),
        "risk_predictor": None,
        "risk_predictor_disabled": True,
        "formal_start_from_frame_14": not args.resume,
        "status": "initializing",
    }
    if args.resume and manifest_path.is_file():
        previous = _read_json(manifest_path)
        manifest["recovery_of"] = previous.get("status")
    _write_json(manifest_path, manifest)

    session = None
    try:
        from og_ego_prim.cli.headless_manual_physical_session import (
            PersistentPhysicalSession,
            build_parser as build_session_parser,
        )

        session_argv = [
            "--task", args.task,
            "--session-dir", str(session_dir),
            "--config", str(config),
            "--model", args.model,
            "--planner-work-dir", str(Path(args.planner_work_dir).resolve()),
            "--prompt-setting", args.prompt_setting,
            "--disable-risk-predictor",
        ]
        if not args.resume:
            session_argv.extend(("--restore-checkpoint", str(baseline)))
        session = PersistentPhysicalSession(build_session_parser().parse_args(session_argv))
        restored = session.session.get("current_frame") or {}
        if not args.resume and int(restored.get("frame_index", -1)) != 14:
            raise RuntimeError(f"branch did not start from frame 14: {restored!r}")
        manifest.update(
            status="running",
            restored_frame_index=int(restored.get("frame_index", -1)),
            restored_completed_action_count=len(session.session.get("completed_actions") or ()),
            snapshot_branch_source=session.session.get("snapshot_branch_source"),
        )
        _write_json(manifest_path, manifest)

        for action_index in range(1, args.max_actions + 1):
            response = session.advance(str(annotation))
            manifest["last_advance"] = {
                "ordinal": action_index,
                "status": response.get("status"),
                "executed_action": response.get("executed_action"),
                "subtask_index": response.get("subtask_index"),
            }
            _write_json(manifest_path, manifest)
            if response.get("status") == "completed":
                verification = _verify_completed_branch(session, args, session_dir)
                manifest.update(status="completed", verification=verification)
                _write_json(manifest_path, manifest)
                return 0
            if response.get("status") != "waiting_for_annotation":
                raise RuntimeError(f"branch stopped before completion: {response!r}")
        raise RuntimeError(f"branch exceeded --max-actions={args.max_actions}")
    except Exception as exc:
        manifest.update(status="failed", error=f"{exc.__class__.__name__}: {exc}")
        _write_json(manifest_path, manifest)
        print(manifest["error"], file=sys.stderr)
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as close_exc:
                manifest.setdefault("close_error", f"{close_exc.__class__.__name__}: {close_exc}")
                _write_json(manifest_path, manifest)


if __name__ == "__main__":
    raise SystemExit(main())
