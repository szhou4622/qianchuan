import io
import json
import os
import socket
import hashlib
import subprocess
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

import channel_runtime as runtime
from services import channel_ledger, diagnostics, update_manifest
from services.channel_update import safe_extract
from services.channel_update import validate_payload
from server import diagnostics_server, update_server


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"QCSCKP_HOME":str(self.home)})
        self.env.start()
        self.old_override=os.environ.pop("QCSCKP_DATA_DIR",None)

    def tearDown(self):
        diagnostics.stop_worker()
        if self.old_override is not None: os.environ["QCSCKP_DATA_DIR"]=self.old_override
        self.env.stop()
        self.temp.cleanup()

    def make_legacy(self):
        p=runtime.layout().legacy
        p.mkdir(parents=True)
        with closing(sqlite3.connect(p/"qianchuan.db")) as c,c:
            c.execute("CREATE TABLE sample(value TEXT)")
            c.execute("INSERT INTO sample VALUES('original')")
            c.execute("CREATE TABLE local_retarget_task(status TEXT,active_dedupe_key TEXT)")
            c.execute("INSERT INTO local_retarget_task VALUES('approved_queued','key')")
        runtime.atomic_json(p/"rule_retargeting.json",{"enabled":True,"strategies":[{"id":"x"}]})
        runtime.atomic_json(p/"profiles/test-owner/rule_retargeting.json",{"enabled":True})
        (p/"license_credentials.dpapi").write_bytes(b"encrypted-test-identity")
        return p


class Profiles(Isolated):
    def test_three_profiles_keep_original_and_same_identity(self):
        legacy=self.make_legacy()
        production=runtime.prepare_profile(lambda *_:True,"production")
        development=runtime.prepare_profile(lambda *_:True,"development")
        with closing(sqlite3.connect(development.data/"qianchuan.db")) as c,c:
            c.execute("INSERT INTO sample VALUES('development only')")
        stable=runtime.prepare_profile(lambda *_:True,"stable")
        runtime.prepare_profile(lambda *_:True,"production")
        with closing(sqlite3.connect(production.data/"qianchuan.db")) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sample").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT status FROM local_retarget_task").fetchone()[0],"cancelled")
        self.assertFalse(runtime.read_json(production.data/"rule_retargeting.json")["enabled"])
        self.assertFalse(runtime.read_json(production.data/"profiles/test-owner/rule_retargeting.json")["enabled"])
        self.assertEqual((production.shared/"identity/license_credentials.dpapi").read_bytes(),b"encrypted-test-identity")
        self.assertFalse((development.data/"license_credentials.dpapi").exists())
        with closing(sqlite3.connect(legacy/"qianchuan.db")) as c:
            self.assertEqual(c.execute("SELECT status FROM local_retarget_task").fetchone()[0],"approved_queued")

    def test_snapshot_failure_preserves_source(self):
        source=self.make_legacy()
        dest=self.home/"copy"
        with mock.patch.object(runtime.shutil,"disk_usage",return_value=type("Disk",(),{"free":1})()):
            with self.assertRaises(RuntimeError): runtime.snapshot(source,dest)
        self.assertFalse(dest.exists())
        self.assertTrue((source/"qianchuan.db").exists())

    def test_two_instances_are_blocked(self):
        a,b=runtime.InstanceLease(),runtime.InstanceLease()
        try:
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())
        finally: a.close();b.close()

    def test_refreshed_identity_never_overwritten(self):
        self.make_legacy()
        p=runtime.prepare_profile(lambda *_:True,"production")
        (p.shared/"identity/license_credentials.dpapi").write_bytes(b"new-credential")
        runtime.prepare_profile(lambda *_:True,"development")
        self.assertEqual((p.shared/"identity/license_credentials.dpapi").read_bytes(),b"new-credential")


class Ledger(Isolated):
    def test_shared_success_and_unknown_across_profiles(self):
        from utils.sqlite_store import SQLiteStore, init_sqlite_schema
        def open_profile(channel):
            paths=runtime.layout(channel)
            db=SQLiteStore(database=str(paths.data/"qianchuan.db"))
            init_sqlite_schema(database=db.config["database"])
            return db
        with mock.patch.object(runtime,"CHANNEL","development"):
            dev=open_profile("development")
            dev.insert("execution_reconciliation",{"reconciliation_uid":"test-u","account_username":"test", "idempotency_key":"immutable-key","status":"submitting"})
            dev.insert("pmc_retargeting_rate_limit",{"target_uid":"target", "material_id":"m", "limit_started_at":"2026-08-31 01:00:00", "use_count":2})
        with mock.patch.object(runtime,"CHANNEL","production"):
            prod=open_profile("production")
            row=prod.select_one("execution_reconciliation",where={"idempotency_key":"immutable-key"})
            self.assertEqual(row["status"],"submitting")
            self.assertEqual(prod.select_one("pmc_retargeting_rate_limit",where={"material_id":"m"})["use_count"],2)
            prod.update("execution_reconciliation",{"status":"confirmed_succeeded"},where={"idempotency_key":"immutable-key"})
        with mock.patch.object(runtime,"CHANNEL","stable"):
            stable=open_profile("stable")
            self.assertEqual(stable.select_one("execution_reconciliation",where={"idempotency_key":"immutable-key"})["status"],"confirmed_succeeded")

    def test_sql_routing_does_not_touch_literals_or_schema(self):
        self.assertIn('channel_guard."execution_reconciliation"',channel_ledger.route_sql('SELECT * FROM "execution_reconciliation" WHERE id=?'))
        self.assertEqual(channel_ledger.route_sql("SELECT 'FROM execution_reconciliation'"),"SELECT 'FROM execution_reconciliation'")
        self.assertEqual(channel_ledger.route_sql("CREATE TABLE execution_reconciliation(id INT)"),"CREATE TABLE execution_reconciliation(id INT)")


