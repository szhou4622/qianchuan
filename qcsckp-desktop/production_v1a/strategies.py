"""V1A 今日累计规则、AND 条件和固定优先级仲裁。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .constants import DEFAULT_CANDIDATE_COOLDOWN_MINUTES
from .storage import RuntimeDatabase, StorageWriter
from .timeutils import utc_iso

ALLOWED_METRICS = frozenset({"spend_cent", "order_count", "gmv_cent", "roi_decimal"})
ALLOWED_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "between"})


class StrategyValidationError(ValueError):
    pass


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise StrategyValidationError(f"无效数值: {value}") from exc


def validate_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
    conditions = trigger.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise StrategyValidationError("策略至少包含一个条件")
    normalized = []
    for condition in conditions:
        if not isinstance(condition, dict):
            raise StrategyValidationError("条件必须是对象")
        metric = str(condition.get("metric") or "")
        operator = str(condition.get("operator") or "")
        if metric not in ALLOWED_METRICS:
            raise StrategyValidationError(f"不支持的指标: {metric}")
        if operator not in ALLOWED_OPERATORS:
            raise StrategyValidationError(f"不支持的比较符: {operator}")
        if operator == "between":
            lower = _decimal(condition.get("min"))
            upper = _decimal(condition.get("max"))
            if lower > upper:
                raise StrategyValidationError("区间下限不能大于上限")
            normalized.append(
                {
                    "metric": metric,
                    "operator": operator,
                    "min": str(lower),
                    "max": str(upper),
                }
            )
        else:
            normalized.append(
                {
                    "metric": metric,
                    "operator": operator,
                    "value": str(_decimal(condition.get("value"))),
                }
            )
    return {"conditions": normalized, "logic": "AND", "window": "today_cumulative"}


def matches_trigger(trigger: dict[str, Any], metrics: dict[str, Any]) -> bool:
    for condition in trigger.get("conditions", []):
        raw = metrics.get(condition["metric"])
        if raw is None:
            return False
        actual = _decimal(raw)
        operator = condition["operator"]
        if operator == "between":
            passed = _decimal(condition["min"]) <= actual <= _decimal(condition["max"])
        else:
            expected = _decimal(condition["value"])
            passed = {
                "gt": actual > expected,
                "gte": actual >= expected,
                "lt": actual < expected,
                "lte": actual <= expected,
            }[operator]
        if not passed:
            return False
    return True


class StrategyService:
    def __init__(self, database: RuntimeDatabase, writer: StorageWriter):
        self.database = database
        self.writer = writer

    def save(
        self,
        *,
        tool_user_id: str,
        target_uid: str,
        title: str,
        priority: int,
        trigger_level: str,
        trigger: dict[str, Any],
        strategy_type: str = "retarget_create",
        action_params: dict[str, Any] | None = None,
        enabled: bool = False,
        cooldown_minutes: int = DEFAULT_CANDIDATE_COOLDOWN_MINUTES,
        strategy_id: str | None = None,
    ) -> str:
        if trigger_level not in {"material", "product"}:
            raise StrategyValidationError("触发层级必须为 material 或 product")
        if strategy_type not in {"retarget_create", "retarget_pause", "retarget_adjust"}:
            raise StrategyValidationError("策略类型无效")
        if priority < 1:
            raise StrategyValidationError("优先级必须大于0")
        if cooldown_minutes < 0:
            raise StrategyValidationError("冷却时间不能小于0")
        normalized = validate_trigger(trigger)
        now = utc_iso()
        strategy_id = strategy_id or f"strategy_{uuid.uuid4().hex}"
        existing = self.database.query_one(
            "SELECT * FROM strategy WHERE strategy_id=? AND tool_user_id=?",
            (strategy_id, tool_user_id),
        )
        version = int(existing["version"]) + 1 if existing else 1
        if existing:
            self.writer.execute(
                """
                UPDATE strategy SET title=?, priority=?, trigger_level=?,
                    strategy_type=?, enabled=?, action_mode='dry_run',
                    trigger_json=?, action_params_json=?, cooldown_minutes=?,
                    version=?, updated_at=?
                WHERE strategy_id=? AND tool_user_id=?
                """,
                (
                    title.strip(),
                    priority,
                    trigger_level,
                    strategy_type,
                    int(enabled),
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    json.dumps(action_params or {}, ensure_ascii=False, sort_keys=True),
                    cooldown_minutes,
                    version,
                    now,
                    strategy_id,
                    tool_user_id,
                ),
            )
        else:
            self.writer.execute(
                """
                INSERT INTO strategy(
                    strategy_id, tool_user_id, target_uid, strategy_type,
                    trigger_level, title, priority, enabled, action_mode,
                    trigger_json, action_params_json, cooldown_minutes,
                    version, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'dry_run', ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    tool_user_id,
                    target_uid,
                    strategy_type,
                    trigger_level,
                    title.strip(),
                    priority,
                    int(enabled),
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    json.dumps(action_params or {}, ensure_ascii=False, sort_keys=True),
                    cooldown_minutes,
                    version,
                    now,
                    now,
                ),
            )
        return strategy_id

    def set_enabled(self, tool_user_id: str, strategy_id: str, enabled: bool) -> None:
        changed = self.writer.execute(
            "UPDATE strategy SET enabled=?, version=version+1, updated_at=? WHERE tool_user_id=? AND strategy_id=?",
            (int(enabled), utc_iso(), tool_user_id, strategy_id),
        )
        if changed != 1:
            raise KeyError(strategy_id)

    def reorder(self, tool_user_id: str, ordered_strategy_ids: list[str]) -> None:
        def op(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for priority, strategy_id in enumerate(ordered_strategy_ids, start=1):
                    cursor = conn.execute(
                        "UPDATE strategy SET priority=?, version=version+1, updated_at=? WHERE tool_user_id=? AND strategy_id=?",
                        (priority, utc_iso(), tool_user_id, strategy_id),
                    )
                    if cursor.rowcount != 1:
                        raise KeyError(strategy_id)
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

        self.writer.submit(op)

    def list_for_target(self, tool_user_id: str, target_uid: str) -> list[dict[str, Any]]:
        rows = self.database.query_all(
            """
            SELECT * FROM strategy
            WHERE tool_user_id=? AND target_uid=?
            ORDER BY priority ASC, created_at ASC
            """,
            (tool_user_id, target_uid),
        )
        for row in rows:
            row["trigger"] = json.loads(row.pop("trigger_json"))
            row["action_params"] = json.loads(row.pop("action_params_json"))
        return rows
