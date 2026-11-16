const $ = id => document.getElementById(id);
const names = { node1: 'Bo 1 · Bình & van', node2: 'Bo 2 · Tuyến ống', node3: 'Bo 3 · Khu bếp' };
const stateNames = {
  WARMUP: 'Khởi động',
  SAFE: 'Bình thường', WATCH: 'Theo dõi', WARNING: 'Cảnh báo',
  DANGER: 'Nguy hiểm', FAULT: 'Lỗi đầu đo', OFFLINE: 'Mất kết nối',
  PARTIAL: 'Giám sát một phần'
};
const actions = {
  WARMUP: 'Đèn vàng; chờ tín hiệu ổn định',
  SAFE: 'LED xanh; tiếp tục đo',
  WATCH: 'LED vàng; tiếp tục theo dõi',
  WARNING: 'Còi nhịp và LED vàng',
  DANGER: 'Còi và LED đỏ; ACK không tắt còi',
  FAULT: 'Còi và LED đỏ; báo lỗi đầu đo',
  OFFLINE: 'Web báo mất kết nối; cảnh báo tại nút vẫn độc lập'
};
const recordedActions = {
  MUTE_30S: 'Đã nhấn ACK: tạm tắt còi 30 giây',
  ACK_DENIED_DANGER: 'Đã nhấn ACK: còi DANGER vẫn bật',
  INSPECTION_CONFIRMED: 'Đã nhấn INSPECTED tại bo'
};
const abnormal = new Set(['WATCH', 'WARNING', 'DANGER', 'FAULT', 'OFFLINE']);
let model = { snapshot: null, history: [], events: [], incidents: [], layout: null, evaluation: [], syncStatus: null, notificationData: null };
let filterAbnormal = false;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}
function n1(value) { return Number.isFinite(Number(value)) ? Number(value).toFixed(1) : '—'; }
function stamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('vi-VN', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}
function shortTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('vi-VN', {
    hour: '2-digit', minute: '2-digit'
  });
}
function badge(state) {
  const key = stateNames[state] ? state : 'OFFLINE';
  return `<span class="state-badge ${key.toLowerCase()}">${stateNames[key]}</span>`;
}

function demoModel() {
  const now = Date.now();
  const channel = (signal_pct, state = 'SAFE', rise_pct = 0) => ({
    signal_pct, state, rise_pct, baseline_pct: Math.max(0, signal_pct - rise_pct), valid: true
  });
  const nodes = [
    { device_id: 'node1', name: names.node1, zone: 'Khu bình gas', online: true, age_s: 1, state: 'SAFE', lpg_distance_to_ignition_m: null, latest: { lpg: channel(4.8), co: channel(3.1), temperature_c: 28.2, humidity_pct: 61, valve_open_sim: true, stove_on_sim: false } },
    { device_id: 'node2', name: names.node2, zone: 'Dọc tuyến ống', online: true, age_s: 2, state: 'WATCH', lpg_distance_to_ignition_m: null, latest: { lpg: channel(15.4, 'WATCH', 10.8), co: channel(4.2), temperature_c: 29.1, humidity_pct: 64, valve_open_sim: true, stove_on_sim: true } },
    { device_id: 'node3', name: names.node3, zone: 'Khu bếp và sinh hoạt', online: true, age_s: 2, state: 'WARNING', lpg_distance_to_ignition_m: null, latest: { lpg: channel(5.1), co: channel(34.2, 'WARNING', 28.6), temperature_c: 28.7, humidity_pct: 62, valve_open_sim: true, stove_on_sim: true } }
  ];
  const history = {};
  for (const node of nodes) {
    history[node.device_id] = Array.from({ length: 36 }, (_, i) => {
      let lpg = node.device_id === 'node2'
        ? (i < 14 ? 5 + i * .38 : i < 23 ? 10 + (i - 14) * 4.7 : 67 - (i - 23) * 4.3)
        : 4.5 + Math.sin(i / 3) * .8;
      let co = node.device_id === 'node3' ? 5 + i * .83 : 4 + Math.sin(i / 4) * .6;
      if (i === 35) { lpg = node.latest.lpg.signal_pct; co = node.latest.co.signal_pct; }
      return {
        time: new Date(now - (35 - i) * 2 * 60_000).toISOString(),
        lpg: channel(Math.max(0, Math.min(100, lpg))), co: channel(Math.max(0, Math.min(100, co))),
        temperature_c: i === 35 ? node.latest.temperature_c : 27.5 + (node.device_id === 'node2' ? 1.4 : .5) + Math.sin(i / 7) * .8,
        humidity_pct: i === 35 ? node.latest.humidity_pct : 60 + Math.sin(i / 5) * 4
      };
    });
  }
  const event = (minutes, device_id, changed_gas, state, signal_pct, rise_pct) => ({
    time: new Date(now - minutes * 60_000).toISOString(), device_id, changed_gas, state,
    lpg: channel(changed_gas === 'LPG' ? signal_pct : 4.5, changed_gas === 'LPG' ? state : 'SAFE', changed_gas === 'LPG' ? rise_pct : 0),
    co: channel(changed_gas === 'CO' ? signal_pct : 4, changed_gas === 'CO' ? state : 'SAFE', changed_gas === 'CO' ? rise_pct : 0)
  });
  return {
    snapshot: { home_id: 'demo-home', server_time: new Date(now).toISOString(), nodes },
    incidents: [],
    evaluation: [],
    layout: { illustrative_only: true, ignition: { name: 'Bếp', x_m: 3.4, y_m: .8 }, leak_candidates: {
      valve: { name: 'Van', x_m: .2, y_m: .1 }, pipe: { name: 'Ống', x_m: 1.8, y_m: .1 }, stove: { name: 'Gần bếp', x_m: 3.1, y_m: .2 }
    } },
    history,
    events: [
      event(3, 'node2', 'LPG', 'WATCH', 15.4, 10.8),
      event(27, 'node3', 'CO', 'WARNING', 34.2, 28.6),
      event(41, 'node2', 'LPG', 'DANGER', 67.0, 62.4),
      event(58, 'node1', 'LPG', 'SAFE', 4.8, .4),
      event(64, 'node3', 'CO', 'FAULT', 5.2, .3)
    ]
  };
}

