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
from services.run_services import get_service_controller


class Api:
    """总 API 接口"""

    def __init__(self):
        """初始化所有 API 模块"""
        init_sqlite_schema()
        self.db = SQLiteStore()
        from .promotion_targets import migrate_legacy_target_scope

        migrate_legacy_target_scope(db=self.db)
        self.dashboard = DashboardApi()
        self.service = get_service_controller()
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

    def listPromotionTargetProducts(self, target_uid=None):
        from .promotion_targets import list_target_products

        try:
            return {
                "success": True,
                "data": list_target_products(target_uid, db=self.db),
            }
        except Exception as e:
            return {"success": False, "message": str(e), "data": []}

    def startPromotionTargetDiscovery(self):
        try:
            return self.service.start_target_discovery()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def getPromotionTargetDiscoveryStatus(self):
        try:
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
        启动服务（必须传入账号密码，由服务端远程校验通过后才真正启动，防止前端被篡改绕过）。

        Args:
            interval: 轮询间隔（秒）
            headful: 轮询阶段是否无头（True=无头）；登录识别阶段始终有头浏览器
            username: 普通用户账号
            password: 密码
        """
        u = (username or "").strip()
        p = password if password is not None else ""
        if not u or not p:
            return self._start_denied_response("启动采集须传入账号与密码，并由服务端校验通过")
        chk = self.account_auth.verify_can_start_service(u, p)
        if not chk.get("ok"):
            return self._start_denied_response(chk.get("message") or "账号校验失败")
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

    # ========== 账号登录校验（远程）==========

    def verify_account_login(self, username: str, password: str):
        """
        远程校验普通用户账号与密码，并返回有效期、禁用状态（见 dev_files/api文档.md）。
        """
        result = self.account_auth.verify_login(username, password)
        data = result.get("data") if isinstance(result, dict) else None
        if (
            isinstance(result, dict)
            and result.get("success")
            and isinstance(data, dict)
            and int(data.get("is_disabled") or 0) != 1
            and self.account_auth._is_within_validity(data)
        ):
            from services.local_feishu_bridge import activate_local_feishu_account

            activate_local_feishu_account(username)
        return result

    def clearDeviceSession(self):
        from services.local_feishu_bridge import deactivate_local_feishu_account
        from services.cloud_retarget_client import clear_device_session

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
        )

        if config is not None and not isinstance(config, dict):
            return {"success": False, "message": "配置须为对象"}
        merged = preview_merge(config)
        ok, msg = validate_rule_retargeting_config(merged)
        if not ok:
            return {"success": False, "message": msg}
        saved = merge_and_save(config)
        out = dict(saved)
        out["success"] = True
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

        c = load_rule_regulation_config()
        out = dict(c)
        stored = configured_chrome_path_or_empty(out.get("browser_executable_path"))
        out["browser_executable_path"] = stored if stored else default_browser_executable_hint()
        out["success"] = True
        return out

    def setRuleRegulationConfig(self, config=None):
        """保存规则化停投配置（可部分字段）。不含执行次数相关字段。"""
        from .rule_regulation_config import (
            merge_and_save,
            preview_merge,
            validate_rule_regulation_config,
        )

        if config is not None and not isinstance(config, dict):
            return {"success": False, "message": "配置须为对象"}
        merged = preview_merge(config)
        ok, msg = validate_rule_regulation_config(merged)
        if not ok:
            return {"success": False, "message": msg}
        saved = merge_and_save(config)
        out = dict(saved)
        out["success"] = True
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
        from .operation_events import query_operation_events_page

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
