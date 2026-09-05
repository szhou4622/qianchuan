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
            match = re.search(r"        function " + name + r"\([\s\S]*?\n        }", html)
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


if __name__ == "__main__":
    unittest.main()
