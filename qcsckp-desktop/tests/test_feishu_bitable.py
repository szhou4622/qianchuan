import unittest
from unittest.mock import MagicMock

from services.feishu_bitable.base_table import FeishuBaseOperator


def _response(*, success=True, message=""):
    result = MagicMock()
    result.success.return_value = success
    result.msg = message
    result.data.records = []
    return result


def _operator():
    operator = object.__new__(FeishuBaseOperator)
    operator.table_id = "tbl-test"
    operator.client = MagicMock()
    operator._load_field_map_and_primary = MagicMock(
        return_value=({"名称": "fld-name"}, {}, "名称", {"名称": 1})
    )
    operator._coerce_cell_value = MagicMock(side_effect=lambda _name, value, _types: value)
    operator._fill_primary_field = MagicMock()
    return operator


class FeishuBitableBatchTests(unittest.TestCase):
    def test_add_rows_splits_more_than_five_hundred_records(self):
        operator = _operator()
        operator.client.base.v1.app_table_record.batch_create.return_value = _response()
        self.assertTrue(
            operator.add_rows([{"name": index, "名称": str(index)} for index in range(1001)])
        )
        self.assertEqual(
            3,
            operator.client.base.v1.app_table_record.batch_create.call_count,
        )

    def test_add_rows_raises_when_any_chunk_fails(self):
        operator = _operator()
        operator.client.base.v1.app_table_record.batch_create.side_effect = [
            _response(),
            _response(success=False, message="rate limited"),
        ]
        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            operator.add_rows([{"名称": str(index)} for index in range(501)])

    def test_update_rows_is_chunked_and_does_not_mutate_input(self):
        operator = _operator()
        operator.client.base.v1.app_table_record.batch_update.return_value = _response()
        rows = [
            {"record_id": f"rec-{index}", "名称": str(index)}
            for index in range(501)
        ]
        self.assertTrue(operator.update_rows(rows))
        self.assertEqual(
            2,
            operator.client.base.v1.app_table_record.batch_update.call_count,
        )
        self.assertEqual("rec-0", rows[0]["record_id"])


if __name__ == "__main__":
    unittest.main()
