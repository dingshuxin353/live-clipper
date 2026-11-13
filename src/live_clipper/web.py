"""Local web console for live-clipper runs."""

from __future__ import annotations

import json
import mimetypes
import posixpath
import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import (
    asr_models,
    config_editor,
    jobs,
    migration_coordinator,
    onboarding_coordinator,
    resource_activity,
    service,
)
from .automation import DEFAULT_LOG_DIR, DEFAULT_STATE_DIR
from .config import RecordingSourceDefaultConfig, ServiceConfig, Settings, load_settings
from .first_run_detection import StartupInspection, inspect_startup_state
from .project_api import ProjectAPI
from .project_migration import MigrationError
from .project_result_api import ProjectResultAPI, ResultAPIError
from .project_result_api import _error as _result_error
from .project_storage import ProjectRepository, database_path

STATIC_DIR = Path(__file__).parent / "web_static"


@dataclass(frozen=True)
class WebPaths:
    output_root: Path = Path("output")
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    input_dir: Path = Path("input")
    service_dir: Path = service.DEFAULT_SERVICE_DIR
    config_path: Path = Path("live-clipper.toml")


def _settings_for_paths(paths: WebPaths) -> Settings:
    try:
        loaded = load_settings(paths.config_path)
    except Exception:
        loaded = Settings()
    return Settings(
        legacy_review_removed=loaded.legacy_review_removed,
        cheap_model_api_key=loaded.cheap_model_api_key,
        asr_api_key=loaded.asr_api_key,
        hf_token=loaded.hf_token,
        paths=loaded.paths,
        recording_source=loaded.recording_source,
        service=loaded.service if loaded.service else ServiceConfig(),
        recording_source_default=RecordingSourceDefaultConfig(
            source_id=loaded.recording_source_default.source_id,
            source_dir=loaded.recording_source_default.source_dir,
            input_dir=paths.input_dir,
            output_root=paths.output_root,
            since_hours=loaded.recording_source_default.since_hours,
            min_age_minutes=loaded.recording_source_default.min_age_minutes,
            stable_check_seconds=loaded.recording_source_default.stable_check_seconds,
        ),
        asr=loaded.asr,
        llm=loaded.llm,
        prompts=loaded.prompts,
        privacy=loaded.privacy,
        web=loaded.web,
        review=loaded.review,
        render=loaded.render,
        scheduler=loaded.scheduler,
        review_automation=loaded.review_automation,
    )


def _project_mode_active(paths: WebPaths) -> bool:
    if not database_path(paths.service_dir).exists():
        return False
    with ProjectRepository(paths.service_dir) as repository:
        return repository.get_data_mode() == "projects"


def _startup_inspection(paths: WebPaths) -> StartupInspection:
    return inspect_startup_state(
        service_dir=paths.service_dir,
        config_path=paths.config_path,
        env_path=paths.config_path.parent / ".env",
    )


def _startup_restricted(paths: WebPaths) -> str | None:
    """Return the read-only startup gate for mutating legacy/service routes."""
    return _startup_inspection(paths).restriction


def _is_project_api_route(method: str, parts: list[str], paths: WebPaths) -> bool:
    if parts[:1] != ["api"]:
        return False
    if len(parts) >= 2 and parts[1] in {"project-form-options", "projects", "studio"}:
        return True
    if not _project_mode_active(paths):
        return False
    return len(parts) >= 2 and parts[1] in {
        "clips",
        "runs",
        "outputs",
        "issues",
        "issue-groups",
        "resources",
        "desktop",
        "legacy",
    }


def _structured_error(error_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message, "error": message}


