"""生产版 V1A 的独立、本机用户级运行目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .constants import RUNTIME_NAME


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    runtime_db: Path
    history_dir: Path
    snapshots_dir: Path
    reports_dir: Path
    logs_dir: Path
    browser_profile_dir: Path
    secrets_dir: Path
    service_state: Path
    migration_manifest: Path

    @classmethod
    def default(cls) -> "RuntimePaths":
        override = (os.getenv("QCSCKP_V1A_DATA_DIR") or "").strip()
        if override:
            root = Path(os.path.expandvars(os.path.expanduser(override))).resolve()
        else:
            local_app_data = os.getenv("LOCALAPPDATA")
            if not local_app_data:
                local_app_data = str(Path.home() / "AppData" / "Local")
            root = Path(local_app_data) / "QCSCKP" / RUNTIME_NAME
        return cls.from_root(root)

    @classmethod
    def from_root(cls, root: Path | str) -> "RuntimePaths":
        resolved = Path(root).expanduser().resolve()
        return cls(
            root=resolved,
            runtime_db=resolved / "runtime.db",
            history_dir=resolved / "history",
            snapshots_dir=resolved / "snapshots",
            reports_dir=resolved / "reports",
            logs_dir=resolved / "logs",
            browser_profile_dir=resolved / "chrome-profile",
            secrets_dir=resolved / "secrets",
            service_state=resolved / "service.json",
            migration_manifest=resolved / "migration-manifest.json",
        )

    def ensure(self) -> "RuntimePaths":
        for path in (
            self.root,
            self.history_dir,
            self.snapshots_dir,
            self.reports_dir,
            self.logs_dir,
            self.browser_profile_dir,
            self.secrets_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def history_db_for_month(self, business_month: str) -> Path:
        if len(business_month) != 7 or business_month[4] != "-":
            raise ValueError("business_month must use YYYY-MM")
        return self.history_dir / f"history-{business_month}.db"
