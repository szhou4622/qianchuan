"""
总 API 接口类
统一暴露所有 API 给前端调用
"""
import sys
import webbrowser
from typing import Any
from urllib.parse import urlparse
from utils.sqlite_store import SQLiteStore, init_sqlite_schema
from .dashboard import DashboardApi
from .account_auth import AccountAuthApi
from config import QIANCHUAN_BACKEND


def _get_service_controller():
    """Keep Playwright out of the official API process import graph."""
    if QIANCHUAN_BACKEND == "official_api":
        from services.official_api_controller import get_official_api_controller

        return get_official_api_controller()
    from services.run_services import get_service_controller

    return get_service_controller()


class Api:
    """总 API 接口"""

    def __init__(self):
        """初始化所有 API 模块"""
        init_sqlite_schema()
        self.db = SQLiteStore()
        from .promotion_targets import migrate_legacy_target_scope

        migrate_legacy_target_scope(db=self.db)
        from services.qianchuan_accounts import (
            migrate_existing_qianchuan_accounts,
        )
        from services.qianchuan_session import migrate_legacy_qcookie

        migrate_existing_qianchuan_accounts(db=self.db)
        try:
            migrate_legacy_qcookie()
        except Exception as exc:
            from utils.log import logger

            logger.warning("[千川会话] 旧Cookie加密迁移暂未完成: %s", exc)
        self.dashboard = DashboardApi()
        self.service = _get_service_controller()
        self.account_auth = AccountAuthApi()

    # ========== 大屏相关 API ==========

    def get_material_history_recent(
        self,
        material_id: str,
        limit: int = 200,
        target_uid: str = None,
    ):
        """获取素材最近 N 条历史点（按 created_at）"""
        return self.dashboard.get_material_history_recent(material_id, limit, target_uid)

    def get_table_data(self, period: str = "1h", sort_by: str = "costDiff", sort_order: str = "desc",
                      page: int = 1, page_size: int = 50, target_uid: str = None):
        """
        获取表格数据（按周期查询素材首尾差值）

        Args:
            period: 查询周期，支持 "1h"(1小时), "15m"(15分钟), "2h" 等，默认 "1h"
            sort_by: 排序字段，默认 "costDiff"
            sort_order: 排序方式 "asc" 或 "desc"，默认 "desc"
            page: 页码，默认 1
            page_size: 每页数量，默认 50

        Returns:
            表格数据
        """
        return self.dashboard.get_table_data(
            period,
            sort_by,
            sort_order,
            page,
            page_size,
            target_uid,
        )

    # ========== 直播 / 商品全域监控计划 ==========

    def getQianchuanAccountOverview(self):
        from api.rule_regulation_config import load_rule_regulation_config
        from api.rule_retargeting_config import load_rule_retargeting_config
        from services.local_feishu_bridge import get_local_feishu_status
        from services.operation_daily_report import (
            get_operation_daily_report_config,
        )
        from services.promotion_browser_lock import browser_queue_snapshot
        from services.qianchuan_accounts import (
            capacity_snapshot_readonly,
            list_qianchuan_accounts,
        )
        from api.promotion_targets import list_promotion_targets
        from services.qianchuan_session import session_status
        from services.qianchuan_catalog import catalog_sync_status
        from services.windows_autostart import get_windows_autostart_status

        try:
            feishu = get_local_feishu_status()
            accounts = list_qianchuan_accounts(
                db=self.db,
                ensure_schema=False,
                perform_repairs=False,
            )
            targets = list_promotion_targets(
                db=self.db,
                ensure_schema=False,
                perform_repairs=False,
            )
            if QIANCHUAN_BACKEND == "official_api":
                from services.official_api_catalog import official_api_session_status

                session = official_api_session_status()
            else:
                session = session_status()
            catalog = catalog_sync_status(
                db=self.db,
                accounts_snapshot=accounts,
                targets_snapshot=targets,
                ensure_schema=False,
            )
            if catalog.get("failure_kind") == "login_required":
                session = {
                    **session,
                    "status": "login_required",
                    "last_error": str(
                        catalog.get("error")
                        or catalog.get("message")
                        or "千川登录状态已失效"
                    ),
                }
            profile = feishu.get("profile") or {}
            feishu_bound = bool(feishu.get("connected")) and bool(
                str(profile.get("authorized_open_id") or "").strip()
            )
            enabled_accounts = [item for item in accounts if item.get("enabled")]
            plans_selected = any(
                item.get("enabled") and item.get("monitor_eligible")
                for item in targets
            )
            retarget_config = load_rule_retargeting_config()
            stop_config = load_rule_regulation_config()
            rules_saved = bool(retarget_config.get("enabled")) or bool(
                stop_config.get("enabled")
            )
            onboarding = {
                "qianchuan_login": bool(session.get("available"))
                and session.get("status") != "login_required",
                "catalog_synced": bool(catalog.get("complete")),
                "catalog_attempted": bool(catalog.get("account_count"))
                and catalog.get("status") not in {"not_synced", "syncing"},
                "catalog_complete": bool(catalog.get("complete")),
                "feishu_bound": feishu_bound,
                "account_routes": (
                    feishu_bound
                    and all(
                        item.get("route_mode") in {"default", "custom"}
                        for item in enabled_accounts
                    )
                ),
                "plans_selected": plans_selected,
                "rules_configured": rules_saved and plans_selected,
                "rules_saved_without_eligible_plan": (
                    rules_saved and not plans_selected
                ),
            }
            return {
                "success": True,
                "accounts": accounts,
                "targets": targets,
                "capacity": capacity_snapshot_readonly(db=self.db),
                "session": session,
                "catalog": catalog,
                "onboarding": onboarding,
                "browser_queue": (
                    {
                        "running": False,
                        "waiting": 0,
                        "backend": "official_api",
                        "message": "官方 API 调度器；不启动 Chrome",
                    }
                    if QIANCHUAN_BACKEND == "official_api"
                    else browser_queue_snapshot()
                ),
                "feishu": {
                    "connected": bool(feishu.get("connected")),
                    "authorized_open_id": str(
                        (feishu.get("profile") or {}).get("authorized_open_id")
                        or ""
                    ),
                    "groups": (feishu.get("profile") or {}).get("groups") or [],
                },
                "autostart": get_windows_autostart_status(),
                "daily_report": get_operation_daily_report_config(
                    account_options=accounts,
                ),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def saveQianchuanAccountSettings(self, account_uid=None, settings=None):
        from services.qianchuan_accounts import save_qianchuan_account_settings

        try:
            return {
                "success": True,
                "data": save_qianchuan_account_settings(
                    account_uid,
                    settings if isinstance(settings, dict) else {},
                    db=self.db,
                ),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def saveQianchuanAccountAutomationSetup(
        self,
        account_uid=None,
        settings=None,
        plan_states=None,
    ):
        from services.qianchuan_accounts import (
            save_qianchuan_account_automation_setup,
        )

        try:
            saved = save_qianchuan_account_automation_setup(
                account_uid,
                settings if isinstance(settings, dict) else {},
                plan_states if isinstance(plan_states, (list, dict)) else [],
                db=self.db,
            )
        except Exception as e:
            return {"success": False, "message": str(e)}
        if QIANCHUAN_BACKEND == "official_api":
            monitoring = {
                "success": True,
                "running": bool(saved.get("enabled")),
                "phase": "official_api_scheduled",
                "backend": "official_api",
                "message": "设置已保存，官方 API 后台采集会按计划运行",
            }
        else:
            try:
                monitoring = self.service.start_from_saved_session()
            except Exception as e:
                monitoring = {
                "success": False,
                "running": False,
                "phase": "start_failed",
                "message": f"设置已保存，但后台监控启动失败：{e}",
                }
        if QIANCHUAN_BACKEND == "official_api":
            try:
                # Official API mode still needs to start its catalog and data
                # schedulers. Saving only a synthetic "scheduled" state leaves
                # the selected plan permanently stale.
                monitoring = self.service.start_from_saved_session()
                enabled_target_uids = [
                    str(item or "").strip()
                    for item in saved.get("immediate_collection_target_uids") or []
                    if str(item or "").strip()
                ]
                if bool(saved.get("enabled")) and enabled_target_uids:
                    from services.official_api_collection import (
                        request_official_api_collection,
                    )

                    monitoring = request_official_api_collection(
                        enabled_target_uids,
                        db=self.db,
                    )
            except Exception as e:
                monitoring = {
                    "success": False,
                    "running": False,
                    "phase": "start_failed",
                    "message": f"设置已保存，但千川官方 API 采集启动失败：{e}",
                }
        operation_log_sync = {
            "success": True,
            "running": False,
            "message": "账户未启用，不启动流水同步",
        }
        if bool(saved.get("enabled")):
            try:
                from services.operation_log_monitor import (
                    request_platform_log_sync,
                )

                operation_log_sync = request_platform_log_sync(
                    saved.get("aavid"),
                    db=self.db,
                )
            except Exception as e:
                operation_log_sync = {
                    "success": False,
                    "running": False,
                    "message": f"账户设置已保存，但流水同步启动失败：{e}",
                }
        return {
            "success": True,
            "data": saved,
            "monitoring": monitoring,
            "operation_log_sync": operation_log_sync,
            "message": str(
                monitoring.get("message")
                or "账户、日报路由和监控计划已保存"
            ),
        }

    def removeQianchuanAccount(self, account_uid=None):
        from services.qianchuan_accounts import remove_qianchuan_account

        try:
            return {
                "success": True,
                "data": remove_qianchuan_account(
                    account_uid,
                    db=self.db,
                ),
                "message": "账户已从工具中移除，相关自动化和日报已关闭",
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def startQianchuanCatalogSync(self, account_uid=None):
        try:
            if QIANCHUAN_BACKEND == "official_api":
                from services.official_api_catalog import start_official_api_catalog_sync

                return start_official_api_catalog_sync(account_uid)
            return self.service.start_catalog_sync(account_uid)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def getQianchuanCatalogSyncStatus(self):
        try:
            if QIANCHUAN_BACKEND == "official_api":
                from services.official_api_catalog import official_api_catalog_status

                return official_api_catalog_status()
            return self.service.catalog_sync_status()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def setWindowsAutostart(self, enabled=False):
        from services.windows_autostart import set_windows_autostart

        try:
            return set_windows_autostart(bool(enabled))
        except Exception as e:
            return {"success": False, "message": str(e)}

    def restoreRc23QianchuanCookie(self):
        from services.qianchuan_session import restore_rc23_cookie

        try:
            return restore_rc23_cookie()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def listPromotionTargets(self, enabled=None):
        from .promotion_targets import list_promotion_targets

        try:
            enabled_filter = None
            if enabled is not None:
                enabled_filter = (
                    str(enabled).strip().lower() not in ("", "0", "false", "no", "off")
                    if isinstance(enabled, str)
                    else bool(enabled)
                )
            return {
                "success": True,
                "data": list_promotion_targets(enabled=enabled_filter, db=self.db),
            }
        except Exception as e:
            return {"success": False, "message": str(e), "data": []}

    def getPromotionTarget(self, target_uid=None):
        from .promotion_targets import get_promotion_target

        try:
            target = get_promotion_target(target_uid, db=self.db)
            if not target:
                return {"success": False, "message": "监控计划不存在"}
            return {"success": True, "data": target}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def savePromotionTarget(self, data=None):
        from .promotion_targets import upsert_promotion_target

        try:
            return {
                "success": True,
                "data": upsert_promotion_target(data or {}, db=self.db),
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def discoverPromotionTarget(self, page_url=None, page_text=None, plan_name=None):
        from .promotion_targets import (
            detect_promotion_scene,
            extract_target_ids,
            upsert_promotion_target,
        )

        try:
            url = str(page_url or "").strip()
            aavid, ad_id = extract_target_ids(url)
            scene = detect_promotion_scene(url, page_text=str(page_text or ""))
            if not aavid or not ad_id:
                return {
                    "success": False,
                    "message": "当前页面未识别到账户或计划，请打开千川计划详情页后再试",
                }
            if not scene:
                return {
                    "success": False,
                    "message": "无法确认是直播还是商品全域计划，已安全停止添加",
                }
            target = upsert_promotion_target(
                {
                    "aavid": aavid,
                    "ad_id": ad_id,
                    "plan_name": plan_name or "",
                    "promotion_scene": scene,
                    "page_url": url,
                    "enabled": True,
                },
                db=self.db,
            )
            return {"success": True, "data": target}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def setPromotionTargetEnabled(self, target_uid=None, enabled=True):
        from .promotion_targets import set_promotion_target_enabled

        try:
            target = set_promotion_target_enabled(
                target_uid,
                (
                    str(enabled).strip().lower() not in ("", "0", "false", "no", "off")
                    if isinstance(enabled, str)
                    else bool(enabled)
                ),
                db=self.db,
            )
            return {"success": True, "data": target}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def clearPromotionTargetWriteBlock(self, target_uid=None):
        from .promotion_targets import set_target_automation_write_block

        try:
            return {
                "success": True,
                "data": set_target_automation_write_block(
                    target_uid,
                    False,
                    db=self.db,
                ),
                "message": "自动写入安全封锁已解除",
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def listPromotionTargetProducts(self, target_uid=None):
        from .promotion_targets import list_target_products

        try:
            return {
                "success": True,
                "data": list_target_products(target_uid, db=self.db),
            }
        except Exception as e:
            return {"success": False, "message": str(e), "data": []}

    def probePromotionTargetRetargetCapability(
        self,
        target_uid=None,
        material_id=None,
    ):
        """只读验证直播/商品计划完整追投表单；不填写任何字段、不点击提交。"""
        import asyncio
        import datetime

        from .promotion_targets import (
            get_promotion_target,
            patch_target_sync_state,
        )
        from .rule_retargeting_config import load_rule_retargeting_config
        from services.plan_system import normalize_plan_system
        from services.promotion_browser_lock import exclusive_browser_operation
        from services.promotion_capability import (
            check_target_capability,
            record_target_capability,
        )

        uid = str(target_uid or "").strip()
        target = get_promotion_target(uid, db=self.db)
        if not target:
            return {"success": False, "message": "监控计划不存在"}
        if not target.get("enabled"):
            return {"success": False, "message": "请先启用监控计划"}
        promotion_scene = str(
            target.get("promotion_scene") or ""
        ).strip().lower()
        if promotion_scene not in ("live", "product"):
            return {"success": False, "message": "推广场景尚未确认"}
        plan_system = normalize_plan_system(
            target.get("plan_system") or "unknown"
        )
        if plan_system == "unknown":
            return {
                "success": False,
                "message": "请先把计划体系明确设置为“全域”或“千川乘方”",
            }
        if QIANCHUAN_BACKEND == "official_api":
            ok, reason = check_target_capability(
                target,
                action="retarget",
                promotion_scene=promotion_scene,
                plan_system=plan_system,
            )
            return {
                "success": ok,
                "backend": "official_api",
                "message": (
                    "官方 API 已完成账户、计划、场景和体系能力校验"
                    if ok
                    else f"官方 API 能力尚未就绪：{reason}；请先刷新该账户计划"
                ),
                "data": {"verified": ok, "reason": reason},
            }
        from services.retargeting_service import (
            QianChuanRetargetingService,
            RETARGET_PROBE_VERSION,
        )
        if str(target.get("last_status") or "").strip().lower() != "ok":
            return {
                "success": False,
                "message": "计划当前不是正常投放状态，不能验证追投入口",
            }
        requested_material_id = str(material_id or "").strip()
        if requested_material_id:
            material_rows = self.db.execute(
                "SELECT material_id FROM pmc_promotion_material "
                "WHERE target_uid=? AND material_id=? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (uid, requested_material_id),
                fetch=True,
            )
        else:
            material_rows = self.db.execute(
                "SELECT material_id FROM pmc_promotion_material "
                "WHERE target_uid=? AND material_id IS NOT NULL "
                "AND TRIM(material_id)<>'' "
                "GROUP BY material_id "
                "ORDER BY MAX(prepay_pay_settle_1h) DESC, "
                "MAX(stat_cost) DESC, MAX(updated_at) DESC LIMIT 2",
                (uid,),
                fetch=True,
            )
        probe_material_ids = [
            str(row.get("material_id") or "").strip()
            for row in material_rows or []
            if str(row.get("material_id") or "").strip()
        ]
        probe_material_id = probe_material_ids[0] if probe_material_ids else ""
        if not probe_material_id:
            return {
                "success": False,
                "message": (
                    "指定素材不属于该计划"
                    if requested_material_id
                    else "该计划尚未采集到素材，无法验证追投入口"
                ),
            }
        try:
            aavid = int(str(target.get("aadvid") or "").strip())
            ad_id = int(str(target.get("ad_id") or "").strip())
        except (TypeError, ValueError):
            return {"success": False, "message": "计划的账户ID或计划ID无效"}

        cfg = load_rule_retargeting_config()

        async def run_probe():
            service = QianChuanRetargetingService.from_rule_file_dict(cfg)
            async with exclusive_browser_operation(f"商品追投能力验证:{uid}"):
                return await service.probe_product_retarget_capability(
                    aavid=aavid,
                    ad_id=ad_id,
                    material_id=probe_material_id,
                    material_ids=probe_material_ids,
                    target_uid=uid,
                    promotion_scene=promotion_scene,
                    plan_system=plan_system,
                    source_url=target.get("sanitized_page_url") or None,
                )

        try:
            result = asyncio.run(run_probe())
        except Exception as exc:
            patch_target_sync_state(
                uid,
                status=None,
                error=f"追投能力验证异常：{exc}",
                capability_updates={
                    "retarget_execute": False,
                    "retarget_batch_execute": False,
                },
                capability_remove_keys=(
                    "retarget_scene",
                    "retarget_plan_system",
                    "retarget_probe_version",
                    "retarget_verified_at",
                    "retarget_target_uid",
                    "retarget_aavid",
                    "retarget_ad_id",
                    "retarget_batch_probe_version",
                    "retarget_batch_verified_at",
                ),
                db=self.db,
            )
            return {
                "success": False,
                "message": f"能力验证异常：{exc}",
                "target": get_promotion_target(uid, db=self.db),
            }

        if not result.success:
            patch_target_sync_state(
                uid,
                status=None,
                error=f"追投能力验证失败：{result.message}",
                capability_updates={
                    "retarget_execute": False,
                    "retarget_batch_execute": False,
                },
                capability_remove_keys=(
                    "retarget_scene",
                    "retarget_plan_system",
                    "retarget_probe_version",
                    "retarget_verified_at",
                    "retarget_target_uid",
                    "retarget_aavid",
                    "retarget_ad_id",
                    "retarget_batch_probe_version",
                    "retarget_batch_verified_at",
                ),
                db=self.db,
            )
            return {
                "success": False,
                "message": result.message,
                "data": result.asdict(),
                "target": get_promotion_target(uid, db=self.db),
            }

        verified_at = (
            datetime.datetime.now()
            .astimezone()
            .isoformat(timespec="seconds")
        )
        record_target_capability(
            self.db,
            target_uid=uid,
            action="retarget",
            promotion_scene=promotion_scene,
            plan_system=plan_system,
            probe_version=RETARGET_PROBE_VERSION,
            verified_at=verified_at,
        )
        batch_updates = {
            "retarget_batch_execute": len(probe_material_ids) >= 2,
        }
        batch_remove_keys = ()
        if len(probe_material_ids) >= 2:
            batch_updates.update(
                {
                    "retarget_batch_probe_version": RETARGET_PROBE_VERSION,
                    "retarget_batch_verified_at": verified_at,
                }
            )
        else:
            batch_remove_keys = (
                "retarget_batch_probe_version",
                "retarget_batch_verified_at",
            )
        patch_target_sync_state(
            uid,
            status=None,
            error="",
            capability_updates=batch_updates,
            capability_remove_keys=batch_remove_keys,
            db=self.db,
        )
        return {
            "success": True,
            "message": result.message,
            "data": result.asdict(),
            "target": get_promotion_target(uid, db=self.db),
        }

    def startPromotionTargetDiscovery(self):
        try:
            if QIANCHUAN_BACKEND == "official_api":
                from services.official_api_catalog import discover_authorized_accounts

                return discover_authorized_accounts()
            return self.service.start_target_discovery()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def startQianchuanAccountSelection(self):
        try:
            if QIANCHUAN_BACKEND == "official_api":
                from services.official_api_catalog import discover_authorized_accounts

                return discover_authorized_accounts()
            return self.service.start_target_discovery(account_only=True)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def getQianchuanOfficialApiConfig(self):
        if QIANCHUAN_BACKEND != "official_api":
            return {"success": False, "message": "当前未启用千川官方 API 模式"}
        from services.qianchuan_open_api.configuration import get_configuration
        return get_configuration()

    def saveQianchuanOfficialApiConfig(self, config=None):
        if QIANCHUAN_BACKEND != "official_api":
            return {"success": False, "message": "当前未启用千川官方 API 模式"}
        from services.qianchuan_open_api.configuration import save_configuration
        payload = config if isinstance(config, dict) else {}
        return save_configuration(payload.get("app_id"), payload.get("app_secret"))

    def startQianchuanOfficialApiAuthorization(self):
        if QIANCHUAN_BACKEND != "official_api":
            return {"success": False, "message": "当前未启用千川官方 API 模式"}
        from services.qianchuan_open_api.configuration import start_authorization
        return start_authorization()

    def saveAndStartQianchuanOfficialApiAuthorization(self, config=None):
        if QIANCHUAN_BACKEND != "official_api":
            return {"success": False, "message": "当前未启用千川官方 API 模式"}
        from services.qianchuan_open_api.configuration import save_and_start_authorization
        payload = config if isinstance(config, dict) else {}
        return save_and_start_authorization(
            payload.get("app_id"),
            payload.get("app_secret"),
        )

    def finishQianchuanOfficialApiAuthorization(self, authCode=None):
        if QIANCHUAN_BACKEND != "official_api":
            return {"success": False, "message": "当前未启用千川官方 API 模式"}
        from services.qianchuan_open_api.configuration import finish_authorization
        result = finish_authorization(authCode)
        if result.get("success") and result.get("completed") and result.get("authorized"):
            try:
                result["monitoring"] = self.service.start_from_saved_session()
            except Exception as exc:
                result["monitoring"] = {
                    "success": False,
                    "running": False,
                    "phase": "start_failed",
                    "message": f"Authorization succeeded, but the official API collector failed to start: {exc}",
                }
        return result

    def clearQianchuanOfficialApiConfig(self):
        if QIANCHUAN_BACKEND != "official_api":
            return {"success": False, "message": "当前未启用千川官方 API 模式"}
        from services.qianchuan_open_api.configuration import disconnect_configuration
        return disconnect_configuration()

    def startQianchuanRelogin(self):
        try:
            if QIANCHUAN_BACKEND == "official_api":
                from services.qianchuan_open_api.configuration import start_authorization
                return start_authorization()
            return self.service.start_target_discovery(login_only=True)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def getPromotionTargetDiscoveryStatus(self):
        try:
            if QIANCHUAN_BACKEND == "official_api":
                return {
                    "success": True,
                    "running": False,
                    "backend": "official_api",
                    "message": "官方 API 模式不使用浏览器识别账户",
                }
            return self.service.target_discovery_status()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_top20_by_cost(self, hours: int = 1):
        """
        获取最近 N 小时内每个素材最新的一条数据，按整体消耗排序取 Top 20

        Args:
            hours: 最近多少小时，默认 1 小时

        Returns:
            Top 20 素材列表，按 stat_cost 降序
        """
        return self.dashboard.get_top20_by_cost(hours)

    def get_latest_crawl_cost_sum(self, hours: int = 1):
        """周期内（最近 N 小时）最晚一批入库记录的消耗总和，与 Top20 时间窗一致。"""
        return self.dashboard.get_latest_crawl_cost_sum(hours)

    def get_dashboard_account_label(self):
        """大屏账户标注（存 data/dashboard_account_label.json）。"""
        return self.dashboard.get_dashboard_account_label()

    def set_dashboard_account_label(self, label: str = None):
        return self.dashboard.set_dashboard_account_label(label or "")

    def get_roi2_assist_table_data(
        self,
        aadvid: str = None,
        sort_by: str = "stat_cost_for_roi2_assist",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 50,
        search: str = None,
        ad_delivery_type: int = None,
        target_uid: str = None,
    ):
        """调控任务表（pmc_roi2_assist_task）分页数据，供大屏侧栏展示。"""
        return self.dashboard.get_roi2_assist_table_data(
            aadvid, sort_by, sort_order, page, page_size,
            search=search, ad_delivery_type=ad_delivery_type, target_uid=target_uid
        )

    # ========== 服务控制相关 API ==========

    def _start_denied_response(self, message: str) -> dict:
        """账号未通过校验时返回与 status 结构兼容的对象（不启动线程）。"""
        st = self.service.status()
        out = dict(st)
        out["success"] = False
        out["phase"] = "error"
        out["message"] = message
        return out

    def startService(self, interval: int = None, headful: bool = True, username: str = None, password: str = None):
        """
        启动服务（必须传入账号密码，并由当前认证模式校验通过后才真正启动）。

        Args:
            interval: 轮询间隔（秒）
            headful: 轮询阶段是否无头（True=无头）；登录识别阶段始终有头浏览器
            username: 普通用户账号
            password: 密码
        """
        u = (username or "").strip()
        p = password if password is not None else ""
        if not u or not p:
            return self._start_denied_response("启动采集须传入账号与密码并通过本机校验")
        from services.cloud_retarget_client import load_device_session

        old_owner = str(
            (load_device_session() or {}).get("username") or ""
        ).strip().casefold()
        new_owner = u.casefold()
        if old_owner and old_owner != new_owner:
            stopped = self.service.stop_and_wait(30)
            if stopped.get("running"):
                return self._start_denied_response(
                    "旧工具账号的千川会话仍在安全退出，请稍后重新启动"
                )
        chk = self.account_auth.verify_can_start_service(u, p)
        if not chk.get("ok"):
            return self._start_denied_response(chk.get("message") or "账号校验失败")
        if old_owner and old_owner != new_owner:
            from services.local_feishu_bridge import (
                cancel_active_local_retarget_tasks,
            )

            cancel_active_local_retarget_tasks(
                old_owner,
                "工具账号已经切换，旧追投提醒已作废",
            )
        from services.local_feishu_bridge import activate_local_feishu_account

        activate_local_feishu_account(u)
        self.service.set_cloud_backup_credentials(u, p)
        # 与界面一致：写入 control_panel.json → crawl 后再启动线程
        from services.control_panel_config import load_scrape_service_config, save_scrape_service_config

        cur = load_scrape_service_config()
        try:
            iv = int(interval) if interval is not None else int(cur.get("interval_seconds") or 600)
        except Exception:
            iv = 600
        iv = max(5, iv)
        save_scrape_service_config(interval_seconds=iv, headless_poll=bool(headful))
        return self.service.start()

    def stopService(self):
        return self.service.stop()

    def getServiceStatus(self):
        return self.service.status()

    def readLogs(self, limit: int = 50):
        return self.service.read_logs(limit)

    def clearLogs(self):
        return self.service.clear_logs()

    def setServiceInterval(self, interval: int):
        """
        更新轮询间隔（在下一轮抓取完成后生效）

        Args:
            interval: 轮询间隔（秒）
        """
        return self.service.setInterval(interval)

    def setFeishuBitableConfig(
        self,
        app_token: str = None,
        personal_base_token: str = None,
        table_id: str = None,
        enabled: bool = None,
        push_mode: str = None,
    ):
        """
        更新飞书多维表连接信息（app_token / personal_base_token / table_id / enabled / push_mode）。
        与轮询间隔相同：前端可随时同步，仅在下一轮抓取开始时生效。
        """
        return self.service.setFeishuBitableConfig(
            app_token=app_token,
            personal_base_token=personal_base_token,
            table_id=table_id,
            enabled=enabled,
            push_mode=push_mode,
        )

    def getScrapeServicePanelConfig(self):
        """抓取服务 Tab：control_panel.json → crawl"""
        from services.control_panel_config import load_scrape_service_config
        from utils.common import configured_chrome_path_or_empty, default_browser_executable_hint

        c = load_scrape_service_config()
        stored = configured_chrome_path_or_empty(c.get("browser_executable_path"))
        display_path = stored if stored else default_browser_executable_hint()
        return {
            "success": True,
            "interval_seconds": c["interval_seconds"],
            "headless_poll": c["headless_poll"],
            "fetch_assist_tasks": c["fetch_assist_tasks"],
            "browser_executable_path": display_path,
        }

    def setScrapeServicePanelConfig(
        self,
        interval_seconds: int = None,
        headless_poll: bool = None,
        fetch_assist_tasks: bool = None,
        browser_executable_path: str = None,
    ):
        """实时写入抓取配置（control_panel.json → crawl，与 setServiceInterval / 启动前写入一致）。"""
        from services.control_panel_config import save_scrape_service_config

        save_scrape_service_config(
            interval_seconds=interval_seconds,
            headless_poll=headless_poll,
            fetch_assist_tasks=fetch_assist_tasks,
            browser_executable_path=browser_executable_path,
        )
        return {"success": True}

    def getFeishuBitablePanelConfig(self):
        """飞书表格 Tab：control_panel.json → feishu_table"""
        from services.control_panel_config import load_feishu_bitable_panel_config

        c = load_feishu_bitable_panel_config()
        return {"success": True, **c}

    def getFeishuWebhookPushConfig(self):
        """飞书机器人 Webhook（control_panel.json → robot.feishu）。"""
        from services.feishu_webhook_push import load_feishu_webhook_push_config
        c = load_feishu_webhook_push_config()
        return {"success": True, **c}

    def setFeishuWebhookPushConfig(self, enabled=None, webhook=None, keyword=None):
        from services.feishu_webhook_push import save_feishu_webhook_push_config

        save_feishu_webhook_push_config(enabled=enabled, webhook=webhook, keyword=keyword)
        # 仅返回最小 dict，与 setFeishuBitableConfig 一致，避免 pywebview 返回体过大/序列化导致前端 await 异常
        return {"success": True}

    def testFeishuWebhookPush(self):
        """立即按当前文件配置推送一次（用于验证 Webhook / 关键词；不要求勾选启用）。"""
        from services.feishu_webhook_push import run_feishu_webhook_push_once
        return run_feishu_webhook_push_once(self.dashboard, ignore_enabled=True)

    def getDingtalkWebhookPushConfig(self):
        """钉钉机器人 Webhook（control_panel.json → robot.dingtalk）。"""
        from services.dingtalk_webhook_push import load_dingtalk_webhook_push_config
        c = load_dingtalk_webhook_push_config()
        return {"success": True, **c}

    def setDingtalkWebhookPushConfig(self, enabled=None, webhook=None, keyword=None):
        from services.dingtalk_webhook_push import save_dingtalk_webhook_push_config

        save_dingtalk_webhook_push_config(enabled=enabled, webhook=webhook, keyword=keyword)
        return {"success": True}

    def testDingtalkWebhookPush(self):
        """立即按当前文件配置推送一次（用于验证 Webhook / 关键词；不要求勾选启用）。"""
        from services.dingtalk_webhook_push import run_dingtalk_webhook_push_once
        return run_dingtalk_webhook_push_once(self.dashboard, ignore_enabled=True)

    # ========== 工具账号登录校验 ==========

    def verify_account_login(self, username: str, password: str):
        """
        校验普通用户账号与密码；本地独立版不访问中心服务器。
        """
        from services.cloud_retarget_client import load_device_session

        old_owner = str(
            (load_device_session() or {}).get("username") or ""
        ).strip().casefold()
        new_owner = str(username or "").strip().casefold()
        if old_owner and old_owner != new_owner:
            stopped = self.service.stop_and_wait(30)
            if stopped.get("running"):
                return {
                    "success": False,
                    "message": (
                        "旧工具账号的千川会话仍在安全退出，"
                        "请稍后重新登录"
                    ),
                }
        result = self.account_auth.verify_login(username, password)
        data = result.get("data") if isinstance(result, dict) else None
        if (
            isinstance(result, dict)
            and result.get("success")
            and isinstance(data, dict)
            and int(data.get("is_disabled") or 0) != 1
            and self.account_auth._is_within_validity(data)
        ):
            if old_owner and old_owner != new_owner:
                from services.local_feishu_bridge import (
                    cancel_active_local_retarget_tasks,
                )

                cancel_active_local_retarget_tasks(
                    old_owner,
                    "工具账号已经切换，旧追投提醒已作废",
                )
                try:
                    from services.operation_log_monitor import stop_record_browser

                    stop_record_browser()
                except Exception:
                    pass
            from services.local_feishu_bridge import activate_local_feishu_account

            activate_local_feishu_account(username)
            try:
                from services.qianchuan_accounts import (
                    migrate_existing_qianchuan_accounts,
                )
                from services.qianchuan_session import migrate_legacy_qcookie

                migrate_legacy_qcookie()
                migrate_existing_qianchuan_accounts(
                    owner_username=username,
                    db=self.db,
                )
            except Exception as exc:
                from utils.log import logger

                logger.warning("[多账户迁移] 工具账号登录后的迁移暂未完成: %s", exc)
        return result

    def clearDeviceSession(self):
        from services.local_feishu_bridge import deactivate_local_feishu_account
        from services.cloud_retarget_client import (
            clear_device_session,
            load_device_session,
        )

        old_owner = str(
            (load_device_session() or {}).get("username") or ""
        ).strip().casefold()
        self.service.stop_and_wait(30)
        try:
            from services.operation_log_monitor import stop_record_browser

            stop_record_browser()
        except Exception:
            pass
        if old_owner:
            from services.local_feishu_bridge import (
                cancel_active_local_retarget_tasks,
            )

            cancel_active_local_retarget_tasks(
                old_owner,
                "工具账号已退出，旧追投提醒已作废",
            )
        deactivate_local_feishu_account()
        return clear_device_session()

    # ========== 本地飞书长连接 ==========

    def getLocalFeishuStatus(self):
        from services.local_feishu_bridge import get_local_feishu_status

        return get_local_feishu_status()

    def saveLocalFeishuConfig(self, config):
        from services.local_feishu_bridge import save_local_feishu_config

        return save_local_feishu_config(config if isinstance(config, dict) else {})

    def testLocalFeishuCredentials(self):
        from services.local_feishu_bridge import test_local_feishu_credentials

        return test_local_feishu_credentials()

    def issueLocalFeishuBindingCode(self, purpose: str):
        from services.local_feishu_bridge import issue_local_feishu_binding_code

        return issue_local_feishu_binding_code(purpose)

    def removeLocalFeishuGroup(self, chat_id: str):
        from services.local_feishu_bridge import remove_local_feishu_group

        return remove_local_feishu_group(chat_id)

    def clearLocalFeishuBinding(self):
        from services.local_feishu_bridge import clear_local_feishu_binding

        return clear_local_feishu_binding()

    def sendLocalFeishuTestCard(self):
        from services.local_feishu_bridge import send_local_feishu_test_card

        return send_local_feishu_test_card()

    def getOperationDailyReportConfig(self):
        from services.operation_daily_report import get_operation_daily_report_config

        return get_operation_daily_report_config()

    def saveOperationDailyReportConfig(self, config):
        from services.operation_daily_report import save_operation_daily_report_config

        return save_operation_daily_report_config(
            config if isinstance(config, dict) else {}
        )

    def sendYesterdayOperationDailyReportNow(self):
        from services.operation_daily_report import (
            send_yesterday_operation_daily_report_now,
        )

        return send_yesterday_operation_daily_report_now()

    def get_app_version(self):
        """当前程序版本号（展示用，与 config.CURRENT_VERSION 一致）。"""
        from config import CURRENT_VERSION
        return {"success": True, "version": CURRENT_VERSION}

    def normalize_version_for_api(self, v: str) -> str:
        """与服务器 version_compare 对齐，去掉前导 v（如 v1.0.0 -> 1.0.0）。"""
        s = (v or "").strip()
        if len(s) > 1 and s[0].lower() == "v" and (s[1].isdigit() or s[1] == "."):
            return s[1:]
        return s

    def check_app_version(self, current_version: str = None):
        """
        远程比对当前版本与服务器最新发布（见 dev_files/版本更新api文档.md）。
        未传 current_version 时使用 config.CURRENT_VERSION。
        """
        if current_version is None or str(current_version).strip() == "":
            from config import CURRENT_VERSION
            current_version = self.normalize_version_for_api(CURRENT_VERSION)
        else:
            current_version = self.normalize_version_for_api(str(current_version))
        return self.account_auth.check_version_update(str(current_version))

    def perform_app_update(self, download_url: str):
        """
        下载 ZIP 并覆盖当前主程序与 bin（仅 Windows / macOS 打包环境）。
        成功时会 os._exit，不会返回给前端。
        """

        if sys.platform == "win32":
            from services.update_service_win import run_desktop_update

            return run_desktop_update(download_url)
        if sys.platform == "darwin":
            from services.update_service_mac import run_desktop_update as run_desktop_update_mac

            return run_desktop_update_mac(download_url)
        return {"success": False, "message": "当前系统不支持在线更新"}

    def open_url_in_browser(self, url: str):
        """
        使用系统默认浏览器打开链接（如飞书文档），仅允许 http/https。
        """
        if not url or not isinstance(url, str):
            return {"success": False, "message": "缺少地址"}
        u = url.strip()
        try:
            p = urlparse(u)
        except Exception:
            return {"success": False, "message": "地址无效"}
        if p.scheme not in ("http", "https") or not p.netloc:
            return {"success": False, "message": "仅支持 http/https 链接"}
        try:
            webbrowser.open(u)
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ========== 规则化追投配置 ==========

    def getRuleRetargetingConfig(self):
        """读取 data/rule_retargeting.json（规范化后返回）。"""
        from utils.common import configured_chrome_path_or_empty, default_browser_executable_hint

        from .rule_retargeting_config import load_rule_retargeting_config

        c = load_rule_retargeting_config()
        out = dict(c)
        stored = configured_chrome_path_or_empty(out.get("browser_executable_path"))
        out["browser_executable_path"] = stored if stored else default_browser_executable_hint()
        out["success"] = True
        return out

    def setRuleRetargetingConfig(self, config=None):
        """
        保存规则化追投配置（可部分字段）。写入前校验数值范围等。
        """
        from .rule_retargeting_config import (
            merge_and_save,
            preview_merge,
            validate_rule_retargeting_config,
            validate_strategy_target_compatibility,
        )
        from .promotion_targets import (
            get_promotion_target,
            set_promotion_target_enabled,
        )

        if config is not None and not isinstance(config, dict):
            return {"success": False, "message": "配置须为对象"}
        merged = preview_merge(config)
        # 旧版策略只保存 target_uid。保存时从受当前工具账号隔离的目标记录
        # 补齐 account_uid，后续命中及执行阶段即可进行跨账户防篡改复核。
        target_map = {}
        for strategy in merged.get("strategies") or []:
            if not isinstance(strategy, dict):
                continue
            target_uid = str(strategy.get("target_uid") or "").strip()
            if target_uid and target_uid not in target_map:
                target_map[target_uid] = get_promotion_target(
                    target_uid,
                    db=self.db,
                )
            target = target_map.get(target_uid)
            if isinstance(target, dict) and not str(
                strategy.get("account_uid") or ""
            ).strip():
                strategy["account_uid"] = str(
                    target.get("account_uid") or ""
                ).strip()
        ok, msg = validate_rule_retargeting_config(merged)
        if not ok:
            return {"success": False, "message": msg}
        targets_for_validation = target_map
        if bool(merged.get("enabled")):
            # Validate the requested end state before changing persistent
            # monitoring flags.  A malformed strategy must never enable a
            # plan as a side effect of a failed save.
            targets_for_validation = {
                target_uid: (
                    {**target, "enabled": True}
                    if isinstance(target, dict)
                    else target
                )
                for target_uid, target in target_map.items()
            }
        ok, msg = validate_strategy_target_compatibility(
            merged,
            targets_for_validation,
        )
        if not ok:
            return {"success": False, "message": msg}
        # Saving an enabled execution strategy is also an explicit request to
        # monitor its selected plan. This repairs stale/off targets in one
        # action, while the eligibility and account checks in the target model
        # still fail closed for unsafe plans.
        if bool(merged.get("enabled")):
            try:
                for target_uid, target in target_map.items():
                    if isinstance(target, dict) and not target.get("enabled"):
                        target_map[target_uid] = set_promotion_target_enabled(
                            target_uid,
                            True,
                            db=self.db,
                        )
            except Exception as exc:
                return {
                    "success": False,
                    "message": f"策略已校验，但绑定计划无法加入监控：{exc}",
                }
        saved = merge_and_save(merged)
        runtime = None
        if QIANCHUAN_BACKEND == "official_api":
            from services.qianchuan_open_api.runtime_settings import (
                enable_execution_for_saved_rules,
            )

            runtime = enable_execution_for_saved_rules(saved)
        out = dict(saved)
        out["success"] = True
        if runtime is not None:
            out["officialApiWritesEnabled"] = bool(
                runtime.get("allow_live_api_writes")
            )
        return out

    def getLiveRetargetPreflight(self):
        """本地真实追投验收前的只读清单；正式环境不启用。"""
        from services.local_test_guard import build_live_retarget_preflight

        try:
            return build_live_retarget_preflight()
        except Exception as exc:
            return {
                "success": False,
                "test_mode": True,
                "ready_to_arm": False,
                "ready_to_execute": False,
                "message": str(exc),
                "checks": [],
                "strategies": [],
            }

    # ========== 规则化调控配置 ==========

    def getRuleRegulationConfig(self):
        """读取 data/rule_regulation.json（规则化停投，规范化后返回）。"""
        from utils.common import configured_chrome_path_or_empty, default_browser_executable_hint

        from .rule_regulation_config import load_rule_regulation_config
        from .promotion_targets import get_promotion_target

        c = load_rule_regulation_config()
        out = dict(c)
        # 旧策略只有 target_uid；读取时补齐账户快照，让前端直接恢复
        # “账户 → 计划”的级联选择，下一次保存后持久化。
        strategies = []
        for raw in out.get("strategies") or []:
            strategy = dict(raw) if isinstance(raw, dict) else {}
            target_uid = str(strategy.get("target_uid") or "").strip()
            target = get_promotion_target(target_uid, db=self.db) if target_uid else None
            if target:
                strategy["account_uid"] = str(target.get("account_uid") or "")
                strategy["aavid"] = str(target.get("aadvid") or "")
            strategies.append(strategy)
        if strategies:
            out["strategies"] = strategies
        stored = configured_chrome_path_or_empty(out.get("browser_executable_path"))
        out["browser_executable_path"] = stored if stored else default_browser_executable_hint()
        out["success"] = True
        return out

    def setRuleRegulationConfig(self, config=None):
        """保存规则化停投配置（可部分字段）。不含执行次数相关字段。"""
        from .rule_regulation_config import (
            bind_and_validate_strategy_targets,
            merge_and_save,
            preview_merge,
            validate_rule_regulation_config,
        )
        from .promotion_targets import (
            get_promotion_target,
            set_promotion_target_enabled,
        )
        from services.qianchuan_accounts import list_qianchuan_accounts

        if config is not None and not isinstance(config, dict):
            return {"success": False, "message": "配置须为对象"}
        merged = preview_merge(config)
        ok, msg = validate_rule_regulation_config(merged)
        if not ok:
            return {"success": False, "message": msg}
        targets_by_uid = {}
        for strategy in merged.get("strategies") or []:
            if not isinstance(strategy, dict):
                continue
            target_uid = str(strategy.get("target_uid") or "").strip()
            if target_uid and target_uid not in targets_by_uid:
                target = get_promotion_target(target_uid, db=self.db)
                if target:
                    targets_by_uid[target_uid] = target
        accounts_by_uid = {
            str(account.get("account_uid") or ""): account
            for account in list_qianchuan_accounts(db=self.db)
            if str(account.get("account_uid") or "")
        }
        targets_for_validation = targets_by_uid
        if bool(merged.get("enabled")):
            targets_for_validation = {
                target_uid: (
                    {**target, "enabled": True}
                    if isinstance(target, dict)
                    else target
                )
                for target_uid, target in targets_by_uid.items()
            }
        ok, msg = bind_and_validate_strategy_targets(
            merged,
            targets_for_validation,
            accounts_by_uid,
        )
        if not ok:
            return {"success": False, "message": msg}
        if bool(merged.get("enabled")):
            try:
                for target_uid, target in targets_by_uid.items():
                    if isinstance(target, dict) and not target.get("enabled"):
                        targets_by_uid[target_uid] = set_promotion_target_enabled(
                            target_uid,
                            True,
                            db=self.db,
                        )
            except Exception as exc:
                return {
                    "success": False,
                    "message": f"策略已校验，但绑定计划无法加入监控：{exc}",
                }
        saved = merge_and_save(merged)
        runtime = None
        if QIANCHUAN_BACKEND == "official_api":
            from services.qianchuan_open_api.runtime_settings import (
                enable_execution_for_saved_rules,
            )

            runtime = enable_execution_for_saved_rules(saved)
        out = dict(saved)
        out["success"] = True
        if runtime is not None:
            out["officialApiWritesEnabled"] = bool(
                runtime.get("allow_live_api_writes")
            )
        return out

    def regulationPauseControl(self):
        """占位：暂停停投（执行侧接入后实现）。"""
        return {"success": True, "message": "（占位）暂停停投尚未接入执行层"}

    def regulationDeleteTask(self):
        """占位：删除停投任务（执行侧接入后实现）。"""
        return {"success": True, "message": "（占位）删除停投任务尚未接入执行层"}

    def listRetargetingRuns(
        self,
        date_from=None,
        date_to=None,
        q=None,
        retargeting_method=None,
        status=None,
        page=1,
        page_size=20,
    ):
        """分页查询 pmc_retargeting_run（列表不含三大 JSON 列）。"""
        from .retargeting_runs import query_pmc_retargeting_runs_page

        st: Any = None
        if status is not None and status != "":
            try:
                st = int(status)
            except (TypeError, ValueError):
                st = None

        try:
            total, items = query_pmc_retargeting_runs_page(
                date_from=date_from,
                date_to=date_to,
                q=q,
                retargeting_method=retargeting_method,
                status=st,
                page=page,
                page_size=page_size,
            )
            try:
                p = max(1, int(page))
            except (TypeError, ValueError):
                p = 1
            try:
                ps = int(page_size)
            except (TypeError, ValueError):
                ps = 20
            ps = max(1, min(ps, 100))
            return {
                "success": True,
                "items": items,
                "total": total,
                "page": p,
                "pageSize": ps,
            }
        except Exception as e:
            return {"success": False, "message": str(e), "items": [], "total": 0, "page": 1, "pageSize": 20}

    def getRetargetingRunDetail(self, run_id=None):
        """单条追投流水详情（含 retargeting_json / trigger_snapshot_json / query_snapshot_json）。"""
        from .retargeting_runs import get_pmc_retargeting_run_by_id

        try:
            rid = int(run_id)
        except (TypeError, ValueError):
            return {"success": False, "message": "无效的 id"}
        if rid < 1:
            return {"success": False, "message": "无效的 id"}
        try:
            row = get_pmc_retargeting_run_by_id(rid)
            if not row:
                return {"success": False, "message": "记录不存在"}
            return {"success": True, "data": row}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def listRegulationRuns(
        self,
        date_from=None,
        date_to=None,
        q=None,
        stop_action=None,
        status=None,
        page=1,
        page_size=20,
    ):
        """分页查询 pmc_regulation_run（列表不含大 JSON 列）。"""
        from .regulation_runs import query_pmc_regulation_runs_page

        st: Any = None
        if status is not None and status != "":
            try:
                st = int(status)
            except (TypeError, ValueError):
                st = None

        try:
            total, items = query_pmc_regulation_runs_page(
                date_from=date_from,
                date_to=date_to,
                q=q,
                stop_action=stop_action,
                status=st,
                page=page,
                page_size=page_size,
            )
            try:
                p = max(1, int(page))
            except (TypeError, ValueError):
                p = 1
            try:
                ps = int(page_size)
            except (TypeError, ValueError):
                ps = 20
            ps = max(1, min(ps, 100))
            return {
                "success": True,
                "items": items,
                "total": total,
                "page": p,
                "pageSize": ps,
            }
        except Exception as e:
            return {"success": False, "message": str(e), "items": [], "total": 0, "page": 1, "pageSize": 20}

    def getRegulationRunDetail(self, run_id=None):
        """单条规则化停投流水详情（含快照 JSON）。"""
        from .regulation_runs import get_pmc_regulation_run_by_id

        try:
            rid = int(run_id)
        except (TypeError, ValueError):
            return {"success": False, "message": "无效的 id"}
        if rid < 1:
            return {"success": False, "message": "无效的 id"}
        try:
            row = get_pmc_regulation_run_by_id(rid)
            if not row:
                return {"success": False, "message": "记录不存在"}
            return {"success": True, "data": row}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ========== 单账户统一操作流水 ==========

    def listOperationAccounts(self):
        from .operation_events import list_operation_accounts

        try:
            return {"success": True, "items": list_operation_accounts()}
        except Exception as e:
            return {"success": False, "message": str(e), "items": []}

    def listOperationEvents(
        self,
        aavid=None,
        date_from=None,
        date_to=None,
        action_type=None,
        source=None,
        status=None,
        operator=None,
        q=None,
        target_uid=None,
        page=1,
        page_size=50,
    ):
        from .operation_events import (
            operation_event_account_summary,
            query_operation_events_page,
        )

        try:
            total, items = query_operation_events_page(
                aavid=aavid,
                date_from=date_from,
                date_to=date_to,
                action_type=action_type,
                source=source,
                status=status,
                operator=operator,
                q=q,
                target_uid=target_uid,
                page=page,
                page_size=page_size,
            )
            return {
                "success": True,
                "items": items,
                "total": total,
                "accountSummary": operation_event_account_summary(aavid),
                "page": max(1, int(page or 1)),
                "pageSize": max(1, min(5000, int(page_size or 50))),
            }
        except Exception as e:
            return {"success": False, "message": str(e), "items": [], "total": 0}

    def getOperationEventDetail(self, event_id=None, aavid=None):
        from .operation_events import get_operation_event

        try:
            row = get_operation_event(event_id, aavid)
            return {"success": bool(row), "data": row, "message": "" if row else "记录不存在"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def getOperationSyncState(self, aavid=None):
        from .operation_events import operation_sync_state

        try:
            return {"success": True, "data": operation_sync_state(aavid)}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def addOfficialApiQianchuanAccount(self, aavid=None):
        try:
            if QIANCHUAN_BACKEND != "official_api":
                return {
                    "success": False,
                    "code": "official_api_required",
                    "message": "当前未启用千川官方 API 模式",
                }
            from services.official_api_catalog import add_authorized_account

            return add_authorized_account(aavid)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def reconcileQianchuanOfficialApiAccount(self, aavid=None):
        """Developer acceptance hook; never launches Chrome or writes to Qianchuan."""
        if QIANCHUAN_BACKEND != "official_api":
            return {
                "success": False,
                "message": "请在独立的官方 API 联调进程中执行只读对账",
            }
        try:
            from services.official_api_reconciliation import reconcile_account_snapshot

            return reconcile_account_snapshot(aavid, db=self.db)
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def syncOperationLogsNow(self, aavid=None):
        from services.operation_log_monitor import request_platform_log_sync

        try:
            return request_platform_log_sync(aavid, db=self.db)
        except Exception as e:
            return {"success": False, "message": str(e)}

    def exportOperationEventsCsv(
        self,
        aavid=None,
        date_from=None,
        date_to=None,
        action_type=None,
        source=None,
        status=None,
        operator=None,
        q=None,
        target_uid=None,
    ):
        from .operation_events import export_operation_events_csv

        try:
            content = export_operation_events_csv(
                aavid=aavid,
                date_from=date_from,
                date_to=date_to,
                action_type=action_type,
                source=source,
                status=status,
                operator=operator,
                q=q,
                target_uid=target_uid,
            )
            return {"success": True, "filename": f"千川账户_{aavid}_操作流水.csv", "content": content}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def startOperationRecordBrowser(self, aavid=None):
        if QIANCHUAN_BACKEND == "official_api":
            return {
                "success": False,
                "backend": "official_api",
                "message": "官方 API 模式不记录浏览器轨迹；请点击立即同步读取千川真实操作日志",
            }
        from services.operation_log_monitor import start_record_browser

        aid = str(aavid or "").strip()
        if not aid:
            return {"success": False, "message": "请先选择千川账户"}
        row = self.db.select_one("pmc_ad_detail_basic", where={"aadvid": aid})
        ad_id = str((row or {}).get("ad_id") or "")
        if not ad_id:
            return {"success": False, "message": "该账户尚无广告ID，请先启动一次采集"}
        return start_record_browser(aid, ad_id)

    def stopOperationRecordBrowser(self):
        from services.operation_log_monitor import stop_record_browser

        return stop_record_browser()

    def getOperationRecordBrowserStatus(self):
        from services.operation_log_monitor import record_browser_status

        return record_browser_status()

    def runImmediateRetargetPrepare(self, material_id=None, retargeting=None, target_uid=None):
        """
        即刻追投：有头浏览器打开投放页并填表，不自动提交；成功写库并限频 +1（不重置窗口起点）。
        """
        from .retargeting_runs import run_immediate_retarget_prepare

        try:
            return run_immediate_retarget_prepare(
                material_id=material_id or "",
                retargeting=retargeting if isinstance(retargeting, dict) else None,
                target_uid=target_uid,
            )
        except Exception as e:
            return {"success": False, "message": str(e)}

    def runImmediateRegulationStopPrepare(self, assist_task_id=None, stop_action=None):
        """
        手动停投：有头浏览器打开投放页并定位调控任务，代为点开暂停/删除确认层，用户自行点「确定」；完成后写 pmc_regulation_run。
        """
        from .regulation_runs import run_immediate_regulation_stop_prepare

        try:
            return run_immediate_regulation_stop_prepare(
                assist_task_id=assist_task_id or "",
                stop_action=stop_action,
            )
        except Exception as e:
            return {"success": False, "message": str(e)}