function renderOverview() {
  const { snapshot, events } = model;
  const nodes = snapshot.nodes;
  const online = nodes.filter(node => node.online).length;
  const active = nodes.filter(node => ['WATCH', 'WARNING', 'DANGER', 'FAULT'].includes(node.state)).length;
  const max = Math.max(...nodes.filter(node => node.online && node.latest).flatMap(node =>
    [Number(node.latest.lpg?.signal_pct), Number(node.latest.co?.signal_pct)]), -1);
  const priority = { DANGER: 6, FAULT: 5, WARNING: 4, WATCH: 3, OFFLINE: 2, SAFE: 1, WARMUP: 0 };
  const worst = nodes.filter(node => node.online)
    .sort((a, b) => priority[b.state] - priority[a.state])[0]?.state ?? 'OFFLINE';
  const state = online === 0 ? 'OFFLINE' : worst !== 'SAFE' ? worst : online < nodes.length ? 'PARTIAL' : 'SAFE';
  $('home').textContent = snapshot.home_id;
  $('updatedAt').textContent = `Cập nhật: ${stamp(snapshot.server_time)}`;
  $('overall').textContent = online === 0 && nodes.every(node => !node.latest) ? 'Chờ dữ liệu' : stateNames[state];
  $('overallCard').className = `card status-panel h-100 state-${state.toLowerCase()}`;
  $('overallDot').style.background = ({ SAFE: '#a5edbd', PARTIAL: '#ffd475', WARMUP: '#ffd475', WATCH: '#ffd475', WARNING: '#ffd475', DANGER: '#ffc0c5', FAULT: '#ffc0c5' })[state] ?? '#ced9e5';
  $('summaryDetail').textContent = online === 0
    ? 'Chưa nhận telemetry mới. Kiểm tra Wokwi, broker và kết nối mạng.'
    : state === 'PARTIAL' ? `${online}/${nodes.length} bo trực tuyến. Các bo đang chạy bình thường; hãy khởi động các bo còn lại để giám sát đủ vị trí.`
    : state === 'SAFE' ? 'Các nút trực tuyến đang ở trạng thái bình thường.'
      : `${online < nodes.length ? `${online}/${nodes.length} bo trực tuyến. ` : ''}Xem điểm đo và nhật ký bên dưới để biết vị trí, mức độ và hành động.`;
  $('onlineCount').innerHTML = `${online}<span>/${nodes.length}</span>`;
  $('attentionCount').textContent = String(active);
  $('maxSignal').textContent = max >= 0 ? `${max.toFixed(1)}%` : '—';
  $('eventCount').textContent = String(events.length);
}

