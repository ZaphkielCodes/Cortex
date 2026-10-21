const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../hub/static');
function load(name) {
    const context = vm.createContext({ setTimeout, clearTimeout });
    vm.runInContext(fs.readFileSync(path.join(root, name), 'utf8'), context);
    return context;
}
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
}
function flatten(node) { return node.textContent + node.children.map(flatten).join('\n'); }
test('canonical multi-account data renders names scopes and failures as text', () => {
    const ctx = load('integrations.js'), list = new Element('div');
    const document = { createElement: tag => new Element(tag) };
    ctx.CortexIntegrations.render(list, [{ name: 'Google Gmail', key: 'gmail', auth_state: 'AUTHORIZED',
        health: { state: 'healthy' }, accounts: [
            { account_id: 'school', identity: { email: '<img src=x onerror=alert(1)>' }, scopes: ['scope.read'], auth_state: 'AUTHORIZED' },
            { account_id: 'personal', scopes: [], auth_state: 'REVOKED' }],
        capabilities: [{ id: 'email.send', available: false, reason: 'SCOPE_MISSING' }],
        recent_actions: [{ capability: 'email.read', status: 'failure', error_category: 'AUTH_REQUIRED' }] }], document);
    const text = flatten(list);
    for (const value of ['Google Gmail', 'personal', 'REVOKED', 'scope.read', 'SCOPE_MISSING', 'AUTH_REQUIRED', '<img']) assert.ok(text.includes(value));
    assert.ok(!text.includes('undefined'));
    assert.equal(list.children[0].children.find(c => c.tag === 'a').href, '/integrations/gmail/connect?account_id=school');
    assert.ok(!list.children[0].children.some(c => c.tag === 'img'));
});
test('unconfigured providers render truthfully without inventing an account', () => {
    const ctx = load('integrations.js'), list = new Element('div');
    ctx.CortexIntegrations.render(list, [{ name: 'Google Places', auth_state: 'UNCONFIGURED', accounts: [] }], { createElement: tag => new Element(tag) });
    assert.match(flatten(list), /UNCONFIGURED/);
    assert.match(flatten(list), /No authorized account/);
    assert.ok(!flatten(list).includes('undefined'));
});
function connection(valid = true) {
    const ctx = load('dashboard-connection.js'), sockets = [], timers = [], events = [];
    class WS {
        constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); }
        close() { this.closed = true; }
        send(value) { events.push(JSON.parse(value)); }
    }
    const api = ctx.CortexDashboardConnection.create({ WebSocket: WS, url: 'ws://localhost/ws-control',
        checkSession: async () => valid, onEvent: e => events.push(e), onConnected() {},
        onAuthRequired: () => events.push('login'), setTimer: (fn, delay) => { timers.push({ fn, delay }); return timers.length; }, clearTimer() {} });
    api.start(); return { api, sockets, timers, events };
}
test('refresh uses session-authenticated control channel without a secret challenge', () => {
    const c = connection();
    c.sockets[0].onopen();
    c.api.send({ type: 'resolve_approval', command_id: 'fixture', decision: 'deny' });
    assert.equal(c.sockets[0].url, 'ws://localhost/ws-control');
    assert.equal(c.events[0].type, 'resolve_approval');
    assert.ok(!JSON.stringify(c.events).includes('secret'));
});
test('authentication close stops reconnection', async () => {
    const c = connection();
    await c.sockets[0].onclose({ code: 4401 });
    assert.deepEqual(c.events, ['login']);
    assert.equal(c.timers.length, 0);
});
test('failed handshake with expired cookie stops rather than loops', async () => {
    const c = connection(false);
    await c.sockets[0].onclose({ code: 1006 });
    assert.deepEqual(c.events, ['login']);
    assert.equal(c.timers.length, 0);
});
test('transient failures back off and stale connections cannot update the UI', async () => {
    const c = connection();
    const old = c.sockets[0];
    await old.onclose({ code: 1006 });
    assert.equal(c.timers[0].delay, 1000);
    c.timers[0].fn();
    await c.sockets[1].onclose({ code: 1006 });
    assert.equal(c.timers[1].delay, 2000);
    old.onmessage({ data: '{"event":"stale"}' });
    assert.deepEqual(c.events, []);
    c.api.stop();
    c.timers[1].fn();
    assert.equal(c.sockets.length, 2);
});

test('cookie auto-login restores the integration view after navigation is initialized', () => {
    function element(id) {
        const classes = new Set(id.startsWith('view-') && id !== 'view-devices' ? ['hidden'] : []);
        const handlers = {};
        return { id, value: '', dataset: {}, classList: {
            add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value)
        }, addEventListener: (type, fn) => { handlers[type] = fn; }, click: () => handlers.click?.() };
    }
    const nodes = new Map(), microtasks = [];
    const get = id => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
    const views = [get('view-devices'), get('view-integrations')];
    const tabs = [element('devices'), element('integrations')];
    tabs.forEach(tab => { tab.dataset.view = tab.id; });
    const context = vm.createContext({
        document: { getElementById: get, querySelectorAll: query => query === '.view' ? views : tabs,
            querySelector: () => tabs[1] },
        sessionStorage: { getItem: () => 'asus', setItem() {}, removeItem() {} },
        fetch: () => new Promise(() => {}), WebSocket: class {}, queueMicrotask: fn => microtasks.push(fn),
        window: { location: { hash: '#integrations', protocol: 'http:', host: 'localhost' },
            CortexDashboardConnection: { create: () => ({ start() {}, stop() {} }) } }
    });
    vm.runInContext(fs.readFileSync(path.join(root, 'app.js'), 'utf8'), context);
    microtasks.forEach(fn => fn());
    assert.equal(get('view-integrations').classList.contains('hidden'), false);
    assert.equal(get('view-devices').classList.contains('hidden'), true);
});
