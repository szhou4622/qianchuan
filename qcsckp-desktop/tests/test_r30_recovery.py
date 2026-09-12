import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils.sqlite_store import SQLiteStore, init_sqlite_schema
from services import official_api_reconciliation as recon
from services.official_api_execution import _verify_control_task, _preflight_read
from services.retarget_verification_contract import verification_duration, DURATION_ERROR
from services import operation_diagnostics as diagnostics
from services import local_feishu_bridge as bridge
from services import official_api_collection as collection
from tests.test_r19_feishu_delivery import FrozenDeliveryTests


class DurationRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name)/'test.db')
        init_sqlite_schema(database=self.path)
        self.db = SQLiteStore(database=self.path)
        self.body = {'advertiser_id': 123, 'ad_id': 456, 'name': 'frozen-task', 'budget': 100,
                     'scene': 'MATERIAL_ADD_BUDGET', 'material_ids': [789],
                     'smart_bid_type': 'SMART_BID_CUSTOM', 'external_action': 'AD_CONVERT_TYPE_LIVE_SUCCESSORDER_PAY', 'bid': 1}
        self.data = {'promotion_scene':'live','task_name':'frozen-task','material_ids':['789'],
                     'budget':'100','duration':'','execution_uid':'exec', 'submission_phase':'accepted'}
        self.row = {'reconciliation_uid':'r','account_username':'owner','task_uid':'task',
                    'action_type':'retarget','aavid':'123','ad_id':'456','control_task_id':'999',
                    'idempotency_key':'stable','status':'unknown_requires_review','attempt_count':8,
                    'request_id':'req','last_error':DURATION_ERROR,'payload_json':json.dumps(self.data)}

    def seed(self, body=None):
        self.db.insert('execution_reconciliation', self.row)
        self.db.insert('qianchuan_api_audit', {'request_uid':'audit','account_username':'owner',
            'request_id':'req','aavid':'123','ad_id':'456','method':'POST','status':'success',
            'endpoint':'/open_api/v1.0/qianchuan/uni_promotion/ad/control_task/create/',
            'request_summary_json':json.dumps({'body': body or self.body})})

    def test_both_control_modes_recover_once_without_post(self):
        for net in (False, True):
            with self.subTest(net=net):
                self.db.execute('DELETE FROM execution_reconciliation')
                self.db.execute('DELETE FROM qianchuan_api_audit')
                body = dict(self.body)
                if net:
                    body.pop('bid');body.update(deep_external_action='AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI',roi2_goal=4)
                self.seed(body)
                self.assertIsNone(verification_duration(self.db,self.row,self.data))
                self.assertEqual(1,recon.recover_duration_mismatches('owner',db=self.db))
                self.assertEqual(0,recon.recover_duration_mismatches('owner',db=self.db))
                row=self.db.select_one('execution_reconciliation',where={'reconciliation_uid':'r'})
                payload=json.loads(row['payload_json'])
                self.assertIsNone(payload['duration'])
                self.assertEqual(8,payload['duration_repair']['previous_attempt_count'])
                self.assertEqual('stable',row['idempotency_key'])
                service=Mock()
                service.create_material_control_task.side_effect=AssertionError('No POST allowed')
                row.update(status='verifying',lease_owner='lease',fencing_token=1)
                self.db.update('execution_reconciliation',{'status':'verifying','lease_owner':'lease','fencing_token':1},where={'reconciliation_uid':'r'})
                task={'task_id':'999','ad_id':'456','material_ids':['789'],'budget':100,'duration':None}
                with patch.object(recon,'get_official_api_service',return_value=service),patch('services.official_api_execution._find_control_task',return_value=task),patch.object(recon,'_finish') as finish:
                    recon._verify_one(self.db,row)
                    self.assertEqual('confirmed_succeeded',finish.call_args.kwargs['status'])
                service.create_material_control_task.assert_not_called()
                self.db.update('execution_reconciliation',{'status':'unknown_requires_review'},where={'reconciliation_uid':'r'})
                self.assertEqual(0,recon.recover_duration_mismatches('owner',db=self.db))

    def test_absent_proof_other_owner_wrong_mode_never_recover(self):
        self.db.insert('execution_reconciliation', self.row)
        self.assertEqual(0,recon.recover_duration_mismatches('owner',db=self.db))
        with self.assertRaisesRegex(RuntimeError,'缺少'):
            verification_duration(self.db,self.row,self.data)
        self.db.execute('DELETE FROM execution_reconciliation')
        self.seed({**self.body,'duration':24})
        self.assertEqual(0,recon.recover_duration_mismatches('owner',db=self.db))
        self.assertEqual(0,recon.recover_duration_mismatches('other',db=self.db))

    def test_volume_duration_is_strict(self):
        with patch('services.official_api_execution._find_control_task',return_value={'task_id':'999','duration':12}):
            with self.assertRaisesRegex(RuntimeError,'duration'):
                _verify_control_task(Mock(),aavid='123',ad_id='456',promotion_scene='live',task_id='999',duration=24)

    def test_repair_eight_attempts_then_unknown_never_resets_again(self):
        self.seed()
        recon.recover_duration_mismatches('owner',db=self.db)
        row=self.db.select_one('execution_reconciliation',where={'reconciliation_uid':'r'})
        row['attempt_count']=8
        with patch.object(recon,'_finish') as finish:
            recon._retry(self.db,row,'still unavailable')
            self.assertEqual('unknown_requires_review',finish.call_args.kwargs['status'])