class Diagnostics(Isolated):
    def event(self):
        return {"event_id":uuid.uuid4().hex,"diagnostic_id":"a"*32,"app_name":"QCSCKP", "channel":"development",
                "version":"0.1.66","build_revision":1,"source_commit":"c1f4443", "occurred_at":int(time.time()),
                "stage":"license","error_code":"dns","http_status":0,"elapsed_ms":20,"request_id":""}

    def test_exception_message_and_secrets_are_never_recorded(self):
        try: raise ValueError("activation_code=DO-NOT-UPLOAD access_token=TOPSECRET")
        except ValueError as e: diagnostics.record_event("license","dns",exception=e)
        exported=Path(diagnostics.export_events()).read_text(encoding="utf-8")
        self.assertNotIn("DO-NOT-UPLOAD",exported)
        self.assertNotIn("TOPSECRET",exported)
        self.assertNotIn(str(self.home),exported)
        events=json.loads(exported)["events"]
        self.assertEqual(len(events),1)
        diagnostics_server.validate_event(events[0])

    def test_no_consent_no_network_and_dedup(self):
        opener=mock.Mock()
        diagnostics.record_event("license","dns")
        diagnostics.record_event("license","dns")
        self.assertEqual(diagnostics.upload_once(opener),0)
        opener.assert_not_called()
        with diagnostics._db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0],1)

    def test_server_auth_rate_limit_idempotency_and_retention(self):
        db=self.home/"server.sqlite3"
        diagnostics_server.initialize(db)
        event=self.event()
        self.assertEqual(diagnostics_server.persist(db,"subject",event,now=100),202)
        self.assertEqual(diagnostics_server.persist(db,"subject",event,now=100),202)
        self.assertEqual(diagnostics_server.persist(db,"other",event,now=100),409)
        for _ in range(9): self.assertEqual(diagnostics_server.persist(db,"subject",self.event(),now=100),202)
        self.assertEqual(diagnostics_server.persist(db,"subject",self.event(),now=100),429)
        self.assertEqual(diagnostics_server.persist(db,"subject",self.event(),now=31*86400),202)
        with closing(sqlite3.connect(db)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0],1)
        self.assertIsNone(diagnostics_server.authenticate({}))
        invalid={**event,"activation_code":"secret"}
        with self.assertRaises(ValueError): diagnostics_server.validate_event(invalid)

    def test_valid_auth_is_product_scoped(self):
        class Reply(io.BytesIO):
            pass
        def opener(_request,**_kwargs):
            return Reply(json.dumps({"ok":True,"app_name":"QCSCKP","code_id":"test","machine_code":"machine"}).encode())
        self.assertEqual(len(diagnostics_server.authenticate({"Authorization":"Bearer test","X-Device-Credential":"test"},opener)),64)
        def other(*_args,**_kwargs): return Reply(b'{"ok":true,"app_name":"Other"}')
        self.assertIsNone(diagnostics_server.authenticate({"Authorization":"Bearer test","X-Device-Credential":"test"},other))

    def test_offline_queue_then_successful_replay(self):
        diagnostics.set_consent(True)
        diagnostics.record_event("license","dns")
        with mock.patch("services.license_storage.LicenseSecureStore") as store:
            store.return_value.load_credentials.return_value={"device_session":"TEST_SESSION","device_credential":"TEST_CREDENTIAL"}
            self.assertEqual(diagnostics.upload_once(mock.Mock(side_effect=TimeoutError())),0)
            class Reply(io.BytesIO): status=202
            captured=[]
            def online(request,**_kw):
                event=json.loads(request.data)
                diagnostics_server.validate_event(event)
                self.assertNotIn("TEST_SESSION",request.data.decode())
                captured.append(event)
                return Reply(b'{}')
            self.assertEqual(diagnostics.upload_once(online),1)
            self.assertEqual(diagnostics.upload_once(online),0)
            self.assertEqual(len(captured),1)