def handle_api_request(
    method: str,
    request_path: str,
    paths: WebPaths | None = None,
    body: dict[str, Any] | None = None,
    *,
    auth_context: str = "browser",
    range_header: str | None = None,
    head_only: bool = False,
    startup: StartupInspection | None = None,
) -> tuple[int, dict[str, str], Any]:
    paths = paths or WebPaths()
    parsed = urlparse(request_path)
    parsed_path = parsed.path
    parts = [unquote(part) for part in parsed_path.split("/") if part]
    try:
        if parts[:2] == ["api", "resources"] and parts[3:] not in (["repair-context"], ["connection"], ["connection-test"]):
            from .resource_api import ResourceAPI

            with ProjectRepository(paths.service_dir) as repository:
                status, payload = ResourceAPI(repository, _settings_for_paths(paths), paths.config_path).dispatch(method, parts, body or {})
            return _json_response(payload, status=status)
        if parts[:2] == ["api", "migration"]:
            coordinator = migration_coordinator.MigrationCoordinator(
                service_dir=paths.service_dir,
                config_path=paths.config_path,
                env_path=paths.config_path.parent / ".env",
                input_dir=paths.input_dir,
                output_root=paths.output_root,
            )
            try:
                routed = coordinator.dispatch(
                    method,
                    request_path,
                    body=body,
                    auth_context=auth_context,
                    startup=startup,
                )
            except MigrationError as exc:
                return _json_response(
                    {
                        "ok": False,
                        "error_code": exc.code,
                        "message": exc.message,
                        "error": {"code": exc.code, "message": exc.message, "fields": exc.fields},
                        "fields": exc.fields,
                    },
                    status=exc.status,
                )
            if routed is not None:
                status, payload = routed
                return _json_response(payload, status=status)
        if parts[:2] == ["api", "onboarding"]:
            coordinator = onboarding_coordinator.OnboardingCoordinator(
                service_dir=paths.service_dir,
                config_path=paths.config_path,
                env_path=paths.config_path.parent / ".env",
                input_dir=paths.input_dir,
                output_root=paths.output_root,
            )
            try:
                routed = coordinator.dispatch(method, request_path, body=body, startup=startup)
            except onboarding_coordinator.OnboardingError as exc:
                return _json_response(
                    {
                        "ok": False,
                        "error_code": exc.code,
                        "message": exc.message,
                        "error": {"code": exc.code, "message": exc.message, "fields": exc.fields},
                        "fields": exc.fields,
                    },
                    status=exc.status,
                )
            if routed is not None:
                status, payload = routed
                return _json_response(payload, status=status)
        if method == "POST" and parts == ["api", "service", "stop"]:
            restricted_entry = _startup_restricted(paths)
            if restricted_entry is not None:
                return _json_response(
                    _structured_error(restricted_entry, "当前数据需要先完成诊断或迁移"),
                    status=409,
                )
        if (
            method in {"GET", "HEAD"}
            and len(parts) == 4
            and parts[:2] == ["api", "outputs"]
            and parts[3] == "media"
            and _project_mode_active(paths)
        ):
            if parsed.query:
                return _json_response(
                    {
                        "ok": False,
                        "error": {
                            "code": "validation_failed",
                            "message": "媒体接口不接受查询参数",
                            "fields": {"query": "不支持 path、file、root 或其他查询参数"},
                        },
                    },
                    status=422,
                )
            with ProjectRepository(paths.service_dir) as repository:
                api = ProjectResultAPI(
                    repository,
                    _settings_for_paths(paths),
                    service_dir=paths.service_dir,
                    auth_context=auth_context,
                    config_path=paths.config_path,
                )
                try:
                    media = api.media(parts[2], range_header, head_only=head_only or method == "HEAD")
                except ResultAPIError as exc:
                    status, headers, payload = _json_response(_result_error(exc), status=exc.status)
                    if exc.code == "range_not_satisfiable" and isinstance(exc.current, dict):
                        headers["Content-Range"] = f"bytes */{exc.current.get('size', 0)}"
                        headers["Accept-Ranges"] = "bytes"
                    return status, headers, payload
            return media.status, media.headers, media.body
        if _is_project_api_route(method, parts, paths) and method not in {"GET", "HEAD"}:
            restricted_entry = _startup_restricted(paths)
            if restricted_entry is not None:
                return _json_response(
                    _structured_error(restricted_entry, "当前数据需要先完成诊断或迁移"),
                    status=409,
                )
        if _is_project_api_route(method, parts, paths):
            api = ProjectAPI(
                paths.service_dir,
                _settings_for_paths(paths),
                auth_context=auth_context,
                config_path=paths.config_path,
            )
            try:
                status, payload = api.handle(method, request_path, body=body)
            finally:
                api.close()
            return _json_response(payload, status=status)
        if method == "POST" and parts == ["api", "service", "stop"]:
            if service.embedded_service_active():
                return _json_response(service.pause_embedded_service())
            return _json_response(service.stop_service(service_dir=paths.service_dir))
        if method == "GET" and parts == ["api", "asr", "models"]:
            settings = _settings_for_paths(paths)
            source = getattr(settings.asr, "model_source", asr_models.DEFAULT_MODEL_SOURCE) if settings.asr else asr_models.DEFAULT_MODEL_SOURCE
            current_backend = settings.asr.backend if settings.asr else None
            current_model = settings.asr.model if settings.asr else None
            return _json_response(
                {
                    "ok": True,
                    "models": asr_models.list_models(
                        paths.service_dir,
                        download_source=source,
                        current_backend=current_backend,
                        current_model=current_model,
                    ),
                    "download_source": source,
                    "current_backend": current_backend,
                    "current_model": current_model,
                    "models_root": str(asr_models.models_root()),
                }
            )
        if method == "GET" and parts == ["api", "config"]:
            payload = config_editor.application_config(paths.config_path)
            return _json_response(payload, status=200 if payload['ok'] else 409)
        if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = jobs.read_job(paths.service_dir, parts[2])
            if job is None:
                return _json_response(_structured_error("job_not_found", "找不到这项模型准备任务。"), status=404)
            return _json_response({"ok": True, "job": job})
    except Exception:  # noqa: BLE001 - API responses must not expose raw exceptions.
        return _json_response(_structured_error("internal_error", "后台服务未能完成此请求。"), status=500)
    return _json_response(_structured_error("route_not_found", "API 路由不存在"), status=404)


