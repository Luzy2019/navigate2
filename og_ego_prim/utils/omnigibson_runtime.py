import os
from pathlib import Path
import sys
from typing import List


WRAPPED_ENV = "ISBENCH_OMNIGIBSON_X11_FIX"
DISABLE_REEXEC_ENV = "ISBENCH_DISABLE_OG_X11_REEXEC"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _python_invocation() -> List[str]:
    main_module = sys.modules.get("__main__")
    main_spec = getattr(main_module, "__spec__", None)
    module_name = getattr(main_spec, "name", None)
    if module_name:
        return ["-m", module_name, *sys.argv[1:]]
    return list(sys.argv)


def maybe_reexec_with_omnigibson_python() -> bool:
    """Restart the current Python command through the OmniGibson X11-safe wrapper.

    LD_PRELOAD must be present before the process starts, so this cannot be fixed
    by mutating os.environ after importing OmniGibson. Scripts should call this
    before importing omnigibson or modules that import omnigibson.
    """

    if os.environ.get(WRAPPED_ENV) == "1":
        return False
    if os.environ.get(DISABLE_REEXEC_ENV) == "1":
        return False

    wrapper = _repo_root() / "entrypoints" / "omnigibson_python.sh"
    if not wrapper.exists():
        return False

    os.environ["ISBENCH_OMNIGIBSON_REEXEC_ORIGINAL"] = " ".join(sys.argv)
    os.environ["ISBENCH_OMNIGIBSON_PYTHON"] = sys.executable
    os.execvp("bash", ["bash", str(wrapper), *_python_invocation()])
    return True
