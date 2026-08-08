"""候选生成、策略仲裁、冻结、分页与多组 dry-run。"""

from __future__ import annotations

import json
import math
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .constants import MAX_MATERIALS_PER_GROUP
from .security import stable_json_hash
from .storage import RuntimeDatabase, StorageWriter, short_transaction
from .strategies import matches_trigger
from .timeutils import BEIJING, business_date, utc_iso, utc_now


def _decimal_sort(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("-Infinity")
    return Decimal(str(value))


def candidate_sort_key(material: dict[str, Any]) -> tuple[Any, ...]:
    # Python 升序排序，因此数值项取负；创建时间和ID使用反向包装不直观，
    # 先用稳定的时间戳负值，无法解析时排最后。
    created = str(material.get("material_created_at") or "")
    try:
        created_ts = -__import__("datetime").datetime.fromisoformat(
            created.replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        created_ts = float("inf")
    return (
        -int(material.get("order_count") or 0),
        -_decimal_sort(material.get("roi_decimal")),
        -int(material.get("gmv_cent") or 0),
        created_ts,
        str(material.get("material_id") or ""),
    )


class CandidateBlocked(ValueError):
    pass


class CandidateService:
    def __init__(self, database: RuntimeDatabase, writer: StorageWriter):
        self.database = database
        self.writer = writer

    def expire_due_candidates(self, tool_user_id: str | None = None) -> dict[str, int]:
        """持久化所有已到期候选，并取消尚未发送的旧预览。"""

        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                user_clause = " AND tool_user_id=?" if tool_user_id else ""
                params: list[Any] = [now]
                if tool_user_id:
                    params.append(tool_user_id)
                batch_rows = conn.execute(
                    """
                    SELECT candidate_batch_id FROM candidate_batch
                    WHERE expires_at<=?
                      AND status IN ('frozen','grouped','pending_approval')
                    """
                    + user_clause,
                    params,
                ).fetchall()
                batch_ids = [str(row["candidate_batch_id"]) for row in batch_rows]
                if batch_ids:
                    placeholders = ",".join("?" for _ in batch_ids)
                    conn.execute(
                        f"UPDATE candidate_batch SET status='expired', terminal_at=?, updated_at=? WHERE candidate_batch_id IN ({placeholders})",
                        [now, now, *batch_ids],
                    )
                    conn.execute(
                        f"UPDATE retarget_group SET status='expired', updated_at=? WHERE candidate_batch_id IN ({placeholders}) AND status='frozen'",
                        [now, *batch_ids],
                    )
                    conn.execute(
                        f"UPDATE feishu_outbox SET status='cancelled', updated_at=? WHERE task_uid IN ({placeholders}) AND status IN ('queued','retry')",
                        [now, *batch_ids],
                    )

                adjustment_params: list[Any] = [now]
                if tool_user_id:
                    adjustment_params.append(tool_user_id)
                adjustment_rows = conn.execute(
                    """
                    SELECT adjustment_candidate_id FROM adjustment_candidate
                    WHERE expires_at<=? AND status IN ('frozen','preview_queued')
                    """
                    + user_clause,
                    adjustment_params,
                ).fetchall()
                adjustment_ids = [
                    str(row["adjustment_candidate_id"]) for row in adjustment_rows
                ]
                if adjustment_ids:
                    placeholders = ",".join("?" for _ in adjustment_ids)
                    conn.execute(
                        f"UPDATE adjustment_candidate SET status='expired', updated_at=? WHERE adjustment_candidate_id IN ({placeholders})",
                        [now, *adjustment_ids],
                    )
                    conn.execute(
                        f"UPDATE feishu_outbox SET status='cancelled', updated_at=? WHERE task_uid IN ({placeholders}) AND status IN ('queued','retry')",
                        [now, *adjustment_ids],
                    )
                return {
                    "candidate_batches": len(batch_ids),
                    "adjustment_candidates": len(adjustment_ids),
                }

        return self.writer.submit(op)

    def _require_trusted_catalog(
        self,
        tool_user_id: str,
        target: dict[str, Any],
        *,
        max_catalog_age_seconds: int,
    ) -> None:
        account = self.database.query_one(
            """
            SELECT enabled, catalog_status, catalog_completed_at, removed_at
            FROM advertiser_account
            WHERE tool_user_id=? AND aavid=?
            """,
            (tool_user_id, str(target["aavid"])),
        )
        if not account or account.get("removed_at"):
            raise CandidateBlocked("account_not_active")
        if int(account.get("enabled") or 0) != 1:
            raise CandidateBlocked("account_disabled")
        if str(account.get("catalog_status") or "") != "complete":
            raise CandidateBlocked("account_catalog_not_complete")
        completed_at = account.get("catalog_completed_at")
        if not completed_at:
            raise CandidateBlocked("account_catalog_not_complete")
        try:
            completed = __import__("datetime").datetime.fromisoformat(
                str(completed_at).replace("Z", "+00:00")
            )
        except Exception as exc:
            raise CandidateBlocked("account_catalog_time_invalid") from exc
        if (utc_now() - completed).total_seconds() > max_catalog_age_seconds:
            raise CandidateBlocked("account_catalog_stale")

    def generate_for_target(
        self,
        tool_user_id: str,
        target_uid: str,
        *,
        max_data_age_seconds: int = 600,
        max_catalog_age_seconds: int = 3600,
    ) -> list[str]:
        self.expire_due_candidates(tool_user_id)
        target = self.database.query_one(
            "SELECT * FROM source_plan WHERE tool_user_id=? AND target_uid=?",
            (tool_user_id, target_uid),
        )
        if not target:
            raise CandidateBlocked("target_not_found")
        if int(target["monitor_enabled"]) != 1 or int(target["monitor_eligible"]) != 1:
            raise CandidateBlocked("target_not_monitorable")
        self._require_trusted_catalog(
            tool_user_id,
            target,
            max_catalog_age_seconds=max_catalog_age_seconds,
        )
        latest_run = self.database.query_one(
            """
            SELECT * FROM collection_run
            WHERE tool_user_id=? AND target_uid=? AND object_type='video_material'
              AND business_date=?
            ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (tool_user_id, target_uid, business_date()),
        )
        if not latest_run or latest_run["status"] != "complete":
            raise CandidateBlocked("latest_material_batch_not_complete")
        observed = __import__("datetime").datetime.fromisoformat(
            str(latest_run["completed_at"]).replace("Z", "+00:00")
        )
        age = max(0, int((utc_now() - observed).total_seconds()))
        if age > max_data_age_seconds:
            raise CandidateBlocked("latest_material_batch_stale")

        materials = self.database.query_all(
            """
            SELECT mi.material_id, mi.material_name, mi.material_created_at,
                   mi.ad_id, ms.delivery_status, ms.show_status,
                   ms.audit_status, ms.block_status,
                   ms.is_effectively_deliverable,
                   lm.spend_cent, lm.order_count, lm.gmv_cent, lm.roi_decimal,
                   lm.observed_at_utc
            FROM material_identity mi
            JOIN material_status_latest ms ON ms.material_uid=mi.material_uid
            JOIN latest_metrics lm ON lm.material_uid=mi.material_uid
            WHERE mi.tool_user_id=? AND mi.aavid=? AND mi.ad_id=?
              AND mi.material_type='video'
              AND ms.collection_run_id=?
              AND lm.collection_run_id=?
              AND ms.is_effectively_deliverable=1
            """,
            (
                tool_user_id,
                target["aavid"],
                target["ad_id"],
                latest_run["collection_run_id"],
                latest_run["collection_run_id"],
            ),
        )
        if not materials:
            return []
        product_links = self.database.query_all(
            """
            SELECT product_id, material_id
            FROM product_material_relation
            WHERE tool_user_id=? AND aavid=? AND ad_id=?
            """,
            (tool_user_id, target["aavid"], target["ad_id"]),
        )
        products_by_material: dict[str, list[str]] = {}
        materials_by_product: dict[str, list[str]] = {}
        for link in product_links:
            products_by_material.setdefault(str(link["material_id"]), []).append(
                str(link["product_id"])
            )
            materials_by_product.setdefault(str(link["product_id"]), []).append(
                str(link["material_id"])
            )
        by_id = {str(row["material_id"]): row for row in materials}
        for row in materials:
            row["product_ids"] = sorted(products_by_material.get(str(row["material_id"]), []))

        strategies = self.database.query_all(
            """
            SELECT * FROM strategy
            WHERE tool_user_id=? AND target_uid=? AND enabled=1
              AND strategy_type='retarget_create' AND action_mode='dry_run'
            ORDER BY priority ASC, created_at ASC
            """,
            (tool_user_id, target_uid),
        )
        claimed: set[str] = set()
        batch_ids: list[str] = []
        for strategy in strategies:
            trigger = json.loads(str(strategy["trigger_json"]))
            matching: set[str] = set()
            if strategy["trigger_level"] == "material":
                for material in materials:
                    mid = str(material["material_id"])
                    if mid not in claimed and matches_trigger(trigger, material):
                        matching.add(mid)
            else:
                for product_id, member_ids in sorted(materials_by_product.items()):
                    members = [by_id[mid] for mid in member_ids if mid in by_id]
                    if not members:
                        continue
                    spend = sum(int(row["spend_cent"] or 0) for row in members)
                    orders = sum(int(row["order_count"] or 0) for row in members)
                    gmv = sum(int(row["gmv_cent"] or 0) for row in members)
                    aggregate = {
                        "spend_cent": spend,
                        "order_count": orders,
                        "gmv_cent": gmv,
                        "roi_decimal": None if spend == 0 else str(Decimal(gmv) / Decimal(spend)),
                    }
                    if matches_trigger(trigger, aggregate):
                        # 商品级命中保留该商品下全部合格视频，不恢复旧版 Top1。
                        matching.update(mid for mid in member_ids if mid in by_id and mid not in claimed)
            if not matching:
                continue
            selected = sorted((by_id[mid] for mid in matching), key=candidate_sort_key)
            claimed.update(matching)
            batch_ids.append(
                self._freeze_batch(tool_user_id, target, strategy, selected, latest_run)
            )
        return batch_ids

    def generate_adjustments_for_target(
        self,
        tool_user_id: str,
        target_uid: str,
        *,
        max_data_age_seconds: int = 600,
        max_catalog_age_seconds: int = 3600,
    ) -> list[str]:
        """Freeze Scene 2 pause/adjustment simulations; never create a real execution."""
        self.expire_due_candidates(tool_user_id)
        target = self.database.query_one(
            "SELECT * FROM source_plan WHERE tool_user_id=? AND target_uid=?",
            (tool_user_id, target_uid),
        )
        if not target or int(target["monitor_enabled"]) != 1 or int(target["monitor_eligible"]) != 1:
            raise CandidateBlocked("target_not_monitorable")
        self._require_trusted_catalog(
            tool_user_id,
            target,
            max_catalog_age_seconds=max_catalog_age_seconds,
        )
        latest_run = self.database.query_one(
            """
            SELECT * FROM collection_run
            WHERE tool_user_id=? AND target_uid=? AND object_type='video_material'
              AND business_date=?
            ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (tool_user_id, target_uid, business_date()),
        )
        if not latest_run or latest_run["status"] != "complete":
            raise CandidateBlocked("latest_material_batch_not_complete")
        observed = __import__("datetime").datetime.fromisoformat(
            str(latest_run["completed_at"]).replace("Z", "+00:00")
        )
        if (utc_now() - observed).total_seconds() > max_data_age_seconds:
            raise CandidateBlocked("latest_material_batch_stale")
        latest_control_run = self.database.query_one(
            """
            SELECT * FROM collection_run
            WHERE tool_user_id=? AND aavid=? AND target_uid=?
              AND object_type='control_task:scene_2'
            ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (tool_user_id, target["aavid"], target_uid),
        )
        if not latest_control_run or latest_control_run["status"] != "complete":
            raise CandidateBlocked("latest_scene2_batch_not_complete")
        control_observed = __import__("datetime").datetime.fromisoformat(
            str(latest_control_run["completed_at"]).replace("Z", "+00:00")
        )
        if (utc_now() - control_observed).total_seconds() > max_data_age_seconds:
            raise CandidateBlocked("latest_scene2_batch_stale")
        tasks = self.database.query_all(
            """
            SELECT * FROM platform_control_task
            WHERE tool_user_id=? AND aavid=? AND source_plan_id=?
              AND assist_task_scene=2
              AND last_collection_run_id=?
            ORDER BY created_at_platform, control_task_id
            """,
            (
                tool_user_id,
                target["aavid"],
                target_uid,
                latest_control_run["collection_run_id"],
            ),
        )
        strategies = self.database.query_all(
            """
            SELECT * FROM strategy
            WHERE tool_user_id=? AND target_uid=? AND enabled=1
              AND strategy_type='retarget_pause'
              AND action_mode='dry_run'
            ORDER BY priority ASC, created_at ASC
            """,
            (tool_user_id, target_uid),
        )
        claimed_tasks: set[str] = set()
        candidate_ids: list[str] = []
        for strategy in strategies:
            trigger = json.loads(str(strategy["trigger_json"]))
            for task in tasks:
                task_uid = str(task["control_task_uid"])
                if task_uid in claimed_tasks or str(task["platform_status"]).lower() not in {
                    "1", "active", "running", "in_delivery", "投放中", "启用"
                }:
                    continue
                material_ids = [
                    str(value)
                    for value in json.loads(str(task.get("material_ids_json") or "[]"))
                ]
                if not material_ids:
                    continue
                placeholders = ",".join("?" for _ in material_ids)
                metrics = self.database.query_one(
                    f"""
                    SELECT COALESCE(SUM(lm.spend_cent),0) AS spend_cent,
                           COALESCE(SUM(lm.order_count),0) AS order_count,
                           COALESCE(SUM(lm.gmv_cent),0) AS gmv_cent
                    FROM latest_metrics lm
                    JOIN material_identity mi ON mi.material_uid=lm.material_uid
                    WHERE lm.tool_user_id=? AND lm.aavid=? AND lm.ad_id=?
                      AND lm.collection_run_id=?
                      AND mi.material_id IN ({placeholders})
                    """,
                    [
                        tool_user_id,
                        target["aavid"],
                        target["ad_id"],
                        latest_run["collection_run_id"],
                        *material_ids,
                    ],
                ) or {"spend_cent": 0, "order_count": 0, "gmv_cent": 0}
                spend = int(metrics["spend_cent"] or 0)
                gmv = int(metrics["gmv_cent"] or 0)
                metrics["roi_decimal"] = None if spend == 0 else str(Decimal(gmv) / Decimal(spend))
                if not matches_trigger(trigger, metrics):
                    continue
                candidate_id = self._freeze_adjustment_candidate(
                    tool_user_id, target, task, strategy, metrics
                )
                candidate_ids.append(candidate_id)
                claimed_tasks.add(task_uid)
        return candidate_ids

    def _freeze_adjustment_candidate(
        self,
        tool_user_id: str,
        target: dict[str, Any],
        task: dict[str, Any],
        strategy: dict[str, Any],
        metrics: dict[str, Any],
    ) -> str:
        params = json.loads(str(strategy.get("action_params_json") or "{}"))
        budget_delta = int(params.get("budget_delta_cent") or 0)
        duration_delta = Decimal(str(params.get("duration_delta_hours") or "0"))
        if budget_delta < 0 or duration_delta < 0:
            raise CandidateBlocked("adjustment_delta_must_not_be_negative")
        budget_before = task.get("budget_current_cent")
        duration_before = task.get("duration_hours_decimal")
        budget_after = None if budget_before is None else int(budget_before) + budget_delta
        duration_after = None if duration_before is None else str(Decimal(str(duration_before)) + duration_delta)
        end_before = task.get("end_time_utc")
        end_after = None
        if end_before and duration_delta:
            try:
                parsed_end = __import__("datetime").datetime.fromisoformat(
                    str(end_before).replace("Z", "+00:00")
                )
                end_after = utc_iso(parsed_end + timedelta(hours=float(duration_delta)))
            except Exception:
                end_after = None
        action_type = str(strategy["strategy_type"])
        fingerprint = stable_json_hash(
            {
                "tool_user_id": tool_user_id,
                "aavid": target["aavid"],
                "target_uid": target["target_uid"],
                "control_task_uid": task["control_task_uid"],
                "task_revision": task["task_revision_fingerprint"],
                "strategy_id": strategy["strategy_id"],
                "strategy_version": strategy["version"],
                "metrics": metrics,
                "action_type": action_type,
                "params": params,
            }
        )
        existing = self.database.query_one(
            "SELECT adjustment_candidate_id FROM adjustment_candidate WHERE tool_user_id=? AND aavid=? AND candidate_fingerprint=?",
            (tool_user_id, target["aavid"], fingerprint),
        )
        if existing:
            return str(existing["adjustment_candidate_id"])
        candidate_id = f"adjustment_{uuid.uuid4().hex}"
        now = utc_now()
        self.writer.execute(
            """
            INSERT INTO adjustment_candidate(
                adjustment_candidate_id, tool_user_id, aavid, target_uid,
                control_task_uid, strategy_id, strategy_version, business_date,
                action_type, budget_kind, budget_before_cent, budget_delta_cent,
                budget_expected_after_cent, duration_before_hours_decimal,
                duration_delta_hours_decimal, duration_expected_after_hours_decimal,
                end_time_before_utc, end_time_expected_after_utc,
                task_revision_fingerprint, metrics_snapshot_json,
                trigger_snapshot_json, candidate_fingerprint, status,
                expires_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     'frozen', ?, ?, ?)
            """,
            (
                candidate_id,
                tool_user_id,
                target["aavid"],
                target["target_uid"],
                task["control_task_uid"],
                strategy["strategy_id"],
                strategy["version"],
                business_date(now),
                action_type,
                task.get("budget_kind"),
                budget_before,
                budget_delta,
                budget_after,
                duration_before,
                str(duration_delta),
                duration_after,
                end_before,
                end_after,
                task["task_revision_fingerprint"],
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                str(strategy["trigger_json"]),
                fingerprint,
                utc_iso(now + timedelta(minutes=30)),
                utc_iso(now),
                utc_iso(now),
            ),
        )
        return candidate_id

    def _freeze_batch(
        self,
        tool_user_id: str,
        target: dict[str, Any],
        strategy: dict[str, Any],
        materials: list[dict[str, Any]],
        collection_run: dict[str, Any],
    ) -> str:
        now = utc_now()
        cooldown = max(1, int(strategy["cooldown_minutes"] or 30))
        material_snapshot = [
            {
                "sequence": index,
                "material_id": str(row["material_id"]),
                "material_name": str(row["material_name"] or row["material_id"]),
                "material_created_at": row["material_created_at"],
                "product_ids": row.get("product_ids", []),
                "status": {
                    "delivery": row["delivery_status"],
                    "show": row["show_status"],
                    "audit": row["audit_status"],
                    "block": row["block_status"],
                    "effectively_deliverable": bool(row["is_effectively_deliverable"]),
                },
            }
            for index, row in enumerate(materials, start=1)
        ]
        metrics_snapshot = {
            str(row["material_id"]): {
                "spend_cent": int(row["spend_cent"] or 0),
                "order_count": int(row["order_count"] or 0),
                "gmv_cent": int(row["gmv_cent"] or 0),
                "roi_decimal": row["roi_decimal"],
                "observed_at_utc": row["observed_at_utc"],
            }
            for row in materials
        }
        content_fingerprint = stable_json_hash(
            {
                "tool_user_id": tool_user_id,
                "aavid": target["aavid"],
                "target_uid": target["target_uid"],
                "strategy_id": strategy["strategy_id"],
                "strategy_version": strategy["version"],
                "business_date": business_date(now),
                "materials": material_snapshot,
                "metrics": metrics_snapshot,
            }
        )
        existing = self.database.query_one(
            """
            SELECT candidate_batch_id, status, terminal_at, candidate_fingerprint
            FROM candidate_batch
            WHERE tool_user_id=? AND aavid=? AND target_uid=?
              AND strategy_id=? AND strategy_version=?
              AND business_date=?
              AND material_snapshot_json=? AND metrics_snapshot_json=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                tool_user_id,
                target["aavid"],
                target["target_uid"],
                strategy["strategy_id"],
                strategy["version"],
                business_date(now),
                json.dumps(material_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(metrics_snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )
        if existing:
            terminal_at = existing.get("terminal_at")
            if not terminal_at:
                return str(existing["candidate_batch_id"])
            terminal = __import__("datetime").datetime.fromisoformat(
                str(terminal_at).replace("Z", "+00:00")
            )
            if (now - terminal).total_seconds() < cooldown * 60:
                return str(existing["candidate_batch_id"])
        fingerprint = stable_json_hash(
            [
                content_fingerprint,
                "initial" if not existing else utc_iso(now),
            ]
        )
        batch_id = f"candidate_{uuid.uuid4().hex}"
        now_iso = utc_iso(now)
        expires = utc_iso(now + timedelta(minutes=30))
        self.writer.execute(
            """
            INSERT INTO candidate_batch(
                candidate_batch_id, tool_user_id, aavid, target_uid,
                strategy_id, strategy_version, business_date,
                candidate_fingerprint, material_snapshot_json,
                metrics_snapshot_json, status, expires_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'frozen', ?, ?, ?)
            """,
            (
                batch_id,
                tool_user_id,
                target["aavid"],
                target["target_uid"],
                strategy["strategy_id"],
                strategy["version"],
                business_date(now),
                fingerprint,
                json.dumps(material_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(metrics_snapshot, ensure_ascii=False, sort_keys=True),
                expires,
                now_iso,
                now_iso,
            ),
        )
        return batch_id

    def page(self, tool_user_id: str, batch_id: str, page: int, page_size: int = 20) -> dict[str, Any]:
        if page < 1 or page_size < 1 or page_size > 20:
            raise ValueError("page_size must be between 1 and 20")
        batch = self.database.query_one(
            "SELECT * FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
            (tool_user_id, batch_id),
        )
        if not batch:
            raise KeyError(batch_id)
        materials = json.loads(str(batch["material_snapshot_json"]))
        metrics = json.loads(str(batch["metrics_snapshot_json"] or "{}"))
        start = (page - 1) * page_size
        visible = []
        for material in materials[start : start + page_size]:
            item = dict(material)
            item["metrics"] = metrics.get(str(item["material_id"]), {})
            visible.append(item)
        return {
            "batch": batch,
            "materials": visible,
            "items": visible,
            "page": page,
            "page_size": page_size,
            "total": len(materials),
            "total_pages": max(1, math.ceil(len(materials) / page_size)),
        }

    def save_groups(
        self,
        tool_user_id: str,
        batch_id: str,
        group_specs: list[dict[str, Any]],
        *,
        created_by_open_id: str | None = None,
    ) -> list[str]:
        batch = self.database.query_one(
            "SELECT * FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
            (tool_user_id, batch_id),
        )
        if not batch:
            raise KeyError(batch_id)
        if batch["status"] not in {"frozen", "grouped"}:
            raise CandidateBlocked(f"batch_{batch['status']}")
        allowed = {
            str(row["material_id"])
            for row in json.loads(str(batch["material_snapshot_json"]))
        }
        expanded: list[tuple[str, list[str]]] = []
        for spec in group_specs:
            mode = str(spec.get("mode") or "selected_group")
            ids = [str(value) for value in spec.get("material_ids") or []]
            if mode == "single_each":
                expanded.extend(("single_each", [material_id]) for material_id in ids)
            else:
                expanded.append((mode, ids))
        if not expanded:
            raise ValueError("至少保存一个模拟分组")
        for mode, ids in expanded:
            if mode not in {"selected_group", "all_group", "single_each"}:
                raise ValueError(f"invalid group mode: {mode}")
            if not ids or len(ids) > MAX_MATERIALS_PER_GROUP:
                raise ValueError("每组必须包含1至20条素材")
            if len(set(ids)) != len(ids):
                raise ValueError("同一组内不能重复素材")
            if not set(ids).issubset(allowed):
                raise ValueError("分组包含冻结候选之外的素材")
            if mode == "all_group" and set(ids) != allowed:
                raise ValueError("全部一组必须包含候选中的全部素材")

        now = utc_iso()
        group_ids: list[str] = []

        def op(conn):
            with short_transaction(conn):
                current = conn.execute(
                    "SELECT status FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
                    (tool_user_id, batch_id),
                ).fetchone()
                if not current:
                    raise KeyError(batch_id)
                if current["status"] not in {"frozen", "grouped"}:
                    raise CandidateBlocked(f"batch_{current['status']}")
                # 桌面端每次提交的是完整分组草稿。候选尚未发卡时原子替换，
                # 使删除/重排后的页面状态与数据库冻结快照完全一致。
                conn.execute(
                    "DELETE FROM retarget_group WHERE candidate_batch_id=? AND status='frozen'",
                    (batch_id,),
                )
                sequence = 0
                for mode, ids in expanded:
                    sequence += 1
                    fingerprint = stable_json_hash(
                        {"candidate_batch_id": batch_id, "material_ids": sorted(ids)}
                    )
                    existing = conn.execute(
                        "SELECT group_uid FROM retarget_group WHERE candidate_batch_id=? AND group_fingerprint=?",
                        (batch_id, fingerprint),
                    ).fetchone()
                    if existing:
                        group_ids.append(str(existing["group_uid"]))
                        continue
                    group_uid = f"group_{uuid.uuid4().hex}"
                    group_ids.append(group_uid)
                    conn.execute(
                        """
                        INSERT INTO retarget_group(
                            group_uid, candidate_batch_id, sequence, group_mode,
                            material_ids_json, material_count, group_fingerprint,
                            status, created_by_open_id, created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'frozen', ?, ?, ?)
                        """,
                        (
                            group_uid,
                            batch_id,
                            sequence,
                            mode,
                            json.dumps(ids),
                            len(ids),
                            fingerprint,
                            created_by_open_id,
                            now,
                            now,
                        ),
                    )
                conn.execute(
                    "UPDATE candidate_batch SET status='grouped', terminal_at=NULL, updated_at=? WHERE candidate_batch_id=?",
                    (now, batch_id),
                )

        self.writer.submit(op)
        return group_ids

    def confirm_groups(
        self,
        tool_user_id: str,
        batch_id: str,
        *,
        authorized_by_open_id: str,
    ) -> list[str]:
        """确认冻结分组并生成纯 dry-run 结果；绝不调用任何平台适配器。"""

        now_dt = utc_now()
        now = utc_iso(now_dt)

        def op(conn):
            with short_transaction(conn):
                batch = conn.execute(
                    "SELECT * FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
                    (tool_user_id, batch_id),
                ).fetchone()
                if not batch:
                    raise KeyError(batch_id)
                if batch["status"] == "completed":
                    return [
                        str(row["group_uid"])
                        for row in conn.execute(
                            "SELECT group_uid FROM retarget_group WHERE candidate_batch_id=? ORDER BY sequence",
                            (batch_id,),
                        ).fetchall()
                    ]
                if batch["status"] != "pending_approval":
                    raise CandidateBlocked(f"batch_{batch['status']}")
                expires_at = __import__("datetime").datetime.fromisoformat(
                    str(batch["expires_at"]).replace("Z", "+00:00")
                )
                if expires_at <= now_dt:
                    conn.execute(
                        "UPDATE candidate_batch SET status='expired', terminal_at=?, updated_at=? WHERE candidate_batch_id=?",
                        (now, now, batch_id),
                    )
                    conn.execute(
                        "UPDATE retarget_group SET status='expired', updated_at=? WHERE candidate_batch_id=? AND status='frozen'",
                        (now, batch_id),
                    )
                    return []
                groups = conn.execute(
                    "SELECT * FROM retarget_group WHERE candidate_batch_id=? ORDER BY sequence",
                    (batch_id,),
                ).fetchall()
                if not groups:
                    raise CandidateBlocked("candidate_groups_missing")
                account = conn.execute(
                    "SELECT account_name FROM advertiser_account WHERE tool_user_id=? AND aavid=?",
                    (tool_user_id, batch["aavid"]),
                ).fetchone()
                plan = conn.execute(
                    "SELECT plan_name FROM source_plan WHERE tool_user_id=? AND target_uid=?",
                    (tool_user_id, batch["target_uid"]),
                ).fetchone()
                confirmed: list[str] = []
                for group in groups:
                    group_uid = str(group["group_uid"])
                    confirmed.append(group_uid)
                    ids = [str(value) for value in json.loads(str(group["material_ids_json"]))]
                    idempotency_key = stable_json_hash(
                        [
                            tool_user_id,
                            batch["aavid"],
                            batch["target_uid"],
                            batch["strategy_id"],
                            batch["strategy_version"],
                            batch_id,
                            group_uid,
                            "retarget_create_dry_run",
                        ]
                    )
                    execution_uid = f"execution_{uuid.uuid4().hex}"
                    inserted = conn.execute(
                        """
                        INSERT OR IGNORE INTO execution_task(
                            execution_uid, tool_user_id, aavid, target_uid,
                            candidate_batch_id, group_uid, operation_type,
                            idempotency_key, strategy_id, strategy_version,
                            authorized_by_open_id, authorized_at, status,
                            request_snapshot_json, result_snapshot_json,
                            created_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 'retarget_create_dry_run', ?, ?, ?, ?, ?,
                                 'dry_run_succeeded', ?, ?, ?, ?)
                        """,
                        (
                            execution_uid,
                            tool_user_id,
                            batch["aavid"],
                            batch["target_uid"],
                            batch_id,
                            group_uid,
                            idempotency_key,
                            batch["strategy_id"],
                            batch["strategy_version"],
                            authorized_by_open_id,
                            now,
                            json.dumps(
                                {
                                    "material_ids": ids,
                                    "mode": group["group_mode"],
                                    "v1a": "simulation",
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            json.dumps(
                                {"executed": False, "reason": "V1A模拟，不执行千川操作"},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                            now,
                        ),
                    ).rowcount
                    if inserted:
                        conn.execute(
                            """
                            INSERT INTO operation_event(
                                event_uid, tool_user_id, aavid, account_name,
                                source_plan_id, source_plan_name, event_time_utc,
                                event_time_beijing, operator_type, operator_id,
                                source, action_type, result_status,
                                request_result_json, created_at
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'feishu_user', ?,
                                     'simulation', 'retarget_create_dry_run',
                                     'simulated', ?, ?)
                            """,
                            (
                                f"operation_{uuid.uuid4().hex}",
                                tool_user_id,
                                batch["aavid"],
                                account["account_name"] if account else batch["aavid"],
                                batch["target_uid"],
                                plan["plan_name"] if plan else None,
                                now,
                                now_dt.astimezone(BEIJING).isoformat(timespec="seconds"),
                                authorized_by_open_id,
                                json.dumps(
                                    {"execution_uid": execution_uid, "material_ids": ids},
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                now,
                            ),
                        )
                    conn.execute(
                        "UPDATE retarget_group SET status='dry_run_completed', updated_at=? WHERE group_uid=?",
                        (now, group_uid),
                    )
                conn.execute(
                    "UPDATE candidate_batch SET status='completed', terminal_at=?, updated_at=? WHERE candidate_batch_id=?",
                    (now, now, batch_id),
                )
                return confirmed

        confirmed = self.writer.submit(op)
        if not confirmed:
            raise CandidateBlocked("batch_expired")
        return confirmed

    def reject_groups(
        self, tool_user_id: str, batch_id: str, *, rejected_by_open_id: str
    ) -> bool:
        now = utc_iso()

        def op(conn):
            with short_transaction(conn):
                batch = conn.execute(
                    "SELECT status FROM candidate_batch WHERE tool_user_id=? AND candidate_batch_id=?",
                    (tool_user_id, batch_id),
                ).fetchone()
                if not batch:
                    raise KeyError(batch_id)
                if batch["status"] == "rejected":
                    return False
                if batch["status"] != "pending_approval":
                    raise CandidateBlocked(f"batch_{batch['status']}")
                conn.execute(
                    "UPDATE candidate_batch SET status='rejected', terminal_at=?, updated_at=? WHERE candidate_batch_id=?",
                    (now, now, batch_id),
                )
                conn.execute(
                    "UPDATE retarget_group SET status='rejected', created_by_open_id=COALESCE(created_by_open_id, ?), updated_at=? WHERE candidate_batch_id=?",
                    (rejected_by_open_id, now, batch_id),
                )
                return True

        return bool(self.writer.submit(op))
