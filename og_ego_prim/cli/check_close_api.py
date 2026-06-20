import argparse
import os
import sys

from openai import OpenAI

from og_ego_prim.models.openai_config import get_openai_request_kwargs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    api_base = os.environ.get("OPENAI_API_BASE", "").strip()
    if not api_key or not api_base:
        print(
            "OPENAI_API_KEY and OPENAI_API_BASE must be configured in "
            "entrypoints/env.local.sh.",
            file=sys.stderr,
        )
        return 2

    try:
        client = OpenAI(api_key=api_key, base_url=api_base)
        client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": "Reply with OK."}],
            max_tokens=2,
            temperature=0,
            **get_openai_request_kwargs(),
        )
    except Exception as exc:
        print(f"Close-source API preflight failed: {exc}", file=sys.stderr)
        return 2

    print(f"Close-source API preflight succeeded for model {args.model}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
