"""统一操作流水查询与真实/模拟双段日报。"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, timedelta
from typing import Any

from .security import stable_json_hash
from .storage import RuntimeDatabase, StorageWriter
from .timeutils import utc_iso


REAL_SOURCES = ("platform_log", "tool_direct")


class OperationReportService:
    def __init__(self, database: RuntimeDatabase, writer: StorageWriter):
        self.database = database
        self.writer = writer

    def query_events(
        self,
        tool_user_id: str,
        *,
        aavid: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        source: str | None = None,
        action_type: str | None = None,
        result_status: str | None = None,
        operator: str | None = None,
        keyword: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["tool_user_id=?"]
        params: list[Any] = [tool_user_id]
        for column, value in (
            ("aavid", aavid),
            ("source", source),
            ("action_type", action_type),
            ("result_status", result_status),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if date_from:
            clauses.append("substr(event_time_beijing,1,10)>=?")
            params.append(date_from)
        if date_to:
            clauses.append("substr(event_time_beijing,1,10)<=?")
            params.append(date_to)
        if operator:
            clauses.append("(operator_id LIKE ? OR operator_type LIKE ?)")
            token = f"%{operator}%"
            params.extend([token, token])
        if keyword:
            clauses.append(
                "(account_name LIKE ? OR source_plan_name LIKE ? OR control_task_id LIKE ? OR action_type LIKE ?)"
            )
            token = f"%{keyword}%"
            params.extend([token, token, token, token])
        params.extend([min(max(limit, 1), 5000), max(offset, 0)])
        return self.database.query_all(
            "SELECT * FROM operation_event WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_time_beijing DESC LIMIT ? OFFSET ?",
            params,
        )

    def export_csv(self, tool_user_id: str, **filters: Any) -> bytes:
        rows = self.query_events(tool_user_id, limit=5000, **filters)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "北京时间",
                "千川账户",
                "账户ID",
                "计划名称",
                "操作来源",
                "操作类型",
                "结果",
                "调控任务ID",
                "错误原因",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["event_time_beijing"],
                    row["account_name"],
                    row["aavid"],
                    row["source_plan_name"],
                    row["source"],
                    row["action_type"],
                    row["result_status"],
                    row["control_task_id"],
                    row["error_message"],
                ]
            )
        return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")

    def daily_summary(
        self, tool_user_id: str, business_date: str, aavid: str | None = None
    ) -> dict[str, Any]:
        where = ["tool_user_id=?", "substr(event_time_beijing,1,10)=?"]
        params: list[Any] = [tool_user_id, business_date]
        if aavid:
            where.append("aavid=?")
            params.append(aavid)
        rows = self.database.query_all(
            "SELECT * FROM operation_event WHERE " + " AND ".join(where), params
        )
        real = [row for row in rows if row["source"] in REAL_SOURCES]
        simulations = [row for row in rows if row["source"] == "simulation"]

        def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
            actions: dict[str, int] = {}
            failures = 0
            for row in items:
                action = str(row["action_type"])
                actions[action] = actions.get(action, 0) + 1
                if str(row["result_status"]).lower() in {"failed", "error", "partial"}:
                    failures += 1
            return {"total": len(items), "actions": actions, "failures": failures}

        completeness_rows = self.database.query_all(
            """
            SELECT aavid,
                   CASE WHEN SUM(CASE WHEN status='complete' THEN 0 ELSE 1 END)=0
                        THEN 'complete' ELSE 'partial' END AS status,
                   MAX(completed_at) AS completed_at
            FROM collection_run r
            WHERE tool_user_id=? AND object_type='operation_log'
              AND started_at=(
                  SELECT MAX(r2.started_at) FROM collection_run r2
                  WHERE r2.tool_user_id=r.tool_user_id AND r2.aavid=r.aavid
                    AND r2.target_uid=r.target_uid AND r2.object_type='operation_log'
              )
            GROUP BY aavid
            """,
            (tool_user_id,),
        )
        completeness = {
            str(row["aavid"]): str(row["status"]) for row in completeness_rows
        }
        candidate_where = ["tool_user_id=?", "business_date=?"]
        candidate_params: list[Any] = [tool_user_id, business_date]
        if aavid:
            candidate_where.append("aavid=?")
            candidate_params.append(aavid)
        retarget_candidates = self.database.query_all(
            "SELECT material_snapshot_json FROM candidate_batch WHERE "
            + " AND ".join(candidate_where),
            candidate_params,
        )
        adjustment_candidates = self.database.query_all(
            "SELECT action_type FROM adjustment_candidate WHERE "
            + " AND ".join(candidate_where),
            candidate_params,
        )
        simulation_actions: dict[str, int] = {}
        if retarget_candidates:
            simulation_actions["retarget_candidate_batch"] = len(retarget_candidates)
            simulation_actions["retarget_candidate_material"] = sum(
                len(json.loads(str(row["material_snapshot_json"])))
                for row in retarget_candidates
            )
        for row in adjustment_candidates:
            action = str(row["action_type"]) + "_candidate"
            simulation_actions[action] = simulation_actions.get(action, 0) + 1
        simulation_summary = {
            "total": len(retarget_candidates) + len(adjustment_candidates),
            "actions": simulation_actions,
            "failures": 0,
            "dry_run_audits": summarize(simulations),
        }
        return {
            "business_date": business_date,
            "aavid": aavid,
            "real_platform_operations": summarize(real),
            "simulation_candidates": simulation_summary,
            "platform_log_completeness": completeness,
            "note": "模拟候选不计入真实追投、停投或调整数量",
        }

    def record_delivery(
        self,
        tool_user_id: str,
        business_date: str,
        route_id: str,
        summary: dict[str, Any],
        *,
        aavid: str | None = None,
        status: str = "queued",
    ) -> str:
        report_uid = "report_" + stable_json_hash(
            [tool_user_id, aavid or "*", business_date, route_id]
        )[:40]
        now = utc_iso()
        completeness = json.dumps(
            summary.get("platform_log_completeness") or {},
            ensure_ascii=False,
            sort_keys=True,
        )
        self.writer.execute(
            """
            INSERT INTO daily_report_delivery(
                report_uid, tool_user_id, aavid, business_date, route_id,
                real_summary_json, simulation_summary_json,
                platform_log_completeness, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_user_id, aavid, business_date, route_id) DO NOTHING
            """,
            (
                report_uid,
                tool_user_id,
                aavid,
                business_date,
                route_id,
                json.dumps(summary["real_platform_operations"], ensure_ascii=False, sort_keys=True),
                json.dumps(summary["simulation_candidates"], ensure_ascii=False, sort_keys=True),
                completeness,
                status,
                now,
                now,
            ),
        )
        return report_uid
