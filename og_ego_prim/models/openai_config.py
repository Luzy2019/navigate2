import json
import os
from typing import Any, Dict


def get_openai_request_kwargs() -> Dict[str, Any]:
    raw_extra_body = os.environ.get("OPENAI_EXTRA_BODY", "").strip()
    if not raw_extra_body:
        return {}

    try:
        extra_body = json.loads(raw_extra_body)
    except json.JSONDecodeError as exc:
        raise ValueError("OPENAI_EXTRA_BODY must be valid JSON.") from exc

    if not isinstance(extra_body, dict):
        raise ValueError("OPENAI_EXTRA_BODY must decode to a JSON object.")

    return {"extra_body": extra_body}
