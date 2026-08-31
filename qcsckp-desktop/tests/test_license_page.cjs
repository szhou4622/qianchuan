// Headless activation-page behavior test: no license or network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../static/license.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function page(bridge) {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            handlers: {}, disabled: false, hidden: false, style: {}, value: '',
            textContent: '', className: '', classList: { contains: () => false },
            addEventListener(event, fn) { this.handlers[event] = fn; },
            focus() {}, appendChild() {},
        });
        return elements.get(id);
    };
    const navigations = [];
    const context = vm.createContext({
        document: { getElementById: element, createElement: () => element('created') },
        window: { addEventListener() {}, location: { replace: u => navigations.push(u) } },
        setInterval, clearInterval, console,
    });
    vm.runInContext(script, context);
    context.bridge = bridge;
    vm.runInContext('api = bridge', context);
    return { element, navigations };
}

(async () => {
    let resolveRepair;
    let calls = 0;
    let checks = 0;
    const ui = page({
        diagnoseLicenseConnection: () => { calls++; return new Promise(resolve => { resolveRepair = resolve; }); },
        getLicenseBootstrapStatus: async () => { checks++; return { authorized: false, network_error: false, message: '请输入激活码' }; },
        enterLicensedApplication: () => { throw new Error('must not enter without license'); },
    });
    const button = ui.element('repairConnectionButton');
    const pending = button.handlers.click();
    assert.equal(button.disabled, true);
    await button.handlers.click();
    assert.equal(calls, 1);
    resolveRepair({ success: true, message: '连接恢复', steps: [{ mode: 'windows_https', message: '<script>not HTML</script>' }] });
    await pending;
    assert.equal(checks, 1);
    assert.equal(button.disabled, false);
    assert.deepEqual(ui.navigations, []);
    assert.equal(ui.element('activationForm').hidden, false);
    assert.ok(ui.element('connectionReport').textContent.includes('<script>not HTML</script>'));

    let entries = 0;
    const active = page({
        diagnoseLicenseConnection: async () => ({ success: true, steps: [] }),
        getLicenseBootstrapStatus: async () => ({ authorized: true }),
        enterLicensedApplication: async () => { entries++; return { success: true }; },
    });
    await active.element('repairConnectionButton').handlers.click();
    assert.equal(entries, 1);
    assert.deepEqual(active.navigations, ['index.html']);

    const offline = page({
        diagnoseLicenseConnection: async () => ({ success: false, steps: [], message: '连接仍失败' }),
        getLicenseBootstrapStatus: () => { throw new Error('must not retry license after failed probe'); },
    });
    await offline.element('repairConnectionButton').handlers.click();
    assert.equal(offline.element('repairConnectionButton').disabled, false);
    assert.equal(offline.element('statusText').textContent, '连接仍失败');
    assert.deepEqual(offline.navigations, []);
    console.log('PASS: double-click protection, pre-login repair, server-gated entry, failure recovery, text-only diagnostics');
})().catch(error => { console.error(error); process.exitCode = 1; });