function renderZones() {
  $('zones').innerHTML = model.snapshot.nodes.map(node => {
    const data = node.latest ?? {};
    const state = node.state;
    const current = node.online && data.lpg && data.co;
    const distance = node.lpg_distance_to_ignition_m == null
      ? 'Khoảng cách đầu LPG đến nguồn cháy: chưa khảo sát'
      : `Đầu LPG cách nguồn cháy ≈ ${n1(node.lpg_distance_to_ignition_m)} m`;
    const switchText = node.online && 'valve_open_sim' in data
      ? `Công tắc giả lập: van ${data.valve_open_sim ? 'mở' : 'đóng'}, bếp ${data.stove_on_sim ? 'bật' : 'tắt'}`
      : 'Chưa có dữ liệu công tắc';
    const flameText = ['f1_flame', 'f2_flame', 'burner1_gas_state', 'f2_state'].some(key => key in data)
      ? `F1 ${data.f1_flame == null ? 'chưa rõ' : data.f1_flame ? 'phát hiện lửa' : 'không phát hiện'} · F2 ${data.f2_flame == null ? 'chưa rõ' : data.f2_flame ? 'có lửa' : 'không có lửa'} · gas đầu đốt 1 ${data.burner1_gas_state == null ? 'chưa rõ' : data.burner1_gas_state ? 'đang cấp' : 'đã ngắt'}${data.f2_state ? ` · ${escapeHtml(data.f2_state)}` : ''}`
      : '';
    const gasRow = (key, label) => {
      const gas = current ? data[key] : null;
      const width = gas ? Math.max(0, Math.min(100, Number(gas.signal_pct))) : 0;
      return `<div class="gas-row ${key}"><div class="d-flex justify-content-between align-items-center gap-2"><span class="gas-label">${label}</span>${gas ? badge(gas.state) : badge('OFFLINE')}</div>
        <div class="d-flex align-items-baseline gap-1 mt-1"><strong class="gas-value">${gas ? n1(gas.signal_pct) : '—'}</strong><span class="gas-unit">${gas ? '% tín hiệu' : ''}</span></div>
        <div class="signal-track mt-1" role="img" aria-label="Tín hiệu ${label}: ${gas ? n1(gas.signal_pct) + ' phần trăm' : 'chưa có dữ liệu'}"><div class="signal-fill" style="width:${width}%"></div></div></div>`;
    };
    return `<div class="col-12 col-md-6 col-xl-4"><article class="card zone-card state-${state.toLowerCase()} h-100">
      <div class="zone-accent"></div><div class="card-body p-4">
        <div class="d-flex justify-content-between align-items-start gap-2">
          <div><div class="zone-type">LPG + CO · ${escapeHtml(node.zone)}</div><h3 class="zone-name mt-1 mb-0">${escapeHtml(node.name)}</h3></div>${badge(state)}
        </div>
        <div class="gas-grid mt-4">${gasRow('lpg', 'LPG')}${gasRow('co', 'CO')}</div>
        <div class="zone-info mt-4"><div><div class="info-label">Nhiệt độ</div><strong class="info-value">${current ? `${n1(data.temperature_c)} °C` : '—'}</strong></div><div><div class="info-label">Độ ẩm</div><strong class="info-value">${current ? `${n1(data.humidity_pct)} %RH` : '—'}</strong></div></div>
        <div class="zone-foot"><div>${node.online ? `Mẫu mới ${Math.round(node.age_s ?? 0)} giây trước` : 'Không có dữ liệu mới từ nút'}</div><div>${escapeHtml(distance)}</div><div>${escapeHtml(switchText)}</div>${flameText ? `<div>${flameText}</div>` : ''}${Number.isFinite(data.lpg_truth_sim_pct) ? `<div>Kịch bản: nguồn LPG ${n1(data.lpg_truth_sim_pct)}%, núm nhiễu ${Number(data.interference_sim_pct)>=0?'+':''}${n1(data.interference_sim_pct)}%, môi trường ${Number(data.environment_bias_sim_pct)>=0?'+':''}${n1(data.environment_bias_sim_pct)}%</div>` : ''}${typeof data.lpg_leak_truth_sim === 'boolean' ? `<div>Nhãn kiểm thử: LPG ${data.lpg_leak_truth_sim ? 'có rò' : 'không rò'}, CO ${data.co_hazard_truth_sim ? 'có nguy cơ' : 'không nguy cơ'}</div>` : ''}${node.valve_reminder ? '<div class="text-warning-emphasis fw-bold">Nhắc kiểm tra: van giả lập đang mở, bếp tắt trên 2 phút.</div>' : ''}${data.inspection_required ? '<div class="text-danger fw-bold">Cần kiểm tra hiện trường rồi nhấn INSPECTED tại bo.</div>' : ''}${Number(data.lost_events)>0 ? `<div class="text-danger fw-bold">Đã mất ${Number(data.lost_events)} sự kiện khi bộ đệm đầy.</div>` : ''}</div>
      </div></article></div>`;
  }).join('');
}

