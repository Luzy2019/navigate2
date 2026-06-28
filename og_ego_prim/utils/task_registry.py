import json
from pathlib import Path
from typing import Iterable, List

from og_ego_prim.utils.constants import TASKS


def iter_task_config_paths(include_subdirs: bool = True) -> List[Path]:
    tasks_root = Path(TASKS)
    pattern = "**/*.json" if include_subdirs else "*.json"
    return sorted(
        path
        for path in tasks_root.glob(pattern)
        if path.is_file() and "test" not in path.name
    )


def task_spec_to_name(task_spec: str) -> str:
    task = task_spec.strip()
    if task.endswith(".json"):
        task = task[:-5]

    for prefix in ("./data/tasks/", "data/tasks/"):
        if task.startswith(prefix):
            task = task[len(prefix) :]

    return task


def get_task_config_path(task_spec: str) -> Path:
    tasks_root = Path(TASKS)
    raw_spec = Path(task_spec)

    path_candidates = []
    if raw_spec.suffix == ".json":
        path_candidates.append(raw_spec)
        if not raw_spec.is_absolute():
            path_candidates.append(tasks_root / raw_spec)
    else:
        task_name = task_spec_to_name(task_spec)
        path_candidates.extend(
            [
                tasks_root / f"{task_name}.json",
                tasks_root / f"{task_name}.json".lstrip("/"),
            ]
        )

    for candidate in path_candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    task_name = task_spec_to_name(task_spec)
    matching_paths = [
        path
        for path in iter_task_config_paths(include_subdirs=True)
        if path.stem == task_name or str(path.relative_to(tasks_root).with_suffix("")) == task_name
    ]
    if len(matching_paths) == 1:
        return matching_paths[0].resolve()
    if len(matching_paths) > 1:
        matches = ", ".join(str(path.relative_to(tasks_root)) for path in matching_paths)
        raise ValueError(f'ambiguous task config "{task_spec}": {matches}')

    for path in iter_task_config_paths(include_subdirs=True):
        with path.open("r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("task_info", {}).get("task_name") == task_name:
            return path.resolve()

    raise FileNotFoundError(f'invalid task config "{task_spec}"')


def resolve_task_specs(task_specs: Iterable[str]) -> List[Path]:
    return [get_task_config_path(task_spec) for task_spec in task_specs]
