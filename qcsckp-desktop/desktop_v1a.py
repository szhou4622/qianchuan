"""千川工具生产版 V1A 桌面入口。"""

from __future__ import annotations

import ctypes
import sys

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
        webview.start(debug=False)
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
