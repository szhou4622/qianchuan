"""
飞书多维表格单表 CRUD 门面（基于 ``FeishuBaseOperator`` / baseopensdk）。

与官方 ``lark_oapi`` 示例的对应关系（能力等价，鉴权方式不同：此处为 ``personal_base_token``）：

- 增 ``insert`` → ``batch_create``
- 删 ``delete`` / ``delete_by_ids`` → ``batch_delete``
- 改 ``update`` → ``batch_update``
- 查 ``select_all`` → ``list`` 分页；``select_one`` / ``select_by_ids`` → ``get``（无 SDK 封装时用多次 get 代替 batch_get）

业务代码优先使用本类的语义化方法；需要表头同步、改名等仍直接用 ``FeishuBaseOperator``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base_table import FeishuBaseOperator


class BitableTable:
    """绑定一张表（app_token + table_id），提供增删改查语义化入口。"""

    def __init__(self, app_token: str, personal_base_token: str, table_id: str):
        self._op = FeishuBaseOperator(app_token, personal_base_token, table_id)
        self.table_id = table_id

    @property
    def inner(self) -> FeishuBaseOperator:
        """需要底层操作（表头、rename、ensure_headers 等）时使用。"""
        return self._op

    # --- Create ---
    def insert(self, rows: List[Dict[str, Any]]) -> bool:
        """新增多行，每行 key 为列显示名。"""
        return self._op.add_rows(rows)

    def insert_pmc_material_rows(
        self,
        rows: List[Dict[str, Any]],
        *,
        local_to_feishu: Optional[Dict[str, str]] = None,
        drop_none: bool = True,
        drop_id: bool = True,
    ) -> bool:
        """
        将 SQLite 表 ``pmc_promotion_material`` 风格的行（英文列名）映射为飞书列名后插入。
        默认映射见 ``pmc_row_mapping.DEFAULT_PMC_MATERIAL_TO_FEISHU``，可用 ``local_to_feishu`` 覆盖。
        """
        from .pmc_row_mapping import (
            map_pmc_material_row_to_feishu,
            pmc_feishu_column_headers,
        )

        # 每次插入前：按列顺序把首列（锁定主键，默认「文本」）对齐为「创建时间」，并创建缺失列
        self._op.ensure_headers(pmc_feishu_column_headers(local_to_feishu))

        return self.insert(
            [
                map_pmc_material_row_to_feishu(
                    r,
                    local_to_feishu=local_to_feishu,
                    drop_none=drop_none,
                    drop_id=drop_id,
                )
                for r in rows
            ]
        )

    # --- Read ---
    def select_all(self) -> List[Dict[str, Any]]:
        """全表扫描（分页 list），返回含 ``record_id``、列名为中文的 dict 列表。"""
        return self._op.get_all_records()

    def select_one(self, record_id: str) -> Optional[Dict[str, Any]]:
        """按 ``record_id`` 查一行；失败返回 None。"""
        return self._op.get_record(record_id)

    def select_by_ids(self, record_ids: List[str]) -> List[Dict[str, Any]]:
        """按多个 ``record_id`` 查询（内部多次 get）。"""
        return self._op.get_records_by_ids(record_ids)

    # --- Update ---
    def update(self, rows: List[Dict[str, Any]]) -> bool:
        """批量更新；每行须含 ``record_id``，其余 key 为列显示名。"""
        return self._op.update_rows(rows)

    # --- Delete ---
    def delete_by_ids(self, record_ids: List[str]) -> bool:
        """批量删除；返回是否全部分片成功。"""
        return self._op.delete_rows(record_ids)