class LocalEvidenceTests(unittest.TestCase):
    def test_preflight_deadline_blocks_read_without_resetting_budget(self):
        from services.qianchuan_open_api.collection_context import CollectionContext
        context=CollectionContext(0)
        action=Mock()
        with self.assertRaises(Exception):_preflight_read(context,action)
        action.assert_not_called()

    def test_bounded_no_secrets_and_no_upload(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(diagnostics,'_path',return_value=Path(temp)/'e.json'),patch.object(diagnostics,'MAX_EVENTS',3):
            for i in range(6):diagnostics.record('stop_evaluation',candidate_count=i,secret='never-store',evaluation={'actual':0,'passed':False})
            rows=diagnostics.read_events()
            self.assertEqual(3,len(rows));self.assertEqual(5,rows[-1]['candidate_count'])
            self.assertNotIn('never-store',json.dumps(rows))


class CardReceiptsTests(unittest.TestCase):
    setUp = FrozenDeliveryTests.setUp
    create = FrozenDeliveryTests.create
    _stop_payload = FrozenDeliveryTests._stop_payload

    def test_receipt_matches_current_business_version_and_message(self):
        with patch.object(self.actual,'_request',return_value={'data':{'message_id':'msg'}}):
            uid=self.create()
            self.actual._deliver_outbox_once()
            self.actual.update_task_cards(uid)
        task=bridge._task_row(uid)
        receipts=self.store.execute("SELECT * FROM feishu_outbox WHERE task_uid=? AND operation='update_card' ORDER BY id DESC",(uid,),fetch=True)
        self.assertTrue(receipts)
        receipt=json.loads(receipts[0]['payload_json'])
        self.assertEqual(bridge._card_result_version(task),receipt['business_version'])
        self.assertEqual('msg',receipts[0]['message_id'])
        self.assertEqual('sent',receipts[0]['status'])
        self.assertEqual(64,len(receipt['content_sha256']))

    def test_task_changes_during_patch_queues_latest_without_create(self):
        with patch.object(self.actual,'_request',return_value={'data':{'message_id':'msg'}}):
            uid=self.create();self.actual._deliver_outbox_once()
        calls=[]
        def patch_call(method,path,**kwargs):
            calls.append(method)
            self.store.update('local_retarget_task',{'status':'unknown_requires_review','result_message':'final'},where={'task_uid':uid})
            return {}
        with patch.object(self.actual,'_request',side_effect=patch_call):
            self.actual._patch_latest_task_card(uid,'msg')
        queued=self.store.execute("SELECT * FROM feishu_outbox WHERE task_uid=? AND status='queued' AND operation='update_card'",(uid,),fetch=True)
        self.assertTrue(queued);self.assertEqual(['PATCH'],calls)
        self.assertEqual(bridge._card_result_version(bridge._task_row(uid)),json.loads(queued[-1]['payload_json'])['business_version'])

    def test_missing_messages_never_marks_terminal_sent(self):
        uid=self.create()
        self.store.insert('execution_reconciliation',{'reconciliation_uid':'rec','account_username':self.owner,
            'task_uid':uid,'action_type':'stop','idempotency_key':'rec','status':'confirmed_succeeded'})
        self.store.execute("DELETE FROM feishu_outbox WHERE task_uid=?",(uid,))
        bridge._refresh_reconciliation_card_update_state(self.store,self.owner,uid)
        row=self.store.select_one('execution_reconciliation',where={'reconciliation_uid':'rec'})
        self.assertEqual('unknown',row['card_update_state'])

    def test_only_verified_duration_repair_can_update_unknown_historical_card(self):
        uid=self.create()
        self.store.update('local_retarget_task',{'status':'unknown_requires_review','action_type':'retarget'},where={'task_uid':uid})
        self.store.insert('execution_reconciliation',{'reconciliation_uid':'repair','account_username':self.owner,
            'task_uid':uid,'action_type':'retarget','idempotency_key':'repair','status':'confirmed_succeeded',
            'payload_json':json.dumps({'duration_repair':{'version':1}})})
        with patch.object(bridge,'_grouped_result_from_runs',return_value=None):
            self.assertFalse(bridge.finalize_reconciled_local_task(uid,succeeded=True,message='verified',result={})['success'])
            result=bridge.finalize_reconciled_local_task(uid,succeeded=True,message='verified',result={'duration_repair':True})
            self.assertTrue(result['success'])
        self.assertEqual('succeeded',bridge._task_row(uid)['status'])

    def test_multiple_recipients_require_receipts_for_every_message(self):
        uid=self.create()
        self.store.update('local_retarget_task',{'card_messages_json':json.dumps([{'message_id':'one'},{'message_id':'two'}])},where={'task_uid':uid})
        self.store.insert('execution_reconciliation',{'reconciliation_uid':'multi','account_username':self.owner,
            'task_uid':uid,'action_type':'stop','idempotency_key':'multi','status':'confirmed_succeeded'})
        with patch.object(self.actual,'_request',return_value={}):
            self.actual._patch_latest_task_card(uid,'one')
            bridge._refresh_reconciliation_card_update_state(self.store,self.owner,uid)
            self.assertEqual('unknown',self.store.select_one('execution_reconciliation',where={'reconciliation_uid':'multi'})['card_update_state'])
            self.actual._patch_latest_task_card(uid,'two')
            bridge._refresh_reconciliation_card_update_state(self.store,self.owner,uid)
            self.assertEqual('sent',self.store.select_one('execution_reconciliation',where={'reconciliation_uid':'multi'})['card_update_state'])


class ResourceRecoveryTests(unittest.TestCase):
    def test_pressure_wait_then_one_serial_hot_cycle_before_backfill(self):
        from contextlib import ExitStack
        stop=Mock();stop.is_set.side_effect=[False,False,False,True]
        store=Mock();limits=[];events=[]
        with ExitStack() as stack:
            for name,value in [('generation',Mock(return_value='epoch')),('resource_pressure',Mock(side_effect=[{'critical':True,'commit_percent':98.4},{'critical':False}]))]:
                stack.enter_context(patch.object(collection.collection_lifecycle,name,value))
            for name,value in [('_STOP',stop),('SQLiteStore',Mock(return_value=store)),('_ensure_collection_schema',Mock()),
                ('schedulable_promotion_targets',Mock(return_value=[])),('_finish_collection_job',Mock()),
                ('_claim_collection_jobs',Mock(side_effect=lambda **kw: limits.append(kw) or [{'target_uid':'t','job_kind':'hot_collection','due_at':'old'}])),
                ('run_collection_cycle',Mock(return_value={'results':[{'target_uid':'t','success':True}]}))]:
                stack.enter_context(patch.object(collection,name,value))
            stack.enter_context(patch('services.material_backfill.schedule_material_backfills'))
            stack.enter_context(patch.object(diagnostics,'record',side_effect=lambda kind,**kw:events.append(kind)))
            collection._loop(300,'epoch')
        self.assertEqual(1,limits[0]['limit']);self.assertEqual(1,len(limits))
        self.assertIn('collection_resource_wait',events);self.assertIn('collection_resource_recovered',events)


if __name__=='__main__':unittest.main()
