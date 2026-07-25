"""Send one command to a running ``interactive_manual_loop`` process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("command", choices=("advance", "status", "close"))
    args = parser.parse_args(argv)
    socket_path = Path(args.output_dir).resolve() / "control.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(json.dumps({"command": args.command}).encode("utf-8"))
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    print(json.dumps(json.loads(b"".join(chunks)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
