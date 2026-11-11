// Run the real control-page functions with a fake desktop bridge and DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../static/control.html'), 'utf8');
function source(name) {
    const match = html.match(new RegExp('(?:async )?function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n        \\}'));
    assert.ok(match, 'page function exists: ' + name);
    return match[0];
}
function fixture(api = {setScrapeServicePanelConfig: async () => ({success: true})}) {
    const nodes = new Map();
    const listeners = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {
            textContent: '', className: '',
            addEventListener: (event, listener) => listeners.set(id + ':' + event, listener),
        });
        return nodes.get(id);
    };
    const input = {path: 'D:/portable/chrome.exe'};
    const context = vm.createContext({
        document: {getElementById: node},
        waitForApi: async () => api,
        getIntervalFromUI: () => 600,
        getHeadlessPollFromUI: () => true,
        getFetchAssistTasksFromUI: () => false,
        getBrowserExecutableFromUI: () => input.path,
        appAlert: async () => {},
        setStatusUI: () => {},
    });
    vm.runInContext([
        html.match(/let scrapeSaveQueue = [^;]+;/)[0],
        html.match(/let scrapeSaveGeneration = [^;]+;/)[0],
        'let lastSyncedInterval = 0;',
        ...['showScrapeSaveStatus', 'syncScrapePanelToBackend', 'renderBrowserStatus'].map(source),
    ].join('\n'), context);
    const clickStart = html.slice(
        html.indexOf("document.getElementById('btnStart').addEventListener('click'"),
        html.indexOf('        wireIntervalSteppers();'),
    );
    vm.runInContext(clickStart, context);
    return {context, node, input, listeners};
}

test('successful save displays confirmation', async () => {
    const {context, node} = fixture();
    assert.equal(await context.syncScrapePanelToBackend(), true);
    assert.match(node('cfgScrapeSaveStatus').textContent, /设置已保存/);
});

test('disk write rejection is shown and returns failure', async () => {
    const {context, node} = fixture({setScrapeServicePanelConfig: async () => {throw Error('file locked');}});
    assert.equal(await context.syncScrapePanelToBackend(), false);
    assert.match(node('cfgScrapeSaveStatus').textContent, /保存失败.*file locked/);
    assert.match(node('cfgScrapeSaveStatus').className, /rose/);
});

test('backend failure response is shown', async () => {
    const {context, node} = fixture({setScrapeServicePanelConfig: async () => ({success: false, message: 'no permission'})});
    assert.equal(await context.syncScrapePanelToBackend(), false);
    assert.match(node('cfgScrapeSaveStatus').textContent, /no permission/);
});

test('missing backend response is not treated as success', async () => {
    const {context} = fixture({setScrapeServicePanelConfig: async () => undefined});
    assert.equal(await context.syncScrapePanelToBackend(), false);
});

test('disconnected desktop bridge is shown', async () => {
    const {context, node} = fixture(null);
    assert.equal(await context.syncScrapePanelToBackend(), false);
    assert.match(node('cfgScrapeSaveStatus').textContent, /未连接到桌面端/);
});

test('bridge lookup exceptions are also shown', async () => {
    const {context, node} = fixture();
    context.waitForApi = async () => {throw Error('bridge unavailable');};
    assert.equal(await context.syncScrapePanelToBackend(), false);
    assert.match(node('cfgScrapeSaveStatus').textContent, /bridge unavailable/);
});

test('rapid edits save in order and retain the latest path', async () => {
    let release;
    const gate = new Promise(resolve => {release = resolve;});
    const paths = [];
    const {context, input, node} = fixture({setScrapeServicePanelConfig: async (...args) => {
        paths.push(args[3]);
        if (paths.length === 1) await gate;
        return {success: true};
    }});
    const first = context.syncScrapePanelToBackend();
    input.path = 'E:/new/chrome.exe';
    const second = context.syncScrapePanelToBackend();
    await new Promise(setImmediate);
    assert.deepEqual(paths, ['D:/portable/chrome.exe']);
    assert.match(node('cfgScrapeSaveStatus').textContent, /正在保存/);
    release();
    assert.deepEqual(await Promise.all([first, second]), [true, true]);
    assert.deepEqual(paths, ['D:/portable/chrome.exe', 'E:/new/chrome.exe']);
    assert.match(node('cfgScrapeSaveStatus').textContent, /设置已保存/);
});

test('save can recover after a failed request', async () => {
    let fail = true;
    const {context, node} = fixture({setScrapeServicePanelConfig: async () => {
        if (fail) throw Error('temporary failure');
        return {success: true};
    }});
    assert.equal(await context.syncScrapePanelToBackend(), false);
    fail = false;
    assert.equal(await context.syncScrapePanelToBackend(), true);
    assert.match(node('cfgScrapeSaveStatus').textContent, /设置已保存/);
});

test('start is blocked if the current form cannot be saved', async () => {
    let starts = 0;
    const {listeners} = fixture({
        setScrapeServicePanelConfig: async () => ({success: false, message: 'disk full'}),
        startService: async () => {starts++;},
    });
    await listeners.get('btnStart:click')();
    assert.equal(starts, 0);
});

test('start waits for the current form to finish saving', async () => {
    let release;
    let starts = 0;
    const gate = new Promise(resolve => {release = resolve;});
    const {listeners} = fixture({
        setScrapeServicePanelConfig: async () => {await gate; return {success: true};},
        startService: async () => {starts++; return {success: true};},
    });
    const pending = listeners.get('btnStart:click')();
    await new Promise(setImmediate);
    assert.equal(starts, 0);
    release();
    await pending;
    assert.equal(starts, 1);
});

test('API mode displays the authorization Chrome path', () => {
    const {context, node} = fixture();
    context.renderBrowserStatus({available: true, name: 'Google Chrome', path: 'D:/Chrome/chrome.exe'}, true);
    assert.match(node('runtimeBrowserName').textContent, /账户授权/);
    assert.equal(node('runtimeBrowserPath').textContent, 'D:/Chrome/chrome.exe');
    assert.equal(node('runtimeBrowserPhase').textContent, 'API 采集无需 Chrome；账户授权需要 Chrome');
});

test('missing Chrome stays visible in API mode and displays recovery instructions', () => {
    const {context, node} = fixture();
    context.renderBrowserStatus({available: false, message: '请打开 chrome://version'}, true);
    assert.match(node('runtimeBrowserName').textContent, /未检测到/);
    assert.match(node('runtimeBrowserName').className, /rose/);
    assert.match(node('runtimeBrowserMessage').textContent, /chrome:\/\/version/);
});

test('legacy mode retains its actual browser phase', () => {
    const {context, node} = fixture();
    context.renderBrowserStatus({available: true, name: 'Google Chrome', path: 'chrome.exe', phase: '后台无头运行'}, false);
    assert.equal(node('runtimeBrowserPhase').textContent, '后台无头运行');
});

test('all inline page scripts remain valid JavaScript', () => {
    for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
        new vm.Script(match[1]);
    }
});
