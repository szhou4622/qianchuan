"""
全局路径配置（统一 PROJECT_ROOT，避免各处口径不一致）

与 qianchuanzhijian/config 一致：
- 仅「打包 / frozen」时 macOS 才拆分：可写数据在 ~/.qcsckp/<hash>/，静态在 .app/Contents/Resources
- 开发环境（非 frozen）：数据与静态都在项目根目录（本文件所在目录）
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _darwin_frozen_data_dir_name() -> str:
    """仅 macOS 打包：~/.qcsckp/<此名>/，由 .app 路径 SHA256 前 16 位派生。"""
    try:
        exe = Path(os.path.realpath(sys.executable))
        if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
            identity = os.path.realpath(str(exe.parent.parent.parent))
        else:
            identity = os.path.realpath(sys.executable)
    except OSError:
        identity = os.path.realpath(sys.executable)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _macos_frozen_resources_dir() -> str:
    """仅 macOS 打包：.app/Contents/Resources"""
    exe_dir = os.path.dirname(os.path.realpath(sys.executable))
    return os.path.join(os.path.dirname(exe_dir), "Resources")


def _get_project_root() -> str:
    """
    可写数据根目录（data / temp / logs）：
    - 开发：项目根 = 本文件所在目录
    - Windows 打包：exe 所在目录
    - macOS 打包：~/.qcsckp/<hash>/（.app 内只读，数据放用户目录）
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            sub = _darwin_frozen_data_dir_name()
            root = Path(os.path.expanduser("~")) / ".qcsckp" / sub
            root.mkdir(parents=True, exist_ok=True)
            return str(root)
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


PROJECT_ROOT = _get_project_root()


CURRENT_VERSION = "0.1.51"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str) -> str:
    return (os.getenv(name) or "").strip()


# 常用目录
_data_dir_override = _env_text("QCSCKP_DATA_DIR")
if _data_dir_override:
    DATA_DIR = os.path.abspath(os.path.expandvars(os.path.expanduser(_data_dir_override)))
    DATA_TEMP_DIR = os.path.join(DATA_DIR, "temp")
    LOGS_DIR = os.path.join(DATA_DIR, "logs")
else:
    DATA_DIR = os.path.join(PROJECT_ROOT, "data")
    DATA_TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
    LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
DB_FILE = os.path.join(DATA_DIR, "qianchuan.db")
DASHBOARD_ACCOUNT_LABEL_FILE = os.path.join(DATA_DIR, "dashboard_config.json")
QIANCHUAN_RUNTIME_SETTINGS_FILE = os.path.join(
    DATA_DIR,
    "qianchuan_runtime_settings.json",
)


def _load_qianchuan_runtime_settings() -> dict:
    """Load non-secret backend choices that must survive an app restart."""
    try:
        with open(QIANCHUAN_RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_QIANCHUAN_RUNTIME_SETTINGS = _load_qianchuan_runtime_settings()

# 本地联调保护。默认全部关闭，正式版行为不变。
TEST_MODE = _env_flag("QCSCKP_TEST_MODE")
TEST_AAVID = _env_text("QCSCKP_TEST_AAVID")
TEST_MATERIAL_ID = _env_text("QCSCKP_TEST_MATERIAL_ID")
ALLOW_LIVE_RETARGET = _env_flag("QCSCKP_ALLOW_LIVE_RETARGET")
LOCAL_TEST_SECRETS_FILE = _env_text("QCSCKP_LOCAL_TEST_SECRETS_FILE")

# 千川业务后端。当前分支是官方 API 发行线：全新电脑没有运行设置文件时必须
# 直接进入 official_api，否则 API 配置接口本身会被 legacy 模式挡住，用户将
# 永远无法完成首次配置。需要回归旧浏览器链路时仍可显式设置环境变量。
QIANCHUAN_BACKEND = (
    _env_text("QCSCKP_QIANCHUAN_BACKEND")
    or str(_QIANCHUAN_RUNTIME_SETTINGS.get("backend") or "official_api")
).lower()
if QIANCHUAN_BACKEND not in {"official_api", "browser_legacy"}:
    QIANCHUAN_BACKEND = "browser_legacy"
QIANCHUAN_OFFICIAL_API_BASE_URL = (
    _env_text("QCSCKP_OE_API_BASE_URL") or "https://api.oceanengine.com"
).rstrip("/")
QIANCHUAN_OAUTH_RELAY_BASE_URL = (
    _env_text("QCSCKP_OE_OAUTH_RELAY_URL")
    or "https://oceanengine-callback.zjack6173.workers.dev"
).rstrip("/")
QIANCHUAN_OAUTH_CALLBACK_URL = QIANCHUAN_OAUTH_RELAY_BASE_URL + "/callback"
# 真实 API 写入默认关闭。只有受控验收明确设置后才可创建/暂停/结束/调整调控任务。
if os.getenv("QCSCKP_ALLOW_LIVE_API_WRITES") is not None:
    ALLOW_LIVE_OFFICIAL_API_WRITES = _env_flag("QCSCKP_ALLOW_LIVE_API_WRITES")
else:
    ALLOW_LIVE_OFFICIAL_API_WRITES = bool(
        _QIANCHUAN_RUNTIME_SETTINGS.get("allow_live_api_writes", False)
    )
QIANCHUAN_API_TOKEN_FILE = os.path.join(DATA_DIR, "qianchuan_open_api_token.json")

def _pick_static_dir() -> str:
    """
    前端 static 根目录：
    - 开发：项目根下 bin/static 或 static（与原先一致）
    - macOS 打包：仅此时用 .app/Contents/Resources 为根
    - Windows 打包：exe 旁目录为根
    """
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        base = _macos_frozen_resources_dir()
    else:
        base = PROJECT_ROOT

    cand = os.path.join(base, "bin", "static")
    if os.path.exists(cand):
        return cand
    cand = os.path.join(base, "static")
    if os.path.exists(cand):
        return cand

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, "bin", "static")
        if os.path.exists(cand):
            return cand
        cand = os.path.join(meipass, "static")
        if os.path.exists(cand):
            return cand

    return os.path.join(base, "static")


