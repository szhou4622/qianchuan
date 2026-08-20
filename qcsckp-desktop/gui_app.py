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
from functools import wraps
import webview
from ctypes.util import find_library
from api import Api
# 项目配置
from config import (
    APP_NAME,
    SOFTWARE_CHINESE_NAME,
    PROJECT_ROOT,
    DATA_DIR,
    DATA_TEMP_DIR,
    STATIC_DIR,
    CURRENT_VERSION,
    TEST_MODE,
    TEST_AAVID,
    TEST_MATERIAL_ID,
    ALLOW_LIVE_RETARGET,
    AUTH_MODE,
    LOCAL_AUTH_USERNAME,
    QIANCHUAN_BACKEND,
)
from services.contact_http import ContactLocalHttpServer
from services.control_panel_config import ensure_all_control_defaults
from services.license_manager import LicenseManager
from services.runtime_supervisor import RUNTIME_SUPERVISOR


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
        self._lease_handle = None

    def acquire_runtime_lease(self) -> bool:
        """按数据目录加跨进程锁，源码运行和打包运行都只能启动一个实例。"""
        if self._lease_handle is not None:
            return True
        os.makedirs(DATA_DIR, exist_ok=True)
        lock_path = os.path.join(DATA_DIR, "qcsckp.instance.lock")
        handle = open(lock_path, "a+b")
        try:
            handle.seek(0)
            if handle.read(1) != b"1":
                handle.seek(0)
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            self._write_show_window_command()
            self.activate_existing_instance()
            return False
        self._lease_handle = handle
        return True

    def release_runtime_lease(self) -> None:
        handle = self._lease_handle
        self._lease_handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        finally:
            handle.close()

    @staticmethod
    def _activate_windows_process(pid: int) -> bool:
        """恢复指定进程的主窗口；用于托盘隐藏后重复双击程序的兜底唤醒。"""
        if sys.platform != "win32" or not pid:
            return False
        try:
            user32 = ctypes.windll.user32
            found = []
            enum_proc = ctypes.WINFUNCTYPE(
                ctypes.c_bool,
                ctypes.c_void_p,
                ctypes.c_void_p,
            )

            @enum_proc
            def callback(hwnd, _lparam):
                window_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(window_pid),
                )
                if int(window_pid.value) != int(pid):
                    return True
                title_length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                if title_length <= 0:
                    return True
                title = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(hwnd, title, title_length + 1)
                if "千川素材看盘工具" not in str(title.value or ""):
                    return True
                # SW_RESTORE 处理最小化，SW_SHOW 处理托盘隐藏。重复启动是由
                # 用户双击触发，新进程此时有权把既有窗口带到前台。
                user32.ShowWindowAsync(hwnd, 9)
                user32.ShowWindowAsync(hwnd, 5)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                found.append(int(hwnd))
                return False

            user32.EnumWindows(callback, 0)
            return bool(found)
        except Exception as exc:
            print(f"[单实例检查] Windows窗口恢复失败: {exc}")
            return False

    def activate_existing_instance(self) -> bool:
        """按规范化可执行路径查找并恢复已经运行的同一工具。"""
        if not PSUTIL_AVAILABLE:
            return False
        current_pid = os.getpid()
        current_key = _canonical_exe_path(sys.executable)
        if not current_key:
            return False
        try:
            for proc in psutil.process_iter(["pid", "exe"]):
                try:
                    pid = int(proc.info.get("pid") or 0)
                    if not pid or pid == current_pid:
                        continue
                    proc_exe = _psutil_resolve_exe(proc)
                    if (
                        proc_exe
                        and _canonical_exe_path(proc_exe) == current_key
                        and self._activate_windows_process(pid)
                    ):
                        print(f"[单实例检查] 已恢复现有窗口: PID={pid}")
                        return True
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                    ValueError,
                ):
                    continue
        except Exception as exc:
            print(f"[单实例检查] 恢复现有实例失败: {exc}")
        return False

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
                        self._activate_windows_process(proc.info["pid"])
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
                    self._activate_windows_process(os.getpid())
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
        # Creating an Icon object is not proof that its event thread actually
        # reached the Windows tray.  Track real readiness so a failed tray can
        # never leave an invisible background process after the window closes.
        self.tray_ready = threading.Event()
        self.tray_failed = threading.Event()
        self.tray_thread = None
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

    def _mark_tray_ready(self, icon):
        """只有系统托盘循环真正就绪后，才允许主窗口隐藏。"""
        try:
            icon.visible = True
            self.tray_ready.set()
            print("[托盘] 托盘图标已显示")
        except Exception as exc:
            self.tray_failed.set()
            self.enable_tray = False
            print(f"[托盘] 显示托盘图标失败: {exc}")

    def _run_tray_icon(self):
        try:
            self.icon.run(setup=self._mark_tray_ready)
        except Exception as exc:
            self.tray_failed.set()
            self.enable_tray = False
            print(f"[托盘] 托盘事件线程异常退出: {exc}")

    def run_tray(self):
        """启动托盘并等待平台线程反馈真实状态。"""
        if not self.icon:
            return

        try:
            if platform.system() == "Darwin" and hasattr(self.icon, "run_detached"):
                self.icon.run_detached(setup=self._mark_tray_ready)
            else:
                self.tray_thread = threading.Thread(
                    target=self._run_tray_icon,
                    name="qcsckp-system-tray",
                    daemon=True,
                )
                self.tray_thread.start()
            print("[托盘] 正在启动托盘图标")
        except Exception as exc:
            self.tray_failed.set()
            self.enable_tray = False
            print(f"[托盘] 启动托盘失败: {exc}")

    def on_window_closing(self, event=None):
        """窗口关闭事件处理 - 隐藏到托盘而不是退出"""
        # force_close 为 True 时允许真正关闭
        if self.force_close:
            return True
        tray_visible = False
        if self.enable_tray and self.icon and self.tray_ready.is_set():
            try:
                tray_visible = bool(self.icon.visible)
            except Exception:
                tray_visible = False
        if tray_visible:
            print("[窗口] 关闭 -> 隐藏到托盘")
            self.window.hide()
            return False  # 阻止关闭
        # 托盘未真正显示时绝不保留隐形后台进程。
        self.force_close = True
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        print("[窗口] 托盘不可用，关闭窗口将完全退出")
        return True


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
    def __init__(self, contact_server=None, license_manager=None, license_url=""):
        # 必须早于 Api() 的 SQLite 建表迁移，保证可以完整恢复rc23数据。
        try:
            from services.rc23_rollback import ensure_rc23_upgrade_snapshot

            ensure_rc23_upgrade_snapshot()
        except Exception as exc:
            print(f"警告：rc23升级快照创建失败：{exc}")
        self.api = Api()
        self.contact_server = contact_server
        self.license_manager = license_manager
        self.license_url = str(license_url or "")
        self._window = None
        self._licensed_runtime_started = False
        self._licensed_runtime_lock = threading.Lock()
        self._license_watchdog_stop = threading.Event()
        self._license_watchdog_thread = None

    def _stop_licensed_runtime(self):
        self._license_watchdog_stop.set()
        with self._licensed_runtime_lock:
            if self._licensed_runtime_started:
                RUNTIME_SUPERVISOR.stop()
                self._licensed_runtime_started = False

    def _start_license_watchdog(self):
        manager = self.license_manager
        if manager is None:
            return
        current = self._license_watchdog_thread
        if current is not None and current.is_alive():
            return
        try:
            interval = max(
                60,
                int(os.getenv("QCSCKP_LICENSE_RECHECK_SECONDS", "60")),
            )
        except (TypeError, ValueError):
            interval = 60
        self._license_watchdog_stop = threading.Event()

        def watch():
            while not self._license_watchdog_stop.wait(interval):
                state = manager.runtime_check()
                window = self._window
                if window is not None:
                    try:
                        public_state = json.dumps(
                            state,
                            ensure_ascii=True,
                            separators=(",", ":"),
                        )
                        window.evaluate_js(
                            "window.applyLicenseRuntimeState&&"
                            f"window.applyLicenseRuntimeState({public_state})"
                        )
                    except Exception:
                        pass
                if state.get("authorized"):
                    continue
                self._stop_licensed_runtime()
                if int(state.get("http_status") or 0) == 401:
                    self.api.clear_license_cloud_sessions()
                if window is not None and self.license_url:
                    try:
                        window.load_url(self.license_url)
                    except Exception:
                        pass
                return

        self._license_watchdog_thread = threading.Thread(
            target=watch,
            daemon=True,
            name="qcsckp-license-watchdog",
        )
        self._license_watchdog_thread.start()

    def getLicenseBootstrapStatus(self):
        manager = self.license_manager
        if manager is None:
            return {
                "success": True,
                "authorized": True,
                "software_name": SOFTWARE_CHINESE_NAME,
                "app_name": APP_NAME,
                "message": "开发环境未启用在线授权门禁",
            }
        result = manager.startup_check()
        try:
            result["machine_code"] = manager.store.get_or_create_device_code()
        except Exception:
            result["machine_code"] = ""
        if int(result.get("http_status") or 0) == 401:
            self.api.clear_license_cloud_sessions()
        return result

    def activateOnlineLicense(self, activationCode=None):
        manager = self.license_manager
        if manager is None:
            return {
                "success": False,
                "authorized": False,
                "message": "当前环境未启用在线授权",
            }
        result = manager.activate(activationCode)
        try:
            result["machine_code"] = manager.store.get_or_create_device_code()
        except Exception:
            result["machine_code"] = ""
        return result

    def enterLicensedApplication(self):
        manager = self.license_manager
        if manager is not None and not manager.is_runtime_authorized():
            return {
                "success": False,
                "authorized": False,
                "message": "软件授权尚未通过，不能进入主界面",
            }
        identity = self.api.activate_license_runtime_identity()
        if not identity.get("success"):
            return {
                "success": False,
                "authorized": True,
                "message": identity.get("message") or "本机授权身份初始化失败",
            }
        with self._licensed_runtime_lock:
            if not self._licensed_runtime_started:
                RUNTIME_SUPERVISOR.start(self)
                self._licensed_runtime_started = True
        self._start_license_watchdog()
        return {"success": True, "authorized": True}

    def getLicenseManagementInfo(self):
        manager = self.license_manager
        if manager is None:
            return self.getLicenseBootstrapStatus()
        result = manager.management_info(refresh=True)
        if not result.get("authorized") and self._licensed_runtime_started:
            self._stop_licensed_runtime()
        if int(result.get("http_status") or 0) == 401:
            self.api.clear_license_cloud_sessions()
        return result

    def unbindCurrentLicense(self):
        manager = self.license_manager
        if manager is None:
            return {
                "success": False,
                "authorized": False,
                "message": "当前环境未启用在线授权",
            }
        result = manager.unbind_current_device()
        if result.get("success") and not result.get("authorized"):
            self._stop_licensed_runtime()
            self.api.clear_license_cloud_sessions()
        return result

    def getContactApiUrl(self):
        """Return only the loopback facade; the UI never receives the remote endpoint."""
        server = self.contact_server
        return {
            "success": bool(server and server.contact_url),
            "app_name": APP_NAME,
            "url": server.contact_url if server else "",
            "preview_url": server.preview_url if server else "",
        }

    def getTableData(
        self,
        period="1h",
        sortBy="costDiff",
        sortOrder="desc",
        page=1,
        pageSize=50,
        targetUid=None,
        aavid=None,
    ):
        return self.api.get_table_data(
            period, sortBy, sortOrder, page, pageSize, targetUid, aavid
        )

    def getTop20ByCost(self, hours=1, aavid=None, targetUid=None):
        return self.api.get_top20_by_cost(hours, aavid, targetUid)

    def getLatestCrawlCostSum(self, hours=1, aavid=None, targetUid=None):
        return self.api.get_latest_crawl_cost_sum(hours, aavid, targetUid)

    def getMaterialHistoryRecent(self, material_id, limit=200, targetUid=None):
        return self.api.get_material_history_recent(material_id, limit, targetUid)

    def getDashboardScopeOptions(self):
        return self.api.get_dashboard_scope_options()

    def getDashboardBootstrap(self):
        return self.api.get_dashboard_bootstrap()

    def getDashboardRefreshState(self, aavid=None, targetUid=None):
        return self.api.get_dashboard_refresh_state(aavid, targetUid)

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

    def getOperationDailyReportConfig(self):
        return self.api.getOperationDailyReportConfig()

    def saveOperationDailyReportConfig(self, config):
        return self.api.saveOperationDailyReportConfig(config)

    def sendYesterdayOperationDailyReportNow(self):
        return self.api.sendYesterdayOperationDailyReportNow()

    # 直播 / 商品全域监控计划
    #
    # pywebview 只会向前端暴露 JSApi 上显式声明的方法。核心 Api 即使已经
    # 实现相应接口，如果这里漏掉转发，页面也会一直等待而拿不到账户或计划。
    def getQianchuanAccountOverview(self):
        return self.api.getQianchuanAccountOverview()

    def saveQianchuanAccountSettings(self, accountUid=None, settings=None):
        return self.api.saveQianchuanAccountSettings(accountUid, settings)

    def saveQianchuanAccountAutomationSetup(
        self,
        accountUid=None,
        settings=None,
        planStates=None,
    ):
        return self.api.saveQianchuanAccountAutomationSetup(
            accountUid,
            settings,
            planStates,
        )

    def removeQianchuanAccount(self, accountUid=None):
        return self.api.removeQianchuanAccount(accountUid)

    def addOfficialApiQianchuanAccount(self, aavid=None):
        return self.api.addOfficialApiQianchuanAccount(aavid)

    def getQianchuanOfficialApiConfig(self):
        return self.api.getQianchuanOfficialApiConfig()

    def saveQianchuanOfficialApiConfig(self, config=None):
        return self.api.saveQianchuanOfficialApiConfig(config)

    def startQianchuanOfficialApiAuthorization(self):
        return self.api.startQianchuanOfficialApiAuthorization()

    def saveAndStartQianchuanOfficialApiAuthorization(self, config=None):
        return self.api.saveAndStartQianchuanOfficialApiAuthorization(config)

    def finishQianchuanOfficialApiAuthorization(self, authCode=None):
        return self.api.finishQianchuanOfficialApiAuthorization(authCode)

    def clearQianchuanOfficialApiConfig(self):
        return self.api.clearQianchuanOfficialApiConfig()

    def startQianchuanCatalogSync(self, accountUid=None):
        return self.api.startQianchuanCatalogSync(accountUid)

    def getQianchuanCatalogSyncStatus(self):
        return self.api.getQianchuanCatalogSyncStatus()

    def setWindowsAutostart(self, enabled=False):
        return self.api.setWindowsAutostart(enabled)

    def restoreRc23QianchuanCookie(self):
        return self.api.restoreRc23QianchuanCookie()

    def listPromotionTargets(self, enabled=None):
        return self.api.listPromotionTargets(enabled)

    def getPromotionTarget(self, targetUid=None):
        return self.api.getPromotionTarget(targetUid)

    def savePromotionTarget(self, data=None):
        return self.api.savePromotionTarget(data)

    def discoverPromotionTarget(self, pageUrl=None, pageText=None, planName=None):
        return self.api.discoverPromotionTarget(pageUrl, pageText, planName)

    def setPromotionTargetEnabled(self, targetUid=None, enabled=True):
        return self.api.setPromotionTargetEnabled(targetUid, enabled)

    def clearPromotionTargetWriteBlock(self, targetUid=None):
        return self.api.clearPromotionTargetWriteBlock(targetUid)

    def listPromotionTargetProducts(self, targetUid=None):
        return self.api.listPromotionTargetProducts(targetUid)

    def probePromotionTargetRetargetCapability(self, targetUid=None):
        return self.api.probePromotionTargetRetargetCapability(targetUid)

    def startPromotionTargetDiscovery(self):
        return self.api.startPromotionTargetDiscovery()

    def startQianchuanAccountSelection(self):
        return self.api.startQianchuanAccountSelection()

    def startQianchuanRelogin(self):
        return self.api.startQianchuanRelogin()

    def getPromotionTargetDiscoveryStatus(self):
        return self.api.getPromotionTargetDiscoveryStatus()

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
            "auth_mode": str(AUTH_MODE or "local"),
            "local_auth_username": (
                str(LOCAL_AUTH_USERNAME or "") if AUTH_MODE == "local" else ""
            ),
        }

    def performAppUpdate(self, download_url, expectedSha256=""):
        return self.api.perform_app_update(download_url, expectedSha256)

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

    def runImmediateRetargetPrepare(
        self,
        material_id=None,
        retargeting=None,
        target_uid=None,
    ):
        return self.api.runImmediateRetargetPrepare(
            material_id,
            retargeting,
            target_uid,
        )

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
            aavid=aavid,
            date_from=date_from,
            date_to=date_to,
            action_type=action_type,
            source=source,
            status=status,
            operator=operator,
            q=q,
            page=page,
            page_size=page_size,
        )

    def getOperationEventDetail(self, event_id=None, aavid=None):
        return self.api.getOperationEventDetail(event_id, aavid)

    def getOperationSyncState(self, aavid=None):
        return self.api.getOperationSyncState(aavid)

    def syncOperationLogsNow(self, aavid=None):
        return self.api.syncOperationLogsNow(aavid)

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
            aavid=aavid,
            date_from=date_from,
            date_to=date_to,
            action_type=action_type,
            source=source,
            status=status,
            operator=operator,
            q=q,
        )

    def saveOperationEventsCsv(
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
        """使用桌面原生“另存为”保存CSV，避免WebView吞掉网页下载。"""

        result = self.api.exportOperationEventsCsv(
            aavid=aavid,
            date_from=date_from,
            date_to=date_to,
            action_type=action_type,
            source=source,
            status=status,
            operator=operator,
            q=q,
        )
        if not result or not result.get("success"):
            return result or {"success": False, "message": "导出数据生成失败"}
        try:
            if not webview.windows:
                return {"success": False, "message": "桌面窗口尚未就绪"}
            filename = str(result.get("filename") or "账户操作流水.csv")
            selected = webview.windows[0].create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(Path.home() / "Desktop"),
                save_filename=filename,
                file_types=("CSV 文件 (*.csv)", "所有文件 (*.*)"),
            )
            if not selected:
                return {"success": True, "cancelled": True, "message": "已取消导出"}
            path_text = str(selected[0] if isinstance(selected, (list, tuple)) else selected)
            if not path_text.lower().endswith(".csv"):
                path_text += ".csv"
            destination = Path(path_text)
            destination.write_text(
                str(result.get("content") or ""),
                encoding="utf-8",
                newline="",
            )
            return {
                "success": True,
                "cancelled": False,
                "path": str(destination),
                "message": f"导出成功：{destination}",
            }
        except Exception as exc:
            return {"success": False, "message": f"保存CSV失败：{exc}"}

    def startOperationRecordBrowser(self, aavid=None):
        return self.api.startOperationRecordBrowser(aavid)

    def stopOperationRecordBrowser(self):
        return self.api.stopOperationRecordBrowser()

    def getOperationRecordBrowserStatus(self):
        return self.api.getOperationRecordBrowserStatus()


