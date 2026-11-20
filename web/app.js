/* XboxDownload-Web frontend.
 *
 * Talks to the JSON API under /api. No build step, no framework — plain
 * ES2019 so it runs in any modern browser.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  accel: [],
  speedResults: [],
  logTimer: null,
};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.message)) || res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('err', isError);
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3200);
}

function fmtSpeed(mib) {
  if (mib === null || mib === undefined) return '—';
  if (mib >= 1) return mib.toFixed(2) + ' MiB/s';
  return (mib * 1024).toFixed(1) + ' KiB/s';
}

function fmtLatency(ms) {
  if (ms === null || ms === undefined) return '—';
  return ms.toFixed(1) + ' ms';
}

function escapeHtml(str) {
  return String(str === null || str === undefined ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// tabs
// ---------------------------------------------------------------------------
function initTabs() {
  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      $$('.tab').forEach((t) => t.classList.remove('active'));
      $$('.panel').forEach((p) => p.classList.remove('active'));
      tab.classList.add('active');
      const panel = document.getElementById('panel-' + tab.dataset.panel);
      if (panel) panel.classList.add('active');
      if (tab.dataset.panel === 'logs') { loadLogs(); startLogTimer(); }
      else stopLogTimer();
    });
  });
}

// ---------------------------------------------------------------------------
// status
// ---------------------------------------------------------------------------
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const dns = s.dns || {};
    const proxy = s.proxy || {};
    setPill('#pillDns', dns.running, dns.running ? '运行中' : '已停止');
    setPill('#pillProxy', proxy.http_connections !== undefined, '端口 80/443');
    $('#pillQueries').innerHTML = '查询 <b>' + (dns.queries || 0) + '</b>';
    $('#pillRelayed').innerHTML = '中继 <b>' + (proxy.relayed_connections || 0) + '</b>';
  } catch (err) {
    setPill('#pillDns', false, '离线');
  }
}

function setPill(sel, ok, text) {
  const el = $(sel);
  el.classList.toggle('on', !!ok);
  el.classList.toggle('off', !ok);
  el.innerHTML = el.innerHTML.replace(/<b>.*?<\/b>/, '<b>' + escapeHtml(text) + '</b>');
}

// ---------------------------------------------------------------------------
// acceleration addresses
// ---------------------------------------------------------------------------

// "显示 Xbox 游戏下载链接" switch. Mirrors the switch of the C# original:
// when it is on, the "1" Xbox endpoints are redirected to the "2" ones
// (assets1.xboxlive.com -> assets2.xboxlive.cn); when it is off they keep
// their own number (assets1.xboxlive.com -> assets1.xboxlive.cn).
const GAME_LINKS_HINTS = {
  true: '开启：assets1.xboxlive.com → assets2.xboxlive.cn，dlassets.xboxlive.com → dlassets2.xboxlive.cn',
  false: '关闭：assets1.xboxlive.com → assets1.xboxlive.cn，dlassets.xboxlive.com → dlassets.xboxlive.cn',
};

function renderGameLinksToggle(on) {
  $('#gameLinksToggle').checked = on;
  $('#gameLinksHint').textContent = GAME_LINKS_HINTS[on] || '';
}

async function saveGameLinksToggle(enabled) {
  const toggle = $('#gameLinksToggle');
  toggle.disabled = true;
  try {
    await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ is_xbox_game_download_links_shown: enabled }),
    });
    renderGameLinksToggle(enabled);
    toast(enabled ? '已开启「显示 Xbox 游戏下载链接」' : '已关闭「显示 Xbox 游戏下载链接」');
  } catch (err) {
    toast('保存失败：' + err.message, true);
    // Roll the checkbox back to the stored state.
    try {
      const s = await api('/api/settings');
      renderGameLinksToggle(s.is_xbox_game_download_links_shown !== false);
    } catch (_) { /* keep the current state */ }
  } finally {
    toggle.disabled = false;
  }
}

