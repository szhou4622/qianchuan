"""千川工具生产版 V1A 桌面入口。"""

from __future__ import annotations

import ctypes
import sys
import threading
from pathlib import Path

from production_v1a.runtime_paths import RuntimePaths
from production_v1a.service_main import start_service, wake_existing
from production_v1a.single_instance import GlobalUserMutex


def _message(text: str, title: str = "千川工具生产版 V1A") -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(text)


def main() -> int:
    paths = RuntimePaths.default().ensure()
    mutex = GlobalUserMutex()
    if not mutex.acquire():
        if not wake_existing(paths):
            _message("工具已经运行，但暂时无法唤醒窗口。请稍后再试。")
        return 0

    service = None
    try:
        service = start_service(paths=paths)
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("缺少 pywebview，无法启动桌面界面") from exc

        url = f"{service.base_url}/#token={service.launch_token}"
        window = webview.create_window(
            "千川工具生产版 V1A",
            url=url,
            width=1440,
            height=920,
            min_size=(1120, 720),
            background_color="#07101F",
        )

        def wake_window() -> None:
            try:
                window.restore()
                window.show()
                window.on_top = True
                window.on_top = False
            except Exception:
                pass

        service.server.window_wake_callback = wake_window
        exit_requested = threading.Event()
        tray = None
        try:
            import pystray
            from PIL import Image, ImageDraw

            icon_path = Path(__file__).resolve().parent / "logo.ico"
            if icon_path.is_file():
                tray_image = Image.open(icon_path).convert("RGBA")
            else:
                tray_image = Image.new("RGBA", (64, 64), "#0b65d8")
                draw = ImageDraw.Draw(tray_image)
                draw.ellipse((14, 14, 50, 50), outline="white", width=5)

            def show_from_tray(_icon=None, _item=None) -> None:
                wake_window()

            def quit_from_tray(icon=None, _item=None) -> None:
                exit_requested.set()
                if icon is not None:
                    icon.stop()
                try:
                    window.destroy()
                except Exception:
                    pass

            tray = pystray.Icon(
                "qcsckp-production-v1a",
                tray_image,
                "千川工具生产版 V1A",
                pystray.Menu(
                    pystray.MenuItem("打开工具", show_from_tray, default=True),
                    pystray.MenuItem("完全退出", quit_from_tray),
                ),
            )
            tray.run_detached()

            def keep_running_in_tray(*_args) -> bool:
                if exit_requested.is_set():
                    return True
                try:
                    window.hide()
                except Exception:
                    pass
                return False

            window.events.closing += keep_running_in_tray
        except Exception:
            # 托盘初始化失败时仍可使用桌面界面；关闭窗口将安全退出。
            tray = None
        webview.start(debug=False)
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        return 0
    except Exception as exc:
        _message(str(exc), "V1A 启动失败")
        return 1
    finally:
        if service is not None:
            service.close()
        mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
