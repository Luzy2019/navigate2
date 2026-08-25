import argparse
import os
import sys

from openai import OpenAI

from og_ego_prim.models.openai_config import get_openai_request_kwargs

# 1. check_close_api.py
# [查看文件 (line 10)](/home/lzy/code/IS-Bench/og_ego_prim/cli/check_close_api.py:10)
# 作用：运行闭源模型评测前，检查 OpenAI 兼容 API 是否可用。
# 执行流程：
# 接收 --model。
# 从环境变量读取：OPENAI_API_KEY
# OPENAI_API_BASE

# 创建 OpenAI 客户端。
# 发送一个很小的测试请求：“Reply with OK.”
# 请求成功返回退出码 0，失败返回 2。
# 运行方式：
# python -m og_ego_prim.cli.check_close_api \
#   --model gpt-4o
# 它只检查请求是否成功，并不检查模型回答是否真的等于 OK。
# 这个脚本主要在 [eval_close.sh (line 49)](/home/lzy/code/IS-Bench/entrypoints/eval_close.sh:49) 中被调用，避免仿真加载半天以后才发现 API 配置错误。

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