async function loadAccel() {
  const data = await api('/api/accel');
  state.accel = data.groups || [];
  const grid = $('#accelGrid');
  grid.innerHTML = '';
  state.accel.forEach((g) => {
    const card = document.createElement('div');
    card.className = 'accel-card' + (g.configured ? ' set' : '');
    const hostPreview = (g.hosts || []).slice(0, 6).join('\n');
    card.innerHTML =
      '<div class="accel-card-head">' +
        '<span class="accel-name">' + escapeHtml(g.label || g.group) + '</span>' +
        '<span class="accel-badge' + (g.configured ? ' on' : '') + '">' +
          (g.configured ? '已配置' : '未配置') + '</span>' +
      '</div>' +
      '<div class="accel-row">' +
        '<input type="text" class="accel-input" placeholder="留空 = 不加速" value="' +
          escapeHtml(g.accel_ip || '') + '">' +
        '<button class="btn small primary accel-save">保存</button>' +
      '</div>' +
      '<div class="accel-hosts">' + escapeHtml(hostPreview) + '</div>';

    card.querySelector('.accel-save').addEventListener('click', async () => {
      const input = card.querySelector('.accel-input');
      const btn = card.querySelector('.accel-save');
      btn.disabled = true;
      try {
        await api('/api/accel', {
          method: 'PUT',
          body: JSON.stringify({ group: g.group, ip: input.value.trim() }),
        });
        toast(g.label + ' 加速地址已保存');
        await loadAccel();
      } catch (err) {
        toast('保存失败：' + err.message, true);
      } finally {
        btn.disabled = false;
      }
    });
    grid.appendChild(card);
  });
}

async function clearAccel() {
  if (!confirm('确定要清空所有加速地址吗？')) return;
  try {
    await api('/api/accel/clear', { method: 'POST' });
    toast('已清空全部加速地址');
    await loadAccel();
  } catch (err) {
    toast('操作失败：' + err.message, true);
  }
}

// ---------------------------------------------------------------------------
// resolution
// ---------------------------------------------------------------------------
async function doResolve() {
  const host = $('#resolveHost').value.trim();
  if (!host) { toast('请输入域名', true); return; }
  try {
    const r = await api('/api/resolve', {
      method: 'POST',
      body: JSON.stringify({ hostname: host, qtype: $('#resolveType').value }),
    });
    const lines = [
      '域名   : ' + r.hostname,
      '来源   : ' + sourceLabel(r.source) + (r.rule_key ? ' (' + r.rule_key + ')' : ''),
      '结果   : ' + (r.ips && r.ips.length ? r.ips.join(', ') : '(无)'),
    ];
    if (r.error) lines.push('错误   : ' + r.error);
    $('#resolveResult').textContent = lines.join('\n');
  } catch (err) {
    toast('解析失败：' + err.message, true);
  }
}

function sourceLabel(src) {
  return {
    manual: '手工映射',
    rule: '内置规则 (加速地址)',
    blacklist: '黑名单拦截',
    cached: '缓存',
    upstream: '正常解析',
  }[src] || src;
}

// ---------------------------------------------------------------------------
// speed test
// ---------------------------------------------------------------------------
async function initSpeedtest() {
  // The group keys match the acceleration groups used by the API, so the
  // pool that gets measured is always the one belonging to the group
  // (xbox_cn1 -> XboxCn1, xbox_cn2 -> XboxCn2, ...).
  const labels = {
    xbox_global: 'Xbox Global（国际 Akamai）',
    xbox_cn1: 'Xbox CN1（国内 assets2.xboxlive.cn）',
    xbox_cn2: 'Xbox CN2（国内 dlassets2.xboxlive.cn）',
    xbox_app: 'MS Store App（国内）',
    ps: 'PlayStation（国内）',
    akamai: 'Akamai 全部（国际）',
    akamaiv2: 'Akamai V2（国际）',
    akamaiv6: 'Akamai V6（国际 IPv6）',
  };
  const groups = ['xbox_cn1', 'xbox_cn2', 'xbox_app', 'ps', 'xbox_global', 'akamai', 'akamaiv2', 'akamaiv6'];
  const groupSel = $('#stGroup');
  groupSel.innerHTML = '';
  groups.forEach((g) => {
    const opt = document.createElement('option');
    opt.value = g;
    opt.textContent = labels[g] || g;
    groupSel.appendChild(opt);
  });
  groupSel.value = 'xbox_cn1';

  const targetSel = $('#stTarget');
  await loadTargets();

  groupSel.addEventListener('change', loadTargets);
}

