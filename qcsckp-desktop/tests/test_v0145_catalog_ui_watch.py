# -*- coding: utf-8 -*-
"""Account catalog UI must recover after popup/focus timer suspension."""

from __future__ import annotations

import os
import unittest


class CatalogUiWatchV0145Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "static",
            "qianchuan_accounts.html",
        )
        with open(cls.page_path, "r", encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_refresh_uses_non_overlapping_recursive_web_status_watch(self):
        self.assertIn("async function watchCatalogSync", self.html)
        self.assertIn("catalogWatchTimer=setTimeout(tick,1200)", self.html)
        self.assertIn("await a.getQianchuanCatalogSyncStatus()", self.html)
        self.assertNotIn("const timer=setInterval(async()=>{\n      let s;\n      try{s=await a.getQianchuanCatalogSyncStatus()", self.html)

    def test_focus_and_visibility_resume_catalog_watch(self):
        self.assertIn("window.addEventListener('focus'", self.html)
        self.assertIn("document.addEventListener('visibilitychange'", self.html)
        self.assertIn("load().then(resumeCatalogWatch)", self.html)

    def test_single_account_refresh_lives_inside_account_card(self):
        self.assertIn('data-refresh-account="${esc(acc.account_uid)}"', self.html)
        self.assertIn("刷新此账户计划", self.html)
        self.assertIn("刷新全部账户计划", self.html)
        self.assertIn("刷新页面显示", self.html)
        self.assertIn("本次没有访问千川后台", self.html)

    def test_plan_groups_put_chengfang_and_live_first(self):
        expected = (
            "const groupDefs=[['chengfang','live','乘方 · 推直播'],"
            "['chengfang','product','乘方 · 推商品'],"
            "['global','live','全域 · 推直播'],"
            "['global','product','全域 · 推商品']"
        )
        self.assertIn(expected, self.html)

    def test_catalog_watch_does_not_stream_full_overview_every_tick(self):
        self.assertIn("loadInFlight", self.html)
        self.assertNotIn(
            "s=await a.getQianchuanCatalogSyncStatus();\n        overview=await a.getQianchuanAccountOverview();",
            self.html,
        )
        self.assertIn(
            "if(!dirty&&!catalogWatchBusy&&!document.hidden)load()},60000)",
            self.html,
        )

    def test_clean_install_opens_login_and_prefills_local_username(self):
        index_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "static",
            "index.html",
        )
        with open(index_path, "r", encoding="utf-8") as handle:
            index_html = handle.read()
        self.assertIn("window.qcLocalAuthUsername", index_html)
        self.assertIn("await api.getEnvironmentInfo()", index_html)
        self.assertIn("openLoginModal();", index_html)


if __name__ == "__main__":
    unittest.main()