_LICENSE_GATE_METHODS = {
    "getLicenseBootstrapStatus",
    "activateOnlineLicense",
    "enterLicensedApplication",
    "getLicenseManagementInfo",
    "unbindCurrentLicense",
    # Users must be able to reach support when activation itself is blocked.
    "getContactApiUrl",
}


def _install_js_api_license_gate() -> None:
    """Wrap every non-license bridge method with a backend authorization gate."""
    for name, method in list(vars(JSApi).items()):
        if name.startswith("_") or name in _LICENSE_GATE_METHODS or not callable(method):
            continue

        @wraps(method)
        def guarded(self, *args, __method=method, **kwargs):
            # A few legacy bridge unit tests instantiate JSApi via
            # object.__new__ and inject only ``api``.  Production construction
            # always sets LicenseManager explicitly in main().
            manager = getattr(self, "license_manager", None)
            if manager is not None and not manager.is_runtime_authorized():
                return {
                    "success": False,
                    "authorized": False,
                    "error": "license_required",
                    "message": "软件授权未通过，请重新激活或联系管理员",
                }
            return __method(self, *args, **kwargs)

        setattr(JSApi, name, guarded)


_install_js_api_license_gate()


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
    contact_server = None
    license_manager = None
    js_api = None
    if not single_instance_checker.acquire_runtime_lease():
        single_instance_checker._write_show_window_command()
        print("[!] 当前数据目录已有工具实例运行，已激活现有窗口")
        return
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
        license_path = os.path.join(STATIC_DIR, "license.html")

        if not os.path.exists(index_path) or not os.path.exists(license_path):
            print(f"[ERR] 授权页或主页面不存在: {license_path} / {index_path}")
            return

        # Windows 上勿用 f"file://{path}"：反斜杠、空格、中文路径会导致 WebView2 解析失败 → 白屏。
        index_url = Path(index_path).resolve().as_uri()
        license_url = Path(license_path).resolve().as_uri()

        # Starting the loopback listener performs no network request.  The
        # shared contact service is consulted only after the UI's first
        # hover/focus/click on "联系作者".
        try:
            contact_server = ContactLocalHttpServer()
            contact_server.start()
            print(f"[联系作者] 本地接口已就绪: {contact_server.contact_url}")
        except Exception as exc:
            contact_server = None
            print(f"[联系作者] 本地接口启动失败，界面将使用内置兜底图: {exc}")

        license_manager = LicenseManager()
        js_api = JSApi(
            contact_server=contact_server,
            license_manager=license_manager,
            license_url=license_url,
        )

        # ===== 创建窗口 =====
        storage_path = os.path.join(DATA_DIR, "storage")
        os.makedirs(storage_path, exist_ok=True)

        window_title = f"千川素材看盘工具 v{CURRENT_VERSION}"
        if TEST_MODE:
            window_title += " · 测试1版（本地测试）"

        window = webview.create_window(
            title=window_title,
            url=license_url,
            width=width,
            height=height,
            x=x,
            y=y,
            resizable=True,
            min_size=(1200, 800),
            js_api=js_api
        )
        js_api._window = window

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

        # Business workers are intentionally not started here.  The license
        # page starts them only after the server confirms this device is active.

        # ===== 启动 webview =====
        print("[START] 启动窗口...")
        webview.start(debug=False, http_server=False, private_mode=False, storage_path=storage_path)

    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"[ERR] 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            if js_api is not None:
                js_api._stop_licensed_runtime()
        except Exception:
            pass
        try:
            if contact_server is not None:
                contact_server.stop()
        except Exception:
            pass
        try:
            RUNTIME_SUPERVISOR.stop()
        except Exception:
            pass
        single_instance_checker.release_runtime_lease()
        print("程序已退出")


if __name__ == "__main__":
    main()