async function loadTargets() {
  try {
    const data = await api('/api/speedtest/targets');
    const group = $('#stGroup').value;
    const list = (data.targets && data.targets[group]) || [];
    const sel = $('#stTarget');
    sel.innerHTML = '';
    list.forEach((t) => {
      const opt = document.createElement('option');
      opt.value = t.url;
      opt.textContent = t.label + ' — ' + t.url;
      sel.appendChild(opt);
    });
    if (!list.length) {
      const opt = document.createElement('option');
      opt.value = 'http://xvcf1.xboxlive.com/Z/routing/extraextralarge.txt';
      opt.textContent = '默认';
      sel.appendChild(opt);
    }
  } catch (err) {
    toast('加载测速目标失败：' + err.message, true);
  }
}

async function runSpeedtest() {
  const btn = $('#btnSpeedtest');
  btn.disabled = true;
  btn.textContent = '测速中…';
  $('#stBody').innerHTML = '<tr class="empty"><td colspan="8">正在测试，请稍候…</td></tr>';
  $('#speedSummary').hidden = true;

  const manual = $('#stManualIps').value.trim();
  const ips = manual ? manual.split(/[,\s]+/).filter(Boolean) : [];

  const body = {
    group: $('#stGroup').value,
    target: $('#stTarget').value || undefined,
    concurrency: parseInt($('#stConcurrency').value, 10) || 8,
    limit: parseInt($('#stLimit').value, 10) || 20,
    ips: ips,
  };

  try {
    const res = await api('/api/speedtest', { method: 'POST', body: JSON.stringify(body) });
    state.speedResults = res.results || [];
    renderSpeedResults(res);
    toast('测速完成，耗时 ' + res.elapsed_s + ' 秒');
  } catch (err) {
    toast('测速失败：' + err.message, true);
    $('#stBody').innerHTML =
      '<tr class="empty"><td colspan="8">测速失败：' + escapeHtml(err.message) + '</td></tr>';
  } finally {
    btn.disabled = false;
    btn.textContent = '开始测速';
  }
}

function renderSpeedResults(res) {
  const body = $('#stBody');
  if (!res.results.length) {
    body.innerHTML = '<tr class="empty"><td colspan="8">没有可测试的地址</td></tr>';
    return;
  }
  body.innerHTML = res.results.map((r, idx) => {
    const status = r.ok
      ? '<span class="tag ok">可用</span>'
      : '<span class="tag fail">' + escapeHtml(r.error || '失败') + '</span>';
    const loc = r.location_en || r.location || '';
    return '<tr>' +
      '<td class="mono">' + escapeHtml(r.ip) + '</td>' +
      '<td>' + escapeHtml(loc) + '</td>' +
      '<td class="num">' + (r.ok ? fmtSpeed(r.speed_mib_s) : '—') + '</td>' +
      '<td class="num">' + fmtLatency(r.latency_ms) + '</td>' +
      '<td class="num">' + (r.ttl === null || r.ttl === undefined ? '—' : r.ttl) + '</td>' +
      '<td class="num">' + ((r.bytes_downloaded || 0) / 1048576).toFixed(2) + '</td>' +
      '<td>' + status + '</td>' +
      '<td><button class="btn small use-ip" data-idx="' + idx + '">使用</button></td>' +
      '</tr>';
  }).join('');

  body.querySelectorAll('.use-ip').forEach((btn) => {
    btn.addEventListener('click', () => {
      const r = res.results[parseInt(btn.dataset.idx, 10)];
      $('#stManualIps').value = r.ip;
      toast('已填入 IP：' + r.ip);
    });
  });

  if (res.best) {
    $('#speedSummary').hidden = false;
    $('#bestIp').textContent = res.best.ip;
    $('#bestSpeed').textContent = fmtSpeed(res.best.speed_mib_s);
    $('#bestLatency').textContent = fmtLatency(res.best.latency_ms);
    $('#btnUseBest').onclick = async () => {
      const group = $('#stGroup').value;
      try {
        await api('/api/accel', {
          method: 'PUT',
          body: JSON.stringify({ group: group, ip: res.best.ip }),
        });
        toast('已将 ' + res.best.ip + ' 设为 ' + group + ' 的加速地址');
        await loadAccel();
      } catch (err) {
        toast('设置失败：' + err.message, true);
      }
    };
  }
}

