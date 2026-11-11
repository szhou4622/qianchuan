from __future__ import annotations

import os
import sys
import datetime
import json
import shutil
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse, parse_qs, unquote, urlencode, quote


def _normalize_browser_path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value.strip().strip('"'))))


def _windows_chrome_registry_paths() -> List[str]:
    """App Paths 记录实际安装位置，兼容用户安装和不同位数的注册表视图。"""
    try:
        import winreg
    except ImportError:
        return []
    paths = []
    key_name = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                if isinstance(value, str) and value.strip():
                    paths.append(value)
            except OSError:
                continue
    return paths


def _chrome_candidates() -> List[str]:
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    paths = []
    if sys.platform == "win32":
        paths.extend(_windows_chrome_registry_paths())
        for variable, suffix in (
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
            ("ProgramW6432", "Google/Chrome/Application/chrome.exe"),
            ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
            ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ):
            root = os.environ.get(variable)
            if root:
                paths.append(os.path.join(root, suffix))
    paths.extend([
        os.path.expanduser("~/AppData/Local/Google/Chrome/Application/chrome.exe"),
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ])
    for command in ("chrome.exe", "chrome"):
        path = shutil.which(command)
        if path:
            paths.append(path)
    return paths


def _find_installed_chrome() -> str:
    seen = set()
    for candidate in _chrome_candidates():
        path = _normalize_browser_path(candidate)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if os.path.basename(path).lower() in {"chrome.exe", "google chrome"} and os.path.isfile(path):
            return path
    return ""


def require_executable_path(browser_path: str = None) -> str:
    """
    获取本机 Google Chrome 路径，供 Playwright 使用。
    日常运行不读取用户资料目录，也不会自动改用 Edge 或下载 Chromium。

    Args:
        browser_path: 可选参数，指定浏览器可执行文件路径

    Returns:
        str: 浏览器可执行文件的路径

    Raises:
        FileNotFoundError: 未检测到可用浏览器时抛出异常，包含下载提示
    """

    if browser_path:
        p = _normalize_browser_path(browser_path)
        filename = os.path.basename(p).lower()
        if os.path.isfile(p) and filename in {"chrome.exe", "google chrome"}:
            return p
        # 升级前界面会预填 Edge。保留旧配置文件，但运行时忽略它并重新探测 Chrome。
        # Chrome 升级、迁移或旧版预填路径失效后，重新查找已安装的 Chrome。
        if filename not in {"chrome.exe", "google chrome", "msedge.exe", "microsoft edge"}:
            raise FileNotFoundError(
                "手动配置的路径不是有效的Google Chrome程序，请重新选择chrome.exe。"
            )

    detected = _find_installed_chrome()
    if detected:
        return detected

    if sys.platform == "darwin":
        raise FileNotFoundError(
            "未检测到 Google Chrome。\n"
            "请在 macOS 上安装 Google Chrome（推荐置于「应用程序」），\n"
            "或通过参数手动指定可执行文件路径，例如：\n"
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )

    raise FileNotFoundError(
        "未检测到Google Chrome浏览器可执行文件。\n"
        "如已安装，请在Chrome地址栏打开 chrome://version，复制「可执行文件路径」，\n"
        "填入「服务控制」的「浏览器程序路径」后重新启动采集。\n"
        "如未安装，请先安装Google Chrome。"
    )


def default_browser_executable_hint() -> str:
    """
    服务控制页预填真实检测结果；未找到时留空，避免保存不存在的默认路径。
    """
    if sys.platform not in {"darwin", "win32"}:
        return ""
    return _find_installed_chrome()


def configured_chrome_path_or_empty(value: Any) -> str:
    """忽略旧 Edge 或已失效的 Chrome 路径，让界面重新探测。"""
    path = str(value or "").strip()
    if not path:
        return ""
    path = _normalize_browser_path(path)
    filename = os.path.basename(path).lower()
    if filename in {"msedge.exe", "microsoft edge"}:
        return ""
    if filename in {"chrome.exe", "google chrome"} and not os.path.isfile(path):
        return ""
    return path


