"""UTC、北京时间和平台时间的统一处理。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:
    # Windows 的系统 Python 可能不附带 IANA tzdata。中国标准时间无夏令时，
    # 固定 UTC+8 与 V1A 的业务日期语义一致，并避免干净安装首次启动失败。
    BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def beijing_iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(BEIJING).isoformat(timespec="seconds")


def business_date(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(BEIJING).date().isoformat()