// ---------------------------------------------------------------------------
// host mappings
// ---------------------------------------------------------------------------
async function loadHosts() {
  const data = await api('/api/hosts');
  const body = $('#hostBody');
  const list = data.mappings || [];
  if (!list.length) {
    body.innerHTML = '<tr class="empty"><td colspan="5">暂无映射</td></tr>';
    return;
  }
  body.innerHTML = list.map((m, idx) =>
    '<tr>' +
      '<td><input type="checkbox" class="hm-toggle" data-idx="' + idx + '"' +
        (m.is_enabled ? ' checked' : '') + '></td>' +
      '<td class="mono">' + escapeHtml(m.host_name) + '</td>' +
      '<td class="mono">' + escapeHtml(m.ip) + '</td>' +
      '<td>' + escapeHtml(m.note || '') + '</td>' +
      '<td><button class="btn small danger hm-del" data-idx="' + idx + '">删除</button></td>' +
    '</tr>').join('');

  body.querySelectorAll('.hm-del').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx, 10);
      try {
        await api('/api/hosts/' + idx, { method: 'DELETE' });
        toast('已删除');
        await loadHosts();
      } catch (err) { toast('删除失败：' + err.message, true); }
    });
  });

  body.querySelectorAll('.hm-toggle').forEach((box) => {
    box.addEventListener('change', async () => {
      const idx = parseInt(box.dataset.idx, 10);
      try {
        await api('/api/hosts/' + idx, {
          method: 'PATCH',
          body: JSON.stringify({ is_enabled: box.checked }),
        });
        await loadHosts();
      } catch (err) { toast('更新失败：' + err.message, true); }
    });
  });
}

async function addHost() {
  const host = $('#hostName').value.trim();
  const ip = $('#hostIp').value.trim();
  if (!host || !ip) { toast('域名和 IP 均不能为空', true); return; }
  try {
    await api('/api/hosts', {
      method: 'POST',
      body: JSON.stringify({ host_name: host, ip: ip, note: $('#hostNote').value.trim() }),
    });
    toast('映射已添加');
    $('#hostName').value = ''; $('#hostIp').value = ''; $('#hostNote').value = '';
    await loadHosts();
  } catch (err) { toast('添加失败：' + err.message, true); }
}

async function clearHosts() {
  if (!confirm('确定要清空所有主机映射吗？')) return;
  try {
    await api('/api/hosts', { method: 'DELETE' });
    toast('已清空');
    await loadHosts();
  } catch (err) { toast('操作失败：' + err.message, true); }
}

async function loadRules() {
  try {
    const data = await api('/api/host-rules');
    $('#rulesList').innerHTML = (data.rules || []).map((r) =>
      '<div class="rule-card">' +
        '<div class="rule-title">' + escapeHtml(r.description) + ' <span class="tag">' +
          escapeHtml(r.key) + '</span></div>' +
        '<div class="rule-host">' + escapeHtml(r.hosts.join('\n')) + '</div>' +
      '</div>').join('');
  } catch (err) { toast('加载规则失败：' + err.message, true); }
}

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------
const SETTING_FIELDS = [
  { key: 'local_proxy_ip', label: '本地代理地址', type: 'text',
    desc: '国际 Xbox 域名（.com）解析到此地址，客户端连接后由代理 302 跳转到国内 .cn 域名。留空=自动探测本机 IP。' },
  { key: 'listening_ip', label: '监听地址', type: 'text',
    desc: 'DNS / HTTP / HTTPS 代理监听的 IP。0.0.0.0 表示所有接口。' },
  { key: 'dns_enabled', label: '启用 DNS 服务', type: 'bool',
    desc: '启动内置 DNS 服务器，按加速规则解析域名。' },
  { key: 'dns_port', label: 'DNS 端口', type: 'number' },
  { key: 'http_enabled', label: '启用 HTTP 代理', type: 'bool' },
  { key: 'http_port', label: 'HTTP 端口', type: 'number' },
  { key: 'https_enabled', label: '启用 HTTPS 代理', type: 'bool' },
  { key: 'https_port', label: 'HTTPS 端口', type: 'number' },
  { key: 'ipv6_enabled', label: '启用 IPv6', type: 'bool',
    desc: '解析 AAAA 记录并在代理中使用 IPv6 地址。' },
  { key: 'auto_select_fastest', label: '自动选择最快节点', type: 'bool' },
  { key: 'speedtest_timeout_s', label: '测速超时（秒）', type: 'number' },
  { key: 'speedtest_chunk_bytes', label: '测速下载量（字节）', type: 'number' },
  { key: 'speedtest_concurrency', label: '测速并发数', type: 'number' },
  { key: 'is_logging', label: '记录服务日志', type: 'bool' },
];

