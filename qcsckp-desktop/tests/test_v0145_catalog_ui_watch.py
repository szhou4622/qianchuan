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
