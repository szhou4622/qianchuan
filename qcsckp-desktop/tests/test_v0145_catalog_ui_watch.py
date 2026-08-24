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

    def test_pending_official_authorization_can_be_restarted_immediately(self):
        self.assertIn(
            "r?.authorization_pending?'重新发起授权':'保存并授权'",
            self.html,
        )
        self.assertIn("$('authorizeApi').disabled=!!apiAuthStarting", self.html)
        self.assertNotIn("$('authorizeApi').disabled=!!apiAuthTimer", self.html)
        self.assertIn("stopApiAuthorizationWatch();\n    apiAuthStarting=true", self.html)
        self.assertIn("generation!==apiAuthWatchGeneration", self.html)

    def test_official_api_authorization_needs_no_callback_input_or_handoff(self):
        self.assertNotIn('id="oauthCallbackUrl"', self.html)
        self.assertNotIn('id="copyOauthCallback"', self.html)
        self.assertNotIn('id="oauthLocalReceiverUrl"', self.html)
        self.assertIn("不需要填写、复制或转交回调地址", self.html)
        self.assertNotIn("oauth_local_receiver_url", self.html)

    def test_single_account_refresh_lives_inside_account_card(self):
        self.assertIn('data-refresh-account="${esc(acc.account_uid)}"', self.html)
        self.assertIn("刷新此账户计划", self.html)
        self.assertIn("刷新全部账户计划", self.html)
        self.assertIn("刷新页面显示", self.html)
        self.assertIn("本次没有访问千川后台", self.html)

    def test_account_add_and_refresh_show_stage_progress(self):
        self.assertIn('id="catalogProgress"', self.html)
        self.assertIn('id="catalogProgressBar"', self.html)
        self.assertIn("progress_percent", self.html)
        self.assertIn("phase_label", self.html)
        self.assertIn("已发现 ${Number(progress.discovered_plans)} 条计划", self.html)
        self.assertIn("已用时 ${Number(progress.elapsed_seconds)||0} 秒", self.html)
        self.assertIn("正在添加账户", self.html)

    def test_plan_list_has_no_advanced_settings_entry(self):
        self.assertNotIn("data-advanced", self.html)
        self.assertNotIn(">高级设置</button>", self.html)

    def test_each_account_plan_list_has_local_search(self):
        self.assertIn('data-account-plan-search', self.html)
        self.assertIn('在此账户内搜索计划名称或ID', self.html)
        self.assertIn('data-plan-search-text', self.html)
        self.assertIn("group.hidden=!Array.from(group.querySelectorAll('.plan'))", self.html)

    def test_plan_groups_put_chengfang_and_live_first(self):
        expected = (
            "const groupDefs=[['chengfang','live','乘方 · 推直播'],"
            "['chengfang','product','乘方 · 推商品'],"
            "['global','live','全域 · 推直播'],"
            "['global','product','全域 · 推商品']"
        )
        self.assertIn(expected, self.html)

    def test_plan_selection_has_immediate_unsaved_monitoring_feedback(self):
        self.assertIn('data-persisted-enabled="${p.enabled?', self.html)
        self.assertIn("待保存并开始监控", self.html)
        self.assertIn("待保存停止监控", self.html)
        self.assertIn("保存并应用监控", self.html)
        self.assertIn("save.classList.add('pending')", self.html)

    def test_account_save_is_single_flight_and_disables_repeat_clicks(self):
        self.assertIn("const savingAccounts=new Set()", self.html)
        self.assertIn("if(!uid||savingAccounts.has(uid))return", self.html)
        self.assertIn("saveButton.disabled=true", self.html)
        self.assertIn("savingAccounts.delete(uid)", self.html)

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

    def test_clean_install_uses_online_activation_without_account_login(self):
        index_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "static",
            "index.html",
        )
        with open(index_path, "r", encoding="utf-8") as handle:
            index_html = handle.read()
        self.assertIn("getLicenseManagementInfo", index_html)
        self.assertIn("refreshSidebarLicense", index_html)
        self.assertNotIn("openLoginModal", index_html)
        self.assertNotIn('id="loginUser"', index_html)
        self.assertNotIn('id="loginPass"', index_html)


if __name__ == "__main__":
    unittest.main()
