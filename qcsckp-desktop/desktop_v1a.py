"""千川工具生产版 V1A 桌面入口。

生产版冻结并复用 v0.1.46 的 ``static/`` 前端。后续 V1A 只通过
``gui_app.JSApi`` 兼容层逐项替换后端服务，不再维护第二套业务界面。
"""

from __future__ import annotations

from gui_app import main as run_frozen_legacy_frontend


def main() -> int:
    """启动冻结的原版前端及其后端兼容层。"""

    result = run_frozen_legacy_frontend()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