class Updates(Isolated):
    def test_manifest_channel_and_content_validation(self):
        payload=self.home/"package"
        files={"QCSCKP.exe":b"exe", "bin/python312.dll":b"dll", "bin/static/index.html":b"index",
               "bin/static/license.html":b"license", "bin/release.json":json.dumps({"app_name":"QCSCKP", "channel":"production", "version":"0.1.66", "build_revision":1}).encode()}
        manifest={"app_name":"QCSCKP", "channel":"production", "version":"0.1.66", "build_revision":1,"critical_files":[]}
        for name,body in files.items():
            target=payload/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body)
            manifest["critical_files"].append({"path":name,"size":len(body),"sha256":hashlib.sha256(body).hexdigest()})
        runtime.atomic_json(payload/"PACKAGE-MANIFEST.json",manifest)
        validate_payload(payload,"production")
        with self.assertRaises(ValueError): validate_payload(payload,"development")
        (payload/"QCSCKP.exe").write_bytes(b"changed")
        with self.assertRaises(ValueError): validate_payload(payload,"production")

    @unittest.skipUnless(os.name=="nt","Windows native updater")
    def test_native_installer_partial_failure_restores_original(self):
        root=self.home/"install"; stage=root/".qcsckp-update/test"; payload=stage/"unpacked/new"
        (root/"bin").mkdir(parents=True)
        (root/"QCSCKP.exe").write_bytes(b"old-exe")
        (root/"bin/value").write_bytes(b"old-bin")
        (payload/"bin").mkdir(parents=True)
        (payload/"QCSCKP.exe").write_bytes(b"new-exe")
        (payload/"bin/value").write_bytes(b"new-bin")
        runtime.atomic_json(payload/"PACKAGE-MANIFEST.json",{})
        context=stage/"context.json"
        runtime.atomic_json(context,{"root":str(root),"stage":str(stage),"payload":str(payload),"old_pid":0})
        helper=Path(__file__).resolve().parents[1]/"packaging/windows/apply_channel_update.ps1"
        command=("$global:moveCount=0; function global:Move-Item { param($LiteralPath,$Destination) "
                 "$global:moveCount++; if($global:moveCount -eq 3){throw 'simulated I/O failure'}; "
                 "Microsoft.PowerShell.Management\\Move-Item -LiteralPath $LiteralPath -Destination $Destination }; "
                 f"& '{helper}' -ContextFile '{context}' -SkipRestart")
        result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-Command",command],capture_output=True,timeout=20)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((root/"QCSCKP.exe").read_bytes(),b"old-exe")
        self.assertEqual((root/"bin/value").read_bytes(),b"old-bin")

    def test_client_channel_mismatch_and_frozen_stable(self):
        class Reply(io.BytesIO): pass
        payload={"app_name":"QCSCKP","version":"0.9.99","channel":"development","build_revision":1,
                 "download_url":{"windows_x64":"https://update.dadaozixun.com/test.zip"},"sha256":{"windows_x64":"a"*64}}
        def opener(*_args,**_kw): return Reply(json.dumps(payload).encode())
        with mock.patch.object(update_manifest,"CHANNEL","production"):
            with self.assertRaises(RuntimeError): update_manifest.check_for_update("0.1.65",opener=opener)
        payload["channel"]="stable"
        with mock.patch.object(update_manifest,"CHANNEL","stable"):
            self.assertFalse(update_manifest.check_for_update("0.1.65",opener=opener)["data"]["has_update"])

    def test_zip_traversal_rejected(self):
        archive=self.home/"bad.zip"
        with zipfile.ZipFile(archive,"w") as z: z.writestr("../escape.txt","bad")
        with self.assertRaises(ValueError): safe_extract(archive,self.home/"unpacked")
        self.assertFalse((self.home/"escape.txt").exists())

    def test_server_default_and_channel_isolation(self):
        root=self.home
        base={"app_name":"QCSCKP","version":"0.1.66","download_url":{},"sha256":{},"notes":[],"force":False,"min_supported_version":"0.1.58"}
        runtime.atomic_json(root/"apps/QCSCKP/latest.json",base)
        runtime.atomic_json(root/"apps/QCSCKP/channels/development/latest.json",{**base,"version":"0.1.99","channel":"development"})
        from http.server import ThreadingHTTPServer
        def handler(*args,**kwargs): return update_server.UpdateHandler(*args,directory=str(root),**kwargs)
        server=ThreadingHTTPServer(("127.0.0.1",0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f"http://127.0.0.1:{server.server_port}/api/update/latest?app_name=QCSCKP"
        try:
            with urlopen(url) as r: self.assertEqual(json.load(r)["version"],"0.1.66")
            with urlopen(url+"&channel=development") as r: self.assertEqual(json.load(r)["version"],"0.1.99")
            with self.assertRaises(HTTPError) as e: urlopen(url+"&channel=../../production")
            self.assertEqual(e.exception.code,400)
            with self.assertRaises(HTTPError): urlopen(url+"&channel=stable")
        finally: server.shutdown();server.server_close();thread.join()


if __name__=="__main__": unittest.main()
