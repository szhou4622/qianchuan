"""随机本机端口、启动令牌、统一响应与 SSE 事件流。"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .runtime import RuntimeContext
from .timeutils import business_date


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400, details: Any = None, retryable: bool = False):
        self.code = code
        self.message = message
        self.status = status
        self.details = details
        self.retryable = retryable
        super().__init__(message)


class V1AHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        runtime: RuntimeContext,
        launch_token: str,
        wake_token: str,
        frontend_dist: Path,
    ):
        super().__init__(("127.0.0.1", 0), V1ARequestHandler)
        self.runtime = runtime
        self.launch_token = launch_token
        self.wake_token = wake_token
        self.frontend_dist = frontend_dist
        self.window_wake_callback = None


class V1ARequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "QCSCKP-V1A"

    @property
    def app(self) -> V1AHttpServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args) -> None:
        # 启动令牌和本机路径不得进入默认 HTTP 日志。
        return

    def do_GET(self) -> None:
        request_id = f"req_{uuid.uuid4().hex}"
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self._assert_launch_token()
                if parsed.path == "/api/v1/health":
                    self._json_ok(request_id, self.app.runtime.health())
                    return
                if parsed.path == "/api/v1/events":
                    tool_user_id = self._require_admin_session()
                    self._sse(tool_user_id)
                    return
                tool_user_id = self._require_admin_session()
                self._handle_query(request_id, parsed, tool_user_id)
                return
            self._serve_frontend(parsed.path)
        except ApiError as exc:
            self._json_error(request_id, exc)
        except Exception as exc:
            self._json_error(
                request_id,
                ApiError(type(exc).__name__, str(exc), status=500),
            )

    def do_POST(self) -> None:
        request_id = f"req_{uuid.uuid4().hex}"
        try:
            parsed = urllib.parse.urlparse(self.path)
            body = self._read_json()
            if parsed.path == "/api/v1/runtime/wake":
                if str(body.get("wake_token") or "") != self.app.wake_token:
                    raise ApiError("unauthorized", "唤醒令牌无效", status=401)
                callback = self.app.window_wake_callback
                if callable(callback):
                    callback()
                self._json_ok(request_id, {"woken": True})
                return
            self._assert_launch_token()
            if parsed.path == "/api/v1/admin/create":
                created = self.app.runtime.auth.create_initial_admin(
                    str(body.get("username") or ""), str(body.get("password") or "")
                )
                session = self.app.runtime.sessions.issue(created.tool_user_id)
                self._json_ok(
                    request_id,
                    {
                        "job_uid": f"secure_{uuid.uuid4().hex}",
                        "status": "succeeded",
                        "result": {
                            "tool_user_id": created.tool_user_id,
                            "username": created.username,
                            "recovery_code": created.recovery_code,
                            "session_token": session,
                        },
                    },
                    status=201,
                )
                return
            if parsed.path == "/api/v1/admin/login":
                user = self.app.runtime.auth.verify_password(
                    str(body.get("username") or ""), str(body.get("password") or "")
                )
                if not user:
                    raise ApiError("invalid_credentials", "账号或密码错误", status=401)
                session = self.app.runtime.sessions.issue(str(user["tool_user_id"]))
                self._json_ok(
                    request_id,
                    {
                        "job_uid": f"secure_{uuid.uuid4().hex}",
                        "status": "succeeded",
                        "result": {**user, "session_token": session},
                    },
                )
                return
            if parsed.path == "/api/v1/admin/recover":
                replacement = self.app.runtime.auth.reset_password_with_recovery_code(
                    str(body.get("username") or ""),
                    str(body.get("recovery_code") or ""),
                    str(body.get("new_password") or ""),
                )
                self._json_ok(
                    request_id,
                    {
                        "job_uid": f"secure_{uuid.uuid4().hex}",
                        "status": "succeeded",
                        "result": {"replacement_recovery_code": replacement},
                    },
                )
                return
            if parsed.path == "/api/v1/admin/logout":
                token = self.headers.get("X-QCSCKP-Session")
                if token:
                    self.app.runtime.sessions.revoke(token)
                self._json_ok(
                    request_id,
                    {
                        "job_uid": f"secure_{uuid.uuid4().hex}",
                        "status": "succeeded",
                        "result": {"logged_out": True},
                    },
                )
                return
            tool_user_id = self._require_admin_session()
            self._handle_command(request_id, parsed.path, tool_user_id, body)
        except ApiError as exc:
            self._json_error(request_id, exc)
        except Exception as exc:
            status = 409 if isinstance(exc, (ValueError, KeyError)) else 500
            self._json_error(
                request_id,
                ApiError(type(exc).__name__, str(exc), status=status),
            )

    def _handle_query(
        self, request_id: str, parsed: urllib.parse.ParseResult, tool_user_id: str
    ) -> None:
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        runtime = self.app.runtime
        path = parsed.path
        if path == "/api/v1/accounts":
            data = runtime.list_accounts(tool_user_id)
        elif path == "/api/v1/plans":
            data = runtime.list_plans(
                tool_user_id,
                aavid=query.get("aavid"),
                plan_system=query.get("plan_system"),
                promotion_scene=query.get("promotion_scene"),
                keyword=query.get("keyword"),
            )
        elif path == "/api/v1/collections":
            clauses = ["tool_user_id=?"]
            params: list[Any] = [tool_user_id]
            if query.get("aavid"):
                clauses.append("aavid=?")
                params.append(query["aavid"])
            if query.get("target_uid"):
                clauses.append("target_uid=?")
                params.append(query["target_uid"])
            data = runtime.database.query_all(
                "SELECT * FROM collection_run WHERE "
                + " AND ".join(clauses)
                + " ORDER BY started_at DESC LIMIT 500",
                params,
            )
        elif path == "/api/v1/control-tasks":
            clauses = ["tool_user_id=?"]
            params = [tool_user_id]
            if query.get("aavid"):
                clauses.append("aavid=?")
                params.append(query["aavid"])
            if query.get("target_uid"):
                clauses.append("source_plan_id=?")
                params.append(query["target_uid"])
            data = runtime.database.query_all(
                "SELECT * FROM platform_control_task WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC",
                params,
            )
        elif path == "/api/v1/strategies":
            target = query.get("target_uid")
            if not target:
                raise ApiError("missing_target_uid", "缺少 target_uid")
            data = runtime.strategies.list_for_target(tool_user_id, target)
        elif path == "/api/v1/candidates":
            rows = runtime.database.query_all(
                "SELECT * FROM candidate_batch WHERE tool_user_id=? ORDER BY created_at DESC LIMIT 500",
                (tool_user_id,),
            )
            for row in rows:
                row["material_count"] = len(json.loads(str(row["material_snapshot_json"])))
                row.pop("material_snapshot_json", None)
                row.pop("metrics_snapshot_json", None)
            data = rows
        elif path == "/api/v1/execution-tasks":
            data = runtime.database.query_all(
                "SELECT * FROM execution_task WHERE tool_user_id=? ORDER BY created_at DESC LIMIT 500",
                (tool_user_id,),
            )
        elif path == "/api/v1/adjustment-candidates":
            data = runtime.database.query_all(
                """
                SELECT ac.*, p.plan_name, p.plan_system, p.promotion_scene,
                       ct.control_task_id, ct.task_name, ct.assist_task_scene
                FROM adjustment_candidate ac
                JOIN source_plan p ON p.target_uid=ac.target_uid
                JOIN platform_control_task ct ON ct.control_task_uid=ac.control_task_uid
                WHERE ac.tool_user_id=?
                ORDER BY ac.created_at DESC LIMIT 500
                """,
                (tool_user_id,),
            )
        elif path.startswith("/api/v1/candidates/"):
            parts = path.strip("/").split("/")
            if len(parts) != 4 or not parts[3] or parts[3] == "page":
                raise ApiError("invalid_path", "候选分页路径无效", status=404)
            batch_id = parts[3]
            data = runtime.candidates.page(
                tool_user_id,
                batch_id,
                int(query.get("page") or 1),
                int(query.get("page_size") or 20),
            )
        elif path == "/api/v1/feishu/status":
            data = runtime.feishu.status(tool_user_id)
        elif path == "/api/v1/feishu/routes":
            data = runtime.database.query_all(
                "SELECT route_id, route_name, personal_open_id, group_chat_ids_json, enabled, created_at, updated_at FROM feishu_route WHERE tool_user_id=? ORDER BY created_at",
                (tool_user_id,),
            )
        elif path == "/api/v1/operation-events":
            data = runtime.reports.query_events(
                tool_user_id,
                aavid=query.get("aavid"),
                date_from=query.get("date_from"),
                date_to=query.get("date_to"),
                source=query.get("source"),
                action_type=query.get("action_type"),
                result_status=query.get("result_status"),
                keyword=query.get("keyword"),
                limit=int(query.get("limit") or 500),
                offset=int(query.get("offset") or 0),
            )
        elif path == "/api/v1/daily-report":
            data = runtime.reports.daily_summary(
                tool_user_id,
                query.get("business_date") or business_date(),
                query.get("aavid"),
            )
        elif path == "/api/v1/migrations":
            data = {
                "sources": runtime.database.query_all(
                    "SELECT * FROM migration_source ORDER BY modified_at DESC"
                ),
                "runs": runtime.database.query_all(
                    "SELECT * FROM migration_run WHERE tool_user_id=? ORDER BY started_at DESC",
                    (tool_user_id,),
                ),
            }
        elif path == "/api/v1/capabilities":
            data = runtime.adapters.capability_matrix()
        elif path == "/api/v1/adapter-evidence":
            data = runtime.database.query_all(
                """
                SELECT adapter_name, adapter_version, endpoint_path, dataset_key,
                       response_schema_hash, capability_name, capability_state,
                       evidence_level, first_seen_at, last_seen_at
                FROM adapter_evidence
                ORDER BY last_seen_at DESC, adapter_name, capability_name
                LIMIT 500
                """
            )
        elif path == "/api/v1/jobs":
            data = runtime.database.query_all(
                "SELECT * FROM background_job WHERE tool_user_id=? ORDER BY created_at DESC LIMIT 200",
                (tool_user_id,),
            )
        elif path.startswith("/api/v1/jobs/"):
            job_uid = path.rsplit("/", 1)[-1]
            data = runtime.jobs.get(job_uid)
            if not data or data.get("tool_user_id") not in {None, tool_user_id}:
                raise ApiError("job_not_found", "任务不存在", status=404)
        else:
            raise ApiError("not_found", "接口不存在", status=404)
        self._json_ok(request_id, data)

    def _handle_command(
        self,
        request_id: str,
        path: str,
        tool_user_id: str,
        body: dict[str, Any],
    ) -> None:
        runtime = self.app.runtime
        if path == "/api/v1/feishu/config":
            # Secret 绝不进入持久化 background_job payload。
            runtime.feishu.save_credentials(
                tool_user_id,
                str(body.get("app_id") or ""),
                str(body.get("app_secret") or ""),
            )
            job_uid = runtime.jobs.create(
                "feishu_credentials_test", {}, tool_user_id=tool_user_id, priority=20
            )
            self._json_ok(request_id, {"job_uid": job_uid, "status": "queued"}, status=202)
            return
        mapping = {
            "/api/v1/qianchuan/login": ("qianchuan_login", 5),
            "/api/v1/accounts/add": ("qianchuan_add_account", 5),
            "/api/v1/accounts/delete": ("qianchuan_delete_account", 10),
            "/api/v1/accounts/refresh-catalog": ("catalog_refresh", 40),
            "/api/v1/accounts/monitor-setup": ("monitor_setup_save", 20),
            "/api/v1/collections/run": ("target_collect", 40),
            "/api/v1/strategies/save": ("strategy_save", 20),
            "/api/v1/strategies/toggle": ("strategy_toggle", 20),
            "/api/v1/strategies/reorder": ("strategy_reorder", 20),
            "/api/v1/candidates/generate": ("candidate_generate", 30),
            "/api/v1/candidates/groups": ("candidate_group_save", 20),
            "/api/v1/candidates/send-preview": ("candidate_preview_send", 20),
            "/api/v1/feishu/reconnect": ("feishu_reconnect", 20),
            "/api/v1/feishu/binding-code": ("feishu_binding_code", 20),
            "/api/v1/feishu/test-card": ("feishu_test_send", 20),
            "/api/v1/migrations/scan": ("migration_scan", 80),
            "/api/v1/migrations/execute": ("migration_execute", 10),
            "/api/v1/migrations/restore": ("migration_restore", 10),
            "/api/v1/operation-logs/sync": ("operation_log_sync", 50),
            "/api/v1/daily-report/send": ("daily_report_send", 60),
        }
        if path not in mapping:
            raise ApiError("not_found", "命令接口不存在", status=404)
        job_type, priority = mapping[path]
        job_uid = runtime.jobs.create(
            job_type,
            body,
            tool_user_id=tool_user_id,
            priority=priority,
        )
        self._json_ok(request_id, {"job_uid": job_uid, "status": "queued"}, status=202)

    def _assert_launch_token(self) -> None:
        value = self.headers.get("Authorization", "")
        if value != f"Bearer {self.app.launch_token}":
            raise ApiError("unauthorized", "本机启动令牌无效", status=401)

    def _require_admin_session(self) -> str:
        tool_user_id = self.app.runtime.sessions.resolve(
            self.headers.get("X-QCSCKP-Session")
        )
        if not tool_user_id:
            raise ApiError("admin_session_required", "请登录本机管理员", status=401)
        return tool_user_id

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise ApiError("payload_too_large", "请求体过大", status=413)
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            raise ApiError("invalid_json", "请求体不是有效JSON") from exc
        if not isinstance(value, dict):
            raise ApiError("invalid_json", "请求体必须是对象")
        return value

    def _sse(self, _tool_user_id: str) -> None:
        subscriber = self.app.runtime.events.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = subscriber.get(timeout=20)
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.app.runtime.events.unsubscribe(subscriber)

    def _serve_frontend(self, path: str) -> None:
        dist = self.app.frontend_dist
        requested = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (dist / requested).resolve()
        if not str(target).startswith(str(dist.resolve())):
            self.send_error(403)
            return
        if not target.is_file():
            target = dist / "index.html"
        if not target.is_file():
            body = b"<!doctype html><meta charset=utf-8><title>QCSCKP V1A</title><h1>Frontend build is missing</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if target.name == "index.html" else "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def _json_ok(self, request_id: str, data: Any, status: int = 200) -> None:
        self._write_json(status, {"request_id": request_id, "success": True, "data": data, "error": None})

    def _json_error(self, request_id: str, error: ApiError) -> None:
        self._write_json(
            error.status,
            {
                "request_id": request_id,
                "success": False,
                "data": None,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                    "retryable": error.retryable,
                },
            },
        )

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