function svgLine(points, color, width = 3) {
  return `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
}
function renderSignalChart(rows) {
  const svg = $('signalChart');
  const left = 45, right = 714, top = 16, bottom = 225;
  const y = value => bottom - Math.max(0, Math.min(100, Number(value))) * (bottom - top) / 100;
  let markup = '<title>Tín hiệu khí theo thời gian, đơn vị phần trăm tín hiệu mô phỏng</title>';
  for (const tick of [0, 25, 50, 75, 100]) {
    markup += `<line x1="${left}" y1="${y(tick)}" x2="${right}" y2="${y(tick)}" stroke="#dbe3eb" stroke-width="1"/><text x="3" y="${y(tick) + 4}" fill="#52677d" font-size="12">${tick}%</text>`;
  }
  for (const level of [12, 28, 65]) {
    markup += `<line x1="${left}" y1="${y(level)}" x2="${right}" y2="${y(level)}" stroke="#bd790c" stroke-dasharray="5 5" stroke-opacity=".7"/><text x="${right + 6}" y="${y(level) + 4}" fill="#835100" font-size="11">${level}</text>`;
  }
  if (!rows.length) {
    markup += '<text x="380" y="120" text-anchor="middle" class="empty-chart">Chưa có lịch sử tín hiệu</text>';
    $('latestChartValue').textContent = '—';
  } else {
    for (const [key, color] of [['lpg', '#2164a8'], ['co', '#b32632']]) {
      const coords = rows.map((row, index) => [left + (rows.length === 1 ? .5 : index / (rows.length - 1)) * (right - left), y(row[key].signal_pct)]);
      const points = coords.map(pair => pair.join(',')).join(' ');
      markup += svgLine(points, color, 3.5);
      const [xLast, yLast] = coords.at(-1);
      markup += `<circle cx="${xLast}" cy="${yLast}" r="5" fill="${color}" stroke="white" stroke-width="2"/>`;
    }
    if (rows.every(row => Number.isFinite(row.lpg_truth_sim_pct))) {
      const truth = rows.map((row, index) => `${left + (rows.length === 1 ? .5 : index / (rows.length - 1)) * (right - left)},${y(row.lpg_truth_sim_pct)}`).join(' ');
      markup += `<polyline points="${truth}" fill="none" stroke="#697a8d" stroke-width="2" stroke-dasharray="6 5"/>`;
    }
    markup += `<text x="${left}" y="255" fill="#52677d" font-size="12">${shortTime(rows[0].time)}</text><text x="${right}" y="255" text-anchor="end" fill="#52677d" font-size="12">${shortTime(rows.at(-1).time)}</text>`;
    $('latestChartValue').textContent = `LPG ${n1(rows.at(-1).lpg.signal_pct)}% · CO ${n1(rows.at(-1).co.signal_pct)}%`;
  }
  svg.innerHTML = markup;
}
function renderEnvironmentChart(rows) {
  const svg = $('environmentChart');
  const left = 40, right = 314, top = 12, bottom = 177;
  const scale = value => bottom - Math.max(0, Math.min(100, value)) * (bottom - top) / 100;
  let markup = '<title>Nhiệt độ và độ ẩm theo thời gian</title>';
  for (const tick of [0, 25, 50, 75, 100]) {
    markup += `<line x1="${left}" y1="${scale(tick)}" x2="${right}" y2="${scale(tick)}" stroke="#dbe3eb"/><text x="2" y="${scale(tick) + 4}" fill="#9d1b2a" font-size="10">${tick / 2}°</text><text x="${right + 5}" y="${scale(tick) + 4}" fill="#165993" font-size="10">${tick}%</text>`;
  }
  if (!rows.length) {
    markup += '<text x="180" y="100" text-anchor="middle" class="empty-chart">Chưa có dữ liệu môi trường</text>';
    $('tempCurrent').textContent = '—'; $('humidityCurrent').textContent = '—';
  } else {
    const x = index => left + (rows.length === 1 ? .5 : index / (rows.length - 1)) * (right - left);
    const temp = rows.map((row, index) => `${x(index)},${scale(Number(row.temperature_c) * 2)}`).join(' ');
    const humidity = rows.map((row, index) => `${x(index)},${scale(Number(row.humidity_pct))}`).join(' ');
    markup += svgLine(temp, '#b32632') + svgLine(humidity, '#2164a8');
    markup += `<text x="${left}" y="203" fill="#52677d" font-size="10">${shortTime(rows[0].time)}</text><text x="${right}" y="203" text-anchor="end" fill="#52677d" font-size="10">${shortTime(rows.at(-1).time)}</text>`;
    $('tempCurrent').textContent = `${n1(rows.at(-1).temperature_c)} °C`;
    $('humidityCurrent').textContent = `${n1(rows.at(-1).humidity_pct)} %RH`;
  }
  svg.innerHTML = markup;
}
function renderCharts() {
  const id = $('deviceSelect').value;
  $('chartTitle').textContent = names[id];
  const rows = model.history[id] ?? [];
  renderSignalChart(rows);
  renderEnvironmentChart(rows);
}

function renderEvents() {
  const rows = filterAbnormal ? model.events.filter(event => abnormal.has(event.state) || event.flame_active === true) : model.events;
  $('eventRows').innerHTML = rows.length ? rows.map(event => {
    const state = event.flame_active === true ? (event.flame_severity === 'FAULT' ? 'FAULT' : 'DANGER') : stateNames[event.state] ? event.state : 'OFFLINE';
    const flameLabels = { F1_FIRE: 'Phát hiện lửa quanh bình', F1_SENSOR_FAULT: 'Lỗi cảm biến lửa quanh bình', F2_IGNITION_TIMEOUT: 'Chưa xác nhận lửa sau khi cấp gas', F2_FLAME_LOST: 'Nghi mất lửa khi vẫn cấp gas', F2_STARTUP_UNCONFIRMED: 'Chưa xác nhận lửa sau khi khởi động', F2_SENSOR_FAULT: 'Lỗi cảm biến lửa', F2_GAS_STATE_FAULT: 'Lỗi tín hiệu cấp gas' };
    const flame = event.flame_channel ? `${event.flame_channel}${event.burner_id ? ' · đầu đốt số 1' : ' · quanh bình'}: ${flameLabels[event.flame_cause] ?? 'Sự kiện lửa'}${event.flame_active ? '' : ' · đã kết thúc'}` : '';
    const changed = event.changed_gas === 'CO' ? ['CO'] : event.changed_gas === 'BOTH' ? ['LPG', 'CO'] : event.changed_gas === 'LPG' ? ['LPG'] : [];
    const gasStates = changed.map(gas => `${gas}: ${stateNames[event[gas.toLowerCase()]?.state] ?? '—'}`).join(' · ');
    const rises = changed.map(gas => {
      const rise = Number(event[gas.toLowerCase()]?.rise_pct);
      return Number.isFinite(rise) ? `${gas} so với nền: ${rise >= 0 ? '+' : ''}${rise.toFixed(1)}%` : '';
    }).filter(Boolean).join(' · ');
    return `<tr class="log-row-${state.toLowerCase()}">
      <td class="log-time">${stamp(event.time)}</td>
      <td><strong>${escapeHtml(names[event.device_id] ?? event.device_id)}</strong><div class="small text-secondary">${escapeHtml(flame || (event.changed_gas === 'BOTH' ? 'LPG + CO' : event.changed_gas === 'NONE' ? 'Thao tác tại bo' : event.changed_gas))}</div></td>
      <td>${badge(state)}${gasStates ? `<div class="small text-secondary mt-1">${escapeHtml(gasStates)}</div>` : ''}</td>
      <td>${flame ? `<strong>${event.flame_active ? 'Đang có cảnh báo' : 'Sự kiện đã đóng'}</strong>` : `<strong>LPG ${n1(event.lpg?.signal_pct)}% · CO ${n1(event.co?.signal_pct)}%</strong>${rises ? `<div class="small text-secondary">${escapeHtml(rises)}</div>` : ''}`}</td>
      <td class="action-text">${event.action ? escapeHtml(recordedActions[event.action] ?? 'Thao tác chưa biết') : `${escapeHtml(actions[state] ?? 'Theo dõi')}${state === 'OFFLINE' ? '' : ' (theo logic bo)'}`}</td>
    </tr>`;
  }).join('') : `<tr><td colspan="5" class="text-center text-secondary py-4">${filterAbnormal ? 'Không có sự kiện bất thường trong nhật ký gần đây.' : 'Chưa có sự kiện. Khi nút đổi trạng thái, log sẽ xuất hiện ở đây.'}</td></tr>`;
}
function renderIncidents() {
  const rows = model.incidents ?? [];
  $('incidentRows').innerHTML = rows.length ? rows.map(item => {
    const open = item.closed_at == null;
    const acknowledged = item.ack_at != null;
    const escalated = item.escalated_at != null;
    const source = item.gas === 'flame' ? `${item.channel ?? 'Lửa'} · ${item.cause ?? 'cảnh báo'}` : item.gas.toUpperCase();
    return `<tr class="${open && escalated ? 'log-row-danger' : ''}"><td>${stamp(new Date(item.opened_at * 1000))}</td><td>${escapeHtml(names[item.device_id] ?? item.device_id)} · ${escapeHtml(source)}</td><td>${badge(item.state)}</td><td>${open ? acknowledged ? 'Đã xác nhận trên web' : escalated ? 'Quá hạn xác nhận — cần xử lý' : 'Chờ xác nhận' : 'Đã kết thúc'}</td><td>${open && !acknowledged ? `<button type="button" class="btn btn-sm btn-outline-primary" data-ack-id="${Number(item.id)}">Xác nhận đã xem</button>` : '—'}</td></tr>`;
  }).join('') : '<tr><td colspan="5" class="text-center text-secondary py-4">Chưa có sự cố cần xác nhận.</td></tr>';
}
function renderIntegrationStatus() {
  const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : '-';
  const sync = model.syncStatus;
  $('syncPending').textContent = count(sync?.pending);
  $('syncLastSuccess').textContent = typeof sync?.last_success_at === 'number'
    ? stamp(sync.last_success_at * 1000) : 'Chưa có lần đồng bộ thành công';
  $('syncDedupeHorizon').textContent = Number.isSafeInteger(sync?.dedupe_horizon_days)
    ? `Biên nhận chống trùng được giữ ${sync.dedupe_horizon_days} ngày sau khi lịch sử nguồn được dọn.`
    : 'Chưa có thông tin thời hạn lưu biên nhận.';
  $('syncStateText').textContent = !sync
    ? location.protocol === 'file:' ? 'Mở trang máy chủ cục bộ để xem trạng thái đồng bộ.' : 'Không tải được trạng thái đồng bộ.'
    : sync.configured !== true
      ? 'Đồng bộ đi chưa được bật. Cấu hình máy chủ đích để bật tính năng này.'
      : sync.last_error
        ? 'Lần đồng bộ gần nhất lỗi hoặc chỉ được xác nhận một phần; bản ghi còn lại sẽ được thử lại.'
        : sync.pending > 0 ? 'Đang chờ máy chủ xác nhận các bản ghi.' : 'Không có bản ghi đang chờ máy chủ xác nhận.';

  const notification = model.notificationData?.status;
  const logs = Array.isArray(model.notificationData?.log) ? model.notificationData.log.slice(0, 5) : [];
  const simulated = notification?.provider === 'mock' && notification.simulated === true;
  $('notificationDisclosure').textContent = simulated
    ? 'Chỉ ghi vào nhật ký giả lập; không gửi tin nhắn ra ngoài.'
    : 'Thông báo đang tắt; không gửi tin nhắn ra ngoài.';
  $('notificationStateText').textContent = !notification
    ? location.protocol === 'file:' ? 'Mở trang máy chủ cục bộ để xem trạng thái thông báo.' : 'Không tải được trạng thái thông báo.'
    : simulated
      ? `Bộ giả lập · ${count(notification.configured_recipients)} người nhận đã cấu hình · chỉ ghi nhận mô phỏng.`
      : 'Chưa bật bộ gửi thông báo.';
  $('notifyPending').textContent = count(notification?.pending);
  $('notifySimulated').textContent = count(notification?.simulated_deliveries);
  $('notifyFailures').textContent = count(notification?.failed_or_dead);
  $('notifyExpiry').textContent = Number.isSafeInteger(notification?.acknowledgement_expiry_s)
    ? `${notification.acknowledgement_expiry_s} giây` : '-';
  $('notificationRows').innerHTML = logs.length ? logs.map(row => {
    const result = row.status === 'simulated' ? 'Đã ghi nhận (giả lập)' : row.status === 'failed' ? 'Thất bại; đã ghi nhật ký thử lại' : 'Đã ghi nhật ký';
    const time = typeof row.attempted_at === 'number' ? stamp(row.attempted_at * 1000) : '-';
    return `<tr><td class="log-time">${escapeHtml(time)}</td><td>${escapeHtml(result)}</td></tr>`;
  }).join('') : '<tr><td colspan="2" class="text-center text-secondary py-3">Chưa có lần thử thông báo nào.</td></tr>';
}

function renderLayout() {
  const layout = model.layout;
  const leak = layout?.leak_candidates?.[$('leakSelect').value || 'valve'];
  const fire = layout?.ignition;
  const values = [leak?.x_m, leak?.y_m, fire?.x_m, fire?.y_m].map(Number);
  $('scenarioDistance').textContent = values.every(Number.isFinite)
    ? `${Math.hypot(values[0] - values[2], values[1] - values[3]).toFixed(2)} m`
    : 'Chưa cấu hình';
  $('scenarioNote').textContent = layout?.illustrative_only
    ? 'Tọa độ minh họa trong site.json; không được dùng khoảng cách này để kết luận an toàn.'
    : 'Tọa độ do người dùng cấu hình; vị trí lỗ rò vẫn là giả định, chưa được hệ thống xác định.';
}
function renderEvaluation() {
  const percent = value => value == null ? '—' : `${Math.round(value * 100)}%`;
  $('evaluationRows').innerHTML = model.evaluation?.length ? model.evaluation.map(row => `<tr><td><strong>${escapeHtml(names[row.device_id] ?? row.device_id)} · ${escapeHtml(row.gas)}</strong></td><td>${row.samples}</td><td>${percent(row.algorithm.precision)}</td><td>${percent(row.algorithm.recall)}</td><td>${percent(row.fixed.precision)}</td><td>${percent(row.fixed.recall)}</td><td><strong>${row.algorithm.fp} / ${row.algorithm.fn}</strong></td><td>${row.episodes ? `${row.episodes.detected_episodes}/${row.episodes.episodes} · ${row.episodes.mean_delay_s == null ? '—' : n1(row.episodes.mean_delay_s) + ' s'}` : '—'}</td></tr>`).join('') : '<tr><td colspan="8" class="text-center text-secondary py-4">Chưa có mẫu Wokwi được gắn nhãn.</td></tr>';
}
function renderEvaluationInvalidSummary() {
  const rows = (model.evaluation ?? []).filter(row => Number.isSafeInteger(row.excluded_invalid_samples) && row.excluded_invalid_samples > 0);
  if (!rows.length) return;
  const details = rows.map(row => `${escapeHtml(names[row.device_id] ?? row.device_id)} · ${escapeHtml(row.gas)}: ${row.excluded_invalid_samples}`);
  $('evaluationRows').innerHTML += `<tr class="evaluation-invalid-summary"><td colspan="8" class="text-secondary small">Mẫu không hợp lệ đã loại khỏi phép tính · ${details.join(' · ')}</td></tr>`;
}
function renderAll() { renderOverview(); renderZones(); renderCharts(); renderEvents(); renderIncidents(); renderIntegrationStatus(); renderLayout(); renderEvaluation(); renderEvaluationInvalidSummary(); }

async function fetchJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
async function refreshLive() {
  let snapshot;
  try {
    snapshot = await fetchJson('api/snapshot');
  } catch (error) {
    model.events = [];
    model.incidents = [];
    model.layout = null;
    model.evaluation = [];
    model.syncStatus = null;
    model.notificationData = null;
    for (const rows of Object.values(model.history ?? {})) rows.length = 0;
    if (model.snapshot?.nodes) {
      for (const node of model.snapshot.nodes) {
        node.online = false;
        node.age_s = null;
        node.state = 'OFFLINE';
        node.latest = null;
      }
      renderAll();
    }
    $('modeBadge').textContent = 'Mất kết nối web';
    $('overall').textContent = 'Không thể cập nhật';
    $('summaryDetail').textContent = 'Kiểm tra dashboard/server.py và đường mạng đến trình duyệt.';
    return;
  }

  const selected = $('deviceSelect').value;
  model.snapshot = snapshot;
  const secondary = [
    { label: 'sync status', url: 'api/sync-status', apply: value => { model.syncStatus = value; }, clear: () => { model.syncStatus = null; } },
    { label: 'notification status', url: 'api/notifications', apply: value => { model.notificationData = value; }, clear: () => { model.notificationData = null; } },
    { label: 'nhật ký sự kiện', url: 'api/events', apply: value => { model.events = value; }, clear: () => { model.events = []; } },
    { label: 'lịch sử biểu đồ', url: `api/history?device=${encodeURIComponent(selected)}`, apply: value => { model.history[selected] = value; }, clear: () => { model.history[selected] = []; } },
    { label: 'danh sách sự cố', url: 'api/incidents', apply: value => { model.incidents = value; }, clear: () => { model.incidents = []; } },
    { label: 'sơ đồ vị trí', url: 'api/layout', apply: value => { model.layout = value; }, clear: () => { model.layout = null; } },
    { label: 'đánh giá', url: 'api/evaluation', apply: value => { model.evaluation = value; }, clear: () => { model.evaluation = []; } }
  ];
  const results = await Promise.allSettled(secondary.map(item => fetchJson(item.url)));
  const failed = [];
  results.forEach((result, index) => {
    if (result.status === 'fulfilled') secondary[index].apply(result.value);
    else {
      secondary[index].clear();
      failed.push(secondary[index].label);
    }
  });
  renderAll();
  $('modeBadge').textContent = failed.length
    ? 'Dữ liệu thiết bị mới · lỗi tải dữ liệu phụ'
    : snapshot.broker?.connected === true
      ? 'MQTT đã kết nối · dữ liệu trực tiếp'
      : snapshot.broker?.connected === false
        ? 'Máy chủ chạy · MQTT chưa kết nối'
        : 'Dữ liệu từ server cục bộ';
  if (failed.length) {
    $('summaryDetail').textContent += ` Không tải được: ${failed.join(', ')}.`;
  }
}
$('incidentRows').addEventListener('click', async event => {
  const id = Number(event.target?.getAttribute?.('data-ack-id'));
  if (!Number.isInteger(id) || id <= 0 || location.protocol === 'file:') return;
  const response = await fetch('api/incidents/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) });
  if (response.ok) refreshLive();
});
$('leakSelect').addEventListener('change', renderLayout);

$('deviceSelect').addEventListener('change', () => {
  if (location.protocol === 'file:') renderCharts();
  else refreshLive();
});
function setFilter(value) {
  filterAbnormal = value;
  for (const [id, selected] of [['filterAll', !value], ['filterAbnormal', value]]) {
    $(id).classList.toggle('active', selected);
    $(id).setAttribute('aria-pressed', String(selected));
  }
  renderEvents();
}
$('filterAll').addEventListener('click', () => setFilter(false));
$('filterAbnormal').addEventListener('click', () => setFilter(true));
function updateClock() { $('clock').textContent = new Date().toLocaleTimeString('vi-VN'); }
updateClock(); setInterval(updateClock, 1000);
if (location.protocol === 'file:') {
  $('previewNotice').classList.remove('d-none');
  $('modeBadge').textContent = 'Xem trước · dữ liệu minh họa';
  model = demoModel(); renderAll();
} else {
  model.history = { node1: [], node2: [], node3: [] };
  refreshLive(); setInterval(refreshLive, 2000);
}