async function loadSettings() {
  try {
    const s = await api('/api/settings');
    renderGameLinksToggle(s.is_xbox_game_download_links_shown !== false);
    $('#settingsGrid').innerHTML = SETTING_FIELDS.map((f) => {
      const v = s[f.key];
      let control;
      if (f.type === 'bool') {
        control = '<input type="checkbox" class="set-input" data-key="' + f.key + '"' +
          (v ? ' checked' : '') + '>';
      } else {
        control = '<input type="' + (f.type === 'number' ? 'number' : 'text') +
          '" class="set-input" data-key="' + f.key + '" value="' + escapeHtml(v) + '">';
      }
      return '<div class="setting-card">' +
        '<label class="setting-label">' + escapeHtml(f.label) + '</label>' +
        (f.type === 'bool'
          ? '<div class="switch-row">' + control + '</div>'
          : control) +
        (f.desc ? '<div class="setting-desc">' + escapeHtml(f.desc) + '</div>' : '') +
        '<button class="btn small set-save" style="margin-top:10px">保存此项</button>' +
      '</div>';
    }).join('');

    $$('.set-save').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const card = btn.closest('.setting-card');
        const input = card.querySelector('.set-input');
        const key = input.dataset.key;
        const value = input.type === 'checkbox' ? input.checked : input.value;
        try {
          await api('/api/settings', {
            method: 'PUT',
            body: JSON.stringify({ [key]: value }),
          });
          toast('设置已保存（重启服务后生效）');
        } catch (err) { toast('保存失败：' + err.message, true); }
      });
    });
  } catch (err) { toast('加载设置失败：' + err.message, true); }
}

async function resetSettings() {
  if (!confirm('确定要恢复默认设置吗？')) return;
  try {
    await api('/api/settings/reset', { method: 'POST' });
    toast('已恢复默认设置');
    await loadSettings();
  } catch (err) { toast('操作失败：' + err.message, true); }
}

// ---------------------------------------------------------------------------
// logs
// ---------------------------------------------------------------------------
async function loadLogs() {
  try {
    const data = await api('/api/logs?limit=200');
    const lines = data.lines || [];
    $('#logBox').textContent = lines.length ? lines.join('\n') : '（暂无日志）';
  } catch (err) { /* keep the last content */ }
}

function startLogTimer() {
  stopLogTimer();
  state.logTimer = setInterval(() => {
    if ($('#autoRefresh').checked) loadLogs();
  }, 5000);
}

function stopLogTimer() {
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
function init() {
  initTabs();
  initSpeedtest();

  $('#btnRefreshAccel').addEventListener('click', loadAccel);
  $('#btnClearAccel').addEventListener('click', clearAccel);
  $('#gameLinksToggle').addEventListener('change', (e) => {
    saveGameLinksToggle(e.target.checked);
  });
  $('#btnResolve').addEventListener('click', doResolve);
  $('#resolveHost').addEventListener('keydown', (e) => { if (e.key === 'Enter') doResolve(); });

  $('#btnSpeedtest').addEventListener('click', runSpeedtest);

  $('#btnRefreshHosts').addEventListener('click', loadHosts);
  $('#btnClearHosts').addEventListener('click', clearHosts);
  $('#btnAddHost').addEventListener('click', addHost);

  $('#btnResetSettings').addEventListener('click', resetSettings);
  $('#btnRefreshLogs').addEventListener('click', loadLogs);

  loadAccel().catch((e) => toast('加载加速地址失败：' + e.message, true));
  loadHosts().catch(() => {});
  loadRules().catch(() => {});
  loadSettings().catch(() => {});
  refreshStatus();
  setInterval(refreshStatus, 5000);
}

document.addEventListener('DOMContentLoaded', init);
