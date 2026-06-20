import json
import os
import time
from pathlib import Path

import numpy as np
import torch


def _to_jsonable(value):
    if isinstance(value, torch.Tensor):
        value = value.detach().cpu()
        if value.numel() <= 256:
            return value.tolist()
        return {
            "shape": list(value.shape),
            "sum": float(value.float().sum().item()),
            "min": float(value.float().min().item()),
            "max": float(value.float().max().item()),
        }
    if isinstance(value, np.ndarray):
        if value.size <= 256:
            return value.tolist()
        return {
            "shape": list(value.shape),
            "sum": float(np.sum(value)),
            "min": float(np.min(value)),
            "max": float(np.max(value)),
        }
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, set):
        return [_to_jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    return value


class EvalTraceLogger:
    def __init__(self, root_dir, enabled=True):
        self.root_dir = Path(root_dir)
        self.enabled = enabled
        self.context = {}
        self.episode_dir = None
        if self.enabled:
            os.makedirs(self.root_dir, exist_ok=True)

    def set_context(self, **kwargs):
        for key, value in kwargs.items():
            if value is not None:
                self.context[key] = _to_jsonable(value)
        if "episode_no" in kwargs and kwargs["episode_no"] is not None:
            self.start_episode(kwargs["episode_no"])

    def start_episode(self, episode_no):
        if not self.enabled:
            return
        episode_name = self._episode_name(episode_no)
        if self.episode_dir is not None and self.episode_dir.name == episode_name:
            return
        self.episode_dir = self.root_dir / episode_name
        os.makedirs(self.episode_dir, exist_ok=True)

    def log_event(self, component, action, payload=None):
        if not self.enabled:
            return
        if self.episode_dir is None:
            self.start_episode(self.context.get("episode_no", "unknown"))
        event = {
            "time": time.time(),
            "component": component,
            "action": action,
            "context": dict(self.context),
            "payload": _to_jsonable(payload or {}),
        }
        self._append_jsonl(self.episode_dir / "events.jsonl", event)
        self._append_jsonl(self.episode_dir / f"{component}_events.jsonl", event)

    def write_json(self, filename, payload):
        if not self.enabled:
            return
        if self.episode_dir is None:
            self.start_episode(self.context.get("episode_no", "unknown"))
        path = self.episode_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_to_jsonable(payload), f, ensure_ascii=False, indent=2)

    def _append_jsonl(self, path, event):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _episode_name(self, episode_no):
        try:
            return f"eps_{int(episode_no):06d}"
        except (TypeError, ValueError):
            return f"eps_{episode_no}"
