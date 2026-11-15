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
vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8'), context);
const demo = vm.runInNewContext('demoModel()', context);
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
assert.match(html, /id="integrationTitle"/);
assert.match(html, /id="syncDedupeHorizon"/);
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
const liveSnapshot = {
  ...demo.snapshot,
  nodes: demo.snapshot.nodes.map(node => ({
    ...node,
    online: true,
    age_s: 1,
    state: 'SAFE',
    latest: {
      ...node.latest,
      lpg: { ...node.latest.lpg, state: 'SAFE' },
      co: { ...node.latest.co, state: 'SAFE' }
    }
  }))
};
let failEvents = false;
let failHistory = false;
let failSnapshot = false;
let failSync = false;
const liveSyncStatus = { configured: true, pending: 7, last_success_at: Date.now() / 1000, last_error: null, dedupe_horizon_days: 365, token: 'do-not-display' };
const liveNotificationData = {
  status: { provider: 'mock', simulated: true, configured_recipients: 2, pending: 1, simulated_deliveries: 3, failed_or_dead: 1, acknowledgement_expiry_s: 60 },
  log: [{ attempted_at: Date.now() / 1000, provider: 'mock', status: 'simulated', recipient_ref: 'a1b2c3d4e5', recipient: 'private@example.test', detail: 'do-not-display-this-payload' }]
};
const liveContext = {
  ...context,
  location: { protocol: 'http:' },
  fetch: async url => failSnapshot && url.includes('api/snapshot')
    ? { ok: false, status: 503 }
    : failSync && url.includes('api/sync-status')
      ? { ok: false, status: 503 }
    : failEvents && url.includes('api/events')
      ? { ok: false, status: 503 }
      : failHistory && url.includes('api/history')
        ? { ok: false, status: 503 }
    : ({
    ok: true,
    json: async () => url.includes('snapshot') ? { ...liveSnapshot, broker: { connected: false } }
      : url.includes('sync-status') ? liveSyncStatus
      : url.includes('notifications') ? liveNotificationData
      : url.includes('events') ? demo.events : url.includes('incidents') ? [] : url.includes('layout') ? demo.layout : url.includes('evaluation') ? [] : demo.history.node2
    })
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8'), liveContext);
setImmediate(async () => {
  assert.equal(element('modeBadge').textContent, 'Máy chủ chạy · MQTT chưa kết nối');
  assert.match(element('zones').innerHTML, /Bình &amp; van/);
  assert.match(element('zones').innerHTML, /state-safe/);
  assert.match(element('signalChart').innerHTML, /polyline/);
  assert.match(element('zones').innerHTML, /15\.4/);
  assert.match(element('latestChartValue').textContent, /15\.4/);
  assert.equal(element('syncPending').textContent, '7');
  assert.match(element('syncStateText').textContent, /Đang chờ máy chủ xác nhận/);
  assert.match(element('syncDedupeHorizon').textContent, /365 ngày/);
  assert.equal(element('notifyPending').textContent, '1');
  assert.equal(element('notifySimulated').textContent, '3');
  assert.equal(element('notifyFailures').textContent, '1');
  assert.match(element('notificationDisclosure').textContent, /không gửi tin nhắn ra ngoài/);
  assert.match(element('notificationRows').innerHTML, /Đã ghi nhận \(giả lập\)/);
  assert.doesNotMatch(element('notificationRows').innerHTML, /a1b2c3d4e5|private@example\.test|do-not-display/);
  assert.doesNotMatch(element('syncStateText').textContent, /do-not-display/);

  vm.runInNewContext(`model.evaluation = [{device_id: 'node1', gas: 'LPG', samples: 3,
    excluded_invalid_samples: 1,
    algorithm: {precision: 1, recall: 1, fp: 0, fn: 0},
    fixed: {precision: 1, recall: 1}, episodes: null}];
    renderEvaluation(); renderEvaluationInvalidSummary();`, liveContext);
  assert.match(element('evaluationRows').innerHTML, /evaluation-invalid-summary/);
  assert.match(element('evaluationRows').innerHTML, /: 1/);

  failSync = true;
  await vm.runInNewContext('refreshLive()', liveContext);
  assert.equal(element('syncPending').textContent, '-');
  assert.match(element('syncStateText').textContent, /Không tải được trạng thái đồng bộ/);
  assert.equal(element('notifySimulated').textContent, '3');
  failSync = false;

  liveSnapshot.nodes[1].latest.lpg.signal_pct = 17.2;
  failEvents = true;
  await vm.runInNewContext('refreshLive()', liveContext);
  const partialSnapshot = vm.runInNewContext('model.snapshot', liveContext);
  assert.ok(partialSnapshot.nodes.every(node => node.online && node.state === 'SAFE'));
  assert.equal(partialSnapshot.nodes[1].latest.lpg.signal_pct, 17.2);
  assert.match(element('zones').innerHTML, /state-safe/);
  assert.match(element('zones').innerHTML, /17\.2/);
  assert.equal(vm.runInNewContext('model.events.length', liveContext), 0);
  assert.match(element('modeBadge').textContent, /dữ liệu phụ/);
  assert.match(element('summaryDetail').textContent, /Không tải được/);

  failEvents = false;
  liveSnapshot.nodes[1].latest.lpg.signal_pct = 18.6;
  failHistory = true;
  await vm.runInNewContext('refreshLive()', liveContext);
  const historyFailureSnapshot = vm.runInNewContext('model.snapshot', liveContext);
  assert.ok(historyFailureSnapshot.nodes.every(node => node.online && node.state === 'SAFE'));
  assert.equal(historyFailureSnapshot.nodes[1].latest.lpg.signal_pct, 18.6);
  assert.match(element('zones').innerHTML, /state-safe/);
  assert.match(element('zones').innerHTML, /18\.6/);
  const selectedHistory = vm.runInNewContext('model.history.node2', liveContext);
  assert.equal(selectedHistory.length, 0);
  assert.doesNotMatch(element('latestChartValue').textContent, /\d/);
  assert.doesNotMatch(element('tempCurrent').textContent, /\d/);
  assert.doesNotMatch(element('humidityCurrent').textContent, /\d/);

  failHistory = false;
  failSnapshot = true;
  await vm.runInNewContext('refreshLive()', liveContext);
  const failedSnapshot = vm.runInNewContext('model.snapshot', liveContext);
  assert.ok(failedSnapshot.nodes.every(node => !node.online && node.state === 'OFFLINE'));
  assert.ok(failedSnapshot.nodes.every(node => node.latest === null && node.age_s === null));
  const failedHistory = vm.runInNewContext('model.history', liveContext);
  assert.ok(Object.values(failedHistory).every(rows => rows.length === 0));
  assert.match(element('zones').innerHTML, /state-offline/);
  assert.doesNotMatch(element('zones').innerHTML, /18\.6/);
  assert.doesNotMatch(element('latestChartValue').textContent, /\d/);
  assert.doesNotMatch(element('tempCurrent').textContent, /\d/);
  assert.doesNotMatch(element('humidityCurrent').textContent, /\d/);
  assert.match(element('modeBadge').textContent, /web/);
  assert.equal(element('syncPending').textContent, '-');
  assert.equal(element('notifySimulated').textContent, '-');
  assert.match(element('notificationDisclosure').textContent, /Thông báo đang tắt; không gửi tin nhắn ra ngoài/);
  console.log('Frontend preview, partial-data handling, and snapshot-failure clearing: OK');
});
