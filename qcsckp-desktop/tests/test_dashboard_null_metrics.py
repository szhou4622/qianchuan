"""Execute actual inline JS with DOM/chart doubles, no browser or network."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


HTML = Path(__file__).resolve().parents[1] / "static" / "dashboard.html"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for frontend execution checks")
class DashboardNullMetricTests(unittest.TestCase):
    def run_js(self, names, body):
        html = HTML.read_text(encoding="utf-8")
        functions = []
        for name in names:
            match = re.search(r"        (?:async )?function " + name + r"\([\s\S]*?\n        }", html)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        code = "const fs=require('fs'),vm=require('vm'),assert=require('assert'); const p=JSON.parse(fs.readFileSync(0,'utf8')); "
        code += "for(const s of p.scripts) new vm.Script(s); eval(p.functions.join('\\n')); " + body
        result = subprocess.run([NODE, "-e", code], input=json.dumps({
            "functions": functions,
            "scripts": re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S | re.I),
        }), text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_optional_values_and_pie_preserve_real_zero_only(self):
        self.run_js(["optionalMetricNumber", "buildPieDataFromTopList"], """
            for(const v of [null,undefined,'','not-a-number']) assert.strictEqual(optionalMetricNumber(v),null);
            assert.strictEqual(optionalMetricNumber(0),0);
            assert.strictEqual(optionalMetricNumber('2.5'),2.5);
            function getMaterialDisplayTitle(item){return item.title;}
            const pie=buildPieDataFromTopList([{title:'missing',currentCost:null},{title:'zero',currentCost:0}]);
            assert.strictEqual(pie.length,1); assert.strictEqual(pie[0].value,0);
        """)

    def test_missing_totals_and_fresh_but_invalid_metrics_are_not_reported_healthy(self):
        self.run_js(["formatYuanCompact", "formatFreshness"], """
            assert.strictEqual(formatYuanCompact(null),'--');
            assert.notStrictEqual(formatYuanCompact(0),'--');
            const recent={newestAt:'2026-09-09 20:29:00',dataAgeSeconds:60};
            assert.ok(formatFreshness({...recent,metricQuality:'missing'}).text.includes('返回指标缺失'));
            assert.ok(formatFreshness({...recent,metricQuality:'all_zero'}).text.includes('待核对'));
            assert.ok(formatFreshness({...recent,metricQuality:'available'}).text.includes('正常'));
        """)

    def test_curves_and_tooltips_do_not_turn_null_into_zero(self):
        self.run_js(["optionalMetricNumber", "renderLineChart", "renderPayRoiLineChart", "renderAmountLineChart"], """
            const seen=[];
            const chart={setOption:o=>seen.push(o)};
            const document={getElementById:()=>({remove(){}})};
            const echarts={init:()=>chart,graphic:{LinearGradient:function(){}}};
            let lineChartInstance=chart,payRoiChartInstance=chart,amountChartInstance=chart;
            renderLineChart({historyData:[{time:'t0',cost:null},{time:'t1',cost:0}]});
            renderPayRoiLineChart([{time:'t0',roi:null},{time:'t1',roi:0}]);
            renderAmountLineChart([{time:'t0',amount:null},{time:'t1',amount:0}]);
            for(const o of seen){
                assert.deepStrictEqual(o.series[0].data,[null,0]);
                assert.ok(o.tooltip.formatter([{name:'t0',value:null}]).includes('--'));
                assert.ok(!o.tooltip.formatter([{name:'t1',value:0}]).includes('--'));
                assert.strictEqual(o.tooltip.formatter([]),'');
            }
        """)

    def test_table_renders_null_metrics_without_crashing(self):
        self.run_js(["renderTable"], """
            const rows=[];
            const tbody={innerHTML:'',appendChild:r=>rows.push(r)};
            const document={getElementById:()=>tbody,createElement:()=>({})};
            const VELOCITY_CONFIG={negative:'n',normal:'n',high:'h',threshold:10};
            let selectedMaterial=null,currentPeriod='1h',currentSortBy='costDiff',currentSortOrder='desc';
            const lucide={createIcons(){}};
            function getMaterialDisplayTitle(item){return item.title;}
            function escapeHtml(value){return String(value);}
            function updateColumnVisibility(){} function updatePagination(){}
            renderTable([{id:'1',title:'missing',velocity:null,currentCost:null,overallOrderCount:null},
                         {id:'2',title:'zero',velocity:0,currentCost:0,overallOrderCount:0}]);
            assert.strictEqual(rows.length,2);
            assert.ok(rows[0].innerHTML.includes('--'));
            assert.ok(!rows[0].innerHTML.includes('null%'));
            assert.ok(rows[1].innerHTML.includes('¥0.00'));
        """)

    def test_absent_record_display_zero_does_not_mutate_source_or_invent_derived_metrics(self):
        self.run_js(["renderTable"], """
            const rows=[];const tbody={innerHTML:'',appendChild:r=>rows.push(r)};
            const document={getElementById:()=>tbody,createElement:()=>({})};
            const VELOCITY_CONFIG={negative:'n',normal:'n',high:'h',threshold:10};
            let selectedMaterial=null,currentPeriod='1h',currentSortBy='costDiff',currentSortOrder='desc';
            const lucide={createIcons(){}};
            function getMaterialDisplayTitle(i){return i.title;} function escapeHtml(v){return String(v);}
            function updateColumnVisibility(){}function updatePagination(){}
            let clicked=null;function selectMaterial(item){clicked=item;}
            const original={id:'1',title:'absent',velocity:null,currentCost:null,overallPayRoi:null,estimatedEcpm:null,displayZeroFields:['currentCost','overallPayRoi','estimatedEcpm','velocity','id']};
            renderTable([original,{id:'2',title:'missing-field',currentCost:null}]);
            assert.ok(rows[0].innerHTML.includes('¥0'));
            assert.ok(rows[0].title.includes('按0展示'));
            assert.ok(rows[0].innerHTML.match(/data-col="estimatedEcpm"[\\s\\S]*?--/));
            assert.ok(rows[1].innerHTML.match(/data-col="currentCost"[\\s\\S]*?--/));
            assert.strictEqual(original.currentCost,null);assert.strictEqual(original.id,'1');
            rows[0].onclick();assert.strictEqual(clicked,original);
        """)

    def test_expired_display_zero_refreshes_table_without_new_collection_version(self):
        self.run_js(["checkDashboardDataVersion"], """
            let selectedDashboardAavid='a',selectedDashboardTargetUid='t';
            let dashboardDataVersion='same',dashboardDisplayVersion='';
            let count=3,refreshes=0;
            const document={getElementById:()=>null};
            function formatFreshness(){return {};}
            async function waitForAPI(){return {getDashboardRefreshState:async()=>({dataVersion:'same',zeroDisplayMaterialCount:count})};}
            async function queryAndUpdateData(){refreshes++;}
            (async()=>{
                await checkDashboardDataVersion();assert.strictEqual(refreshes,1);
                await checkDashboardDataVersion();assert.strictEqual(refreshes,1);
                count=0; // Same stored rows, now stale or collection failed.
                await checkDashboardDataVersion();assert.strictEqual(refreshes,2);
                await checkDashboardDataVersion();assert.strictEqual(refreshes,2);
                count=3;await checkDashboardDataVersion();assert.strictEqual(refreshes,3);
            })().catch(e=>{console.error(e);process.exitCode=1;});
        """)

    def test_compact_status_moves_coverage_to_tooltip_without_hiding_errors(self):
        self.run_js(["formatFreshness", "freshnessDetails"], """
            const s={newestAt:'2026-09-11 14:53:20',dataAgeSeconds:120,collectionStatus:'complete',metricQuality:'sparse_report',reportedMaterialCount:58,noReportMaterialCount:164,zeroDisplayMaterialCount:164};
            assert.strictEqual(formatFreshness(s).text,'数据正常 · 2分钟前');
            assert.ok(freshnessDetails(s).includes('58条有报表'));
            assert.ok(freshnessDetails(s).includes('原始数据保留为空'));
            assert.ok(freshnessDetails(s).includes(s.newestAt));
            for(const [patch,expected] of [[{collectionStatus:'failed'},'采集异常'],[{collectionStatus:'collecting'},'正在采集'],[{dataAgeSeconds:900},'数据延迟'],[{metricQuality:'missing'},'报表返回指标缺失']]){
                const result=formatFreshness({...s,...patch}).text;
                assert.ok(result.startsWith(expected));assert.ok(!result.includes(s.newestAt));assert.ok(!result.includes('164'));
            }
        """)

    def test_compact_total_keeps_zero_convention_and_resets_warning_tooltip(self):
        self.run_js(["updateLatestCrawlCostDisplay", "formatYuanCompact"], """
            const val={},meta={};const document={getElementById:id=>id==='latestCrawlCostValue'?val:meta};
            let dashboardScopeGeneration=1,data={rowCount:3,totalCost:null,missingCostCount:3,noReportMaterialCount:3,zeroDisplayMaterialCount:3,metricScopeLabel:'scope'};
            async function fetchLatestCrawlCostSum(){return data;}
            (async()=>{
                await updateLatestCrawlCostDisplay();assert.strictEqual(meta.textContent,'总统计 3 条素材');assert.ok(val.textContent.includes('0.00'));assert.ok(meta.title.includes('3条仅按0展示'));assert.strictEqual(data.totalCost,null);
                data={rowCount:3,totalCost:12.5,missingCostCount:1,noReportMaterialCount:0};
                await updateLatestCrawlCostDisplay();assert.ok(meta.textContent.includes('指标不完整'));assert.ok(!meta.title.includes('仅按0展示'));
                data={rowCount:3,totalCost:12.5,missingCostCount:0,noReportMaterialCount:0};
                await updateLatestCrawlCostDisplay();assert.strictEqual(meta.textContent,'总统计 3 条素材');assert.ok(!meta.title.includes('原始消耗为空'));
                data=null;await updateLatestCrawlCostDisplay();assert.strictEqual(val.textContent,'--');
            })().catch(e=>{console.error(e);process.exitCode=1;});
        """)


if __name__ == "__main__":
    unittest.main()
