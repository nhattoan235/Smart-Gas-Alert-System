const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: '', innerHTML: '', className: '', style: {}, value: id === 'deviceSelect' ? 'node2' : '',
    attributes: {}, listeners: {},
    classList: { remove() {}, toggle() {} },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; }
  });
  return elements.get(id);
}
const context = {
  document: { getElementById: element },
  location: { protocol: 'file:' },
  setInterval() {},
  Date, Math, Number, String, Set, console
};
vm.runInNewContext(fs.readFileSync('app.js', 'utf8'), context);
const demo = vm.runInNewContext('demoModel()', context);
const html = fs.readFileSync('index.html', 'utf8');
for (const [, asset] of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
  assert.ok(fs.existsSync(path.join(__dirname, asset)), `Missing asset: ${asset}`);
}
assert.match(element('modeBadge').textContent, /dữ liệu minh họa/);
assert.match(element('zones').innerHTML, /Bình &amp; van/);
assert.match(element('zones').innerHTML, /LPG/);
assert.match(element('zones').innerHTML, /CO/);
assert.match(element('zones').innerHTML, /15\.4/);
assert.match(element('signalChart').innerHTML, /polyline/);
assert.match(element('latestChartValue').textContent, /LPG .*CO/);
assert.match(element('environmentChart').innerHTML, /Nhiệt độ và độ ẩm/);
assert.match(element('eventRows').innerHTML, /Còi và LED đỏ/);
assert.match(element('eventRows').innerHTML, /log-row-danger/);
assert.match(element('eventRows').innerHTML, /CO: Cảnh báo/);
assert.match(element('scenarioDistance').textContent, /3\.28 m/);
element('filterAbnormal').listeners.click();
assert.doesNotMatch(element('eventRows').innerHTML, /log-row-safe/);

vm.runInNewContext("model.snapshot.nodes[0].state = 'SAFE'; model.snapshot.nodes.slice(1).forEach(node => { node.online = false; node.state = 'OFFLINE'; }); renderOverview()", context);
assert.equal(element('overall').textContent, 'Giám sát một phần');
assert.match(element('summaryDetail').textContent, /1\/3 bo trực tuyến/);
vm.runInNewContext("model.snapshot.nodes[0].state = 'DANGER'; renderOverview()", context);
assert.equal(element('overall').textContent, 'Nguy hiểm');

elements.clear();
const liveContext = {
  ...context,
  location: { protocol: 'http:' },
  fetch: async url => ({
    ok: true,
    json: async () => url.includes('snapshot') ? { ...demo.snapshot, broker: { connected: false } }
      : url.includes('events') ? demo.events : url.includes('incidents') ? [] : url.includes('layout') ? demo.layout : url.includes('evaluation') ? [] : demo.history.node2
  })
};
vm.runInNewContext(fs.readFileSync('app.js', 'utf8'), liveContext);
setImmediate(() => {
  assert.equal(element('modeBadge').textContent, 'Máy chủ chạy · MQTT chưa kết nối');
  assert.match(element('zones').innerHTML, /Bình &amp; van/);
  assert.match(element('signalChart').innerHTML, /polyline/);
  console.log('Frontend preview, live mode, and abnormal-log filter: OK');
});