def browser_runtime_info(browser_path: str = None) -> Dict[str, Any]:
    """返回自动化实际使用的本机浏览器，不启动浏览器。"""
    try:
        resolved = require_executable_path(browser_path)
    except Exception as exc:
        return {
            "available": False,
            "name": "Google Chrome",
            "path": "",
            "is_chrome": False,
            "message": str(exc),
        }
    filename = os.path.basename(resolved).lower()
    is_chrome = filename in {"chrome.exe", "google chrome"} or "chrome" in filename
    return {
        "available": True,
        "name": "Google Chrome" if is_chrome else filename,
        "path": os.path.normpath(resolved),
        "is_chrome": is_chrome,
        "message": "" if is_chrome else "当前程序不是Google Chrome",
    }


def timestamp_to_datetime(ts):
    """
    时间戳(10位秒/13位毫秒)转日期字符串，格式为"%Y-%m-%d %H:%M:%S"
    输入: int/float/str
    返回: str
    """
    try:
        ts = int(ts)
    except Exception:
        raise ValueError("无效时间戳")
    # 判断十位还是十三位
    if ts > 1e12:  # 毫秒
        ts = ts / 1000
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def datetime_to_timestamp(dt_str, to13=False):
    """
    日期字符串转时间戳
    dt_str: 日期字符串，格式如 "2023-12-25 12:34:56"
    to13: 返回13位毫秒时间戳，默认为False（返回10位秒级）
    返回: int
    """
    if len(dt_str) == 10 and dt_str.count('-') == 2:
        dt_str += " 00:00:00"
    try:
        dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        raise ValueError("日期格式应为 'YYYY-MM-DD HH:MM:SS'")
    ts = int(dt.timestamp())
    if to13:
        return int(ts * 1000)
    else:
        return ts



def build_qianchuan_url(base_url, query_params, hash_params):
    """
    将字典参数重新封装为巨量千川风格的 URL
    :param base_url: 基础路径 (如 https://qianchuan.jinritemai.com/uni-prom/detail)
    :param query_params: 标准查询参数字典 (? 之后的部分)
    :param hash_params: 锚点参数字典 (# 之后的部分)
    """

    def process_internal_json(data):
        """
        处理千川特有的嵌套 JSON 逻辑：
        uniDetail 内部的 bcf, cc, adr 等字段必须先被序列化成字符串，
        然后整个 uniDetail 才能被序列化。
        """
        if not isinstance(data, dict):
            return data
        
        # 深度拷贝，避免修改原字典
        processed = data.copy()

        # 1. 先处理 uniDetail 内部的嵌套对象 (bcf, cc)
        # 千川逻辑：bcf 和 cc 在 uniDetail 内部是以 JSON 字符串形式存在的
        if 'uniDetail' in processed and isinstance(processed['uniDetail'], dict):
            inner = processed['uniDetail'].copy()
            for sub_key in ['bcf', 'cc']:
                if sub_key in inner and isinstance(inner[sub_key], dict):
                    # separators=(',', ':') 压缩空格，确保生成的字符串最紧凑，符合 URL 规范
                    inner[sub_key] = json.dumps(inner[sub_key], separators=(',', ':'), ensure_ascii=False)
            
            # 2. 将处理好的 uniDetail 整体转为 JSON 字符串
            processed['uniDetail'] = json.dumps(inner, separators=(',', ':'), ensure_ascii=False)

        # 3. 处理 hash 里的 adr 等其他可能的字典
        for k, v in processed.items():
            if k != 'uniDetail' and isinstance(v, dict):
                processed[k] = json.dumps(v, separators=(',', ':'), ensure_ascii=False)
        
        return processed

     # 处理标准参数和 Hash 参数
    final_query = process_internal_json(query_params)
    final_hash = process_internal_json(hash_params)

    # 组合成 URL
    # safe='/' 表示不对斜杠编码
    url = f"{base_url}?{urlencode(final_query)}#{urlencode(final_hash)}"

    return url


# ----- 飞书 / 钉钉 Webhook 推送：与大屏 get_table_data 列一致（勿在 services 间互相 import）-----

def _webhook_fmt_yuan(n: Any) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "--"
    return f"¥{v:,.2f}"


def _webhook_fmt_num(n: Any, *, suffix: str = "") -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "--" + suffix
    if v == int(v):
        return str(int(v)) + suffix
    return f"{v:.2f}{suffix}"


def _webhook_fmt_time_cell(raw: Any) -> str:
    if raw is None:
        return "-"
    s = str(raw).strip()
    if not s:
        return "-"
    return s.replace("\n", " ").replace("\r", " ")


