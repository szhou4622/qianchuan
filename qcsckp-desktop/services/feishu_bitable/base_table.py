import os
import uuid
from typing import List, Dict, Any, Optional, Tuple, Set

from baseopensdk import BaseClient, JSON
from baseopensdk.api.base.v1 import *
from dotenv import load_dotenv, find_dotenv

# --- 配置 ---
# 强烈建议将这些信息保存在 .env 文件中
# .env 文件内容示例:
# APP_TOKEN="bascnxxxxxxxxxxxxxxx"
# PERSONAL_BASE_TOKEN="pt-xxxxxxxxxxxxxxxxxxxxxxxx"
# TABLE_ID="tblxxxxxxxxxxxxxxx"

load_dotenv(find_dotenv())

from utils.log import logger

from .bitable_types import coerce_value_for_field_type


def _feishu_field_update_ok(res: Any) -> bool:
    """更新字段接口：无实际变更时飞书可能返回 msg=DataNotChange 且 code!=0，仍视为成功。"""
    if res.success():
        return True
    msg = getattr(res, "msg", None) or ""
    return "DataNotChange" in msg


class _BatchCreateRecordBody:
    """仅含 fields，供 SDK JSON 序列化。勿用 AppTableRecord：vars() 会带上 record_id/created_by 等 null，飞书会当作列名校验并返回 FieldNameNotFound。"""

    def __init__(self, fields: Dict[str, Any]):
        self.fields = fields


class _BatchUpdateRecordBody:
    def __init__(self, record_id: str, fields: Dict[str, Any]):
        self.record_id = record_id
        self.fields = fields


