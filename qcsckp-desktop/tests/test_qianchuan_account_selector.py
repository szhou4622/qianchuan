from pathlib import Path
import unittest


class QianchuanAccountSelectorTests(unittest.TestCase):
    def test_official_account_selector_is_searchable_and_not_prompt_limited(self):
        html = (
            Path(__file__).resolve().parents[1] / "static" / "qianchuan_accounts.html"
        ).read_text(encoding="utf-8")
        self.assertIn("function chooseOfficialAccount(candidates)", html)
        self.assertIn("account-picker-list", html)
        self.assertIn("输入账户名称或完整账户ID", html)
        self.assertNotIn("window.prompt(`请选择要添加的千川账户", html)


if __name__ == "__main__":
    unittest.main()
