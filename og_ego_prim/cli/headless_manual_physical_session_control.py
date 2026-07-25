"""Send a command to a running persistent physical manual session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("command", choices=("advance", "status", "checkpoint", "restore", "close"))
    parser.add_argument("--perception-json")
    parser.add_argument("--frame-index", type=int)
    args = parser.parse_args(argv)
    if args.command == "advance" and not args.perception_json:
        parser.error("advance requires --perception-json")
    if args.command == "restore" and args.frame_index is None:
        parser.error("restore requires --frame-index")
    payload = {
        "command": args.command,
        "perception_json": args.perception_json,
        "frame_index": args.frame_index,
    }
    socket_path = Path(args.session_dir).resolve() / "control.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload).encode("utf-8"))
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