STATIC_DIR = _pick_static_dir()


# --- SQLite 裁剪 pmc_promotion_material（北京时间）---
# 目标保留约 100 万行；仅当总行数 > 触发线（如 120 万）才删，避免长期压在「刚好要裁」的临界状态。
SQLITE_PRUNE_ENABLED = True
SQLITE_PRUNE_MAX_ROWS = 1_000_000
SQLITE_PRUNE_TRIGGER_ROWS = 1_200_000
# 仅在每日北京时间 [22:00, 次日 06:00) 内尝试裁剪（跨午夜；与入库 created_at 的 +8 一致）
SQLITE_PRUNE_DAILY_START_HOUR = 22
SQLITE_PRUNE_DAILY_END_HOUR = 6
# 满足触发条件时：每轮最多删 5 万行；每轮之间间隔 10 分钟
SQLITE_PRUNE_MAX_ROWS_PER_CYCLE = 50_000
SQLITE_PRUNE_CYCLE_INTERVAL_SEC = 600
# 每批 DELETE 行数及批间休眠（秒）
SQLITE_PRUNE_DELETE_BATCH_SIZE = 5_000
SQLITE_PRUNE_BATCH_SLEEP_SEC = 15
# 非裁剪窗口或低于触发线时主循环休眠（秒）
SQLITE_PRUNE_LOOP_IDLE_SEC = 300
SQLITE_PRUNE_START_DELAY_SEC = 5

# 并发写：等待锁超时（秒）；采集+大屏+追投+裁剪同库时可酌情调大。
SQLITE_BUSY_TIMEOUT_SEC = 30.0
# 是否使用 WAL 日志模式（利于同库多读与并发）。
SQLITE_JOURNAL_MODE_WAL = True

# 工具账号默认在本机校验。只有开发者显式设置 QCSCKP_AUTH_MODE=remote
# 且提供 QCSCKP_API_BASE_URL 时，才允许访问旧的远程账号、版本及备份接口。
AUTH_MODE = (_env_text("QCSCKP_AUTH_MODE") or "local").lower()
if AUTH_MODE not in {"local", "remote"}:
    AUTH_MODE = "local"
API_BASE_URL = _env_text("QCSCKP_API_BASE_URL").rstrip("/")
REMOTE_SERVICES_ENABLED = AUTH_MODE == "remote" and bool(API_BASE_URL)

# 随安装包交付的本地工具账号。密码只保存 PBKDF2-HMAC-SHA256 结果，
# 不在源码、日志和本地数据文件中保存明文。
LOCAL_AUTH_USERNAME = _env_text("QCSCKP_LOCAL_AUTH_USERNAME") or "qcsckp_local"
LOCAL_AUTH_PASSWORD_SALT = "5c8a04f47e6d2ab047f3a9cc05cd9e6c"
LOCAL_AUTH_PASSWORD_HASH = (
    _env_text("QCSCKP_LOCAL_AUTH_PASSWORD_HASH")
    or "b31e77ae460ffdc5a09d6388d596194c9f22c0d8ccfe67f0d1fa40e781c060b4"
)
LOCAL_AUTH_PBKDF2_ITERATIONS = 240_000

# 千川素材云端备份
PMC_CLOUD_BACKUP_PATH = "/api/pmc_promotion_backup.php"
PMC_CLOUD_BACKUP_MAX_ROWS = 2000

# 广告详情基础信息云端备份（与 dev_files/广告详情基础信息同步API说明.md 一致，单次最多 500 行）
PMC_AD_DETAIL_BASIC_PATH = "/api/pmc_ad_detail_basic.php"
PMC_AD_DETAIL_BASIC_MAX_ROWS = 500
