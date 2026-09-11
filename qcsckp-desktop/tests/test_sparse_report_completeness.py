"""A sparse complete report is different from a failed or malformed response."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from api.dashboard_optimized import OptimizedDashboardQueries
from services.material_metric_contract import evidence
from services.failure_report import build_failure_report
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class SparseReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='qcsckp-sparse-report-')
        self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'db.sqlite')
        init_sqlite_schema(database=self.path)
        self.db=SQLiteStore(database=self.path)
        self.now=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.db.insert('qianchuan_account',{'account_uid':'a','aavid':'1001','owner_username':'owner','enabled':1})
        self.db.insert('promotion_target',{'target_uid':'t','account_uid':'a','aadvid':'1001','ad_id':'2001','promotion_scene':'live','plan_system':'chengfang','enabled':1,'last_status':'ok','capability_json':json.dumps({'material_metric_source':'chengfang_anchor_material_report'})})
        self.stack=ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(OptimizedDashboardQueries,'_owner',return_value='owner'))
        self.q=OptimizedDashboardQueries(self.db)

    def row(self, mid, cost=None, roi=None, gmv=None, state='not_in_report'):
        row={'aadvid':'1001','ad_id':'2001','target_uid':'t','material_id':mid,'stat_date':self.now[:10],'collected_at':self.now,'stat_cost':cost,'prepay_pay_order_count':roi,'pay_gmv_include_coupon':gmv,'metric_row_state':state}
        self.db.insert('pmc_promotion_material_latest',row)
        self.db.insert('pmc_material_metric_snapshot',{**row,'account_username':'owner','bucket_key':self.now})

    def test_sparse_complete_report_displays_known_totals_and_curves(self):
        self.row('1',49.81,2.01,99.9,'reported')
        self.row('2',0,0,0,'reported')
        self.row('3')
        status=self.q.get_refresh_state()
        self.assertEqual(('complete','sparse_report',2,1,0),(status['collectionStatus'],status['metricQuality'],status['reportedMaterialCount'],status['noReportMaterialCount'],status['missingReportedMetricCount']))
        self.assertEqual(49.81,self.q.get_latest_cost_sum()['totalCost'])
        history=self.q.get_scope_history()['data']
        self.assertTrue(history)
        self.assertTrue(all(p['cost']==49.81 and p['amount']==99.9 for p in history))
        absent=next(r for r in self.q.get_table_data()['data'] if r['id']=='3')
        self.assertIsNone(absent['currentCost'])
        self.assertEqual('not_in_report',absent['metricRowState'])

    def test_returned_row_missing_field_still_reports_actual_missing_metrics(self):
        self.row('1',49.81,None,None,'reported')
        self.row('2')
        status=self.q.get_refresh_state()
        self.assertEqual('missing',status['metricQuality'])
        self.assertEqual(1,status['missingReportedMetricCount'])
        self.assertIsNone(self.q.get_scope_history()['data'][-1]['amount'])

    def test_all_absent_is_not_invented_zero_history(self):
        self.row('1')
        self.assertEqual('sparse_report',self.q.get_refresh_state()['metricQuality'])
        self.assertIsNone(self.q.get_latest_cost_sum()['totalCost'])
        self.assertIsNone(self.q.get_scope_history()['data'][-1]['cost'])

    def test_legacy_null_cannot_be_reclassified_without_new_evidence(self):
        self.row('1',state='legacy_unknown')
        self.assertEqual('missing',self.q.get_refresh_state()['metricQuality'])

    def test_failure_status_does_not_become_success_just_because_data_was_saved(self):
        self.row('1',1,1,1,'reported')
        self.db.update('promotion_target',{'last_status':'error'},where={'target_uid':'t'})
        self.assertEqual('failed',self.q.get_refresh_state()['collectionStatus'])

    def test_diagnostic_distinguishes_absent_report_rows_from_missing_fields(self):
        self.row('1',1,1,1,'reported');self.row('2')
        materials=[{'material_id':'1','stats_info':{'stat_cost_for_roi2':1},'raw':{'stats_info':{'stat_cost_for_roi2':{'Value':1}}},'report_row_present':True},{'material_id':'2','stats_info':{},'raw':{},'report_row_present':False}]
        trace=evidence(materials,[{'material_id':'1','stat_cost':1},{'material_id':'2','stat_cost':None}],requested_fields=['stat_cost_for_roi2'],report_units={},observed_at=self.now,stat_date=self.now[:10],source='chengfang_anchor_material_report',scope={})
        self.assertEqual(1,trace['report_rows_absent'])
        self.assertEqual(0,trace['reported_field_counts']['stat_cost_for_roi2']['missing'])
        self.db.update('promotion_target',{'capability_json':json.dumps({'material_metric_evidence':trace})},where={'target_uid':'t'})
        self.assertEqual('sparse_report_complete',build_failure_report(db_path=self.path)['metric_findings'][0]['finding'])

    def test_real_frontend_labels_sparse_rows_and_does_not_hide_staleness(self):
        html=(Path(__file__).resolve().parents[1]/'static/dashboard.html').read_text(encoding='utf-8')
        body=re.search(r'function formatFreshness\(state\) \{[\s\S]*?\n        \}',html).group(0)
        js=body+"\nconst s={newestAt:'2026-09-11 00:30:48',metricQuality:'sparse_report',reportedMaterialCount:57,noReportMaterialCount:165,dataAgeSeconds:60,collectionStatus:'complete'}; if(formatFreshness(s).text!=='数据正常 · 1分钟前')throw Error('bad compact status'); s.dataAgeSeconds=900;if(!formatFreshness(s).text.startsWith('数据延迟'))throw Error('age hidden'); s.collectionStatus='failed';if(!formatFreshness(s).text.startsWith('采集异常'))throw Error('error hidden');"
        node=Path('C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
        result=subprocess.run([str(node),'-e',js],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(0,result.returncode,result.stderr)

    def enable_zero_display(self):
        cap={'material_metric_source':'chengfang_anchor_material_report','material_sync_complete':True,
             'material_metric_scope':{'data_period':'ALL_DATA'},
             'material_metric_evidence':{'requested_fields':['stat_cost_for_roi2','total_pay_order_count_for_roi2','total_prepay_and_pay_order_roi2']}}
        self.db.update('promotion_target',{'last_status':'ok','capability_json':json.dumps(cap)},where={'target_uid':'t'})

    def test_absent_row_gets_display_hints_without_changing_numeric_rule_inputs(self):
        self.row('1'); self.enable_zero_display()
        row=self.q.get_table_data()['data'][0]
        self.assertEqual(['currentCost','overallOrderCount','overallPayRoi'],row['displayZeroFields'])
        self.assertIsNone(row['currentCost'])
        self.assertIsNone(self.db.select_one('pmc_promotion_material_latest',where={'material_id':'1'})['stat_cost'])
        summary=self.q.get_latest_cost_sum()
        self.assertEqual(1,summary['zeroDisplayMaterialCount'])
        self.assertIsNone(summary['totalCost'])

    def test_failed_partial_stale_previous_day_and_returned_missing_do_not_get_zero_hints(self):
        self.row('1'); self.enable_zero_display()
        for status in ('error','collecting'):
            self.db.update('promotion_target',{'last_status':status},where={'target_uid':'t'})
            self.assertEqual([],self.q.get_table_data()['data'][0]['displayZeroFields'])
        self.enable_zero_display()
        old=(datetime.now()-timedelta(minutes=11)).strftime('%Y-%m-%d %H:%M:%S')
        self.db.update('pmc_promotion_material_latest',{'collected_at':old},where={'material_id':'1'})
        self.assertEqual([],self.q.get_table_data()['data'][0]['displayZeroFields'])
        self.db.update('pmc_promotion_material_latest',{'collected_at':self.now,'metric_row_state':'reported'},where={'material_id':'1'})
        self.assertEqual([],self.q.get_table_data()['data'][0]['displayZeroFields'])
        self.db.update('pmc_promotion_material_latest',{'metric_row_state':'not_in_report','stat_date':'2000-01-01'},where={'material_id':'1'})
        self.assertEqual([],self.q.get_table_data()['data'][0]['displayZeroFields'])
        self.db.update('promotion_target',{'capability_json':'{}'},where={'target_uid':'t'})
        self.assertEqual([],self.q.get_table_data()['data'][0]['displayZeroFields'])