def _json_response(payload: Any, *, status: int = 200) -> tuple[int, dict[str, str], Any]:
    return status, {"Content-Type": "application/json; charset=utf-8"}, payload


class LiveClipperRequestHandler(BaseHTTPRequestHandler):
    paths = WebPaths()
    access_token: str | None = None
    restricted_startup: str | None = None

    def _auth_context(self) -> str:
        token = self.access_token
        if not token:
            return "bearer" if self.headers.get("Authorization", "").startswith("Bearer ") else "browser"
        if self.headers.get("Authorization", "") == f"Bearer {token}":
            return "bearer"
        cookie = self.headers.get("Cookie", "")
        return "browser" if f"lc_token={token}" in cookie else "none"

    def _authorized(self) -> bool:
        return self._auth_context() != "none"

    def _reject_unauthorized(self) -> None:
        body = json.dumps(_structured_error("unauthorized", "无法验证与后台服务的连接，请重新打开 Venus。")).encode("utf-8")
        self._emit_response(
            HTTPStatus.UNAUTHORIZED,
            {"Content-Type": "application/json; charset=utf-8"},
            body,
        )

    def _emit_response(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        *,
        head_only: bool = False,
    ) -> None:
        """Emit the complete HTTP response under one client-disconnect boundary."""
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed_path = urlparse(self.path).path
        if parsed_path == "/" or parsed_path.startswith("/static/"):
            self._serve_static(head_only=True)
            return
        if not parsed_path.startswith("/api/"):
            self._serve_spa(head_only=True)
            return
        self._serve_api("HEAD", head_only=True)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed_path = urlparse(self.path).path
        if parsed_path == "/" or parsed_path.startswith("/static/"):
            self._serve_static()
            return
        if not parsed_path.startswith("/api/"):
            self._serve_spa()
            return
        self._serve_api("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve_api("POST")

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve_api("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve_api("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _serve_api(self, method: str, *, head_only: bool = False) -> None:
        if not self._authorized():
            self._reject_unauthorized()
            return
        parsed_path = urlparse(self.path).path
        inspection: StartupInspection | None = None
        if self.restricted_startup:
            inspection = _startup_inspection(self.paths)
            current_restriction = inspection.restriction
            if current_restriction is None:
                self.restricted_startup = None
            elif (
                (method == "GET" and parsed_path == "/api/onboarding")
                or parsed_path == "/api/migration"
                or parsed_path.startswith("/api/migration/")
            ):
                body_payload: dict[str, Any] | None = None
                if method in {"POST", "PATCH", "DELETE"}:
                    raw_length = self.headers.get("Content-Length")
                    length = int(raw_length) if raw_length else 0
                    if length:
                        raw_body = self.rfile.read(length).decode("utf-8")
                        body_payload = json.loads(raw_body) if raw_body else None
                status, headers, payload = handle_api_request(
                    method,
                    self.path,
                    self.paths,
                    body=body_payload,
                    auth_context=self._auth_context(),
                    head_only=head_only,
                    startup=inspection,
                )
            elif current_restriction is not None:
                status, headers, payload = _json_response(
                    _structured_error(current_restriction, "当前数据需要先完成诊断或迁移"),
                    status=409,
                )
            if current_restriction is not None:
                response_body = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                )
                self._emit_response(status, headers, response_body, head_only=head_only)
                return
        body_payload: dict[str, Any] | None = None
        if method in {"POST", "PATCH", "DELETE"}:
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length) if raw_length else 0
            if length:
                raw_body = self.rfile.read(length).decode("utf-8")
                body_payload = json.loads(raw_body) if raw_body else None
        status, headers, payload = handle_api_request(
            method,
            self.path,
            self.paths,
            body=body_payload,
            auth_context=self._auth_context(),
            range_header=self.headers.get("Range"),
            head_only=head_only,
            startup=inspection,
        )
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._emit_response(status, headers, body, head_only=head_only)

    def _serve_static(self, *, head_only: bool = False) -> None:
        target = STATIC_DIR / "react" / "index.html" if self.path == "/" else _static_path(self.path)
        if target is None or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _serve_spa(self, *, head_only: bool = False) -> None:
        target = STATIC_DIR / "react" / "index.html"
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