class FeishuBaseOperator:
    """
    飞书多维表格（个人 Base Open / baseopensdk）单表操作。

    记录级 API 对应关系（与开放平台一致，鉴权为 personal_base_token）：
    - 增: add_rows → batch_create
    - 删: delete_rows → batch_delete（≤500/批，内部分片）
    - 改: update_rows → batch_update
    - 查: get_all_records / get_record / get_records_by_ids → list / get（无 batch_get 时用多次 get）
        列数据 key 均为列「显示名」；写入值按接口返回的列 ``type`` 做转换（见 ``bitable_types``）。
        本地 SQLite 行（英文列名）→ 飞书列名请用 ``pmc_row_mapping.map_pmc_material_row_to_feishu``。
    """

    def __init__(self, app_token: str, personal_base_token: str, table_id: str):
        """
        初始化操作类
        :param app_token: 多维表格的 App Token
        :param personal_base_token: 个人基础凭证
        :param table_id: 目标数据表的 Table ID
        """
        if not all([app_token, personal_base_token, table_id]):
            raise ValueError("app_token、personal_base_token 与 table_id 均必填。")

        self.app_token = app_token
        self.personal_base_token = personal_base_token
        self.table_id = table_id
        self.client: BaseClient = self._initialize_client()

    def _initialize_client(self) -> BaseClient:
        """构建并返回一个认证过的 BaseClient 实例"""
        return BaseClient.builder() \
            .app_token(self.app_token) \
            .personal_base_token(self.personal_base_token) \
            .build()

    def _get_field_map(self, inverse: bool = False) -> Dict[str, str]:
        """
        获取字段（表头）的名称和ID的映射字典。
        :param inverse: 是否反转映射关系（ID -> Name）。默认为 False (Name -> ID)。
        :return: 映射字典
        """
        field_map, _, _, _ = self._load_field_map_and_primary()
        if not field_map:
            return {}
        if inverse:
            return {v: k for k, v in field_map.items()}
        return dict(field_map)

    def _field_id_to_name_map(self) -> Dict[str, str]:
        """field_id -> 列显示名（用于解析 list/get 返回的 fields）。"""
        return self._get_field_map(inverse=True)

    def _decode_record_row(
        self,
        record_id: str,
        fields: Optional[Dict[str, Any]],
        id_to_name: Dict[str, str],
    ) -> Dict[str, Any]:
        row: Dict[str, Any] = {"record_id": record_id}
        for field_id, value in (fields or {}).items():
            row[id_to_name.get(field_id, field_id)] = value
        return row

    def _load_field_map_and_primary(self) -> Tuple[Dict[str, str], Optional[str], Optional[str], Dict[str, int]]:
        """一次请求拿到 列名->field_id、主键列 field_id、主键列显示名、列名->字段类型 type。

        说明：base-api（个人 Base Open SDK）批量写入记录时，请求体 fields 的 key 为**字段显示名**，
        与 feishu-sdk-demo/createTableWithData.ts 一致；用 fldxxx 作 key 会报 FieldNameNotFound。
        """
        try:
            req = ListAppTableFieldRequest.builder().table_id(self.table_id).build()
            res = self.client.base.v1.app_table_field.list(req)
            if not res.success():
                raise Exception(f"获取字段失败: {res.msg}")
            items = res.data.items or []
            field_map = {f.field_name: f.field_id for f in items}
            field_types: Dict[str, int] = {}
            for f in items:
                if f.field_name is not None and f.type is not None:
                    field_types[f.field_name] = int(f.type)
            primary_id = None
            primary_name = None
            for f in items:
                if f.is_primary:
                    primary_id = f.field_id
                    primary_name = f.field_name
                    break
            return field_map, primary_id, primary_name, field_types
        except Exception as e:
            logger.error(f"获取字段映射失败: {e}")
            return {}, None, None, {}

    @staticmethod
    def _coerce_cell_value(field_name: str, value: Any, field_types: Dict[str, int]) -> Any:
        """按当前表中该列的 type（接口下发，非写死列名）转换单元格值。"""
        return coerce_value_for_field_type(field_types.get(field_name), value)

    @staticmethod
    def _fill_primary_field(
        fields: Dict[str, Any],
        field_map: Dict[str, str],
        primary_field_name: Optional[str],
        row: Dict[str, Any],
        field_types: Dict[str, int],
    ) -> None:
        """若本行未写主键列，则用常见列或第一个有值的列兜底，避免创建记录失败。"""
        if not primary_field_name or primary_field_name in fields:
            return
        op = FeishuBaseOperator
        for key in ("用户ID", "姓名", "名称", "标题", "名字"):
            if key in row and field_map.get(key) and row[key] is not None and str(row[key]).strip() != "":
                fields[primary_field_name] = op._coerce_cell_value(primary_field_name, row[key], field_types)
                return
        for _fn, v in row.items():
            if field_map.get(_fn) and v is not None and str(v).strip() != "":
                fields[primary_field_name] = op._coerce_cell_value(primary_field_name, v, field_types)
                return
        fields[primary_field_name] = op._coerce_cell_value(primary_field_name, "-", field_types)

    def _list_all_fields_ordered(self) -> List[Tuple[str, str]]:
        """按表格列顺序返回 [(field_id, field_name), ...]（含分页）。"""
        ordered: List[Tuple[str, str]] = []
        page_token: Optional[str] = None
        has_more = True
        while has_more:
            try:
                b = ListAppTableFieldRequest.builder() \
                    .table_id(self.table_id) \
                    .page_size(100)
                if page_token:
                    b = b.page_token(page_token)
                req = b.build()
                res = self.client.base.v1.app_table_field.list(req)
                if not res.success():
                    logger.error(f"列出字段失败: {res.msg}")
                    break
                for f in res.data.items or []:
                    if f.field_id and f.field_name:
                        ordered.append((f.field_id, f.field_name))
                has_more = bool(res.data.has_more)
                page_token = res.data.page_token
            except Exception as e:
                logger.error(f"列出字段出错: {e}")
                break
        return ordered

    def _list_all_field_items(self) -> List[Any]:
        """分页拉取全部字段定义（含 type、property、ui_type），供更新字段时原样带回。"""
        out: List[Any] = []
        page_token: Optional[str] = None
        has_more = True
        while has_more:
            try:
                b = ListAppTableFieldRequest.builder() \
                    .table_id(self.table_id) \
                    .page_size(100)
                if page_token:
                    b = b.page_token(page_token)
                req = b.build()
                res = self.client.base.v1.app_table_field.list(req)
                if not res.success():
                    logger.error(f"列出字段失败: {res.msg}")
                    break
                for f in res.data.items or []:
                    out.append(f)
                has_more = bool(res.data.has_more)
                page_token = res.data.page_token
            except Exception as e:
                logger.error(f"列出字段出错: {e}")
                break
        return out

    def _build_app_table_field_for_update(self, new_name: str, meta: Optional[Any]) -> Any:
        """
        构造更新字段请求体。飞书更新字段多为「全量」语义：只传 field_name 会丢掉 type/property，
        服务端校验不通过时会返回 field validation failed，故改名时须带上 list 接口下发的 type 与 property。
        """
        name = (new_name or "").strip()
        b = AppTableField.builder().field_name(name)
        if meta is not None:
            t = getattr(meta, "type", None)
            if t is not None:
                b = b.type(int(t))
            prop = getattr(meta, "property", None)
            if prop is not None:
                b = b.property(prop)
            ui = getattr(meta, "ui_type", None)
            if ui:
                b = b.ui_type(ui)
        return b.build()

    def _update_field_name_by_id(
        self,
        field_id: str,
        new_name: str,
        quiet: bool = False,
        *,
        meta_by_id: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        按 field_id 改列显示名；quiet=True 成功时不写日志。

        与官网 ``app_table_field.update`` 一致；个人 Base 使用 ``self.client.base.v1.app_table_field.update``。
        须携带当前字段的 type（及 property 等），否则易出现 field validation failed。
        """
        try:
            meta: Optional[Any] = None
            if meta_by_id is not None:
                meta = meta_by_id.get(field_id)
            if meta is None:
                for f in self._list_all_field_items():
                    if getattr(f, "field_id", None) == field_id:
                        meta = f
                        break
            update_body = self._build_app_table_field_for_update(new_name, meta)
            req = UpdateAppTableFieldRequest.builder() \
                .table_id(self.table_id) \
                .field_id(field_id) \
                .request_body(update_body) \
                .build()
            res = self.client.base.v1.app_table_field.update(req)
            if _feishu_field_update_ok(res):
                if not quiet:
                    if res.success():
                        logger.info(f"  ~ 已重命名为「{new_name}」")
                    else:
                        logger.info(f"  ~ 字段更新成功（无变更，DataNotChange）")
                return True
            logger.error(f"  - 将字段重命名为「{new_name}」失败: {res.msg}")
            return False
        except Exception as e:
            logger.error(f"  - 重命名字段出错: {e}")
            return False

    @staticmethod
    def _feishu_temp_field_name(i: int, suffix: str) -> str:
        """
        两阶段改名时的临时列名。勿用首尾下划线或连续 ``__``，飞书会报 field validation failed。
        """
        return f"tmp{suffix}{i:02d}"

    def _apply_ordered_renames(
        self,
        old_pairs: List[Tuple[str, str]],
        target_names: List[str],
        m: int,
    ) -> bool:
        """
        将当前表前 m 列按顺序改名为 target_names[0]..target_names[m-1]（两阶段临时名防重名）。
        old_pairs: [(field_id, current_name), ...] 与飞书列顺序一致。
        """
        if m <= 0:
            return True
        if all(old_pairs[i][1] == target_names[i] for i in range(m)):
            logger.info(f"  前 {m} 列名称已与目标一致，跳过重命名。")
            return True
        old_ids = [p[0] for p in old_pairs]
        suffix = uuid.uuid4().hex[:10]
        meta_by_id = {f.field_id: f for f in self._list_all_field_items() if getattr(f, "field_id", None)}

        # 仅改第 1 列且目标名未被其它列占用时，优先直接改名（跳过易被飞书拒绝的临时名）
        if m == 1:
            other_titles = {old_pairs[j][1] for j in range(1, len(old_pairs))}
            if target_names[0] not in other_titles:
                if self._update_field_name_by_id(
                    old_ids[0], target_names[0], quiet=True, meta_by_id=meta_by_id
                ):
                    logger.info(f"  ~ 第 1 列: -> 「{target_names[0]}」（直接重命名）")
                    return True
                logger.info("  直接重命名失败，尝试两阶段重命名...")

        logger.info(f"  阶段 A：将 {m} 列临时重命名以避免名称冲突...")
        for i in range(m):
            tmp = self._feishu_temp_field_name(i, suffix)
            if not self._update_field_name_by_id(old_ids[i], tmp, quiet=True, meta_by_id=meta_by_id):
                logger.error("  中止：临时重命名失败。")
                return False

        logger.info("  阶段 B：重命名为目标表头...")
        for i in range(m):
            if not self._update_field_name_by_id(
                old_ids[i], target_names[i], quiet=True, meta_by_id=meta_by_id
            ):
                logger.error(f"  中止：无法将第 {i + 1} 列设为「{target_names[i]}」。")
                return False
            logger.info(f"  ~ 第 {i + 1} 列: -> 「{target_names[i]}」")
        return True

    def _rebuild_headers_replace_mode(self, new_field_names: List[str]) -> None:
        """
        keep_original_fields=False：按列下标对齐，尽量「改名」保留数据，而非整表删列。
        原 [key1,key2]、目标 [key2,key3,key4] → 第1列 key1→key2，第2列 key2→key3，再新建 key4；
        若旧列多于目标，多出来的列会尝试删除（主键列删除失败时仅记录日志）。
        """
        if not new_field_names:
            logger.warning("new_field_names 为空，中止替换模式。")
            return

        ordered = self._list_all_fields_ordered()
        old_pairs = ordered
        old_ids = [p[0] for p in old_pairs]
        m = min(len(old_ids), len(new_field_names))

        if not self._apply_ordered_renames(old_pairs, new_field_names, m):
            return

        if len(old_ids) > len(new_field_names):
            logger.info(f"  正在删除 {len(old_ids) - len(new_field_names)} 个多余列...")
            for idx in range(len(new_field_names), len(old_ids)):
                fid, fname = old_pairs[idx]
                try:
                    req = DeleteAppTableFieldRequest.builder() \
                        .table_id(self.table_id) \
                        .field_id(fid) \
                        .build()
                    res = self.client.base.v1.app_table_field.delete(req)
                    if res.success():
                        logger.info(f"  - 已删除多余字段: 「{fname}」")
                    else:
                        logger.error(f"  - 删除「{fname}」失败: {res.msg}")
                except Exception as e:
                    logger.error(f"  - 删除「{fname}」出错: {e}")

    def rebuild_headers(self, new_field_names: List[str], keep_original_fields: bool = False):
        """
        重建或更新表头（字段）。
        :param new_field_names: 新的表头名称列表（有序：与表格中列顺序一一对应用于「替换」模式）。
        :param keep_original_fields: 是否保留原有字段。
                                     False: 按顺序将前几列改名为 new_field_names 对应项，删掉多出来的旧列，再创建少的列。
                                     True: 先按顺序把前 min(旧列数, 目标数) 列改成目标名（例如主键「文本」→「用户ID」），
                                           **不删** 多出来的旧列；再仅为仍不存在的名称创建新列。
        若只需「对齐前几列 + 补缺列、不删列」，可改用 ensure_headers()。
        """
        logger.info("--- 正在重建表头 ---")
        if not keep_original_fields:
            logger.info("模式：按顺序重命名并创建末尾列（已重命名列上的数据保留）。")
            self._rebuild_headers_replace_mode(new_field_names)
        else:
            logger.info("模式：保留多余列 — 按列顺序对齐前缀，再仅为确实缺失的名称创建新列。")
            ordered = self._list_all_fields_ordered()
            if new_field_names and ordered:
                m = min(len(ordered), len(new_field_names))
                if not self._apply_ordered_renames(ordered, new_field_names, m):
                    logger.info("--- 表头重建结束（重命名失败后中止） ---\n")
                    return
            elif new_field_names and not ordered:
                logger.info("  无现有列；即将创建全部目标字段。")
        current_fields_map = self._get_field_map()

        fields_to_create = [name for name in new_field_names if name not in current_fields_map]
        if not fields_to_create:
            logger.info("没有需要新建的新字段。")
            return

        logger.info(f"正在创建新字段: {fields_to_create}")
        for field_name in fields_to_create:
            try:
                # 默认创建为单行文本类型 (type=1)
                # 如需其他类型，请参考飞书文档修改
                field_body = AppTableField.builder().field_name(field_name).type(1).build()
                req = CreateAppTableFieldRequest.builder().table_id(self.table_id).request_body(field_body).build()
                res = self.client.base.v1.app_table_field.create(req)
                if res.success():
                    logger.info(f"  + 已创建字段: 「{field_name}」")
                else:
                    logger.error(f"  - 创建字段「{field_name}」失败: {res.msg}")
            except Exception as e:
                logger.error(f"  - 创建字段「{field_name}」出错: {e}")
        logger.info("--- 表头重建结束 ---\n")

    def ensure_headers(self, field_names: List[str]):
        """
        在保留所有旧列的前提下同步表头：
        1) 按列顺序把前若干列改名为 field_names[0..]（解决「文本」与「用户ID」并排重复：会把第1列改成用户ID而不是新建一列）；
        2) 再为仍未出现的名称创建新列。
        不会删除任何已有列。
        """
        self.rebuild_headers(field_names, keep_original_fields=True)

    def add_rows(self, data: List[Dict[str, Any]]):
        """
        批量添加新行（记录）。
        :param data: key 须为飞书表「列显示名」。若数据来源为 SQLite ``pmc_promotion_material``（英文列名），
                     请先使用 ``pmc_row_mapping.map_pmc_material_row_to_feishu`` 再传入。
        单元格值会按接口返回的列 ``type`` 自动转换（见 ``bitable_types``）。
        """
        if not data:
            logger.warning("未提供要新增的数据。")
            return

        logger.info(f"--- 正在新增 {len(data)} 行 ---")
        field_map, _, primary_name, field_types = self._load_field_map_and_primary()
        if not field_map:
            logger.error("无法获取字段映射，中止新增行。")
            return

        records_to_create = []
        for row in data:
            fields = {}
            for field_name, value in row.items():
                if field_name in field_map:
                    fields[field_name] = self._coerce_cell_value(field_name, value, field_types)
                else:
                    logger.warning(f"警告：表中不存在字段「{field_name}」，该行跳过该列。")
            self._fill_primary_field(fields, field_map, primary_name, row, field_types)
            records_to_create.append(_BatchCreateRecordBody(fields))

        try:
            body = BatchCreateAppTableRecordRequestBody.builder().records(records_to_create).build()
            req = BatchCreateAppTableRecordRequest.builder() \
                .table_id(self.table_id) \
                .request_body(body) \
                .build()
            res = self.client.base.v1.app_table_record.batch_create(req)
            if res.success():
                logger.info(f"已成功新增 {len(res.data.records)} 行。")
            else:
                raise Exception(res.msg)
        except Exception as e:
            logger.error(f"新增行出错: {e}")
        logger.info("--- 新增行结束 ---\n")

    def update_rows(self, data: List[Dict[str, Any]]):
        """
        批量更新行（记录）。
        :param data: 每行需含 'record_id'；其余 key 为**列显示名**（与 add_rows 一致）。
                     示例: [{'record_id': 'recxxxx', '年龄': 26}, {'record_id': 'recyyyyy', '备注': '已更新'}]
        """
        if not data:
            logger.warning("未提供要更新的数据。")
            return

        logger.info(f"--- 正在更新 {len(data)} 行 ---")
        field_map, _, _, field_types = self._load_field_map_and_primary()
        if not field_map:
            logger.error("无法获取字段映射，中止更新行。")
            return
        
        records_to_update = []
        for row in data:
            record_id = row.pop('record_id', None)
            if not record_id:
                logger.warning(f"警告：某行数据缺少「record_id」，已跳过: {row}")
                continue
            
            fields = {}
            for field_name, value in row.items():
                if field_name in field_map:
                    fields[field_name] = self._coerce_cell_value(field_name, value, field_types)
                else:
                    logger.warning(f"警告：未找到字段「{field_name}」，记录「{record_id}」跳过该字段。")
            
            records_to_update.append(_BatchUpdateRecordBody(record_id, fields))

        try:
            body = BatchUpdateAppTableRecordRequestBody.builder().records(records_to_update).build()
            req = BatchUpdateAppTableRecordRequest.builder() \
                .table_id(self.table_id) \
                .request_body(body) \
                .build()
            res = self.client.base.v1.app_table_record.batch_update(req)
            if res.success():
                logger.info(f"已成功处理 {len(res.data.records)} 条记录的更新。")
            else:
                raise Exception(res.msg)
        except Exception as e:
            logger.error(f"更新行出错: {e}")
        logger.info("--- 更新行结束 ---\n")
        
    def get_all_records(self, fields_to_id_map: Dict[str, str] = None) -> List[Dict[str, Any]]:
        """
        获取表内所有记录，自动处理分页。
        返回的数据会包含 'record_id'。
        :param fields_to_id_map: (可选) 传入字段映射，避免重复请求。
        :return: 包含所有记录的字典列表。
        """
        logger.info("--- 正在获取全部记录（分页） ---")
        if fields_to_id_map is None:
            id_to_name = self._field_id_to_name_map()
        else:
            id_to_name = {v: k for k, v in fields_to_id_map.items()}

        all_records = []
        page_token = None
        has_more = True

        while has_more:
            try:
                b = ListAppTableRecordRequest.builder() \
                    .table_id(self.table_id) \
                    .page_size(500)
                if page_token:
                    b = b.page_token(page_token)
                req = b.build()
                res = self.client.base.v1.app_table_record.list(req)
                if not res.success():
                    raise Exception(res.msg)

                for item in res.data.items:
                    all_records.append(self._decode_record_row(item.record_id, item.fields, id_to_name))
                
                has_more = res.data.has_more
                page_token = res.data.page_token
                logger.info(f"  本页拉取 {len(res.data.items)} 条… 累计: {len(all_records)}")

            except Exception as e:
                logger.error(f"拉取记录出错: {e}")
                break
        
        logger.info(f"--- 拉取结束。共 {len(all_records)} 条记录 ---\n")
        return all_records

    def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """
        按 record_id 查询单条记录（对应开放平台「检索记录」get）。
        返回字典含 record_id 与各列显示名；失败返回 None。
        """
        return self._get_record_raw(record_id, self._field_id_to_name_map())

    def get_records_by_ids(self, record_ids: List[str]) -> List[Dict[str, Any]]:
        """
        按 record_id 列表查询多条记录。

        说明：官方 lark_oapi 的 batch_get 在 baseopensdk 中无对应封装时，此处用多次 get 实现；
        单次最多建议控制数量以免触发频控。
        """
        if not record_ids:
            return []
        id_to_name = self._field_id_to_name_map()
        seen: Set[str] = set()
        out: List[Dict[str, Any]] = []
        for rid in record_ids:
            if not rid or rid in seen:
                continue
            seen.add(rid)
            row = self._get_record_raw(rid, id_to_name)
            if row is not None:
                out.append(row)
        return out

    def _get_record_raw(self, record_id: str, id_to_name: Dict[str, str]) -> Optional[Dict[str, Any]]:
        try:
            req = GetAppTableRecordRequest.builder() \
                .table_id(self.table_id) \
                .record_id(record_id) \
                .build()
            res = self.client.base.v1.app_table_record.get(req)
            if not res.success() or not res.data or not res.data.record:
                logger.warning(f"获取单条记录失败 ({record_id}): {getattr(res, 'msg', '')}")
                return None
            r = res.data.record
            return self._decode_record_row(r.record_id, r.fields, id_to_name)
        except Exception as e:
            logger.error(f"获取单条记录出错 ({record_id}): {e}")
            return None

    def delete_rows(self, record_ids: List[str]) -> bool:
        """
        批量删除记录（对应 batch_delete）。单次请求最多 500 条，内部自动分片。
        :param record_ids: rec 开头的记录 ID 列表
        :return: 全部分片均成功则为 True
        """
        if not record_ids:
            return True
        batch = 500
        ok = True
        logger.info(f"--- 正在删除 {len(record_ids)} 条记录 ---")
        for i in range(0, len(record_ids), batch):
            chunk = record_ids[i : i + batch]
            try:
                body = BatchDeleteAppTableRecordRequestBody.builder().records(chunk).build()
                req = BatchDeleteAppTableRecordRequest.builder() \
                    .table_id(self.table_id) \
                    .request_body(body) \
                    .build()
                res = self.client.base.v1.app_table_record.batch_delete(req)
                if res.success():
                    logger.info(f"  已删除 {len(chunk)} 条记录（第 {i // batch + 1} 批）。")
                else:
                    logger.error(f"  批量删除失败: {res.msg}")
                    ok = False
            except Exception as e:
                logger.error(f"  批量删除出错: {e}")
                ok = False
        logger.info("--- 删除结束 ---\n")
        return ok

    def rename_field(self, old_name: str, new_name: str):
        """
        重命名字段（表头显示名）。
        带锁的「主键列」不能删除，但一般可以改名，例如：
        base_op.rename_field("文本", "用户ID")
        改名后，add_rows 里请用新列名作为键；自动补主键按主键列显示名写入。
        """
        logger.info(f"--- 正在将字段「{old_name}」重命名为「{new_name}」 ---")
        field_map = self._get_field_map()
        field_id = field_map.get(old_name)

        if not field_id:
            logger.warning(f"未找到字段「{old_name}」。")
            return

        if self._update_field_name_by_id(field_id, new_name):
            logger.info("已成功重命名字段。")
        logger.info("--- 重命名操作结束 ---\n")