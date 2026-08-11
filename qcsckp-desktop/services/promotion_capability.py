"""监控目标写操作能力证据的作用域校验与持久化。

乘方目标不能因为旧版 ``*_execute=true`` 就复用全域写操作。只有同时记录
推广场景、计划体系、探测器版本和验证时间的目标级证据才允许执行。
传统全域的旧证据继续兼容；直播全域原有无需探测的链路也保持兼容。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Tuple

from services.plan_system import normalize_plan_system


RETARGET_FORM_PROBE_VERSION = "retarget-form-v3"
MANUAL_RETARGET_PROBE_VERSION = "manual-retarget-submit-v1"
REGULATION_MANUAL_PROBE_VERSION = "manual-stop-batch-v1"
OFFICIAL_API_CAPABILITY_VERSION = "official-open-api-v1"
CAPABILITY_MAX_AGE_DAYS = 30

_ACTION_FIELDS = {
    "retarget": (
        "retarget_execute",
        "retarget_scene",
        "retarget_plan_system",
        "retarget_probe_version",
        "retarget_verified_at",
        "retarget_target_uid",
        "retarget_aavid",
        "retarget_ad_id",
    ),
    "regulation": (
        "regulation_execute",
        "regulation_scene",
        "regulation_plan_system",
        "regulation_probe_version",
        "regulation_verified_at",
        "regulation_target_uid",
        "regulation_aavid",
        "regulation_ad_id",
    ),
}

_ACTION_PROBE_VERSIONS = {
    "retarget": {
        RETARGET_FORM_PROBE_VERSION,
        MANUAL_RETARGET_PROBE_VERSION,
        OFFICIAL_API_CAPABILITY_VERSION,
    },
    "regulation": {
        REGULATION_MANUAL_PROBE_VERSION,
        OFFICIAL_API_CAPABILITY_VERSION,
    },
}


def parse_target_capability(target_or_capability: Any) -> Dict[str, Any]:
    """接受目标行、capability 字典或 JSON，始终返回独立字典。"""
    raw = target_or_capability
    if isinstance(raw, Mapping):
        if isinstance(raw.get("capability"), Mapping):
            raw = raw.get("capability")
        elif "capability_json" in raw:
            raw = raw.get("capability_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _normalize_scene(value: Any) -> str:
    scene = str(value or "").strip().lower()
    return scene if scene in {"live", "product"} else ""


def capability_is_required(*, promotion_scene: Any, plan_system: Any) -> bool:
    """商品或乘方写操作必须具备能力证据；直播全域兼容原有链路。"""
    return (
        _normalize_scene(promotion_scene) == "product"
        or normalize_plan_system(plan_system or "unknown") == "chengfang"
    )


def _verified_at_is_recent(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
    age = now - parsed
    return -timedelta(minutes=5) <= age <= timedelta(
        days=CAPABILITY_MAX_AGE_DAYS
    )


def check_target_capability(
    target_or_capability: Any,
    *,
    action: str,
    promotion_scene: Any,
    plan_system: Any,
    require_batch: bool = False,
) -> Tuple[bool, str]:
    """校验某一写操作的目标级证据，并返回 ``(通过, 原因)``。"""
    fields = _ACTION_FIELDS.get(str(action or "").strip().lower())
    if not fields:
        return False, "未知的能力类型"
    scene = _normalize_scene(promotion_scene)
    system = normalize_plan_system(plan_system or "unknown")
    if not scene:
        return False, "推广场景无效"
    if system == "unknown":
        return False, "计划体系尚未确认"

    capability = parse_target_capability(target_or_capability)
    (
        execute_key,
        scene_key,
        system_key,
        version_key,
        verified_key,
        target_key,
        aavid_key,
        ad_id_key,
    ) = fields
    scope_keys = (
        scene_key,
        system_key,
        version_key,
        verified_key,
        target_key,
        aavid_key,
        ad_id_key,
    )
    has_scope = any(
        str(capability.get(key) or "").strip()
        for key in scope_keys
    )
    required = capability_is_required(
        promotion_scene=scene,
        plan_system=system,
    )

    # 升级前可能只有 *_execute=true，没有作用域字段；仅兼容这类已经实际
    # 验证过的全域证据。新目标不能因为被手动标成“全域”就在没有任何证据
    # 的情况下获得写权限。
    if not required and not has_scope:
        if bool(capability.get(execute_key)):
            if action == "retarget" and require_batch:
                return False, "多素材追投能力尚未通过受控验证"
            return True, ""
        return False, f"{action} 执行能力尚未通过受控验证"
    if not bool(capability.get(execute_key)):
        return False, f"{action} 执行能力尚未通过受控验证"

    if not has_scope:
        # 仅兼容升级前已经验证过的直播全域裸布尔证据。商品旧探测没有
        # 覆盖完整表单，乘方也从未建立独立作用域，因此两者必须重新验证。
        if scene == "live" and system == "global":
            if action == "retarget" and require_batch:
                return False, "多素材追投能力尚未通过受控验证"
            return True, ""
        return False, "能力证据缺少目标、场景、体系、版本或验证时间"

    missing = [
        key
        for key in scope_keys
        if not str(capability.get(key) or "").strip()
    ]
    if missing:
        return False, "能力证据字段不完整：" + "、".join(missing)
    if _normalize_scene(capability.get(scene_key)) != scene:
        return False, "能力证据的推广场景与监控目标不一致"
    if normalize_plan_system(capability.get(system_key)) != system:
        return False, "能力证据的计划体系与监控目标不一致"
    probe_version = str(capability.get(version_key) or "").strip()
    if probe_version not in _ACTION_PROBE_VERSIONS[action]:
        return False, "能力证据的探测器版本已过期，请重新验证"
    if not _verified_at_is_recent(capability.get(verified_key)):
        return False, "能力证据已过期或验证时间无效，请重新验证"
    target = (
        target_or_capability
        if isinstance(target_or_capability, Mapping)
        else {}
    )
    expected_uid = str(target.get("target_uid") or "").strip()
    expected_aavid = str(
        target.get("aadvid") or target.get("aavid") or ""
    ).strip()
    expected_ad_id = str(target.get("ad_id") or "").strip()
    if expected_uid and str(capability.get(target_key) or "").strip() != expected_uid:
        return False, "能力证据绑定的监控目标不一致"
    if expected_aavid and str(capability.get(aavid_key) or "").strip() != expected_aavid:
        return False, "能力证据绑定的千川账户不一致"
    if expected_ad_id and str(capability.get(ad_id_key) or "").strip() != expected_ad_id:
        return False, "能力证据绑定的计划ID不一致"
    if action == "retarget" and require_batch:
        if not bool(capability.get("retarget_batch_execute")):
            return False, "多素材追投能力尚未通过受控验证"
        if str(capability.get("retarget_batch_probe_version") or "").strip() not in {
            RETARGET_FORM_PROBE_VERSION,
            OFFICIAL_API_CAPABILITY_VERSION,
        }:
            return False, "多素材追投探测器版本已过期，请重新验证"
        if not _verified_at_is_recent(
            capability.get("retarget_batch_verified_at")
        ):
            return False, "多素材追投能力已过期，请重新验证"
    return True, ""


def record_target_capability(
    db: Any,
    *,
    target_uid: Any,
    action: str,
    promotion_scene: Any,
    plan_system: Any,
    probe_version: Any,
    verified_at: Any = None,
) -> Dict[str, Any]:
    """在重新核对目标作用域后，合并保存一次成功的受控验证证据。"""
    uid = str(target_uid or "").strip()
    fields = _ACTION_FIELDS.get(str(action or "").strip().lower())
    scene = _normalize_scene(promotion_scene)
    system = normalize_plan_system(plan_system or "unknown")
    version = str(probe_version or "").strip()
    if not uid or not fields or not scene or system == "unknown" or not version:
        raise ValueError("能力证据缺少目标、动作、场景、体系或探测器版本")

    with db.transaction() as conn:
        # 与采集状态回写使用同样的写锁顺序，避免双方整份 JSON 互相覆盖。
        db.execute("BEGIN IMMEDIATE", connection=conn)
        target = db.select_one(
            "promotion_target",
            where={"target_uid": uid},
            connection=conn,
        ) or {}
        target_scene = _normalize_scene(target.get("promotion_scene"))
        target_system = normalize_plan_system(
            target.get("plan_system") or "unknown"
        )
        if not target or target_scene != scene or target_system != system:
            raise ValueError("能力证据作用域与当前监控目标不一致")

        capability = parse_target_capability(target)
        (
            execute_key,
            scene_key,
            system_key,
            version_key,
            verified_key,
            target_key,
            aavid_key,
            ad_id_key,
        ) = fields
        capability.update(
            {
                execute_key: True,
                scene_key: scene,
                system_key: system,
                version_key: version,
                verified_key: str(verified_at or "").strip()
                or (datetime.utcnow() + timedelta(hours=8)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                target_key: uid,
                aavid_key: str(
                    target.get("aadvid") or target.get("aavid") or ""
                ).strip(),
                ad_id_key: str(target.get("ad_id") or "").strip(),
            }
        )
        db.update(
            "promotion_target",
            {
                "capability_json": json.dumps(
                    capability,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            },
            where={"target_uid": uid},
            connection=conn,
        )
    return capability
