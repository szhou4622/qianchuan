"""Exercise the actual license bridge/page with mock authorization and runtime.

No GUI, credentials, business database, background service or API is started.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from gui_app import JSApi


class LicensedEntryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = JSApi.__new__(JSApi)
        self.bridge.license_manager = Mock()
        self.bridge.license_manager.is_runtime_authorized.return_value = True
        self.bridge.api = Mock()
        self.bridge.api.activate_license_runtime_identity.return_value = {"success": True}
        self.bridge._licensed_runtime_lock = threading.Lock()
        self.bridge._licensed_runtime_started = False
        self.bridge._start_license_watchdog = Mock()
        self.state = patch("gui_app.startup_state", return_value={"terminal": False})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.runtime_patch = patch("gui_app.RUNTIME_SUPERVISOR")
        self.runtime = self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    def test_unauthorized_cannot_prepare_identity_or_start_runtime(self):
        self.bridge.license_manager.is_runtime_authorized.return_value = False
        result = self.bridge.enterLicensedApplication()
        self.assertFalse(result["success"])
        self.assertFalse(result["authorized"])
        self.bridge.api.activate_license_runtime_identity.assert_not_called()
        self.runtime.start.assert_not_called()
        self.bridge._start_license_watchdog.assert_not_called()

    def test_terminal_window_cannot_start_runtime(self):
        with patch("gui_app.startup_state", return_value={"terminal": True}):
            result = self.bridge.enterLicensedApplication()
        self.assertFalse(result["success"])
        self.runtime.start.assert_not_called()
        self.bridge.api.activate_license_runtime_identity.assert_not_called()

    def test_identity_failure_does_not_revoke_valid_license(self):
        self.bridge.api.activate_license_runtime_identity.return_value = {"success": False, "message": "本机身份初始化失败"}
        result = self.bridge.enterLicensedApplication()
        self.assertFalse(result["success"])
        self.assertTrue(result["authorized"])
        self.assertFalse(self.bridge._licensed_runtime_started)
        self.runtime.start.assert_not_called()
        self.bridge.license_manager.unbind_current_device.assert_not_called()

    def test_missing_runtime_module_is_not_an_activation_failure(self):
        self.runtime.start.side_effect = ModuleNotFoundError("No module named 'utils.sqlite_prune_scheduler'")
        result = self.bridge.enterLicensedApplication()
        self.assertFalse(result["success"])
        self.assertTrue(result["authorized"])
        self.assertEqual("runtime_start_failed", result["error"])
        self.assertFalse(self.bridge._licensed_runtime_started)
        self.assertNotIn("激活失败", result["message"])
        self.assertNotIn("激活码无效", result["message"])
        self.bridge._start_license_watchdog.assert_not_called()
        self.bridge.license_manager.activate.assert_not_called()
        self.bridge.license_manager.unbind_current_device.assert_not_called()
        self.bridge.api.clear_license_cloud_sessions.assert_not_called()

    def test_failed_start_can_retry_and_success_does_not_double_start(self):
        self.runtime.start.side_effect = [RuntimeError("local startup failed"), None]
        first = self.bridge.enterLicensedApplication()
        self.assertFalse(first["success"])
        self.bridge._start_license_watchdog.assert_not_called()
        second = self.bridge.enterLicensedApplication()
        self.assertTrue(second["success"])
        self.assertTrue(second["authorized"])
        self.assertTrue(self.bridge._licensed_runtime_started)
        self.bridge._start_license_watchdog.assert_called_once()
        third = self.bridge.enterLicensedApplication()
        self.assertTrue(third["success"])
        self.assertEqual(2, self.runtime.start.call_count)


@unittest.skipUnless(shutil.which("node"), "Node.js required for actual license-page execution")
class LicensedEntryPageTests(unittest.TestCase):
    def run_page(self, body):
        html = (Path(__file__).resolve().parents[1] / "static" / "license.html").read_text(encoding="utf-8")
        code = r"""
            const fs=require('fs'),vm=require('vm'),assert=require('assert');
            const scripts=JSON.parse(fs.readFileSync(0,'utf8')),nodes=new Map(),navigations=[];
            function element(){return {hidden:false,disabled:false,value:'',textContent:'',innerHTML:'',style:{},events:{},
              classList:{add(){},remove(){},contains(){return false;}},addEventListener(name,fn){this.events[name]=fn;},
              focus(){},appendChild(){},setAttribute(){}};}
            const document={getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},createElement:element};
            const window={location:{replace:url=>navigations.push(url)},addEventListener(){}};
            const context=vm.createContext({window,document,console,setInterval(){throw Error('unexpected timer');},clearInterval(){},fetch(){throw Error('unexpected network');}});
            for(const script of scripts)vm.runInContext(script,context);
            function setApi(api){context.mockApi=api;vm.runInContext('api=mockApi',context);}
            const valid={success:true,authorized:true,message:'软件授权有效',license:{is_permanent:true,license_type_label:'永久授权'}};
            (async()=>{ BODY })().catch(error=>{console.error(error);process.exitCode=1;});
        """.replace("BODY", body)
        result = subprocess.run([shutil.which("node"), "-e", code],
            input=json.dumps(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)),
            text=True, encoding="utf-8", capture_output=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_authorized_runtime_failure_stays_on_license_page_and_can_retry(self):
        self.run_page("""
            let calls=0,activationCalls=0;
            setApi({getLicenseBootstrapStatus:async()=>valid,activateOnlineLicense:async()=>{activationCalls++;return valid;},
              enterLicensedApplication:async()=>++calls===1?{success:false,authorized:true,error:'runtime_start_failed',message:'后台服务启动失败，请重试'}:{success:true,authorized:true}});
            await context.checkAuthorization();
            assert.strictEqual(navigations.length,0);
            assert.match(nodes.get('statusText').textContent,/授权(?:有效|已通过)/);
            assert.ok(nodes.get('statusText').textContent.includes('启动'));
            assert.ok(!nodes.get('statusText').textContent.includes('激活失败'));
            assert.strictEqual(nodes.get('activationForm').hidden,true);
            assert.strictEqual(nodes.get('runtimeEntryPanel').hidden,false);
            assert.strictEqual(nodes.get('repairConnectionButton').hidden,true);
            assert.strictEqual(nodes.get('retryEntryButton').disabled,false);
            await nodes.get('retryEntryButton').events.click();
            assert.deepStrictEqual(navigations,['index.html']);
            assert.strictEqual(activationCalls,0);
        """)

    def test_successful_activation_followed_by_runtime_failure_does_not_claim_bad_code(self):
        self.run_page("""
            let entries=0;
            setApi({activateOnlineLicense:async code=>{assert.strictEqual(code,'mock-code');return valid;},
              enterLicensedApplication:async()=>{entries++;return {success:false,authorized:true,error:'runtime_start_failed',message:'后台服务启动失败'};}});
            nodes.get('activationCode').value='mock-code';
            await nodes.get('activationForm').events.submit({preventDefault(){}});
            assert.strictEqual(entries,1);assert.strictEqual(navigations.length,0);
            assert.match(nodes.get('statusText').textContent,/授权(?:有效|已通过)/);
            assert.ok(!nodes.get('statusText').textContent.includes('激活失败'));
            assert.strictEqual(nodes.get('activationCode').value,'');
            assert.strictEqual(nodes.get('activationForm').hidden,true);
            assert.strictEqual(nodes.get('retryEntryButton').disabled,false);
        """)

    def test_unauthorized_status_never_calls_entry_or_navigates(self):
        self.run_page("""
            let entries=0;
            setApi({getLicenseBootstrapStatus:async()=>({success:true,authorized:false,message:'需要授权'}),
              enterLicensedApplication:async()=>{entries++;return {success:true};}});
            await context.checkAuthorization();
            assert.strictEqual(entries,0);assert.strictEqual(navigations.length,0);
            assert.strictEqual(nodes.get('activationForm').hidden,false);
        """)

    def test_explicit_authorization_revocation_is_not_runtime_retry(self):
        self.run_page("""
            setApi({getLicenseBootstrapStatus:async()=>valid,
              enterLicensedApplication:async()=>({success:false,authorized:false,message:'软件授权已失效'})});
            await context.checkAuthorization();
            assert.strictEqual(navigations.length,0);
            assert.strictEqual(nodes.get('activationForm').hidden,false);
            assert.strictEqual(nodes.get('runtimeEntryPanel').hidden,true);
            assert.strictEqual(nodes.get('repairConnectionButton').hidden,false);
        """)

    def test_bridge_exception_after_valid_license_keeps_runtime_retry_separate(self):
        self.run_page("""
            setApi({getLicenseBootstrapStatus:async()=>valid,
              enterLicensedApplication:async()=>{throw Error('local runtime module unavailable');}});
            await context.checkAuthorization();
            assert.strictEqual(navigations.length,0);
            assert.strictEqual(nodes.get('activationForm').hidden,true);
            assert.strictEqual(nodes.get('runtimeEntryPanel').hidden,false);
            assert.strictEqual(nodes.get('retryEntryButton').disabled,false);
            assert.ok(!nodes.get('statusText').textContent.includes('激活失败'));
        """)


if __name__ == '__main__':
    unittest.main()
