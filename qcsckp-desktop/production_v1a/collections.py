"""可信目录、视频素材与调控任务采集落库。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .adapters import AdapterRegistry
from .adapters.models import NormalizedControlTask, NormalizedMaterial, NormalizedPlan, PaginatedResult
from .security import redact_mapping, stable_json_hash
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .timeutils import BEIJING, beijing_iso, business_date, utc_iso


def account_uid(tool_user_id: str, aavid: str) -> str:
    return "acct_" + stable_json_hash([tool_user_id, str(aavid)])[:24]


def target_uid(tool_user_id: str, aavid: str, ad_id: str) -> str:
    return "target_" + stable_json_hash([tool_user_id, str(aavid), str(ad_id)])[:24]


def material_uid(tool_user_id: str, aavid: str, ad_id: str, material_id: str) -> str:
    return "material_" + stable_json_hash(
        [tool_user_id, str(aavid), str(ad_id), str(material_id)]
    )[:24]


def product_uid(tool_user_id: str, aavid: str, ad_id: str, product_id: str) -> str:
    return "product_" + stable_json_hash(
        [tool_user_id, str(aavid), str(ad_id), str(product_id)]
    )[:24]


def _normalize_platform_time(value: Any) -> tuple[str, str]:
    current: datetime
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            number = float(value)
            if number > 10_000_000_000:
                number /= 1000
            current = datetime.fromtimestamp(number, tz=timezone.utc)
        else:
            text = str(value or "").strip().replace("Z", "+00:00")
            current = datetime.fromisoformat(text)
            if current.tzinfo is None:
                current = current.replace(tzinfo=BEIJING)
    except Exception:
        current = datetime.now(timezone.utc)
    return utc_iso(current), beijing_iso(current)


def _classify_operation(content: str) -> str:
    normalized = content.lower()
    is_retarget = any(token in normalized for token in ("追投", "加热", "retarget"))
    if is_retarget and any(token in normalized for token in ("停投", "暂停", "pause")):
        return "retarget_pause"
    rules = (
        (("预算", "budget"), "budget_update"),
        (("延长", "时长", "duration"), "duration_update"),
        (("roi",), "roi_update"),
        (("出价", "bid"), "bid_update"),
        (("新建", "create"), "plan_create"),
        (("复制", "copy"), "plan_copy"),
        (("删除", "delete"), "plan_delete"),
        (("启用", "开启", "enable"), "plan_enable"),
        (("暂停", "pause"), "plan_pause"),
        (("追投", "加热", "retarget"), "retarget_create"),
    )
    for tokens, action in rules:
        if any(token in normalized for token in tokens):
            return action
    return "other"


def control_task_uid(
    tool_user_id: str, aavid: str, ad_id: str, control_task_id: str
) -> str:
    return "control_" + stable_json_hash(
        [tool_user_id, str(aavid), str(ad_id), str(control_task_id)]
    )[:24]


ACTIVE_PLATFORM_STATUSES = {
    "1",
    "active",
    "running",
    "in_delivery",
    "投放中",
    "启用",
}


class CollectionService:
    def __init__(
        self,
        database: RuntimeDatabase,
        writer: StorageWriter,
        adapters: AdapterRegistry,
    ):
        self.database = database
        self.writer = writer
        self.adapters = adapters

    def add_or_refresh_account(self, tool_user_id: str, aavid: str) -> dict[str, Any]:
        identity = self.adapters.all()[0].fetch_account_identity(str(aavid))
        uid = account_uid(tool_user_id, identity.aavid)
        now = utc_iso()
        self.writer.execute(
            """
            INSERT INTO advertiser_account(
                account_uid, tool_user_id, aavid, account_name, enabled,
                daily_report_enabled, catalog_status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 0, 0, 'not_synced', ?, ?)
            ON CONFLICT(tool_user_id, aavid) DO UPDATE SET
                account_name=excluded.account_name,
                removed_at=NULL,
                updated_at=excluded.updated_at
            """,
            (uid, tool_user_id, identity.aavid, identity.account_name, now, now),
        )
        return {
            "account_uid": uid,
            "tool_user_id": tool_user_id,
            "aavid": identity.aavid,
            "account_name": identity.account_name,
        }

    def refresh_catalog(
        self,
        tool_user_id: str,
        aavid: str,
        progress=None,
    ) -> dict[str, Any]:
        account = self.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=? AND removed_at IS NULL",
            (tool_user_id, str(aavid)),
        )
        if not account:
            account = self.add_or_refresh_account(tool_user_id, str(aavid))
        else:
            # 每次目录刷新都重新读取权威 advName，禁止使用店铺名或缓存名称。
            account = {**account, **self.add_or_refresh_account(tool_user_id, str(aavid))}

        results: list[tuple[Any, PaginatedResult, str]] = []
        adapters = self.adapters.all()
        for index, adapter in enumerate(adapters, start=1):
            if progress:
                progress(index - 1, len(adapters), f"读取{adapter.plan_system}·{adapter.promotion_scene}目录")
            run_id = self._start_run(
                tool_user_id,
                str(aavid),
                None,
                f"plan_catalog:{adapter.plan_system}:{adapter.promotion_scene}",
                stable_json_hash(
                    {
                        "dataset": adapter.plan_dataset,
                        "mar_goal": adapter.mar_goal,
                    }
                ),
                adapter.adapter_version,
            )
            try:
                result = adapter.discover_plans(str(aavid))
            except Exception as exc:
                result = PaginatedResult(
                    rows=(),
                    platform_total_count=None,
                    expected_pages=None,
                    successful_pages=0,
                    failed_pages=(1,),
                    raw_count=0,
                    unique_count=0,
                    duplicate_count=0,
                    status="schema_changed" if "schema_changed" in str(exc) else "failed",
                    platform_server_time=None,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
            self._finish_run(run_id, result)
            self._record_adapter_evidence(
                adapter,
                result,
                endpoint_path=adapter.PLAN_ENDPOINT,
                dataset_key=adapter.plan_dataset,
                capability_name="read_catalog",
            )
            results.append((adapter, result, run_id))

        statuses = [result.status for _, result, _ in results]
        complete = all(status == "complete" for status in statuses)
        all_plans: dict[str, list[NormalizedPlan]] = {}
        for _adapter, result, _run_id in results:
            for plan in result.rows:
                all_plans.setdefault(plan.ad_id, []).append(plan)

        normalized: list[NormalizedPlan] = []
        for ad_id, variants in all_plans.items():
            unique_identity = {(v.plan_system, v.promotion_scene) for v in variants}
            if len(unique_identity) > 1:
                primary = variants[0]
                normalized.append(
                    NormalizedPlan(
                        aavid=primary.aavid,
                        ad_id=ad_id,
                        plan_name=primary.plan_name,
                        plan_system="unknown",
                        promotion_scene="unknown",
                        platform_status=primary.platform_status,
                        verification_state="conflict",
                        adapter_version=primary.adapter_version,
                        raw_evidence={"conflicting_adapters": sorted(unique_identity)},
                    )
                )
            else:
                normalized.append(variants[0])

        # 若历史已有计划而本轮四类均返回0，视为异常空，不清空旧目录。
        previous_count = self.database.query_one(
            "SELECT COUNT(*) AS c FROM source_plan WHERE tool_user_id=? AND aavid=?",
            (tool_user_id, str(aavid)),
        )
        suspicious_empty = bool(
            complete
            and int((previous_count or {}).get("c", 0)) > 0
            and not normalized
        )
        catalog_status = (
            "suspicious_empty"
            if suspicious_empty
            else "complete"
            if complete
            else "partial"
        )
        if not suspicious_empty:
            self._upsert_catalog_plans(tool_user_id, str(aavid), normalized, mark_missing=complete)
        self.writer.execute(
            """
            UPDATE advertiser_account
            SET catalog_status=?, catalog_completed_at=?, updated_at=?
            WHERE tool_user_id=? AND aavid=?
            """,
            (
                catalog_status,
                utc_iso() if complete and not suspicious_empty else None,
                utc_iso(),
                tool_user_id,
                str(aavid),
            ),
        )
        if progress:
            progress(len(adapters), len(adapters), "目录刷新完成" if complete else "目录不完整，保留上次完整结果")
        return {
            "aavid": str(aavid),
            "account_name": account["account_name"],
            "status": catalog_status,
            "plan_count": len(normalized),
            "adapter_results": [
                {
                    "adapter": adapter.adapter_name,
                    "status": result.status,
                    "unique_count": result.unique_count,
                    "failed_pages": list(result.failed_pages),
                    "collection_run_id": run_id,
                }
                for adapter, result, run_id in results
            ],
        }

    def collect_materials(
        self, tool_user_id: str, target: dict[str, Any]
    ) -> dict[str, Any]:
        if int(target.get("monitor_enabled") or 0) != 1:
            raise ValueError("target_not_monitored")
        if int(target.get("monitor_eligible") or 0) != 1:
            raise ValueError("target_not_eligible")
        adapter = self.adapters.get(str(target["plan_system"]), str(target["promotion_scene"]))
        run_id = self._start_run(
            tool_user_id,
            str(target["aavid"]),
            str(target["target_uid"]),
            "video_material",
            stable_json_hash({"material_type": "video", "business_date": business_date()}),
            adapter.adapter_version,
        )
        result = adapter.fetch_materials(str(target["aavid"]), str(target["ad_id"]))
        self._finish_run(run_id, result)
        self._record_adapter_evidence(
            adapter,
            result,
            endpoint_path=adapter.MATERIAL_ENDPOINT,
            dataset_key=adapter.material_dataset,
            capability_name="read_video_material",
        )
        if result.status != "complete":
            return {"collection_run_id": run_id, "status": result.status, "persisted": 0}
        self._persist_material_batch(tool_user_id, target, run_id, list(result.rows))
        return {
            "collection_run_id": run_id,
            "status": "complete",
            "persisted": result.unique_count,
            "videos_only": True,
        }

    def collect_control_tasks(
        self, tool_user_id: str, target: dict[str, Any]
    ) -> dict[str, Any]:
        adapter = self.adapters.get(str(target["plan_system"]), str(target["promotion_scene"]))
        total = 0
        statuses: dict[int, str] = {}
        for scene in (1, 2, 3):
            run_id = self._start_run(
                tool_user_id,
                str(target["aavid"]),
                str(target["target_uid"]),
                f"control_task:scene_{scene}",
                stable_json_hash({"assist_task_scene": scene}),
                adapter.adapter_version,
            )
            result = adapter.fetch_control_tasks(
                str(target["aavid"]), str(target["ad_id"]), scene
            )
            self._finish_run(run_id, result)
            self._record_adapter_evidence(
                adapter,
                result,
                endpoint_path=adapter.CONTROL_ENDPOINT,
                dataset_key=adapter.control_dataset(scene),
                capability_name=f"read_control_task_scene_{scene}",
            )
            statuses[scene] = result.status
            if result.status == "complete":
                self._persist_control_tasks(tool_user_id, target, run_id, list(result.rows))
                total += result.unique_count
        return {"status_by_scene": statuses, "persisted": total}

    def collect_operation_logs(
        self,
        tool_user_id: str,
        aavid: str,
        *,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        """按账户同步真实平台日志；浏览器访问轨迹永不在此生成。"""

        account = self.database.query_one(
            "SELECT * FROM advertiser_account WHERE tool_user_id=? AND aavid=? AND removed_at IS NULL",
            (tool_user_id, aavid),
        )
        if not account:
            raise KeyError(aavid)
        plans = self.database.query_all(
            "SELECT * FROM source_plan WHERE tool_user_id=? AND aavid=? AND verification_state='verified'",
            (tool_user_id, aavid),
        )
        persisted = 0
        statuses: dict[str, str] = {}
        for target in plans:
            adapter = self.adapters.get(
                str(target["plan_system"]), str(target["promotion_scene"])
            )
            run_id = self._start_run(
                tool_user_id,
                aavid,
                str(target["target_uid"]),
                "operation_log",
                stable_json_hash([start_time, end_time, target["ad_id"]]),
                adapter.adapter_version,
                run_business_date=start_time[:10],
            )
            rows: list[dict[str, Any]] = []
            failed_pages: list[int] = []
            platform_time = None
            total_count = None
            page = 1
            try:
                while page <= 100:
                    result = adapter.fetch_operation_logs(
                        aavid, str(target["ad_id"]), start_time, end_time, page
                    )
                    rows.extend(result.rows)
                    platform_time = result.platform_server_time or platform_time
                    total_count = result.total_count if result.total_count is not None else total_count
                    if not result.has_more:
                        break
                    page += 1
            except Exception:
                failed_pages.append(page)
            status = "complete" if not failed_pages else "partial"
            summary = PaginatedResult(
                rows=tuple(rows),
                platform_total_count=total_count,
                expected_pages=None,
                successful_pages=page - len(failed_pages),
                failed_pages=tuple(failed_pages),
                raw_count=len(rows),
                unique_count=len(rows),
                duplicate_count=0,
                status=status,
                platform_server_time=platform_time,
                error_code="operation_log_page_failed" if failed_pages else None,
                error_message="平台日志存在缺页" if failed_pages else None,
            )
            self._finish_run(run_id, summary)
            if rows:
                persisted += self._persist_operation_logs(
                    tool_user_id, account, target, rows
                )
            statuses[str(target["target_uid"])] = status
        return {"aavid": aavid, "persisted": persisted, "status_by_plan": statuses}

    def _persist_operation_logs(
        self,
        tool_user_id: str,
        account: dict[str, Any],
        target: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> int:
        now = utc_iso()

        def op(conn):
            count = 0
            with short_transaction(conn):
                for raw in rows:
                    platform_log_id = str(
                        raw.get("logId")
                        or raw.get("optLogId")
                        or raw.get("id")
                        or stable_json_hash(raw)
                    )
                    raw_time = raw.get("operateTime") or raw.get("optTime") or raw.get("createTime") or raw.get("time")
                    utc_time, beijing_time = _normalize_platform_time(raw_time)
                    content = str(
                        raw.get("content")
                        or raw.get("optContent")
                        or raw.get("description")
                        or raw.get("operateType")
                        or ""
                    )
                    event_uid = "platform_" + stable_json_hash(
                        [tool_user_id, account["aavid"], platform_log_id]
                    )[:40]
                    sanitized = {
                        key: value
                        for key, value in redact_mapping(raw).items()
                        if "ip" not in str(key).lower()
                    }
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO operation_event(
                            event_uid, tool_user_id, aavid, account_name,
                            source_plan_id, source_plan_name, control_task_id,
                            event_time_utc, event_time_beijing, platform_time,
                            operator_type, operator_id, source, action_type,
                            result_status, request_result_json, platform_log_id,
                            created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'platform_log', ?, 'succeeded', ?, ?, ?)
                        """,
                        (
                            event_uid,
                            tool_user_id,
                            account["aavid"],
                            account["account_name"],
                            target["target_uid"],
                            target["plan_name"],
                            raw.get("controlTaskId") or raw.get("taskId"),
                            utc_time,
                            beijing_time,
                            str(raw_time or ""),
                            str(raw.get("operatorType") or "platform_user"),
                            raw.get("operatorName") or raw.get("userName") or raw.get("operator"),
                            _classify_operation(content),
                            json.dumps(
                                {"content": content, "platform": sanitized},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            platform_log_id,
                            now,
                        ),
                    )
                    count += cursor.rowcount
            return count

        inserted = int(self.writer.submit(op))
        self._archive_operation_logs(tool_user_id, str(account["aavid"]), rows)
        return inserted

    def _archive_operation_logs(
        self,
        tool_user_id: str,
        aavid: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Write the long-term platform log copy into its Beijing business month."""
        grouped: dict[str, list[tuple[Any, ...]]] = {}
        now = utc_iso()
        for raw in rows:
            platform_log_id = str(
                raw.get("logId")
                or raw.get("optLogId")
                or raw.get("id")
                or stable_json_hash(raw)
            )
            raw_time = (
                raw.get("operateTime")
                or raw.get("optTime")
                or raw.get("createTime")
                or raw.get("time")
            )
            utc_time, beijing_time = _normalize_platform_time(raw_time)
            sanitized = {
                key: value
                for key, value in redact_mapping(raw).items()
                if "ip" not in str(key).lower()
            }
            grouped.setdefault(beijing_time[:7], []).append(
                (
                    tool_user_id,
                    aavid,
                    platform_log_id,
                    utc_time,
                    beijing_time,
                    json.dumps(sanitized, ensure_ascii=False, sort_keys=True),
                    now,
                )
            )
        for month, records in grouped.items():
            history_path = self.database.initialize_history(month)

            def archive(conn, *, path=history_path, values=tuple(records)):
                conn.execute("ATTACH DATABASE ? AS history_archive", (str(path),))
                try:
                    with short_transaction(conn):
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO history_archive.platform_operation_log(
                                tool_user_id, aavid, platform_log_id,
                                event_time_utc, event_time_beijing, payload_json, created_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )
                finally:
                    conn.execute("DETACH DATABASE history_archive")

            self.writer.submit(archive)

    def _start_run(
        self,
        tool_user_id: str,
        aavid: str,
        target: str | None,
        object_type: str,
        filters_hash: str,
        adapter_version: str,
        run_business_date: str | None = None,
    ) -> str:
        run_id = f"collect_{uuid.uuid4().hex}"
        self.writer.execute(
            """
            INSERT INTO collection_run(
                collection_run_id, tool_user_id, aavid, target_uid, object_type,
                business_date, filters_hash, started_at, adapter_version, status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                run_id,
                tool_user_id,
                aavid,
                target,
                object_type,
                run_business_date or business_date(),
                filters_hash,
                utc_iso(),
                adapter_version,
            ),
        )
        return run_id

    def _finish_run(self, run_id: str, result: PaginatedResult) -> None:
        self.writer.execute(
            """
            UPDATE collection_run SET
                platform_total_count=?, expected_pages=?, successful_pages=?,
                failed_pages_json=?, raw_count=?, unique_count=?, duplicate_count=?,
                completed_at=?, platform_server_time=?, status=?, error_code=?, error_message=?
            WHERE collection_run_id=?
            """,
            (
                result.platform_total_count,
                result.expected_pages,
                result.successful_pages,
                json.dumps(result.failed_pages),
                result.raw_count,
                result.unique_count,
                result.duplicate_count,
                utc_iso(),
                result.platform_server_time,
                result.status,
                result.error_code,
                result.error_message,
                run_id,
            ),
        )

    def _record_adapter_evidence(
        self,
        adapter,
        result: PaginatedResult,
        *,
        endpoint_path: str,
        dataset_key: str,
        capability_name: str,
    ) -> None:
        if not result.response_schema_hashes:
            return
        now = utc_iso()
        business_hash = stable_json_hash(
            {
                "adapter": adapter.adapter_name,
                "adapter_version": adapter.adapter_version,
                "plan_system": adapter.plan_system,
                "promotion_scene": adapter.promotion_scene,
                "dataset_key": dataset_key,
            }
        )
        for schema_hash in result.response_schema_hashes:
            evidence_uid = "evidence_" + stable_json_hash(
                [
                    adapter.adapter_name,
                    adapter.adapter_version,
                    endpoint_path,
                    dataset_key,
                    schema_hash,
                    capability_name,
                ]
            )[:32]
            self.writer.execute(
                """
                INSERT INTO adapter_evidence(
                    evidence_uid, adapter_name, adapter_version, endpoint_path,
                    http_method, dataset_key, request_business_fields_hash,
                    response_schema_hash, capability_name, capability_state,
                    evidence_level, first_seen_at, last_seen_at
                ) VALUES(?, ?, ?, ?, 'POST', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(adapter_name, adapter_version, endpoint_path, http_method,
                            dataset_key, response_schema_hash, capability_name)
                DO UPDATE SET capability_state=excluded.capability_state,
                              evidence_level=excluded.evidence_level,
                              last_seen_at=excluded.last_seen_at
                """,
                (
                    evidence_uid,
                    adapter.adapter_name,
                    adapter.adapter_version,
                    endpoint_path,
                    dataset_key,
                    business_hash,
                    schema_hash,
                    capability_name,
                    adapter.read_capability_state,
                    adapter.evidence_level,
                    now,
                    now,
                ),
            )

    def _upsert_catalog_plans(
        self,
        tool_user_id: str,
        aavid: str,
        plans: list[NormalizedPlan],
        *,
        mark_missing: bool,
    ) -> None:
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                seen: set[str] = set()
                for plan in plans:
                    uid = target_uid(tool_user_id, aavid, plan.ad_id)
                    seen.add(plan.ad_id)
                    verified = plan.verification_state == "verified"
                    active = plan.platform_status.lower() in ACTIVE_PLATFORM_STATUSES
                    eligible = int(verified and active)
                    reason = None
                    if not verified:
                        reason = f"identity_{plan.verification_state}"
                    elif not active:
                        reason = f"platform_status_{plan.platform_status}"
                    conn.execute(
                        """
                        INSERT INTO source_plan(
                            target_uid, tool_user_id, aavid, ad_id, plan_name,
                            plan_system, promotion_scene, platform_status,
                            verification_state, catalog_seen_at, monitor_enabled,
                            monitor_eligible, retarget_eligible, pause_eligible,
                            adjust_eligible, ineligible_reason, adapter_version,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, 0, ?, ?, ?, ?)
                        ON CONFLICT(tool_user_id, aavid, ad_id) DO UPDATE SET
                            plan_name=excluded.plan_name,
                            plan_system=excluded.plan_system,
                            promotion_scene=excluded.promotion_scene,
                            platform_status=excluded.platform_status,
                            verification_state=excluded.verification_state,
                            catalog_seen_at=excluded.catalog_seen_at,
                            monitor_eligible=excluded.monitor_eligible,
                            retarget_eligible=0,
                            pause_eligible=0,
                            adjust_eligible=0,
                            ineligible_reason=excluded.ineligible_reason,
                            adapter_version=excluded.adapter_version,
                            updated_at=excluded.updated_at
                        """,
                        (
                            uid,
                            tool_user_id,
                            aavid,
                            plan.ad_id,
                            plan.plan_name,
                            plan.plan_system,
                            plan.promotion_scene,
                            plan.platform_status,
                            plan.verification_state,
                            now,
                            eligible,
                            reason,
                            plan.adapter_version,
                            now,
                            now,
                        ),
                    )
                if mark_missing:
                    rows = conn.execute(
                        "SELECT ad_id FROM source_plan WHERE tool_user_id=? AND aavid=?",
                        (tool_user_id, aavid),
                    ).fetchall()
                    for row in rows:
                        if str(row["ad_id"]) not in seen:
                            conn.execute(
                                """
                                UPDATE source_plan SET platform_status='not_seen',
                                    monitor_eligible=0, retarget_eligible=0,
                                    pause_eligible=0, adjust_eligible=0,
                                    ineligible_reason='not_seen_in_latest_complete_catalog',
                                    updated_at=?
                                WHERE tool_user_id=? AND aavid=? AND ad_id=?
                                """,
                                (now, tool_user_id, aavid, row["ad_id"]),
                            )

        self.writer.submit(op)

    def _persist_material_batch(
        self,
        tool_user_id: str,
        target: dict[str, Any],
        run_id: str,
        materials: list[NormalizedMaterial],
    ) -> None:
        now_utc = utc_iso()
        now_bj = beijing_iso()

        def op(conn):
            with short_transaction(conn):
                for material in materials:
                    uid = material_uid(
                        tool_user_id, material.aavid, material.ad_id, material.material_id
                    )
                    conn.execute(
                        """
                        INSERT INTO material_identity(
                            material_uid, tool_user_id, aavid, ad_id, material_id,
                            material_name, material_type, material_created_at,
                            first_seen_at, last_seen_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 'video', ?, ?, ?)
                        ON CONFLICT(tool_user_id, aavid, ad_id, material_id) DO UPDATE SET
                            material_name=excluded.material_name,
                            material_created_at=COALESCE(excluded.material_created_at, material_identity.material_created_at),
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            uid,
                            tool_user_id,
                            material.aavid,
                            material.ad_id,
                            material.material_id,
                            material.material_name,
                            material.material_created_at,
                            now_utc,
                            now_utc,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO material_status_latest(
                            material_uid, delivery_status, show_status, show_status_reason,
                            audit_status, block_status, platform_raw_status_json,
                            is_in_delivery_list, is_effectively_deliverable,
                            observed_at, collection_run_id
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(material_uid) DO UPDATE SET
                            delivery_status=excluded.delivery_status,
                            show_status=excluded.show_status,
                            show_status_reason=excluded.show_status_reason,
                            audit_status=excluded.audit_status,
                            block_status=excluded.block_status,
                            platform_raw_status_json=excluded.platform_raw_status_json,
                            is_in_delivery_list=excluded.is_in_delivery_list,
                            is_effectively_deliverable=excluded.is_effectively_deliverable,
                            observed_at=excluded.observed_at,
                            collection_run_id=excluded.collection_run_id
                        """,
                        (
                            uid,
                            material.delivery_status,
                            material.show_status,
                            material.show_status_reason,
                            material.audit_status,
                            material.block_status,
                            json.dumps(material.platform_raw_status, ensure_ascii=False, sort_keys=True),
                            int(material.is_in_delivery_list),
                            int(material.is_effectively_deliverable),
                            now_utc,
                            run_id,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO latest_metrics(
                            material_uid, tool_user_id, aavid, ad_id,
                            spend_cent, order_count, gmv_cent, roi_decimal,
                            platform_time, observed_at_utc, observed_at_beijing,
                            collection_run_id
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                        ON CONFLICT(material_uid) DO UPDATE SET
                            spend_cent=excluded.spend_cent,
                            order_count=excluded.order_count,
                            gmv_cent=excluded.gmv_cent,
                            roi_decimal=excluded.roi_decimal,
                            observed_at_utc=excluded.observed_at_utc,
                            observed_at_beijing=excluded.observed_at_beijing,
                            collection_run_id=excluded.collection_run_id
                        """,
                        (
                            uid,
                            tool_user_id,
                            material.aavid,
                            material.ad_id,
                            material.spend_cent,
                            material.order_count,
                            material.gmv_cent,
                            material.roi_decimal,
                            now_utc,
                            now_bj,
                            run_id,
                        ),
                    )
                    business_hour = now_bj[:13]
                    business_day = now_bj[:10]
                    conn.execute(
                        """
                        INSERT INTO hourly_metrics(
                            tool_user_id, aavid, ad_id, material_id, business_hour,
                            spend_cent, order_count, gmv_cent, roi_decimal, observed_at_utc
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(tool_user_id, aavid, ad_id, material_id, business_hour)
                        DO UPDATE SET spend_cent=excluded.spend_cent,
                            order_count=excluded.order_count, gmv_cent=excluded.gmv_cent,
                            roi_decimal=excluded.roi_decimal, observed_at_utc=excluded.observed_at_utc
                        """,
                        (
                            tool_user_id, material.aavid, material.ad_id, material.material_id,
                            business_hour, material.spend_cent, material.order_count,
                            material.gmv_cent, material.roi_decimal, now_utc,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO daily_metrics(
                            tool_user_id, aavid, ad_id, material_id, business_date,
                            spend_cent, order_count, gmv_cent, roi_decimal, revision,
                            observed_at_utc
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        ON CONFLICT(tool_user_id, aavid, ad_id, material_id, business_date)
                        DO UPDATE SET spend_cent=excluded.spend_cent,
                            order_count=excluded.order_count, gmv_cent=excluded.gmv_cent,
                            roi_decimal=excluded.roi_decimal, revision=daily_metrics.revision+1,
                            observed_at_utc=excluded.observed_at_utc
                        """,
                        (
                            tool_user_id, material.aavid, material.ad_id, material.material_id,
                            business_day, material.spend_cent, material.order_count,
                            material.gmv_cent, material.roi_decimal, now_utc,
                        ),
                    )
                    for product_id in material.product_ids:
                        p_uid = product_uid(
                            tool_user_id, material.aavid, material.ad_id, product_id
                        )
                        conn.execute(
                            """
                            INSERT INTO product_identity(
                                product_uid, tool_user_id, aavid, ad_id,
                                product_id, first_seen_at, last_seen_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(tool_user_id, aavid, ad_id, product_id)
                            DO UPDATE SET last_seen_at=excluded.last_seen_at
                            """,
                            (
                                p_uid,
                                tool_user_id,
                                material.aavid,
                                material.ad_id,
                                product_id,
                                now_utc,
                                now_utc,
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO product_material_relation(
                                tool_user_id, aavid, ad_id, product_id,
                                material_id, observed_at
                            ) VALUES(?, ?, ?, ?, ?, ?)
                            ON CONFLICT(tool_user_id, aavid, ad_id, product_id, material_id)
                            DO UPDATE SET observed_at=excluded.observed_at
                            """,
                            (
                                tool_user_id,
                                material.aavid,
                                material.ad_id,
                                product_id,
                                material.material_id,
                                now_utc,
                            ),
                        )

        self.writer.submit(op)
        history_path = self.database.initialize_history(now_bj[:7])

        def archive(history_conn):
            history_conn.execute("ATTACH DATABASE ? AS history_archive", (str(history_path),))
            try:
                with short_transaction(history_conn):
                    for material in materials:
                        values = (
                            tool_user_id, material.aavid, material.ad_id, material.material_id,
                            now_bj[:13], material.spend_cent, material.order_count,
                            material.gmv_cent, material.roi_decimal, now_utc,
                        )
                        history_conn.execute(
                            """
                            INSERT INTO history_archive.hourly_metrics(
                                tool_user_id, aavid, ad_id, material_id, business_hour,
                                spend_cent, order_count, gmv_cent, roi_decimal, observed_at_utc
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(tool_user_id, aavid, ad_id, material_id, business_hour)
                            DO UPDATE SET spend_cent=excluded.spend_cent,
                                order_count=excluded.order_count, gmv_cent=excluded.gmv_cent,
                                roi_decimal=excluded.roi_decimal, observed_at_utc=excluded.observed_at_utc
                            """,
                            values,
                        )
                        history_conn.execute(
                            """
                            INSERT INTO history_archive.daily_metrics(
                                tool_user_id, aavid, ad_id, material_id, business_date,
                                spend_cent, order_count, gmv_cent, roi_decimal, revision,
                                observed_at_utc
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                            ON CONFLICT(tool_user_id, aavid, ad_id, material_id, business_date)
                            DO UPDATE SET spend_cent=excluded.spend_cent,
                                order_count=excluded.order_count, gmv_cent=excluded.gmv_cent,
                                roi_decimal=excluded.roi_decimal, revision=daily_metrics.revision+1,
                                observed_at_utc=excluded.observed_at_utc
                            """,
                            (
                                tool_user_id, material.aavid, material.ad_id, material.material_id,
                                now_bj[:10], material.spend_cent, material.order_count,
                                material.gmv_cent, material.roi_decimal, now_utc,
                            ),
                        )
            finally:
                history_conn.execute("DETACH DATABASE history_archive")

        self.writer.submit(archive)

    def _persist_control_tasks(
        self,
        tool_user_id: str,
        target: dict[str, Any],
        run_id: str,
        tasks: list[NormalizedControlTask],
    ) -> None:
        now = utc_iso()
        rows = []
        for task in tasks:
            budget_remaining = None
            utilization = None
            if task.budget_current_cent is not None and task.budget_used_cent is not None:
                budget_remaining = task.budget_current_cent - task.budget_used_cent
                if task.budget_current_cent > 0:
                    utilization = str(
                        Decimal(task.budget_used_cent) / Decimal(task.budget_current_cent)
                    )
            rows.append(
                (
                    control_task_uid(
                        tool_user_id,
                        task.aavid,
                        task.source_plan_id,
                        task.control_task_id,
                    ),
                    tool_user_id,
                    task.aavid,
                    target["target_uid"],
                    task.control_task_id,
                    task.task_name,
                    task.assist_task_scene,
                    task.retarget_method,
                    json.dumps(task.material_ids),
                    task.platform_status,
                    task.budget_kind,
                    task.budget_current_cent,
                    task.budget_used_cent,
                    budget_remaining,
                    utilization,
                    task.duration_hours_decimal,
                    task.start_time_utc,
                    task.end_time_utc,
                    None,
                    task.roi_or_bid_decimal,
                    None,
                    task.updated_at_platform,
                    task.task_revision_fingerprint,
                    run_id,
                    now,
                    now,
                )
            )
        self.writer.executemany(
            """
            INSERT INTO platform_control_task(
                control_task_uid, tool_user_id, aavid, source_plan_id,
                control_task_id, task_name, assist_task_scene, retarget_method,
                material_ids_json, platform_status, budget_kind,
                budget_current_cent, budget_used_cent, budget_remaining_cent,
                budget_utilization_decimal, duration_hours_decimal,
                start_time_utc, end_time_utc, remaining_duration_hours_decimal,
                roi_or_bid_decimal, created_at_platform, updated_at_platform,
                task_revision_fingerprint, last_collection_run_id,
                created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_user_id, aavid, source_plan_id, control_task_id) DO UPDATE SET
                task_name=excluded.task_name,
                assist_task_scene=excluded.assist_task_scene,
                retarget_method=excluded.retarget_method,
                material_ids_json=excluded.material_ids_json,
                platform_status=excluded.platform_status,
                budget_kind=excluded.budget_kind,
                budget_current_cent=excluded.budget_current_cent,
                budget_used_cent=excluded.budget_used_cent,
                budget_remaining_cent=excluded.budget_remaining_cent,
                budget_utilization_decimal=excluded.budget_utilization_decimal,
                duration_hours_decimal=excluded.duration_hours_decimal,
                start_time_utc=excluded.start_time_utc,
                end_time_utc=excluded.end_time_utc,
                roi_or_bid_decimal=excluded.roi_or_bid_decimal,
                updated_at_platform=excluded.updated_at_platform,
                task_revision_fingerprint=excluded.task_revision_fingerprint,
                last_collection_run_id=excluded.last_collection_run_id,
                updated_at=excluded.updated_at
            """,
            rows,
        )