def _static_path(request_path: str) -> Path | None:
    parsed = urlparse(request_path).path
    if not parsed.startswith("/static/"):
        return None
    relative = posixpath.normpath(unquote(parsed[len("/static/"):]))
    if relative.startswith("../") or relative == "..":
        return None
    return STATIC_DIR / relative


class _LocalHTTPServer(ThreadingHTTPServer):
    request_queue_size = socket.SOMAXCONN


def run_web_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    paths: WebPaths | None = None,
    access_token: str | None = None,
    restricted_startup: str | None = None,
) -> None:
    paths = paths or WebPaths()
    if restricted_startup is None:
        resource_activity.recover_interrupted(paths.service_dir)
    elif restricted_startup == "migration_required":
        migration_coordinator.MigrationCoordinator(
            service_dir=paths.service_dir,
            config_path=paths.config_path,
            env_path=paths.config_path.parent / ".env",
            input_dir=paths.input_dir,
            output_root=paths.output_root,
        ).recover_interrupted()
    handler = type(
        "ConfiguredLiveClipperRequestHandler",
        (LiveClipperRequestHandler,),
        {
            "paths": paths,
            "access_token": access_token or None,
            "restricted_startup": restricted_startup,
        },
    )
    server = _LocalHTTPServer((host, port), handler)
    print(f"[Web] Venus 控制台已启动: http://{host}:{port}", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("[Web] 警告: 当前服务允许局域网访问。请勿暴露到公网。", flush=True)
    print("[Web] 本服务用于本机工作台。按 Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Web] 已停止本地控制台。", flush=True)
    finally:
        server.server_close()
