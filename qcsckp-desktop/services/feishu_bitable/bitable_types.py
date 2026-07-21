"""
飞书多维表格「字段类型」枚举（field.type 数值）。

这是开放平台约定的**类型码**，与业务列名、与本地 SQLite 列名均无关。
新增类型时飞书会分配新数字；未在下面列出的 type 在写入时会**原样透传** JSON，由服务端校验。

参考：多维表格字段类型说明（与项目内 createTableWithData.ts 中 FIELD_TYPE 一致）。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional


class BitableFieldType(IntEnum):
    TEXT = 1
    NUMBER = 2
    SINGLE_SELECT = 3
    MULTI_SELECT = 4
    DATE = 5
    CHECKBOX = 7
    USER = 11
    URL = 15
    PURE_NUMBER = 28


def _try_field_type(ft: Optional[int]) -> Optional[BitableFieldType]:
    if ft is None:
        return None
    try:
        return BitableFieldType(ft)
    except ValueError:
        return None


def coerce_value_for_field_type(field_type_code: Optional[int], value: Any) -> Any:
    """
    根据「当前表里该列的 type」（来自 ListAppTableField，不是写死的列名）把 Python 值转成接口易接受的形态。

    - 若 ``field_type_code`` 为 None（接口未返回类型）：不转换。
    - 若为飞书将来新增的 type（不在 BitableFieldType 中）：不转换，原样提交。
    """
    if value is None:
        return None

    ft = _try_field_type(field_type_code)
    if ft is None:
        # 接口未给 type，或飞书新增了尚未收录的 type 码：不转换
        return value

    if isinstance(value, dict):
        return value
    if isinstance(value, list) and ft is not BitableFieldType.MULTI_SELECT:
        return value

    if ft is BitableFieldType.TEXT:
        return str(value)

    if ft in (BitableFieldType.NUMBER, BitableFieldType.PURE_NUMBER):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return None
            try:
                return float(s) if ("." in s or "e" in s.lower() or "E" in s) else int(s)
            except ValueError:
                return value
        return value

    if ft is BitableFieldType.CHECKBOX:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y", "是")
        return bool(value)

    if ft is BitableFieldType.DATE:
        if isinstance(value, (int, float)):
            return int(value)
        return value

    if ft is BitableFieldType.SINGLE_SELECT:
        return str(value)

    if ft is BitableFieldType.MULTI_SELECT:
        if isinstance(value, list):
            return value
        return [str(value)]

    if ft is BitableFieldType.URL:
        if isinstance(value, dict):
            return value
        return str(value)

    return value
