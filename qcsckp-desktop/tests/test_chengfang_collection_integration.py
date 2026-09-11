"""Chengfang source selection, history and SQLite handoff regressions."""
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from services import official_api_collection as collection
from services.qianchuan_open_api.errors import ApiRequestError
from services.qianchuan_open_api.client import ApiResponse
from api.dashboard_optimized import OptimizedDashboardQueries
from services.failure_report import build_failure_report
from utils.sqlite_store import SQLiteStore, init_sqlite_schema


class ChengfangCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chengfang-source-')
        self.path = str(Path(self.temp.name) / 'test.db')
        init_sqlite_schema(database=self.path)
        self.db = SQLiteStore(database=self.path)
        self.now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.units = {'stat_cost_for_roi2': '0', 'total_prepay_and_pay_order_roi2': '0', 'total_pay_order_gmv_include_coupon_for_roi2': '0'}
        self.target = {'target_uid':'target','account_uid':'account','aadvid':'1001','ad_id':'2001','plan_system':'chengfang','promotion_scene':'live','platform_status':'active','enabled':1,'verification_state':'verified','monitor_eligible':1,'capacity_state':'active'}
        self.cap = {'marketing_goal':'LIVE_PROM_GOODS','report_metric_units':self.units,'report_config_synced_at':self.now,'plan_detail_synced_at':self.now,'control_history_synced_at':self.now,'material_metric_contract':'native_v1','material_metric_contract_since':'2026-09-01 00:00:00'}
        self.target['capability_json'] = json.dumps(self.cap)
        self.db.insert('qianchuan_account', {'account_uid':'account','aavid':'1001','owner_username':'test-owner','enabled':1,'directory_selected':1})
        self.db.insert('promotion_target', self.target)
        self.scope = {'metric_scope':'chengfang_anchor_material','attribution':'unique_plan_in_complete_catalog','advertiser_id':'1001','aadvid':'1001','ad_id':'2001','anchor_id':'9001','ecp_app_id':'1','data_period':'ALL_DATA'}
        self.service = Mock()
        self.service.list_plan_materials.return_value = ([{'material_id':'3001','material_name':'video','stats_info':{'stat_cost_for_roi2':0},'raw':{'stats_info':{'stat_cost_for_roi2':0}}},{'material_id':'3002','stats_info':{'stat_cost_for_roi2':0},'raw':{'stats_info':{'stat_cost_for_roi2':0}}}], ['membership-id'])
        reports=[{'material_id':'3001','stats_info':{'stat_cost_for_roi2':1092.68,'total_prepay_and_pay_order_roi2':5.05,'total_pay_order_gmv_include_coupon_for_roi2':5523.4},'raw':{'metrics':{'stat_cost_for_roi2':{'Value':1092.68,'ValueStr':'1,092.68'},'total_prepay_and_pay_order_roi2':{'Value':5.05},'total_pay_order_gmv_include_coupon_for_roi2':{'Value':5523.4}}}}]
        self.service.list_chengfang_live_material_report.return_value=(reports,['report-id'], self.scope)
        self.service.list_control_tasks.return_value=([],[])
        self.service.list_plan_products.return_value=([],[])
        self.service.get_plan_detail.return_value=({'aavid':'1001','ad_id':'2001','marketing_goal':'LIVE_PROM_GOODS','adlab_scene':'OVERALL_PROJECT','platform_status':'active'},ApiResponse(data={},raw={},request_id='detail-id'))
        self.service.get_report_config.return_value=(self.units,ApiResponse(data={},raw={},request_id='config-id'))
        self.stack=ExitStack()
        self.stack.enter_context(patch.object(OptimizedDashboardQueries, '_owner', return_value='test-owner'))
        for name,value in [('services.official_api_collection.get_official_api_service',self.service),('services.qianchuan_session.current_session_owner','test-owner'),('services.official_api_collection._owner_key','test-owner')]:
            self.stack.enter_context(patch(name,return_value=value))
        self.wake=self.stack.enter_context(patch('services.retargeting_rule_runner.request_retargeting_rule_evaluation'))
        self.stack.enter_context(patch('services.regulation_rule_runner.request_regulation_rule_evaluation'))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def read(self, **kwargs):
        return collection.read_target_material_metrics(self.service,self.target,start_date=self.now[:10],end_date=self.now[:10],fields=list(self.units),units=self.units,**kwargs)

    def test_same_plan_native_zero_becomes_verified_report_value(self):
        materials,*_=self.read()
        self.assertEqual(1092.68,materials[0]['stats_info']['stat_cost_for_roi2'])
        self.assertEqual({},materials[1]['stats_info'])
        row=collection._material_snapshot(materials[1],target=self.target,units=self.units,request_id='report-id')
        self.assertIsNone(row['stat_cost'], 'Missing report row must not fall back to native zero')

    def test_report_failure_leaves_previous_sqlite_value_and_no_wakeup(self):
        self.db.insert('pmc_promotion_material_latest',{'account_username':'test-owner','aadvid':'1001','ad_id':'2001','target_uid':'target','material_id':'3001','stat_cost':18,'collected_at':self.now})
        self.service.list_chengfang_live_material_report.side_effect=ApiRequestError('incomplete',code='client_pagination_incomplete')
        with self.assertRaises(ApiRequestError):
            collection.collect_target(self.target,db=self.db,rotate_maintenance=True)
        row=self.db.select_one('pmc_promotion_material_latest',where={'target_uid':'target','material_id':'3001'})
        self.assertEqual(18,row['stat_cost'])
        self.wake.assert_not_called()

    def test_global_path_does_not_call_chengfang_report(self):
        self.target['plan_system']='global'
        rows,*_=self.read()
        self.service.list_chengfang_live_material_report.assert_not_called()
        self.assertEqual(0,rows[0]['stats_info']['stat_cost_for_roi2'])

    def test_historical_read_uses_same_source_and_day(self):
        self.read(delivery_only=False,parallel_workers=1)
        args=self.service.list_chengfang_live_material_report.call_args
        self.assertEqual(self.now[:10],args.kwargs['start_date'])
        self.assertEqual(self.now[:10],args.kwargs['end_date'])
        self.assertFalse(self.service.list_plan_materials.call_args.kwargs['delivery_only'])

    def test_contract_change_records_new_baseline_even_if_both_costs_are_zero(self):
        reports, ids, scope = self.service.list_chengfang_live_material_report.return_value
        reports[0]['raw']['metrics']['stat_cost_for_roi2']['Value'] = 0
        prior=collection._material_snapshot({'material_id':'3001','stats_info':{'stat_cost_for_roi2':0,'total_prepay_and_pay_order_roi2':5.05,'total_pay_order_gmv_include_coupon_for_roi2':5523.4}},target=self.target,units=self.units,request_id='old')
        prior.update(collected_at=self.now,stat_date=self.now[:10],delivery_state='delivering')
        self.db.insert('pmc_promotion_material_latest',prior)
        collection.collect_target(self.target,db=self.db,rotate_maintenance=True)
        row=self.db.select_one('pmc_promotion_material_latest',where={'target_uid':'target','material_id':'3001'})
        self.assertEqual(0,row['stat_cost'])
        self.assertTrue(self.db.select('pmc_material_metric_snapshot',where={'target_uid':'target','material_id':'3001'}))

    def test_positive_material_disappearing_same_day_keeps_last_good_batch(self):
        collection.collect_target(self.target,db=self.db,rotate_maintenance=True)
        target=self.db.select_one('promotion_target',where={'target_uid':'target'})
        self.service.list_chengfang_live_material_report.return_value=([],['new-report-id'],self.scope)
        with self.assertRaisesRegex(ApiRequestError,'同日已取得消耗'):
            collection.collect_target(target,db=self.db,rotate_maintenance=True)
        row=self.db.select_one('pmc_promotion_material_latest',where={'target_uid':'target','material_id':'3001'})
        self.assertEqual(1092.68,row['stat_cost'])

    def test_collect_to_sqlite_dashboard_and_report_resets_old_zero_baseline(self):
        old=(datetime.now()-timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        self.db.insert('pmc_material_metric_snapshot',{'account_username':'test-owner','aadvid':'1001','ad_id':'2001','target_uid':'target','material_id':'3001','bucket_key':old,'collected_at':old,'stat_date':self.now[:10],'stat_cost':0})
        result=collection.collect_target(self.target,db=self.db,rotate_maintenance=True)
        self.assertTrue(result['success'])
        row=self.db.select_one('pmc_promotion_material_latest',where={'target_uid':'target','material_id':'3001'})
        self.assertEqual((1092.68,5.05,5523.4),(row['stat_cost'],row['prepay_pay_order_count'],row['pay_gmv_include_coupon']))
        cap=json.loads(self.db.select_one('promotion_target',where={'target_uid':'target'})['capability_json'])
        self.assertTrue(cap['material_metric_contract'].startswith('chengfang_anchor_report_v1:'))
        self.assertGreater(cap['material_metric_contract_since'],old)
        data=OptimizedDashboardQueries(self.db).get_table_data(target_uid='target')['data']
        actual=next(r for r in data if str(r['id'])=='3001')
        self.assertEqual(1092.68,actual['currentCost'])
        self.assertEqual(0,actual['costDiff'])
        trace=build_failure_report(db_path=self.path)['material_metric_evidence'][0]
        self.assertEqual('chengfang_anchor_material_report',trace['source'])
        sample=next(s for s in trace['samples'] if s['fields']['stat_cost_for_roi2']['parsed_value']==1092.68)
        self.assertTrue(sample['same_observation'])
        self.assertEqual(1092.68,sample['fields']['stat_cost_for_roi2']['stored_value'])