def _webhook_fmt_ecpm(n: Any) -> str:
    if n is None:
        return "--"
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "--"
    return f"{v:.2f}"


def _webhook_rows_from_table_data(data: List[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    for mat in data:
        mid = mat.get("id") or mat.get("material_id") or ""
        raw_name = mat.get("title")
        if raw_name is None or str(raw_name).strip() == "":
            raw_name = mat.get("video_name")
        if raw_name is None or str(raw_name).strip() == "":
            video_name = "未命名"
        else:
            video_name = _webhook_fmt_time_cell(raw_name)
        create_time = mat.get("createTime") or mat.get("video_create_time") or ""
        current_cost = mat.get("currentCost") if mat.get("currentCost") is not None else mat.get("stat_cost")
        cost_diff = mat.get("costDiff") if mat.get("costDiff") is not None else mat.get("stat_cost_diff")
        max_raw = mat.get("periodEndTime") if mat.get("periodEndTime") is not None else mat.get("period_end_time")
        min_raw = mat.get("periodStartTime") if mat.get("periodStartTime") is not None else mat.get("period_start_time")
        ecpm_raw = mat.get("estimatedEcpm")
        if ecpm_raw is None:
            ecpm_raw = mat.get("estimated_ecpm")
        row = (
            str(mid),
            video_name,
            str(create_time or ""),
            _webhook_fmt_yuan(current_cost),
            _webhook_fmt_yuan(cost_diff),
            _webhook_fmt_ecpm(ecpm_raw),
            _webhook_fmt_time_cell(max_raw),
            _webhook_fmt_time_cell(min_raw),
            _webhook_fmt_num(mat.get("netRoi")),
            _webhook_fmt_yuan(mat.get("netAmount")),
            _webhook_fmt_num(mat.get("overallPayRoi")),
            _webhook_fmt_yuan(mat.get("overallAmount")),
            _webhook_fmt_num(mat.get("hourRefundRate"), suffix="%"),
            _webhook_fmt_num(mat.get("netSettleRate"), suffix="%"),
            _webhook_fmt_num(mat.get("netOrderCount")),
            _webhook_fmt_num(
                mat.get("overallOrderCount") if mat.get("overallOrderCount") is not None else mat.get("overall_order_count")
            ),
            _webhook_fmt_num(
                mat.get("overallShowCount") if mat.get("overallShowCount") is not None else mat.get("overall_show_count")
            ),
            _webhook_fmt_num(
                mat.get("overallClickCount") if mat.get("overallClickCount") is not None else mat.get("overall_click_count")
            ),
            _webhook_fmt_num(
                mat.get("overallCtr") if mat.get("overallCtr") is not None else mat.get("overall_ctr"),
                suffix="%",
            ),
            _webhook_fmt_num(
                mat.get("overallConversionRate")
                if mat.get("overallConversionRate") is not None
                else mat.get("overall_conversion_rate"),
                suffix="%",
            ),
        )
        rows.append(row)
    return rows


def material_rows_for_webhook_push(data: List[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    """与大屏表格一致的行数据，供飞书/钉钉 Webhook 共用。"""
    return _webhook_rows_from_table_data(data if isinstance(data, list) else [])


WEBHOOK_PUSH_TABLE_HEADERS: List[str] = [
    "素材ID",
    "素材名称",
    "创建时间",
    "整体消耗",
    "时段流速",
    "预估ECPM",
    "最新入库时间",
    "周期内起始数据入库时间",
    "净成交ROI",
    "净成交金额",
    "整体支付ROI",
    "整体成交金额",
    "1h退款率",
    "净成交结算率",
    "净成交订单数",
    "整体成交订单数",
    "整体展现次数",
    "整体点击次数",
    "整体点击率",
    "整体转化率"
]


# 飞书/钉钉推送共用的主文案（不含关键词）
WEBHOOK_PUSH_BASE_TITLE = "千川素材看盘 Top15（1h / 时段流速降序）"


def build_webhook_push_title(keyword: str) -> str:
    kw = (keyword or "").strip()
    if kw:
        return f"[{kw}] {WEBHOOK_PUSH_BASE_TITLE}"
    return WEBHOOK_PUSH_BASE_TITLE
