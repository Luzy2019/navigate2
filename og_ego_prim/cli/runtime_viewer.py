"""Read-only HTTP server for offline benchmark replay artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit


RUN_MARKERS = frozenset(
    {
        "replay_manifest.json",
        "runtime_timeline.jsonl",
        "report.json",
        "video.mp4",
        "topdown.mp4",
        "replay_camera.mp4",
        "replay_topdown.mp4",
    }
)
KNOWN_MEDIA = {
    "camera": ("replay_camera.mp4", "video.mp4"),
    "topdown": ("replay_topdown.mp4", "topdown.mp4"),
}
MAX_METADATA_BYTES = 64 * 1024 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
OBSERVATION_IMAGE_RE = re.compile(
    r"^obs(?:_\d+)?\.(?:png|jpe?g|webp)$",
    re.IGNORECASE,
)
OBSERVATION_ARTIFACT_DIRECTORIES = frozenset(
    {
        "observations",
        "frame_observations",
    }
)


class RequestProblem(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _resolve_file(root: Path, components: Iterable[str]) -> Optional[Path]:
    candidate = root.joinpath(*components)
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return None
    if not _is_within(resolved, root) or not resolved.is_file():
        return None
    return resolved


def _safe_components(raw_components: Iterable[str]) -> Tuple[str, ...]:
    components: List[str] = []
    for raw in raw_components:
        try:
            value = unquote(raw, errors="strict")
        except UnicodeDecodeError as exc:
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "Invalid URL encoding") from exc
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise RequestProblem(HTTPStatus.BAD_REQUEST, "Invalid path")
        components.append(value)
    return tuple(components)


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise RequestProblem(HTTPStatus.NOT_FOUND, "Artifact not found") from exc
    if size > MAX_METADATA_BYTES:
        raise RequestProblem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON artifact is too large")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RequestProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid JSON artifact") from exc
    if not isinstance(value, Mapping):
        raise RequestProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "JSON artifact must be an object")
    return value


def _nested_value(value: Mapping[str, Any], *paths: Tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                break
            current = current[key]
        else:
            if current is not None:
                return current
    return None


def _file_if_contained(run_dir: Path, name: str) -> Optional[Path]:
    return _resolve_file(run_dir, (name,))


def _is_observation_artifact_directory(path: Path) -> bool:
    """Identify directories that cannot contain independent replay runs."""

    if path.name.casefold() in OBSERVATION_ARTIFACT_DIRECTORIES:
        return True
    try:
        with os.scandir(path) as entries:
            found_image = False
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    return False
                if OBSERVATION_IMAGE_RE.fullmatch(entry.name) is None:
                    return False
                found_image = True
            return found_image
    except OSError:
        # A transiently unreadable directory may still contain a nested run.
        return False


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    directory: Path
    summary: Mapping[str, Any]


class RunCatalog:
    """Discovers replay and legacy result directories below one fixed root."""

    def __init__(self, results_root: Path) -> None:
        self.results_root = results_root.resolve(strict=True)
        if not self.results_root.is_dir():
            raise ValueError(f"results root is not a directory: {self.results_root}")
        self._records: Dict[str, RunRecord] = {}

    def scan(self) -> List[Mapping[str, Any]]:
        records: Dict[str, RunRecord] = {}
        for current, directories, files in os.walk(
            self.results_root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if not (current_path / name).is_symlink()
            ]
            # Observation captures can contain thousands of frame files. Keep
            # every unrecognized child so nested retries / episodes are still
            # discovered, and prune only explicit or image-only artifacts
            # before descending into them (including when this directory is
            # not itself a run).
            directories[:] = [
                name
                for name in directories
                if not _is_observation_artifact_directory(current_path / name)
            ]
            marker_names = RUN_MARKERS.intersection(files)
            if not marker_names:
                continue
            try:
                run_dir = current_path.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError):
                continue
            if not _is_within(run_dir, self.results_root):
                continue
            record = self._build_record(run_dir, marker_names)
            records[record.run_id] = record

        self._records = records
        ordered = sorted(
            records.values(),
            key=lambda item: (item.summary.get("modified_at", ""), item.summary["relative_path"]),
            reverse=True,
        )
        return [item.summary for item in ordered]

    def get(self, run_id: str) -> RunRecord:
        if not re.fullmatch(r"[0-9a-f]{64}", run_id):
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Run not found")
        record = self._records.get(run_id)
        if record is None:
            self.scan()
            record = self._records.get(run_id)
        if record is None:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Run not found")
        return record

    def _build_record(self, run_dir: Path, marker_names: Iterable[str]) -> RunRecord:
        relative = run_dir.relative_to(self.results_root).as_posix()
        relative_label = relative if relative != "." else self.results_root.name
        run_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()
        markers = set(marker_names)

        manifest: Mapping[str, Any] = {}
        report: Mapping[str, Any] = {}
        manifest_path = _file_if_contained(run_dir, "replay_manifest.json")
        report_path = _file_if_contained(run_dir, "report.json")
        if manifest_path is not None:
            try:
                manifest = _read_json(manifest_path)
            except RequestProblem:
                manifest = {}
        if report_path is not None:
            try:
                report = _read_json(report_path)
            except RequestProblem:
                report = {}

        task = _nested_value(
            manifest,
            ("task",),
            ("task_name",),
            ("task_id",),
            ("metadata", "task"),
            ("metadata", "task_name"),
            ("metadata", "task_id"),
        ) or _nested_value(report, ("task",), ("task_name",), ("metadata", "task"))
        scene = _nested_value(
            manifest,
            ("scene",),
            ("scene_name",),
            ("metadata", "scene"),
            ("metadata", "scene_name"),
        ) or _nested_value(report, ("scene",), ("scene_name",), ("metadata", "scene"))
        started_at = _nested_value(
            manifest,
            ("started_at",),
            ("metadata", "started_at"),
            ("run", "started_at"),
        )
        status = _nested_value(
            manifest,
            ("status",),
            ("run", "status"),
        ) or _nested_value(report, ("termination", "reason"), ("status",))

        media: Dict[str, str] = {}
        for role, candidates in KNOWN_MEDIA.items():
            for name in candidates:
                if _file_if_contained(run_dir, name) is not None:
                    media[role] = name
                    break

        marker_timestamps: List[float] = []
        for name in markers:
            marker_path = _file_if_contained(run_dir, name)
            if marker_path is None:
                continue
            try:
                marker_timestamps.append(marker_path.stat().st_mtime)
            except OSError:
                continue
        modified_timestamp = max(marker_timestamps, default=run_dir.stat().st_mtime)
        summary: Dict[str, Any] = {
            "id": run_id,
            "name": run_dir.name,
            "relative_path": relative_label,
            "task": str(task) if task is not None else None,
            "scene": str(scene) if scene is not None else None,
            "started_at": str(started_at) if started_at is not None else None,
            "modified_at": datetime.fromtimestamp(
                modified_timestamp,
                tz=timezone.utc,
            ).isoformat(),
            "status": str(status) if status is not None else None,
            "legacy": manifest_path is None,
            "has_manifest": manifest_path is not None,
            "has_events": _file_if_contained(run_dir, "runtime_timeline.jsonl") is not None,
            "has_report": report_path is not None,
            "media": media,
        }
        return RunRecord(run_id=run_id, directory=run_dir, summary=summary)


def _parse_range(value: str, size: int) -> Tuple[int, int]:
    match = RANGE_RE.fullmatch(value.strip())
    if match is None or size <= 0:
        raise RequestProblem(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise RequestProblem(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range")
    # Do not let unbounded digit strings escape as a ValueError from Python's
    # integer conversion limit.  Treat malformed/oversized ranges uniformly
    # as unsatisfiable requests at the HTTP boundary.
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise RequestProblem(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "Invalid byte range",
                )
            start = max(size - suffix_length, 0)
            return start, size - 1

        start = int(start_text)
        end = size - 1 if not end_text else min(int(end_text), size - 1)
    except ValueError as exc:
        raise RequestProblem(
            HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
            "Invalid byte range",
        ) from exc

    if start >= size:
        raise RequestProblem(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range")
    if end < start:
        raise RequestProblem(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range")
    return start, end


class ReplayViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        results_root: Path,
        viewer_root: Path,
    ) -> None:
        self.results_root = results_root.resolve(strict=True)
        self.viewer_root = viewer_root.resolve(strict=True)
        self.catalog = RunCatalog(self.results_root)
        super().__init__(server_address, handler_class)


class ReplayViewerHandler(BaseHTTPRequestHandler):
    server: ReplayViewerServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        # Browsers commonly cancel speculative media requests while seeking.
        # Treat that expected disconnect as a quiet request termination.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send_problem(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed", send_body=True)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(
            f"[runtime-viewer] {self.address_string()} - {format_string % args}",
            flush=True,
        )

    def _dispatch(self, *, send_body: bool) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                raise RequestProblem(HTTPStatus.BAD_REQUEST, "Query parameters are not supported")
            raw_path = parsed.path
            if raw_path == "/api/runs":
                self._send_json(
                    {"runs": self.server.catalog.scan()},
                    send_body=send_body,
                )
                return
            if raw_path.startswith("/api/runs/"):
                self._serve_run_api(raw_path, send_body=send_body)
                return
            self._serve_viewer_asset(raw_path, send_body=send_body)
        except RequestProblem as exc:
            extra_headers = None
            if exc.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
                extra_headers = {"Accept-Ranges": "bytes"}
            self._send_problem(exc.status, exc.message, send_body=send_body, headers=extra_headers)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_run_api(self, raw_path: str, *, send_body: bool) -> None:
        raw_segments = raw_path.split("/")
        if len(raw_segments) < 5:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "API endpoint not found")
        route = _safe_components(raw_segments[3:5])
        run_id, resource = route
        record = self.server.catalog.get(run_id)

        if resource == "manifest" and len(raw_segments) == 5:
            self._send_json_file(record, "replay_manifest.json", send_body=send_body)
            return
        if resource == "report" and len(raw_segments) == 5:
            self._send_json_file(record, "report.json", send_body=send_body)
            return
        if resource == "events" and len(raw_segments) == 5:
            self._send_events(record, send_body=send_body)
            return
        if resource == "artifacts" and len(raw_segments) >= 6:
            components = _safe_components(raw_segments[5:])
            self._send_artifact(record, components, send_body=send_body)
            return
        raise RequestProblem(HTTPStatus.NOT_FOUND, "API endpoint not found")

    def _send_json_file(self, record: RunRecord, name: str, *, send_body: bool) -> None:
        path = _resolve_file(record.directory, (name,))
        if path is None:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Artifact not found")
        payload = _read_json(path)
        self._send_json(payload, send_body=send_body)

    def _send_events(self, record: RunRecord, *, send_body: bool) -> None:
        path = _resolve_file(record.directory, ("runtime_timeline.jsonl",))
        if path is None:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Artifact not found")
        events: List[Any] = []
        errors: List[Mapping[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        errors.append({"line": line_number, "message": exc.msg})
        except (OSError, UnicodeError) as exc:
            raise RequestProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid event log") from exc
        self._send_json(
            {"events": events, "errors": errors, "count": len(events)},
            send_body=send_body,
        )

    def _send_artifact(
        self,
        record: RunRecord,
        components: Tuple[str, ...],
        *,
        send_body: bool,
    ) -> None:
        path = _resolve_file(record.directory, components)
        if path is None:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Artifact not found")
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header is not None:
            try:
                start, end = _parse_range(range_header, size)
            except RequestProblem as exc:
                self._send_problem(
                    exc.status,
                    exc.message,
                    send_body=send_body,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{size}",
                    },
                )
                return
            status = HTTPStatus.PARTIAL_CONTENT

        content_length = max(end - start + 1, 0)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body or content_length == 0:
            return
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = content_length
            while remaining:
                block = stream.read(min(64 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _serve_viewer_asset(self, raw_path: str, *, send_body: bool) -> None:
        asset_name = {
            "/": "index.html",
            "/index.html": "index.html",
            "/app.css": "app.css",
            "/app.js": "app.js",
        }.get(raw_path)
        if asset_name is None:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Asset not found")
        path = _resolve_file(self.server.viewer_root, (asset_name,))
        if path is None:
            raise RequestProblem(HTTPStatus.NOT_FOUND, "Asset not found")
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self._send_bytes(
            data,
            content_type=content_type,
            send_body=send_body,
            cache_control="no-cache",
        )

    def _send_json(self, value: Any, *, send_body: bool) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(
            data,
            content_type="application/json; charset=utf-8",
            send_body=send_body,
            cache_control="no-store",
        )

    def _send_problem(
        self,
        status: int,
        message: str,
        *,
        send_body: bool,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        data = json.dumps(
            {"error": HTTPStatus(status).phrase, "message": message},
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            self.send_header("Allow", "GET, HEAD")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_bytes(
        self,
        data: bytes,
        *,
        content_type: str,
        send_body: bool,
        cache_control: str,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self._send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )


def create_server(
    results_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    viewer_root: Optional[str | Path] = None,
) -> ReplayViewerServer:
    if host != "127.0.0.1":
        raise ValueError("runtime viewer only accepts --host 127.0.0.1")
    results_path = Path(results_root).expanduser().resolve(strict=True)
    assets_path = (
        Path(viewer_root).expanduser().resolve(strict=True)
        if viewer_root is not None
        else Path(__file__).resolve().parents[2] / ".website"
    )
    if not assets_path.is_dir():
        raise ValueError(f"viewer assets directory does not exist: {assets_path}")
    required_assets = ("index.html", "app.css", "app.js")
    missing = [name for name in required_assets if _resolve_file(assets_path, (name,)) is None]
    if missing:
        raise ValueError(f"viewer assets are incomplete: {', '.join(missing)}")
    return ReplayViewerServer(
        (host, port),
        ReplayViewerHandler,
        results_root=results_path,
        viewer_root=assets_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Browse completed IS-Bench replay artifacts")
    parser.add_argument(
        "--results-root",
        default="results",
        help="Read-only root containing benchmark result directories",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        server = create_server(args.results_root, args.host, args.port)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"runtime viewer: {exc}") from exc
    host, port = server.server_address[:2]
    print(f"Runtime viewer: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
