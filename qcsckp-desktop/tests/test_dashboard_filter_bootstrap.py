from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardFilterBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = (ROOT / "static" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        cls.index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.gui = (ROOT / "gui_app.py").read_text(encoding="utf-8")

    def test_initial_filter_state_is_loading_not_false_zero(self):
        self.assertIn('id="dashboardAccountFilter" disabled', self.dashboard)
        self.assertIn('id="dashboardPlanFilter" disabled', self.dashboard)
        self.assertIn("正在加载账户和计划", self.dashboard)
        self.assertNotIn(
            'id="dashboardScopeSummary" class="text-[11px] text-slate-500 tabular-nums shrink-0">0个账户 · 0条计划',
            self.dashboard,
        )

    def test_frontend_requires_versioned_bootstrap_contract(self):
        self.assertIn("const DASHBOARD_CONTRACT_VERSION = 2", self.dashboard)
        self.assertIn("getDashboardBootstrap", self.dashboard)
        self.assertIn("dashboardContractVersion", self.dashboard)
        self.assertIn("当前后台不支持大屏协议", self.dashboard)
        self.assertIn("前端与后台版本不匹配", self.dashboard)

    def test_bootstrap_retries_and_surfaces_failures(self):
        self.assertIn(
            "const DASHBOARD_BOOTSTRAP_RETRY_DELAYS = [0, 500, 1000, 2000, 3000, 5000]",
            self.dashboard,
        )
        self.assertIn('id="dashboardScopeError"', self.dashboard)
        self.assertIn('id="dashboardScopeRetryBtn"', self.dashboard)
        self.assertIn("范围数据不一致", self.dashboard)
        self.assertIn("loadDashboardBootstrap({", self.dashboard)

    def test_bootstrap_recovers_on_ready_focus_visibility_and_page_activation(self):
        self.assertIn("pywebviewready", self.dashboard)
        self.assertIn("window.addEventListener('focus'", self.dashboard)
        self.assertIn("visibilitychange", self.dashboard)
        self.assertIn("dashboard-activated", self.dashboard)
        self.assertIn("{ type: 'dashboard-activated' }", self.index)

    def test_bridge_exposes_bootstrap_method(self):
        self.assertIn("def getDashboardBootstrap(self):", self.gui)
        self.assertIn("return self.api.get_dashboard_bootstrap()", self.gui)

    def test_default_curves_follow_scope_and_material_click_switches_detail(self):
        self.assertIn("def getScopeHistoryRecent", self.gui)
        self.assertIn("function startScopeHistoryPolling()", self.dashboard)
        self.assertIn("api.getScopeHistoryRecent(", self.dashboard)
        self.assertIn("范围：全部启用账户", self.dashboard)
        self.assertIn("startHistoryPolling(item.id, item.targetUid)", self.dashboard)
        self.assertIn("startScopeHistoryPolling();", self.dashboard)

    def test_scope_change_requeries_materials_and_restarts_selected_curve(self):
        self.assertNotIn("clearSelectionAndCharts()", self.dashboard)
        self.assertGreaterEqual(
            self.dashboard.count("await queryAndUpdateData(true)"), 2
        )
        self.assertIn("fetchMaterials(requestScope)", self.dashboard)
        self.assertIn(
            "startHistoryPolling(selectedMaterial.id, selectedMaterial.targetUid)",
            self.dashboard,
        )

    def test_scope_is_forwarded_to_table_pie_and_cost_total_queries(self):
        self.assertIn("scopedTargetUid || null", self.dashboard)
        self.assertIn("scopedAavid || null", self.dashboard)
        self.assertIn("fetchTop20ByCost(1, scope)", self.dashboard)
        self.assertIn("fetchLatestCrawlCostSum(hours, scope)", self.dashboard)
        self.assertIn("generation !== dashboardScopeGeneration", self.dashboard)
        self.assertIn("官方API本轮指标为0", self.dashboard)


if __name__ == "__main__":
    unittest.main()
