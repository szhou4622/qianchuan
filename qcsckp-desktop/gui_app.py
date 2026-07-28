#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
千川素材看盘工具 - GUI 应用
使用 pywebview 展示前端页面
支持 Windows 和 macOS
支持单实例检查、系统托盘（关闭到托盘，托盘退出才是真退出）
"""
import os
import shutil
import sys
import platform
from pathlib import Path
import ctypes
import threading
import time
import json
import webview
from ctypes.util import find_library
from api import Api
# 项目配置
from config import (
    PROJECT_ROOT,
    DATA_DIR,
    DATA_TEMP_DIR,
    STATIC_DIR,
    CURRENT_VERSION,
    TEST_MODE,
    TEST_AAVID,
    TEST_MATERIAL_ID,
    ALLOW_LIVE_RETARGET,
)
from utils.sqlite_prune_scheduler import start_sqlite_prune_background_thread
from services.webhook_push_runtime import start_webhook_push_background_threads
from services.regulation_rule_runner import start_regulation_rule_runner_background_thread
from services.retargeting_rule_runner import start_retargeting_rule_runner_background_thread
from services.retarget_task_worker import start_retarget_task_worker_background_thread
from services.operation_log_monitor import start_platform_log_sync_background_thread
from services.control_panel_config import ensure_all_control_defaults


# ── 打包环境强制 stdout/stderr 使用 UTF-8 ───────────────────────────────
import io
for _sname in ('stdout', 'stderr'):
    _s = getattr(sys, _sname, None)
    if _s is None:
        continue
    try:
        if hasattr(_s, 'reconfigure'):
            _s.reconfigure(encoding='utf-8', errors='replace')
        elif hasattr(_s, 'buffer'):
            setattr(sys, _sname, io.TextIOWrapper(_s.buffer, encoding='utf-8', errors='replace'))
    except Exception:
        pass
del _sname, _s
# ───────────────────────────────────────────────────────────────────────

# psutil 用于跨平台进程检查
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("警告: psutil 未安装，单实例检查功能可能不可用")

# Windows 专用
try:
    import win32api # type: ignore
except Exception:
    win32api = None

# 屏幕信息
try:
    import screeninfo
    SCREENINFO_AVAILABLE = True
except ImportError:
    SCREENINFO_AVAILABLE = False
    print("警告: screeninfo 未安装，将使用默认屏幕尺寸")

# 托盘依赖
try:
    import pystray
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except ImportError:
    pystray = None
    PYSTRAY_AVAILABLE = False
    print("警告: pystray 未安装，托盘功能将不可用")

webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False


def _canonical_exe_path(path: str) -> str:
    """
    单实例判定用的可执行路径：绝对路径 + 解析符号链接。
    macOS 下同一 .app 可能从「/Applications/xxx.app」或别名路径启动，
    realpath 后应对齐到同一物理路径；Windows 下对盘符路径做大小写统一。
    """
    if not path:
        return ""
    try:
        p = os.path.realpath(os.path.abspath(path))
    except OSError:
        p = os.path.abspath(path)
    if sys.platform == "win32":
        return p.lower()
    return p


def _psutil_resolve_exe(proc) -> str:
    """取进程可执行文件路径；iter 里 exe 为空时再试一次 Process.exe()（macOS 上偶发）。"""
    raw = (proc.info.get("exe") or "").strip()
    if raw:
        return raw
    pid = proc.info.get("pid")
    if pid is None:
        return ""
    try:
        return (psutil.Process(pid).exe() or "").strip()
    except (psutil.Error, ValueError):
        return ""


# ==================== 单实例检查管理器 ====================
class SingleInstanceChecker:
    """单实例：仅当「规范化后的可执行文件路径」与当前进程一致时视为同一应用。"""

    def __init__(self, project_root):
        # project_root 保留参数以兼容旧调用；单实例只认可执行路径，不再用工作目录推断
        self.command_file = os.path.join(DATA_DIR, "command.json")
        self._command_mtime = None

    def check_single_instance(self):
        """
        检查是否已有实例在运行（仅比较可执行文件路径，需 psutil）。
        返回 True 表示已有同路径实例，新进程应退出并唤醒已有窗口。
        """
        if not PSUTIL_AVAILABLE:
            print("[单实例检查] 未安装 psutil，跳过单实例检查")
            return False

        try:
            current_pid = os.getpid()
            current_key = _canonical_exe_path(sys.executable)
            if not current_key:
                return False

            for proc in psutil.process_iter(["pid", "exe"]):
                try:
                    if proc.info["pid"] == current_pid:
                        continue
                    proc_exe = _psutil_resolve_exe(proc)
                    if not proc_exe:
                        continue
                    if _canonical_exe_path(proc_exe) == current_key:
                        print(
                            f"[单实例检查] 已有同路径实例: PID={proc.info['pid']}, exe={proc_exe}"
                        )
                        self._write_show_window_command()
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            return False
        except Exception as e:
            print(f"[单实例检查] 检查出错: {e}")
            return False

    def _write_show_window_command(self):
        """写入显示窗口命令"""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(self.command_file, 'w', encoding='utf-8') as f:
                json.dump({"show_window": True}, f)
        except Exception as e:
            print(f"[单实例检查] 写入命令文件失败: {e}")

    def check_and_show_window_from_command(self):
        """
        检查是否有显示窗口的命令
        通过 webview.windows[0] 获取窗口
        """
        try:
            if not os.path.exists(self.command_file):
                return

            current_mtime = os.path.getmtime(self.command_file)
            if self._command_mtime == current_mtime:
                return

            self._command_mtime = current_mtime
            with open(self.command_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("show_window"):
                if webview.windows:
                    w = webview.windows[0]
                    w.show()
                    w.restore()
                    # 清除命令
                    with open(self.command_file, 'w', encoding='utf-8') as f:
                        json.dump({"show_window": False}, f)
        except Exception:
            pass


# 创建单实例检查器全局实例
single_instance_checker = SingleInstanceChecker(PROJECT_ROOT)


# ==================== 托盘管理器 ====================
class TrayApplication:
    """系统托盘管理器"""

    def __init__(self, window):
        self.window = window
        self.icon = None
        self.enable_tray = PYSTRAY_AVAILABLE
        self.force_close = False
        self._setup_tray()

    def draw_app_icon(self):
        """绘制应用图标"""
        w, h = 64, 64
        center_x, center_y = w // 2, h // 2

        image = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # 绘制蓝色背景圆形
        draw.ellipse((4, 4, w-4, h-4), fill="#4A90E2")

        # 绘制 "QC" 字样（千川）
        draw.text((14, 18), "QC", fill="white")

        return image

    def load_icon(self):
        """加载图标"""
        # 在静态资源目录和数据目录中查找 logo
        search_dirs = [STATIC_DIR, DATA_DIR, PROJECT_ROOT]
        for base_dir in search_dirs:
            if not os.path.exists(base_dir):
                continue
            for name in ['logo.ico', 'logo.png', 'logo.jpg', 'icon.ico', 'icon.png']:
                path = os.path.join(base_dir, name)
                if os.path.exists(path):
                    try:
                        return Image.open(path)
                    except Exception:
                        pass

        # 找不到则绘制默认图标
        return self.draw_app_icon()

    def on_tray_clicked(self, icon, item):
        """处理托盘菜单点击"""
        action = str(item)
        if action in ("显示", "Show"):
            self.window.show()
            self.window.restore()
        elif action in ("退出", "Quit"):
            self.quit_app()

    def quit_app(self):
        """退出应用"""
        print("[托盘] 触发退出...")
        self.force_close = True
        if self.icon:
            self.icon.stop()
        self.window.destroy()
        # 退出进程
        os._exit(0)

    def _setup_tray(self):
        """配置托盘图标"""
        if not self.enable_tray:
            return

        try:
            image = self.load_icon()

            # 根据系统语言设置菜单
            menu_text_show = "显示"
            menu_text_quit = "退出"
            try:
                import locale
                loc = locale.getlocale(locale.LC_MESSAGES)
                if loc and loc[0] and loc[0].lower().startswith("en"):
                    menu_text_show = "Show"
                    menu_text_quit = "Quit"
            except Exception:
                pass

            menu = pystray.Menu(
                pystray.MenuItem(menu_text_show, self.on_tray_clicked, default=True),
                pystray.MenuItem(menu_text_quit, self.on_tray_clicked)
            )

            self.icon = pystray.Icon("千川素材看盘工具", image, "千川素材看盘工具", menu)
            print("[托盘] 托盘图标已配置")

        except Exception as e:
            print(f"[托盘] 配置托盘失败: {e}")
            self.enable_tray = False

    def run_tray(self):
        """启动托盘"""
        if not self.icon:
            return

        try:
            if platform.system() == "Darwin" and hasattr(self.icon, "run_detached"):
                self.icon.run_detached()
            else:
                threading.Thread(target=self.icon.run, daemon=True).start()
            print("[托盘] 托盘已启动")
        except Exception as e:
            print(f"[托盘] 启动托盘失败: {e}")

    def on_window_closing(self, event=None):
        """窗口关闭事件处理 - 隐藏到托盘而不是退出"""
        # force_close 为 True 时允许真正关闭
        if self.force_close:
            return True
        if self.enable_tray and self.icon:
            print("[窗口] 关闭 -> 隐藏到托盘")
            self.window.hide()
            return False  # 阻止关闭
        return True  # 无托盘时直接关闭


def configure_macos_lifecycle(window, tray_app):
    """
    macOS：向 pywebview 的 NSApplication delegate 注入生命周期方法。
    - 点击 Dock 图标：恢复并前置主窗口（隐藏到托盘后常用）
    - Cmd+Q / Dock 右键「退出」：走与托盘「退出」一致的清理并结束进程

    依赖 pyobjc-framework-Cocoa（macOS 上 pip/uv 安装即可）；未安装时跳过并打印提示。
    """
    if platform.system() != "Darwin":
        return

    try:
        from AppKit import NSApp  # type: ignore
    except ImportError:
        print(
            "[!] 未安装 pyobjc（AppKit），Dock 点击恢复与 Dock/Cmd+Q 退出可能异常。"
            " macOS 请执行: uv sync 或 pip install pyobjc-framework-Cocoa"
        )
        return

    def hook_logic():
        from AppKit import NSApplication  # type: ignore

        app = NSApplication.sharedApplication()
        delegate = None
        for _ in range(50):
            delegate = app.delegate()
            if delegate is not None:
                break
            time.sleep(0.1)

        if delegate is None:
            print("[!] macOS：超时未获取 NSApp.delegate，跳过 Dock/Cmd+Q 补丁")
            return

        DelegateClass = delegate.__class__
        print(f"[macOS] 已定位 AppDelegate: {DelegateClass.__name__}")

        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
            print("[macOS] Dock 图标被点击，恢复窗口...")
            try:
                window.restore()
                window.show()
                NSApp.activateIgnoringOtherApps_(True)
            except Exception as e:
                print(f"[macOS] 恢复窗口失败: {e}")
            return True

        def applicationShouldTerminate_(self, sender):
            print("[macOS] 收到退出请求（Cmd+Q 或 Dock 右键退出）")
            if tray_app is not None:
                tray_app.force_close = True
                if getattr(tray_app, "icon", None):
                    try:
                        tray_app.icon.stop()
                    except Exception:
                        pass
            try:
                window.destroy()
            except Exception as e:
                print(f"[macOS] 销毁窗口异常: {e}")
            # NSTerminateNow == 1，立即退出，避免仅关窗后进程仍驻留
            return 1

        try:
            DelegateClass.applicationShouldHandleReopen_hasVisibleWindows_ = (
                applicationShouldHandleReopen_hasVisibleWindows_
            )
            DelegateClass.applicationShouldTerminate_ = applicationShouldTerminate_
            print("[macOS] Dock / Cmd+Q 生命周期补丁已注入")
        except Exception as e:
            print(f"[macOS] 注入 AppDelegate 失败: {e}")

    threading.Thread(target=hook_logic, daemon=True).start()


# ===== 创建 js_api =====
class JSApi:
    def __init__(self):
        self.api = Api()

    def getTableData(self, period="1h", sortBy="costDiff", sortOrder="desc", page=1, pageSize=50):
        return self.api.get_table_data(period, sortBy, sortOrder, page, pageSize)

    def getTop20ByCost(self, hours=1):
        return self.api.get_top20_by_cost(hours)

    def getLatestCrawlCostSum(self, hours=1):
        return self.api.get_latest_crawl_cost_sum(hours)

    def getMaterialHistoryRecent(self, material_id, limit=200):
        return self.api.get_material_history_recent(material_id, limit)

    def getDashboardAccountLabel(self):
        return self.api.get_dashboard_account_label()

    def setDashboardAccountLabel(self, label=""):
        return self.api.set_dashboard_account_label(label)

    def getRoi2AssistTableData(
        self,
        aadvid=None,
        sortBy="stat_cost_for_roi2_assist",
        sortOrder="desc",
        page=1,
        pageSize=50,
        search=None,
        adDeliveryType=None,
    ):
        return self.api.get_roi2_assist_table_data(
            aadvid, sortBy, sortOrder, page, pageSize,
            search=search, ad_delivery_type=adDeliveryType
        )

    # 服务控制
    def startService(self, interval=None, headful=True, username=None, password=None):
        return self.api.startService(interval, headful, username, password)

    def stopService(self):
        return self.api.stopService()

    def getServiceStatus(self):
        return self.api.getServiceStatus()

    def readLogs(self, limit=50):
        return self.api.readLogs(limit)

    def clearLogs(self):
        return self.api.clearLogs()

    def setServiceInterval(self, interval):
        return self.api.setServiceInterval(interval)

    def getScrapeServicePanelConfig(self):
        return self.api.getScrapeServicePanelConfig()

    def setScrapeServicePanelConfig(
        self,
        interval_seconds=None,
        headless_poll=None,
        fetch_assist_tasks=None,
        browser_executable_path=None,
    ):
        return self.api.setScrapeServicePanelConfig(
            interval_seconds,
            headless_poll,
            fetch_assist_tasks,
            browser_executable_path,
        )

    def getFeishuBitablePanelConfig(self):
        return self.api.getFeishuBitablePanelConfig()

    def setFeishuBitableConfig(self, app_token=None, personal_base_token=None, table_id=None, enabled=None, push_mode=None):
        return self.api.setFeishuBitableConfig(app_token, personal_base_token, table_id, enabled, push_mode)

    def getFeishuWebhookPushConfig(self):
        return self.api.getFeishuWebhookPushConfig()

    def setFeishuWebhookPushConfig(self, enabled=None, webhook=None, keyword=None):
        return self.api.setFeishuWebhookPushConfig(enabled, webhook, keyword)

    def testFeishuWebhookPush(self):
        return self.api.testFeishuWebhookPush()

    def getDingtalkWebhookPushConfig(self):
        return self.api.getDingtalkWebhookPushConfig()

    def setDingtalkWebhookPushConfig(self, enabled=None, webhook=None, keyword=None):
        return self.api.setDingtalkWebhookPushConfig(enabled, webhook, keyword)

    def testDingtalkWebhookPush(self):
        return self.api.testDingtalkWebhookPush()

    def verifyAccountLogin(self, username, password):
        return self.api.verify_account_login(username, password)

    def getLocalTestLoginCredentials(self):
        from services.local_test_guard import load_local_test_login_credentials

        return load_local_test_login_credentials()

    def clearDeviceSession(self):
        return self.api.clearDeviceSession()

    def getLocalFeishuStatus(self):
        return self.api.getLocalFeishuStatus()

    def saveLocalFeishuConfig(self, config):
        return self.api.saveLocalFeishuConfig(config)

    def testLocalFeishuCredentials(self):
        return self.api.testLocalFeishuCredentials()

    def issueLocalFeishuBindingCode(self, purpose):
        return self.api.issueLocalFeishuBindingCode(purpose)

    def removeLocalFeishuGroup(self, chatId):
        return self.api.removeLocalFeishuGroup(chatId)

    def clearLocalFeishuBinding(self):
        return self.api.clearLocalFeishuBinding()

    def sendLocalFeishuTestCard(self):
        return self.api.sendLocalFeishuTestCard()

    def checkAppVersion(self, currentVersion=None):
        return self.api.check_app_version(currentVersion)

    def getAppVersion(self):
        return self.api.get_app_version()

    def getEnvironmentInfo(self):
        return {
            "test_mode": bool(TEST_MODE),
            "test_aavid": str(TEST_AAVID or ""),
            "test_material_id": str(TEST_MATERIAL_ID or ""),
            "live_retarget_armed": bool(ALLOW_LIVE_RETARGET),
            "live_retarget_consumed": os.path.isfile(
                os.path.join(DATA_DIR, "live_retarget_consumed.json")
            ),
        }

    def performAppUpdate(self, download_url):
        return self.api.perform_app_update(download_url)

    def openUrlInBrowser(self, url):
        return self.api.open_url_in_browser(url)

    def getRuleRetargetingConfig(self):
        return self.api.getRuleRetargetingConfig()

    def setRuleRetargetingConfig(self, config=None):
        return self.api.setRuleRetargetingConfig(config)

    def getLiveRetargetPreflight(self):
        return self.api.getLiveRetargetPreflight()

    def getRuleRegulationConfig(self):
        return self.api.getRuleRegulationConfig()

    def setRuleRegulationConfig(self, config=None):
        return self.api.setRuleRegulationConfig(config)

    def regulationPauseControl(self):
        return self.api.regulationPauseControl()

    def regulationDeleteTask(self):
        return self.api.regulationDeleteTask()

    def runImmediateRetargetPrepare(self, material_id=None, retargeting=None):
        return self.api.runImmediateRetargetPrepare(material_id, retargeting)

    def runImmediateRegulationStopPrepare(self, assist_task_id=None, stop_action=None):
        return self.api.runImmediateRegulationStopPrepare(assist_task_id, stop_action)

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
        return self.api.listRetargetingRuns(
            date_from,
            date_to,
            q,
            retargeting_method,
            status,
            page,
            page_size,
        )

    def getRetargetingRunDetail(self, run_id=None):
        return self.api.getRetargetingRunDetail(run_id)

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
        return self.api.listRegulationRuns(
            date_from,
            date_to,
            q,
            stop_action,
            status,
            page,
            page_size,
        )

    def getRegulationRunDetail(self, run_id=None):
        return self.api.getRegulationRunDetail(run_id)

    def listOperationAccounts(self):
        return self.api.listOperationAccounts()

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
        page=1,
        page_size=50,
    ):
        return self.api.listOperationEvents(
            aavid, date_from, date_to, action_type, source, status, operator, q, page, page_size
        )

    def getOperationEventDetail(self, event_id=None, aavid=None):
        return self.api.getOperationEventDetail(event_id, aavid)

    def getOperationSyncState(self, aavid=None):
        return self.api.getOperationSyncState(aavid)

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
    ):
        return self.api.exportOperationEventsCsv(
            aavid, date_from, date_to, action_type, source, status, operator, q
        )

    def startOperationRecordBrowser(self, aavid=None):
        return self.api.startOperationRecordBrowser(aavid)

    def stopOperationRecordBrowser(self):
        return self.api.stopOperationRecordBrowser()

    def getOperationRecordBrowserStatus(self):
        return self.api.getOperationRecordBrowserStatus()


# ==================== 屏幕信息获取 ====================
def get_real_screen_info():
    """获取物理分辨率（跨平台）"""
    system = platform.system()

    # Windows 分支
    if system == "Windows" and win32api:
        try:
            physical_width = win32api.GetSystemMetrics(0)  # SM_CXSCREEN
            physical_height = win32api.GetSystemMetrics(1)  # SM_CYSCREEN
            return {'width': physical_width, 'height': physical_height}
        except Exception as e:
            print(f"Win32 API failed: {e}")

    # macOS 分支
    if system == "Darwin":
        try:
            lib_path = find_library("CoreGraphics")
            if not lib_path:
                raise RuntimeError("CoreGraphics not found")
            cg = ctypes.CDLL(lib_path)
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
            cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t

            did = cg.CGMainDisplayID()
            w = cg.CGDisplayPixelsWide(did)
            h = cg.CGDisplayPixelsHigh(did)
            if w and h:
                return {'width': int(w), 'height': int(h)}
        except Exception as e:
            print(f"CoreGraphics failed: {e}")

    return None


def get_primary_screen():
    """智能获取主显示屏幕"""
    print("[屏幕] 开始检测屏幕...")

    # 1. 优先获取物理分辨率
    real_info = get_real_screen_info()
    if real_info:
        print(f"[屏幕] 物理分辨率: {real_info['width']}x{real_info['height']}")

    # 2. 使用 screeninfo 获取布局信息
    if SCREENINFO_AVAILABLE:
        try:
            monitors = screeninfo.get_monitors()
            if monitors:
                print(f"[屏幕] screeninfo 检测到 {len(monitors)} 个显示器")

                # 寻找主屏幕
                primary_monitor = None
                for monitor in monitors:
                    if hasattr(monitor, 'is_primary') and monitor.is_primary:
                        primary_monitor = monitor
                        break
                    elif monitor.x == 0 and monitor.y == 0 and not primary_monitor:
                        primary_monitor = monitor

                if primary_monitor:
                    print(f"[屏幕] 主屏幕: {primary_monitor.width}x{primary_monitor.height}")

                    # 对比 Win32 和 screeninfo 分辨率
                    if real_info:
                        scale = primary_monitor.width / real_info['width'] * 100
                        print(f"[屏幕] 屏幕缩放: {scale:.0f}%")

                    # 优先使用物理分辨率
                    if real_info:
                        return type('Screen', (), {
                            'width': real_info['width'],
                            'height': real_info['height'],
                            'x': 0,
                            'y': 0
                        })()

                    return primary_monitor
        except Exception as e:
            print(f"[屏幕] screeninfo 获取失败: {e}")

    # 3. 使用物理分辨率
    if real_info:
        return type('Screen', (), {
            'width': real_info['width'],
            'height': real_info['height'],
            'x': 0,
            'y': 0
        })()

    # 4. 默认方案
    print("[屏幕] 使用默认屏幕设置")
    return type('DefaultScreen', (), {
        'width': 1920, 'height': 1080, 'x': 0, 'y': 0
    })()


# ==================== 主程序 ====================

def _cleanup_data_temp_dir():
    """启动时清空 data/temp（更新 zip、解压目录、备份的 exe/bin、临时 bat 等），失败不影响启动。"""
    if not os.path.isdir(DATA_TEMP_DIR):
        return
    try:
        shutil.rmtree(DATA_TEMP_DIR, ignore_errors=True)
        print("[*] 已清理临时目录: data/temp")
    except Exception as e:
        print(f"[!] 清理 data/temp 失败（可忽略）: {e}")


def main():
    try:
        # ===== 单实例检查 =====
        print("[*] 检查单实例运行...")
        if getattr(sys, 'frozen', False):
            if single_instance_checker.check_single_instance():
                print("[!] 程序已在运行中，已激活现有窗口")
                print(" 程序退出")
                return
        print("[OK] 单实例检查通过")

        _cleanup_data_temp_dir()

        # ===== 屏幕信息 =====
        primary_screen = get_primary_screen()

        # ===== 窗口尺寸设置 =====
        width = 1920
        height = 1200

        # 根据屏幕大小调整
        max_width = int(primary_screen.width * 0.85)
        max_height = int(primary_screen.height * 0.85)

        min_width = 1200
        min_height = 800

        if width > max_width:
            width = max(max_width, min_width)
        if height > max_height:
            height = max(max_height, min_height)

        # 居中位置
        x = (primary_screen.width - width) // 2
        y = (primary_screen.height - height) // 2

        print(f"[窗口] 大小: {width}x{height}, 位置: ({x}, {y})")

        # ===== 加载 HTML 页面 =====
        index_path = os.path.join(STATIC_DIR, "index.html")

        if not os.path.exists(index_path):
            print(f"[ERR] index.html 不存在: {index_path}")
            return

        # Windows 上勿用 f"file://{path}"：反斜杠、空格、中文路径会导致 WebView2 解析失败 → 白屏。
        index_url = Path(index_path).resolve().as_uri()

        js_api = JSApi()

        # ===== 创建窗口 =====
        storage_path = os.path.join(DATA_DIR, "storage")
        os.makedirs(storage_path, exist_ok=True)

        window_title = f"千川素材看盘工具 v{CURRENT_VERSION}"
        if TEST_MODE:
            window_title += " · 测试1版（本地测试）"

        window = webview.create_window(
            title=window_title,
            url=index_url,
            width=width,
            height=height,
            x=x,
            y=y,
            resizable=True,
            min_size=(1200, 800),
            js_api=js_api
        )

        # ===== 创建托盘 =====
        tray_app = None
        if PYSTRAY_AVAILABLE:
            tray_app = TrayApplication(window)
            tray_app.run_tray()

            # 绑定窗口关闭事件 - 隐藏到托盘
            try:
                window.events.closing += tray_app.on_window_closing
            except AttributeError:
                pass
        else:
            print("[!] 托盘功能不可用，窗口关闭后将直接退出")

        # ===== 启动命令监听线程（用于单实例激活） =====
        def command_poll():
            """后台线程：监听命令文件，用于激活已有窗口"""
            while True:
                single_instance_checker.check_and_show_window_from_command()
                threading.Event().wait(0.25)

        threading.Thread(target=command_poll, daemon=True).start()

        # ===== macOS：Dock 点击恢复窗口；Dock 右键 / Cmd+Q 退出 =====
        if platform.system() == "Darwin":
            configure_macos_lifecycle(window, tray_app)

        # ===== 服务管理页默认配置（data/control_panel.json）=====
        ensure_all_control_defaults()

        # Local-test-only convenience for handing the visible QianChuan login
        # window to the user without putting credentials on the command line.
        if TEST_MODE and os.getenv("QCSCKP_AUTO_START_SERVICE", "").strip() == "1":
            def _auto_start_local_test_service():
                time.sleep(1.0)
                try:
                    creds = js_api.getLocalTestLoginCredentials()
                    if not creds.get("success"):
                        print(
                            "[TEST] Auto-start skipped: "
                            f"{creds.get('message') or 'local credentials unavailable'}"
                        )
                        return
                    try:
                        interval = max(
                            5,
                            int(os.getenv("QCSCKP_AUTO_START_INTERVAL", "600")),
                        )
                    except Exception:
                        interval = 600
                    result = js_api.startService(
                        interval,
                        True,
                        creds.get("username"),
                        creds.get("password"),
                    )
                    print(
                        "[TEST] Scraper auto-start requested: "
                        f"success={result.get('success', True)} "
                        f"phase={result.get('phase', '')}"
                    )
                except Exception as exc:
                    print(f"[TEST] Scraper auto-start failed: {exc}")

            threading.Thread(
                target=_auto_start_local_test_service,
                daemon=True,
                name="qcsckp-test-auto-start",
            ).start()

        # ===== SQLite：后台裁剪旧数据（不阻塞窗口；策略见 config / sqlite_prune_scheduler）=====
        start_sqlite_prune_background_thread()

        # ===== 飞书 / 钉钉 Webhook：每整点推送（见 services/webhook_push_runtime.py）=====
        start_webhook_push_background_threads()

        # ===== 规则化追投调度（见 services/retargeting_rule_runner.py；enabled 见 rule_retargeting.json）=====
        start_retargeting_rule_runner_background_thread()

        # ===== 飞书卡片已批准追投：本地任务队列领取、复核与执行 =====
        start_retarget_task_worker_background_thread()

        # ===== 千川/巨量纵横后台操作日志：已发现页面每5分钟增量同步 =====
        start_platform_log_sync_background_thread()

        # ===== 规则化停投调度（见 services/regulation_rule_runner.py；enabled 见 rule_regulation.json，默认 10 分钟一轮）=====
        start_regulation_rule_runner_background_thread()

        # ===== 启动 webview =====
        print("[START] 启动窗口...")
        webview.start(debug=True, http_server=False, private_mode=False, storage_path=storage_path)

    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"[ERR] 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            from services.local_feishu_bridge import deactivate_local_feishu_account

            deactivate_local_feishu_account()
        except Exception:
            pass
        print("程序已退出")


if __name__ == "__main__":
    main()
