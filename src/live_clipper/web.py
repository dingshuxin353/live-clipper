"""Local web console for live-clipper runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import posixpath
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .automation import DEFAULT_LOG_DIR, DEFAULT_STATE_DIR, check_automation_runs, _pid_is_running
from .pipeline import cleanup_local_artifacts, cleanup_plan
from .render_clips import render_selected_clips
from .status import build_run_status
from .utils import read_json, write_json


STATIC_DIR = Path(__file__).parent / "web_static"


@dataclass(frozen=True)
class WebPaths:
    output_root: Path = Path("output")
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    input_dir: Path = Path("input")


def _safe_read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def _count_candidates(path: Path) -> int:
    data = _safe_read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            return len(candidates)
    return 0


def _tail_text(path: Path, *, max_lines: int = 200) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _load_state(paths: WebPaths, run_id: str) -> dict[str, Any]:
    state = _safe_read_json(paths.state_dir / f"{run_id}.json")
    return state if isinstance(state, dict) else {}


def _log_path_for_run(paths: WebPaths, run_id: str, state: dict[str, Any]) -> Path:
    if isinstance(state.get("log_path"), str):
        return Path(state["log_path"])
    return paths.log_dir / f"{run_id}.log"


def _phase_for(status: dict[str, Any], running: bool) -> str:
    files = status["files"]
    if files["selected_clips.json"]["exists"] and files["clips"].get("count", 0) > 0:
        return "cleanup_ready"
    if files["selected_clips.json"]["exists"]:
        return "ready_to_render"
    if files["codex_brief.json"]["exists"]:
        return "needs_codex_selection"
    if running:
        return "running"
    if status["exists"]:
        return "waiting_or_manual"
    return "missing"


def _run_summary(run_dir: Path, paths: WebPaths) -> dict[str, Any]:
    status = build_run_status(run_dir, write_report=False)
    state = _load_state(paths, run_dir.name)
    pid = state.get("pid")
    running = isinstance(pid, int) and _pid_is_running(pid)
    phase = _phase_for(status, running)
    files = status["files"]
    refined_count = _count_candidates(run_dir / "refined_candidates.json")
    merged_count = _count_candidates(run_dir / "merged_candidates.json")
    selected_count = _count_candidates(run_dir / "selected_clips.json")
    log_path = _log_path_for_run(paths, run_dir.name, state)
    updated_at = state.get("updated_at")
    if not updated_at and run_dir.exists():
        updated_at = run_dir.stat().st_mtime

    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "source_name": _source_name(run_dir),
        "phase": phase,
        "next_step": status["next_step"],
        "requires_codex": phase in {"needs_codex_selection", "cleanup_ready"},
        "running": running,
        "pid": pid,
        "candidate_count": refined_count or merged_count,
        "selected_count": selected_count,
        "clip_count": files["clips"].get("count", 0),
        "log_path": str(log_path) if log_path.exists() else None,
        "updated_at": updated_at,
    }


def _source_name(run_dir: Path) -> str:
    metadata = _safe_read_json(run_dir / "run_metadata.json")
    if isinstance(metadata, dict):
        return str(metadata.get("source_name") or metadata.get("source_video_path") or run_dir.name)
    return run_dir.name


def build_runs_index(paths: WebPaths = WebPaths()) -> dict[str, Any]:
    runs = []
    if paths.output_root.exists():
        for run_dir in sorted(paths.output_root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
            if run_dir.is_dir():
                runs.append(_run_summary(run_dir, paths))
    return {
        "ok": True,
        "runs": runs,
        "requires_codex": any(run["requires_codex"] for run in runs),
    }


def build_clip_list(run_dir: Path) -> list[dict[str, Any]]:
    clips_dir = run_dir / "clips"
    if not clips_dir.exists():
        return []
    clips = []
    for path in sorted(clips_dir.glob("*.mp4")):
        clips.append({
            "name": path.name,
            "path": str(path),
            "url": f"/media/runs/{quote(run_dir.name)}/clips/{quote(path.name)}",
            "bytes": path.stat().st_size,
            "updated_at": path.stat().st_mtime,
        })
    return clips


def _cleanup_preview(run_dir: Path, paths: WebPaths) -> dict[str, Any]:
    try:
        targets = cleanup_plan(run_dir, input_dir=paths.input_dir)
    except FileNotFoundError:
        targets = []
    return {
        "targets": targets,
        "deletable_bytes": sum(target["bytes"] for target in targets if target.get("deletable")),
    }


def _step(label: str, file_status: dict[str, Any], *, agnes: bool = False) -> dict[str, Any]:
    done = bool(file_status["exists"])
    return {
        "label": label,
        "done": done,
        "state": "done" if done else "pending",
        "agnes": agnes,
        "path": file_status.get("path"),
        "count": file_status.get("count"),
    }


def _steps_from_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    files = status["files"]
    steps = [
        _step("NAS 录制检测", files["run_metadata.json"]),
        _step("本地复制", files["run_metadata.json"]),
        _step("ASR 语音识别", files["transcript.json"]),
        _step("Agnes 扫描", files["merged_candidates.json"], agnes=True),
        _step("Agnes 精炼", files["refined_candidates.json"], agnes=True),
        _step("Codex 选择", files["selected_clips.json"]),
        _step("渲染导出", files["clips"]),
        _step("清理归档", files["run_metadata.json"]),
    ]
    if not steps[5]["done"] and files["codex_brief.json"]["exists"]:
        steps[5]["state"] = "waiting"
    if files["selected_clips.json"]["exists"] and not files["clips"].get("count", 0):
        steps[6]["state"] = "active"
    return steps


def build_run_detail(run_id: str, paths: WebPaths = WebPaths(), *, log_lines: int = 200) -> dict[str, Any]:
    run_dir = paths.output_root / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        return {"ok": False, "error": f"任务不存在: {run_id}"}
    status = build_run_status(run_dir, write_report=False)
    run = _run_summary(run_dir, paths)
    state = _load_state(paths, run_id)
    log_path = _log_path_for_run(paths, run_id, state)
    files = status["files"]
    cleanup = _cleanup_preview(run_dir, paths)
    can_delete_local_source = any(
        target.get("kind") == "local_source_video" and target.get("deletable")
        for target in cleanup["targets"]
    )
    return {
        "ok": True,
        "run": run,
        "steps": _steps_from_status(status),
        "files": files,
        "clips": build_clip_list(run_dir),
        "cleanup": cleanup,
        "state": state,
        "actions": {
            "can_check": True,
            "can_render": files["selected_clips.json"]["exists"] and files["clips"].get("count", 0) == 0,
            "can_cleanup_preview": files["selected_clips.json"]["exists"],
            "can_cleanup": files["selected_clips.json"]["exists"] and files["clips"].get("count", 0) > 0,
            "can_delete_local_source": can_delete_local_source and files["clips"].get("count", 0) > 0,
        },
        "log": {
            "path": str(log_path) if log_path.exists() else None,
            "tail": _tail_text(log_path, max_lines=log_lines) if log_path.exists() else "",
        },
    }


def handle_api_request(method: str, request_path: str, paths: WebPaths = WebPaths()) -> tuple[int, dict[str, str], Any]:
    parsed_path = urlparse(request_path).path
    parts = [unquote(part) for part in parsed_path.split("/") if part]
    try:
        if method == "GET" and parts == ["api", "runs"]:
            return _json_response(build_runs_index(paths))
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "runs"]:
            return _json_response(build_run_detail(parts[2], paths))
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "log":
            detail = build_run_detail(parts[2], paths)
            return _json_response(detail.get("log", {"tail": ""}) if detail.get("ok") else detail)
        if method == "POST" and parts == ["api", "automation", "check"]:
            return _json_response(check_automation_runs(paths.output_root, state_dir=paths.state_dir))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "render":
            return _json_response(_render_run(paths.output_root / parts[2]))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cleanup-preview":
            return _json_response(_cleanup_run(paths.output_root / parts[2], paths, confirm=False))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cleanup-confirm":
            return _json_response(_cleanup_run(paths.output_root / parts[2], paths, confirm=True))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "delete-local-source":
            return _json_response(_delete_local_source(paths.output_root / parts[2], paths))
        if method == "POST" and len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "clips" and parts[5] == "delete":
            return _json_response(_delete_clip(paths.output_root / parts[2], parts[4]))
    except Exception as exc:  # noqa: BLE001 - local UI should surface actionable failures.
        return _json_response({"ok": False, "error": str(exc)}, status=500)
    return _json_response({"ok": False, "error": "API 路由不存在"}, status=404)


def _json_response(payload: Any, *, status: int = 200) -> tuple[int, dict[str, str], Any]:
    return status, {"Content-Type": "application/json; charset=utf-8"}, payload


def _render_run(run_dir: Path) -> dict[str, Any]:
    selection_path = run_dir / "selected_clips.json"
    if not selection_path.exists():
        return {"ok": False, "error": "缺少 selected_clips.json，暂不能渲染"}
    rendered = render_selected_clips(selection_path)
    return {"ok": True, "rendered": [str(path) for path in rendered]}


def _cleanup_run(run_dir: Path, paths: WebPaths, *, confirm: bool) -> dict[str, Any]:
    report = cleanup_local_artifacts(run_dir, input_dir=paths.input_dir, confirm=confirm)
    return {"ok": True, **report}


def _delete_clip(run_dir: Path, clip_name: str) -> dict[str, Any]:
    clips_dir = run_dir / "clips"
    clip_path = clips_dir / clip_name
    if not _path_is_relative_to(clip_path, clips_dir):
        raise ValueError("切片路径越界，已阻止删除")
    if clip_path.suffix.lower() != ".mp4":
        raise ValueError("只能删除 mp4 成片")
    if not clip_path.exists():
        raise FileNotFoundError(clip_path)
    clip_path.unlink()
    return {"ok": True, "deleted": str(clip_path)}


def _delete_local_source(run_dir: Path, paths: WebPaths) -> dict[str, Any]:
    selected_path = run_dir / "selected_clips.json"
    clips = build_clip_list(run_dir)
    if not selected_path.exists() or not clips:
        raise RuntimeError("尚未检测到已渲染成片，暂不允许删除本机原录像副本")

    targets = cleanup_plan(run_dir, input_dir=paths.input_dir)
    for target in targets:
        if target.get("kind") != "local_source_video" or not target.get("deletable"):
            continue
        source_path = Path(target["path"])
        if not _path_is_relative_to(source_path, paths.input_dir):
            raise ValueError("原录像副本不在受控 input 目录内，已阻止删除")
        source_path.unlink(missing_ok=True)
        metadata_path = run_dir / "run_metadata.json"
        metadata = read_json(metadata_path)
        metadata["pipeline"] = {
            **metadata.get("pipeline", {}),
            "local_source_deleted": True,
            "local_source_deleted_path": str(source_path),
        }
        write_json(metadata_path, metadata)
        return {"ok": True, "deleted": str(source_path), "protected_original": target.get("reason")}
    raise RuntimeError("没有找到可删除的本机原录像副本")


class LiveClipperRequestHandler(BaseHTTPRequestHandler):
    paths = WebPaths()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/" or self.path.startswith("/static/"):
            self._serve_static(head_only=True)
            return
        if self.path.startswith("/media/"):
            self._serve_media(head_only=True)
            return
        status, headers, _payload = handle_api_request("GET", self.path, self.paths)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/" or self.path.startswith("/static/"):
            self._serve_static()
            return
        if self.path.startswith("/media/"):
            self._serve_media()
            return
        self._serve_api("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve_api("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _serve_api(self, method: str) -> None:
        status, headers, payload = handle_api_request(method, self.path, self.paths)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, *, head_only: bool = False) -> None:
        target = STATIC_DIR / "index.html" if self.path == "/" else _static_path(self.path)
        if target is None or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve_media(self, *, head_only: bool = False) -> None:
        clip_path = _media_clip_path(self.path, self.paths)
        if clip_path is None or not clip_path.exists() or not clip_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        file_size = clip_path.stat().st_size
        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        status = HTTPStatus.OK
        if range_header and range_header.startswith("bytes="):
            raw_start, _, raw_end = range_header.removeprefix("bytes=").partition("-")
            start = int(raw_start) if raw_start else 0
            end = int(raw_end) if raw_end else file_size - 1
            end = min(end, file_size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        if head_only:
            return
        with clip_path.open("rb") as file:
            file.seek(start)
            self.wfile.write(file.read(length))


def _static_path(request_path: str) -> Path | None:
    parsed = urlparse(request_path).path
    if not parsed.startswith("/static/"):
        return None
    relative = posixpath.normpath(unquote(parsed[len("/static/"):]))
    if relative.startswith("../") or relative == "..":
        return None
    return STATIC_DIR / relative


def _media_clip_path(request_path: str, paths: WebPaths) -> Path | None:
    parsed = urlparse(request_path).path
    parts = [unquote(part) for part in parsed.split("/") if part]
    if len(parts) != 5 or parts[0] != "media" or parts[1] != "runs" or parts[3] != "clips":
        return None
    run_id = parts[2]
    clip_name = parts[4]
    clips_dir = paths.output_root / run_id / "clips"
    clip_path = clips_dir / clip_name
    if not _path_is_relative_to(clip_path, clips_dir):
        return None
    return clip_path


def run_web_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    paths: WebPaths = WebPaths(),
) -> None:
    handler = type("ConfiguredLiveClipperRequestHandler", (LiveClipperRequestHandler,), {"paths": paths})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"[Web] Live Clipper 控制台已启动: http://{host}:{port}", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("[Web] 警告: 当前服务允许局域网访问。请勿暴露到公网。", flush=True)
    print("[Web] 本服务用于本机工作台。按 Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Web] 已停止本地控制台。", flush=True)
    finally:
        server.server_close()
