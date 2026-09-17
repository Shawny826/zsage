'use strict';

/* ------------------------------------------------------------------ *
 * 工具
 * ------------------------------------------------------------------ */
const $ = (sel, root = document) => root.querySelector(sel);
const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtInt = (n) => (n == null ? '0' : Number(n).toLocaleString('zh-CN'));

function fmtTok(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(Math.round(n));
}

function curSym() {
  const c = state.bootstrap?.pricing?.display_currency;
  return c === 'USD' ? '$' : '¥';
}

function fmtMoney(v) {
  v = v || 0;
  const a = Math.abs(v);
  const d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : a >= 0.01 ? 3 : 4;
  return curSym() + v.toFixed(d);
}

function fmtUSD(v) {
  const rate = state.bootstrap?.pricing?.usd_to_cny || 7.1;
  const c = state.bootstrap?.pricing?.display_currency || 'CNY';
  const usd = c === 'USD' ? v : v / rate;
  return '$' + (usd >= 100 ? usd.toFixed(0) : usd >= 1 ? usd.toFixed(2) : usd.toFixed(4));
}

function fmtMs(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return Math.round(ms) + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return Math.floor(ms / 60000) + 'm' + Math.round((ms % 60000) / 1000) + 's';
}

const fmtPct = (r) => ((r || 0) * 100).toFixed(1) + '%';

function fmtTime(ms, withDate = true) {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  return withDate ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}` : hm;
}

function fmtDay(iso) {
  return iso ? iso.slice(5).replace('-', '/') : '';
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { throw new Error('返回不是 JSON：' + text.slice(0, 200)); }
  if (data && data.error) throw new Error(data.error);
  return data;
}

const STATUS_TAG = {
  completed: '<span class="tag ok">完成</span>',
  error: '<span class="tag err">错误</span>',
  cancelled: '<span class="tag mut">取消</span>',
  running: '<span class="tag run">进行中</span>',
};

const DOW = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const COLORS = {
  input: '#4c8dff', cache_read: '#9db4f7', cache_write: '#c4a6f5',
  output: '#fb923c', cost: '#7c3aed', req: '#93b4f0', accent: '#2f6feb',
};
const TOKEN_LEGEND = [
  { key: 'input_fresh', label: '新增输入', color: COLORS.input },
  { key: 'cache_read', label: '缓存命中', color: COLORS.cache_read },
  { key: 'output', label: '输出', color: COLORS.output },
];
const COST_LEGEND = [
  { key: 'cost_input', label: '新增输入', color: COLORS.input },
  { key: 'cost_cache_read', label: '缓存命中', color: COLORS.cache_read },
  { key: 'cost_cache_write', label: '缓存写入', color: COLORS.cache_write },
  { key: 'cost_output', label: '输出', color: COLORS.output },
];

/* ------------------------------------------------------------------ *
 * 状态
 * ------------------------------------------------------------------ */
const state = {
  tab: 'overview',
  range: '30d',
  filters: { from: '', to: '', model: '', provider: '', project: '', agent: '', source: '', status: '', priced: '', q: '' },
  bootstrap: null,
  summary: null,
  live: null,
  events: null,
  eventSort: { key: 'started_at', order: 'desc' },
  modelSort: { key: 'cost', order: 'desc' },
  facetSig: null,
  models: null,
  modelEdits: {},
  stFilter: '',
  ignoredDraft: null,
  page: 1,
  size: 100,
  drawerId: null,
  // 每个标签页一个会话 id：服务端靠心跳判断"还有没有人在看"，没人了就把自己关掉
  sid: (crypto.randomUUID?.() || `sid-${Math.random().toString(36).slice(2)}`),
};

function localDate(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function rangeBounds() {
  if (state.range === 'custom') return { from: state.filters.from, to: state.filters.to };
  if (state.range === 'all') return {};
  const now = new Date();
  const to = localDate(now);
  if (state.range === 'today') return { from: to, to };
  const days = state.range === '7d' ? 7 : 30;
  const start = new Date(now.getTime() - (days - 1) * 86400000);
  return { from: localDate(start), to };
}

function queryString(extra = {}) {
  const p = new URLSearchParams();
  const b = rangeBounds();
  if (b.from) p.set('from', b.from);
  if (b.to) p.set('to', b.to);
  for (const k of ['model', 'provider', 'project', 'agent', 'source', 'status', 'priced', 'q']) {
    if (state.filters[k]) p.set(k, state.filters[k]);
  }
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}

/* ------------------------------------------------------------------ *
 * 图表（手写 SVG，无外部依赖）
 * ------------------------------------------------------------------ */
function niceMax(v) {
  if (!v || v <= 0) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = Math.pow(10, exp);
  const m = v / base;
  const step = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
  return step * base;
}

function svgOpen(w, h) {
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none">`;
}

function chartBarsLine(container, rows) {
  const W = Math.max(container.clientWidth || 640, 320);
  const H = 230;
  const m = { l: 54, r: 56, t: 14, b: 28 };
  if (!rows.length) { container.innerHTML = '<div class="empty">该时间范围内没有请求</div>'; return; }
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const maxBar = niceMax(Math.max(...rows.map((r) => r.requests)));
  const maxLine = niceMax(Math.max(...rows.map((r) => r.cost), 0.0001));
  const bw = iw / rows.length;
  const barW = Math.max(Math.min(bw * 0.62, 26), 1.5);
  const x = (i) => m.l + bw * i + bw / 2;
  const yBar = (v) => m.t + ih - (v / maxBar) * ih;
  const yLine = (v) => m.t + ih - (v / maxLine) * ih;

  let s = svgOpen(W, H);
  for (let i = 0; i <= 4; i++) {
    const y = m.t + (ih / 4) * i;
    s += `<line x1="${m.l}" y1="${y}" x2="${m.l + iw}" y2="${y}" stroke="#eef1f4" stroke-width="1"/>`;
    s += `<text x="${m.l - 8}" y="${y + 3}" text-anchor="end">${fmtTok(maxBar * (4 - i) / 4)}</text>`;
    s += `<text x="${m.l + iw + 8}" y="${y + 3}" text-anchor="start">${fmtMoney(maxLine * (4 - i) / 4)}</text>`;
  }
  rows.forEach((r, i) => {
    const y = yBar(r.requests);
    s += `<rect x="${x(i) - barW / 2}" y="${y}" width="${barW}" height="${m.t + ih - y}" fill="${COLORS.req}" rx="2"/>`;
    if (r.errors) {
      const ey = yBar(r.errors);
      s += `<rect x="${x(i) - barW / 2}" y="${ey}" width="${barW}" height="${m.t + ih - ey}" fill="#e03131" rx="2"/>`;
    }
  });
  const pts = rows.map((r, i) => `${x(i)},${yLine(r.cost)}`).join(' ');
  s += `<polyline points="${pts}" fill="none" stroke="${COLORS.cost}" stroke-width="2" stroke-linejoin="round"/>`;
  rows.forEach((r, i) => {
    s += `<circle cx="${x(i)}" cy="${yLine(r.cost)}" r="2.5" fill="#fff" stroke="${COLORS.cost}" stroke-width="1.6"/>`;
  });
  const step = Math.ceil(rows.length / 12);
  rows.forEach((r, i) => {
    if (i % step === 0) s += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${fmtDay(r.date)}</text>`;
  });
  rows.forEach((r, i) => {
    s += `<rect x="${m.l + bw * i}" y="${m.t}" width="${bw}" height="${ih}" fill="transparent"><title>${r.date}\n请求 ${fmtInt(r.requests)}（错误 ${r.errors}）\n费用 ${fmtMoney(r.cost)}\ntoken ${fmtTok(r.tokens)}</title></rect>`;
  });
  s += '</svg>';
  container.innerHTML = s;
}

function chartStacked(container, rows, series) {
  const W = Math.max(container.clientWidth || 640, 320);
  const H = 230;
  const m = { l: 54, r: 16, t: 14, b: 28 };
  if (!rows.length) { container.innerHTML = '<div class="empty">该时间范围内没有请求</div>'; return; }
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const totals = rows.map((r) => series.reduce((s, k) => s + (r[k.key] || 0), 0));
  const maxV = niceMax(Math.max(...totals));
  const bw = iw / rows.length;
  const barW = Math.max(Math.min(bw * 0.68, 26), 1.5);

  let s = svgOpen(W, H);
  for (let i = 0; i <= 4; i++) {
    const y = m.t + (ih / 4) * i;
    s += `<line x1="${m.l}" y1="${y}" x2="${m.l + iw}" y2="${y}" stroke="#eef1f4"/>`;
    s += `<text x="${m.l - 8}" y="${y + 3}" text-anchor="end">${fmtTok(maxV * (4 - i) / 4)}</text>`;
  }
  rows.forEach((r, i) => {
    let acc = 0;
    const cx = m.l + bw * i + (bw - barW) / 2;
    series.forEach((k) => {
      const v = r[k.key] || 0;
      if (!v) return;
      const h = (v / maxV) * ih;
      const y = m.t + ih - acc - h;
      s += `<rect x="${cx}" y="${y}" width="${barW}" height="${h}" fill="${k.color}"><title>${r.date} ${k.label} ${fmtInt(v)}</title></rect>`;
      acc += h;
    });
  });
  const step = Math.ceil(rows.length / 12);
  rows.forEach((r, i) => {
    if (i % step === 0) s += `<text x="${m.l + bw * i + bw / 2}" y="${H - 8}" text-anchor="middle">${fmtDay(r.date)}</text>`;
  });
  s += '</svg>';
  container.innerHTML = s;
}

function chartDonut(container, parts, centerTop, centerSub) {
  const total = parts.reduce((s, p) => s + p.value, 0);
  const R = 52, C = 2 * Math.PI * R;
  let off = 0;
  let s = `<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
    <svg viewBox="0 0 132 132" width="132" height="132" style="flex:0 0 auto">`;
  s += `<circle cx="66" cy="66" r="${R}" fill="none" stroke="#eef1f5" stroke-width="17"/>`;
  if (total > 0) {
    parts.forEach((p) => {
      if (!p.value) return;
      const len = (p.value / total) * C;
      s += `<circle cx="66" cy="66" r="${R}" fill="none" stroke="${p.color}" stroke-width="17"
        stroke-dasharray="${len - 1.5} ${C - len + 1.5}" stroke-dashoffset="${-off}"
        transform="rotate(-90 66 66)"><title>${esc(p.label)} ${fmtMoney(p.value)}</title></circle>`;
      off += len;
    });
  }
  s += `<text x="66" y="62" text-anchor="middle" style="font-size:15px;font-weight:600;fill:#14181f">${centerTop}</text>`;
  s += `<text x="66" y="79" text-anchor="middle" style="font-size:11px">${esc(centerSub || '')}</text>`;
  s += '</svg><div style="flex:1 1 180px;min-width:170px">';
  parts.forEach((p) => {
    const share = total > 0 ? (p.value / total) * 100 : 0;
    s += `<div style="display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:12.5px">
      <span><i style="display:inline-block;width:9px;height:9px;border-radius:3px;background:${p.color};margin-right:6px"></i>${esc(p.label)}</span>
      <span class="num">${fmtMoney(p.value)} <span class="muted">${share.toFixed(1)}%</span></span></div>`;
  });
  s += '</div></div>';
  container.innerHTML = s;
}

function chartHeat(container, cells) {
  const map = new Map(cells.map((c) => [`${c.dow}-${c.hour}`, c]));
  const max = Math.max(1, ...cells.map((c) => c.requests));
  const color = (v) => {
    if (!v) return '#f2f4f7';
    const t = Math.pow(v / max, 0.6);
    const r = Math.round(232 - t * (232 - 47));
    const g = Math.round(238 - t * (238 - 111));
    const b = Math.round(245 - t * (245 - 235));
    return `rgb(${r},${g},${b})`;
  };
  let s = '<div class="heat"><div class="lbl"></div>';
  for (let h = 0; h < 24; h++) s += `<div class="hlbl">${h % 3 === 0 ? h : ''}</div>`;
  for (let d = 0; d < 7; d++) {
    s += `<div class="lbl">${DOW[d]}</div>`;
    for (let h = 0; h < 24; h++) {
      const c = map.get(`${d}-${h}`);
      const v = c ? c.requests : 0;
      const title = c ? `${DOW[d]} ${String(h).padStart(2, '0')}:00\n请求 ${fmtInt(v)}\n费用 ${fmtMoney(c.cost)}\ntoken ${fmtTok(c.tokens)}` : `${DOW[d]} ${h}:00\n无请求`;
      s += `<div class="cell" style="background:${color(v)}" title="${esc(title)}"></div>`;
    }
  }
  s += '</div>';
  container.innerHTML = s;
}

function hbarTable(container, items, opts = {}) {
  const { valueFmt = fmtMoney, max = 10, barColor = COLORS.accent, showShare = true } = opts;
  if (!items.length) { container.innerHTML = '<div class="empty">无数据</div>'; return; }
  const top = items.slice(0, max);
  const maxV = Math.max(...top.map((i) => i.value), 1e-9);
  container.innerHTML = `<table class="data"><tbody>` + top.map((it) => `
    <tr title="${esc(it.title || '')}">
      <td class="l">${esc(it.label)}${it.tag ? ` <span class="tag ${it.tagClass || 'mut'}">${esc(it.tag)}</span>` : ''}</td>
      <td style="width:34%"><div class="bar" style="margin:0"><i style="width:${((it.value / maxV) * 100).toFixed(1)}%;background:${barColor}"></i></div></td>
      <td class="num">${valueFmt(it.value)}</td>
      <td class="num muted">${showShare && it.share != null ? (it.share * 100).toFixed(1) + '%' : ''}</td>
    </tr>`).join('') + '</tbody></table>';
}

/* ------------------------------------------------------------------ *
 * 渲染：概览
 * ------------------------------------------------------------------ */
function kpiCard(label, value, foot, opts = {}) {
  return `<div class="kpi">
    <div class="label">${esc(label)}</div>
    <div class="value ${opts.small ? 'small' : ''}">${value}</div>
    ${foot ? `<div class="foot">${foot}</div>` : ''}
    ${opts.bar != null ? `<div class="bar"><i style="width:${Math.min(opts.bar * 100, 100).toFixed(1)}%;background:${opts.barColor || COLORS.accent}"></i></div>` : ''}
  </div>`;
}

function renderOverview() {
  const s = state.summary, k = s.kpi;
  const running = (state.live?.running || []).length;
  const usdNote = (state.bootstrap?.pricing?.display_currency === 'CNY')
    ? `≈ ${fmtUSD(k.cost)}` : `≈ ${curSym()}${(k.cost * (state.bootstrap?.pricing?.usd_to_cny || 7.1)).toFixed(2)}`;
  const billed = k.input_fresh + k.cache_read;
  el('ov-kpis').innerHTML = [
    kpiCard('请求数', fmtInt(k.requests), `会话 ${fmtInt(k.sessions)} · 轮次 ${fmtInt(k.turns)}${running ? ` · <span class="tag run">进行中 ${running}</span>` : ''}`),
    kpiCard('折算费用', fmtMoney(k.cost), `${usdNote} · 按公开 API 单价逐条折算`, { small: true }),
    kpiCard('总 token', fmtTok(k.tokens), `新增 ${fmtTok(k.input_fresh)} · 缓存 ${fmtTok(k.cache_read)} · 输出 ${fmtTok(k.output)}`),
    kpiCard('缓存命中率', fmtPct(k.cache_hit_rate), `命中 ${fmtTok(k.cache_read)} / 计费输入 ${fmtTok(billed)}`, { small: true, bar: k.cache_hit_rate, barColor: COLORS.cache_read }),
    kpiCard('错误率', fmtPct(k.error_rate), `错误 ${fmtInt(k.errors)} · 取消 ${fmtInt(k.cancelled)} · 重试 ${fmtInt(k.retries)}`, { small: true, bar: k.error_rate, barColor: '#e03131' }),
    kpiCard('平均延迟', fmtMs(k.avg_ms), `P50 ${fmtMs(k.p50_ms)} · P95 ${fmtMs(k.p95_ms)}`, { small: true }),
    kpiCard('平均首字延迟', fmtMs(k.avg_ttft_ms), 'time to first token', { small: true }),
    kpiCard('模型数', fmtInt(k.models), `推理 token ${fmtTok(k.reasoning)}`, { small: true }),
  ].join('');

  const notices = [];
  if (k.unpriced_requests) {
    notices.push(`<div class="notice">有 ${fmtInt(k.unpriced_requests)} 条请求（${fmtTok(k.unpriced_tokens)} token）没有匹配到单价，未计入费用：
      ${k.unpriced_models.map(esc).join('、')}。<a href="#settings">去「设置」页</a>填价格，
      或取消勾选把它们排除在统计之外。</div>`);
  }
  const assumed = (state.bootstrap?.models || []).filter((m) => m.pricing && m.pricing.source !== 'official');
  if (assumed.length) {
    const names = [...new Set(assumed.map((m) => m.pricing.label || m.model_id))];
    notices.push(`<div class="notice info">以下 ${names.length} 个模型的单价是估算值（没取到官方价目）：
      ${names.map(esc).join('、')}。<a href="#settings">去「设置」页核对</a> ——
      那里能一键采用 models.dev 的官方价。</div>`);
  }
  el('ov-notice').innerHTML = notices.join('');
  renderPriceConfig();

  chartBarsLine(el('ov-trend'), s.series_daily.map((d) => ({
    date: d.date, requests: d.requests, cost: d.cost, errors: d.errors, tokens: d.tokens,
  })));
  el('ov-trend-legend').innerHTML =
    `<span><i style="background:${COLORS.req}"></i>请求数</span>
     <span><i style="background:#e03131"></i>错误数</span>
     <span><i style="background:${COLORS.cost}"></i>费用（${curSym()}）</span>`;

  const mixParts = COST_LEGEND.map((c) => ({ label: c.label, color: c.color, value: k[c.key] || 0 }));
  chartDonut(el('ov-mix'), mixParts, fmtMoney(k.cost), '合计');
  el('ov-mix-hint').textContent = `${k.requests} 条请求`;

  hbarTable(el('ov-models'), s.by_model.map((m) => ({
    label: m.key,
    value: m.cost,
    share: m.cost_share,
    tag: m.price_source === 'official' ? null : (m.price_source ? '估算' : '未定价'),
    tagClass: m.price_source === 'official' ? 'mut' : 'est',
    title: `${m.requests} 次请求 · ${fmtTok(m.tokens)} token · 平均 ${fmtMs(m.avg_ms)}`,
  })), { max: 9, showShare: true });
  el('ov-model-hint').textContent = `共 ${s.by_model.length} 个模型`;

  renderRecent();
}

function renderRecent() {
  const rows = state.live?.recent || [];
  if (!rows.length) { el('ov-recent').innerHTML = '<div class="empty">还没有请求</div>'; return; }
  el('ov-recent').innerHTML = `<table class="data">
    <thead><tr><th class="l">时间</th><th class="l">模型</th><th class="l">项目</th><th>token</th><th>费用</th><th>耗时</th><th>状态</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="clickable" data-id="${r.id}">
      <td class="l">${fmtTime(r.started_at, false)}</td>
      <td class="l">${esc(r.model_id)}${r.attempt_index ? `<span class="tag mut">重试${r.attempt_index + 1}</span>` : ''}</td>
      <td class="l muted">${esc((r.project_dir || '').split('\\').pop() || '—')}</td>
      <td class="num">${fmtTok(r.tokens.total)}</td>
      <td class="num">${fmtMoney(r.cost.total)}</td>
      <td class="num">${fmtMs(r.duration_ms)}</td>
      <td>${STATUS_TAG[r.status] || esc(r.status)}</td></tr>`).join('')}</tbody></table>`;
}

/* ------------------------------------------------------------------ *
 * 渲染：分析
 * ------------------------------------------------------------------ */
const MODEL_COLUMNS = [
  { key: 'key', label: '模型', align: 'l', sortable: false },
  { key: 'requests', label: '请求', fmt: fmtInt },
  { key: 'input_fresh', label: '新增输入', fmt: fmtTok },
  { key: 'cache_read', label: '缓存命中', fmt: fmtTok },
  { key: 'output', label: '输出', fmt: fmtTok },
  { key: 'cache_hit_rate', label: '命中率', fmt: fmtPct },
  { key: 'cost', label: '费用', fmt: fmtMoney },
  { key: 'cost_share', label: '占比', fmt: (v) => ((v || 0) * 100).toFixed(1) + '%' },
  { key: 'avg_ms', label: '平均耗时', fmt: fmtMs },
  { key: 'error_rate', label: '错误率', fmt: fmtPct },
];

function renderModelTable() {
  const rows = [...(state.summary?.by_model || [])];
  const { key, order } = state.modelSort;
  rows.sort((a, b) => {
    const av = key === 'key' ? a.key : (a[key] ?? -1);
    const bv = key === 'key' ? b.key : (b[key] ?? -1);
    const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : (av - bv);
    return order === 'asc' ? cmp : -cmp;
  });
  const head = MODEL_COLUMNS.map((c) => {
    const active = state.modelSort.key === c.key;
    const arrow = active ? (state.modelSort.order === 'asc' ? ' ▲' : ' ▼') : '';
    return `<th class="${c.align === 'l' ? 'l' : ''}" data-sort="${c.key}" data-sortable="${c.sortable === false ? 'no' : 'yes'}">${c.label}${arrow}</th>`;
  }).join('');
  const body = rows.map((r) => `<tr>
    <td class="l">${esc(r.key)}${r.price_source && r.price_source !== 'official'
      ? ` <span class="tag est">${r.price_source === 'assumed' ? '估算价' : '未定价'}</span>` : ''}
      <div class="muted" style="font-size:11.5px">${esc(r.cost_rule || '')}</div></td>
    ${MODEL_COLUMNS.slice(1).map((c) => `<td class="num">${c.fmt(r[c.key])}</td>`).join('')}
  </tr>`).join('');
  const totals = ['合计', state.summary.kpi.requests, state.summary.kpi.input_fresh,
    state.summary.kpi.cache_read, state.summary.kpi.output, state.summary.kpi.cache_hit_rate,
    state.summary.kpi.cost, 1, state.summary.kpi.avg_ms, state.summary.kpi.error_rate];
  const foot = `<tfoot><tr>${totals.map((v, i) => `<td class="${i === 0 ? 'l' : 'num'}">${i === 0 ? v
    : MODEL_COLUMNS[i] ? MODEL_COLUMNS[i].fmt(v) : v}</td>`).join('')}</tr></tfoot>`;
  el('an-model-table').innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}</table></div>`;

  el('an-model-table').querySelectorAll('th[data-sort]').forEach((th) => {
    if (th.dataset.sortable === 'no') return;
    th.onclick = () => {
      const k = th.dataset.sort;
      state.modelSort = { key: k, order: state.modelSort.key === k && state.modelSort.order === 'desc' ? 'asc' : 'desc' };
      renderModelTable();
    };
  });
}

function renderAnalytics() {
  const s = state.summary;
  renderModelTable();

  chartStacked(el('an-tokens'), s.series_daily, TOKEN_LEGEND);
  el('an-tokens-legend').innerHTML = TOKEN_LEGEND
    .map((k) => `<span><i style="background:${k.color}"></i>${k.label}</span>`).join('');

  chartHeat(el('an-heat'), s.heatmap);

  hbarTable(el('an-projects'), s.by_project.map((p) => ({
    label: (p.directory || p.key).replace(/^[A-Z]:\\/, ''),
    value: p.cost, share: p.cost_share,
    title: `${p.requests} 次请求 · ${fmtTok(p.tokens)} token`,
  })), { max: 8 });

  hbarTable(el('an-agents'), s.by_agent.map((a) => ({
    label: a.key, value: a.cost, share: a.cost_share,
    title: `${a.requests} 次请求 · ${fmtTok(a.tokens)} token`,
  })), { max: 8, barColor: '#0ea5a4' });

  hbarTable(el('an-sources'), s.by_source.map((a) => ({
    label: a.key, value: a.cost, share: a.cost_share,
    title: `${a.requests} 次请求 · ${fmtTok(a.tokens)} token`,
  })), { max: 8, barColor: '#f59e0b' });

  renderCacheCard();
  renderTopRequests();
  renderPriceTable();
}

function renderCacheCard() {
  const s = state.summary;
  const priceOf = new Map((state.bootstrap?.models || [])
    .filter((m) => m.pricing).map((m) => [m.model_id, m.pricing]));
  const rate = state.bootstrap?.pricing?.usd_to_cny || 7.1;
  const isUSD = (state.bootstrap?.pricing?.display_currency || 'CNY') === 'USD';

  // 命中部分本可按原价计费，实际只付缓存价，差额即省下的钱（按单价表币种算完再换算成展示币种）
  let saved = 0;
  s.by_model.forEach((m) => {
    const p = priceOf.get(m.key);
    if (!p || p.input == null) return;
    const unitCache = p.cache_read != null ? p.cache_read : p.input * 0.1;
    const delta = (p.input - unitCache) * (m.cache_read / 1e6);      // 以单价表币种计
    const inDisplay = p.currency === (isUSD ? 'USD' : 'CNY')
      ? delta : (isUSD ? delta / rate : delta * rate);
    saved += inDisplay;
  });
  const cost = s.kpi.cost;
  const hit = s.kpi.cache_read, miss = s.kpi.input_fresh;
  el('an-cache').innerHTML = `
    <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
      ${kpiCard('缓存命中率', fmtPct(s.kpi.cache_hit_rate), `命中 ${fmtTok(hit)} · 新增 ${fmtTok(miss)}`, { small: true, bar: s.kpi.cache_hit_rate, barColor: COLORS.cache_read })}
      ${kpiCard('估计省下', fmtMoney(saved), '命中部分按原价与缓存价的差额', { small: true })}
      ${kpiCard('若不命中', fmtMoney(cost + saved), `当前实际 ${fmtMoney(cost)}`, { small: true })}
      ${kpiCard('缓存写入', fmtTok(s.kpi.cache_write), 'cache write token', { small: true })}
    </div>
    <p class="muted" style="font-size:12px;margin:10px 0 0">说明：DeepSeek 分时定价按高峰价估算节省额，未逐条区分高峰/低谷。</p>`;
}

function renderTopRequests() {
  const rows = state.summary.top_requests || [];
  el('an-top').innerHTML = `<table class="data">
    <thead><tr><th class="l">时间</th><th class="l">模型</th><th class="l">项目</th><th>token</th><th>费用</th><th>耗时</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="clickable" data-id="${r.id}">
      <td class="l">${fmtTime(r.started_at)}</td>
      <td class="l">${esc(r.model_id)}</td>
      <td class="l muted">${esc((r.project_id || '').replace(/^proj_/, ''))}</td>
      <td class="num">${fmtTok(r.tokens.total)}</td>
      <td class="num">${fmtMoney(r.cost)}</td>
      <td class="num">${fmtMs(r.duration_ms)}</td></tr>`).join('')}</tbody></table>`;
}

function priceConfigHTML() {
  const pricing = state.bootstrap?.pricing || {};
  const ignoredModels = pricing.ignored_models || [];
  const unknownPrice = pricing.unknown_model_price || { enabled: false };
  const ignoredDetail = ignoredModels.length
    ? ignoredModels.map((p) => `<code>${esc(p)}</code>`).join(' ')
    : '<span class="muted">未配置（所有模型都参与统计）</span>';
  const unknownDetail = unknownPrice.enabled
    ? `<code>${esc(unknownPrice.currency || 'USD')} 输入 ${unknownPrice.input ?? '—'} / 缓存读 ${unknownPrice.cache_read ?? '—'} / 缓存写 ${unknownPrice.cache_write ?? '—'} / 输出 ${unknownPrice.output ?? '—'}（每百万 token）</code>`
    : '<span class="muted">已禁用（未匹配规则的模型记为“未定价”，费用按 0 计）</span>';
  return `<div class="price-config">
    <div class="row"><span class="k">🚫 忽略模型</span><span class="v">${ignoredDetail}</span></div>
    <div class="row"><span class="k">⚙️ 未知模型价格</span><span class="v">${unknownDetail}</span></div>
    <div class="row"><span class="k">💰 汇率 / 展示币种</span><span class="v"><code>${esc(pricing.display_currency || 'CNY')}</code> · USD→CNY ${pricing.usd_to_cny ?? '—'}</span></div>
    <div class="hint">改 <code>prices.json</code> 里的 <code>ignored_models</code> / <code>unknown_model_price</code> 后点“刷新”即可生效，无需重启服务。</div>
  </div>`;
}

/** 概览页与单价表上方共用同一份配置摘要 */
function renderPriceConfig() {
  const ov = el('ov-price-config');
  if (ov) ov.innerHTML = priceConfigHTML();
}

function renderPriceTable() {
  const rules = state.bootstrap?.pricing?.rules || [];
  const used = new Map((state.bootstrap?.models || []).map((m) => [m.model_id, m]));
  const configInfo = priceConfigHTML();

  const body = rules.map((r) => {
    const hits = [...used.values()].filter((m) => m.pricing && m.pricing.label === (r.label || r.match));
    const n = hits.reduce((s, m) => s + m.requests, 0);
    const badge = r.source === 'official'
      ? '<span class="tag ok">官方价</span>' : '<span class="tag est">估算</span>';
    return `<tr>
      <td class="l"><code>${esc(r.match)}</code></td>
      <td class="l">${esc(r.label)} ${badge}</td>
      <td class="l muted">${esc(r.currency)}</td>
      <td class="num">${r.input ?? '—'}</td>
      <td class="num">${r.cache_read ?? '—'}</td>
      <td class="num">${r.cache_write ?? '—'}</td>
      <td class="num">${r.output ?? '—'}</td>
      <td class="num">${fmtInt(n)}</td>
      <td class="l muted" style="white-space:normal;max-width:340px">${esc(r.note || '')}</td>
    </tr>`;
  }).join('');
  el('an-prices').innerHTML = `${configInfo}<div class="table-wrap"><table class="data">
    <thead><tr><th class="l">规则</th><th class="l">名称</th><th class="l">币种</th>
      <th>输入</th><th>缓存读</th><th>缓存写</th><th>输出</th><th>命中请求</th><th class="l">备注</th></tr></thead>
    <tbody>${body}</tbody></table>
    <p class="muted" style="font-size:12px;padding:10px 16px 14px">
      单价单位：每 100 万 token。文件位置：<code>prices.json</code>，改完点“刷新”即可生效。</p></div>`;
}

/* ------------------------------------------------------------------ *
 * 渲染：请求事件
 * ------------------------------------------------------------------ */
async function loadEvents() {
  const params = queryString({
    page: state.page, size: state.size,
    sort: state.eventSort.key, order: state.eventSort.order,
  });
  state.events = await fetchJSON('/api/events?' + params);
  renderEvents();
}

function renderEvents() {
  const data = state.events;
  if (!data) return;
  el('ev-hint').innerHTML = `共 ${fmtInt(data.total)} 条 · 合计 ${fmtMoney(data.cost_total)} · ${fmtTok(data.tokens_total)} token · 点击行看详情`;

  const th = (key, label, cls = '') => {
    const active = state.eventSort.key === key;
    const arrow = active ? (state.eventSort.order === 'asc' ? ' ▲' : ' ▼') : '';
    return `<th class="${cls}" data-sort="${key}">${label}${arrow}</th>`;
  };
  const rows = data.items.map((r) => `<tr class="clickable" data-id="${r.id}">
    <td class="l">${fmtTime(r.started_at)}</td>
    <td class="l">${esc(r.model_id)}${r.attempt_index ? ` <span class="tag mut">#${r.attempt_index + 1}</span>` : ''}</td>
    <td class="l muted">${esc((r.project_dir || '').split('\\').pop() || '—')}</td>
    <td class="l muted">${esc(r.query_source || '')}</td>
    <td class="num">${fmtTok(r.tokens.input)}</td>
    <td class="num muted">${fmtTok(r.tokens.cache_read)}</td>
    <td class="num">${fmtTok(r.tokens.output)}</td>
    <td class="num">${fmtMoney(r.cost.total)}${r.cost.total ? '' : '<span class="tag est">未定价</span>'}</td>
    <td class="num">${fmtMs(r.duration_ms)}</td>
    <td class="num">${fmtMs(r.ttft_ms)}</td>
    <td>${STATUS_TAG[r.status] || esc(r.status)}</td>
  </tr>`).join('');

  el('ev-table').innerHTML = `<div class="table-wrap"><table class="data">
    <thead><tr>${th('started_at', '时间', 'l')}
      <th class="l">模型</th><th class="l">项目</th><th class="l">来源</th>
      ${th('tokens', '输入(含缓存)')}<th>缓存命中</th><th>输出</th>${th('cost', '费用')}
      ${th('duration', '耗时')}${th('ttft', '首字')}<th>状态</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="11"><div class="empty">没有符合条件的请求</div></td></tr>'}</tbody>
  </table></div>`;

  el('ev-table').querySelectorAll('th[data-sort]').forEach((node) => {
    node.onclick = () => {
      const k = node.dataset.sort;
      state.eventSort = {
        key: k,
        order: state.eventSort.key === k && state.eventSort.order === 'desc' ? 'asc' : 'desc',
      };
      state.page = 1;
      loadEvents();
    };
  });

  const pages = Math.max(Math.ceil(data.total / data.size), 1);
  el('ev-pager').innerHTML = `
    <button id="pg-first" ${data.page <= 1 ? 'disabled' : ''}>« 首页</button>
    <button id="pg-prev" ${data.page <= 1 ? 'disabled' : ''}>上一页</button>
    <span>第 ${data.page} / ${pages} 页</span>
    <button id="pg-next" ${data.page >= pages ? 'disabled' : ''}>下一页</button>
    <button id="pg-last" ${data.page >= pages ? 'disabled' : ''}>末页 »</button>
    <span class="spacer"></span>
    <span>每页
      <select id="pg-size">${[50, 100, 200, 500].map((n) => `<option ${n === data.size ? 'selected' : ''}>${n}</option>`).join('')}</select>
      条</span>`;
  const go = (p) => { state.page = Math.min(Math.max(p, 1), pages); loadEvents(); };
  el('pg-first').onclick = () => go(1);
  el('pg-prev').onclick = () => go(data.page - 1);
  el('pg-next').onclick = () => go(data.page + 1);
  el('pg-last').onclick = () => go(pages);
  el('pg-size').onchange = (e) => { state.size = Number(e.target.value); state.page = 1; loadEvents(); };
}

/* ------------------------------------------------------------------ *
 * 抽屉：单条请求详情
 * ------------------------------------------------------------------ */
async function openDrawer(id) {
  state.drawerId = id;
  el('drawer').classList.remove('hidden');
  el('drawer-content').innerHTML = '<div class="empty">载入中…</div>';
  let d;
  try {
    d = await fetchJSON('/api/event?id=' + encodeURIComponent(id));
  } catch (e) {
    el('drawer-content').innerHTML = `<div class="notice">载入失败：${esc(e.message)}</div>`;
    return;
  }
  const r = d.request, tk = r.tokens, c = r.cost;
  el('drawer-title').textContent = `${r.model_id} · ${fmtTime(r.started_at)}`;
  const row = (k, v) => `<dt>${esc(k)}</dt><dd>${v}</dd>`;
  const tokenRows = [
    ['输入合计（含缓存）', fmtInt(tk.input)],
    ['— 其中新增输入', fmtInt(tk.input_fresh)],
    ['— 其中缓存命中', fmtInt(tk.cache_read)],
    ['缓存写入', fmtInt(tk.cache_write)],
    ['输出', fmtInt(tk.output)],
    ['— 其中推理', fmtInt(tk.reasoning)],
    ['合计', fmtInt(tk.total)],
  ].map(([a, b]) => `<tr><td class="l">${a}</td><td class="num">${b}</td></tr>`).join('');
  const costRows = [
    ['新增输入', c.components.input],
    ['缓存命中', c.components.cache_read],
    ['缓存写入', c.components.cache_write],
    ['输出', c.components.output],
  ].map(([a, b]) => `<tr><td class="l">${a}</td><td class="num">${fmtMoney(b)}</td></tr>`).join('');

  el('drawer-content').innerHTML = `
    <dl class="kv">
      ${row('状态', (STATUS_TAG[r.status] || esc(r.status)) + (r.finish_reason ? ` <span class="tag mut">${esc(r.finish_reason)}</span>` : ''))}
      ${row('模型 / Provider', `${esc(r.model_id)} <span class="muted">${esc(r.provider_id)}</span>`)}
      ${row('单价规则', c.priced ? `${esc(c.label || '')} <span class="tag ${c.source === 'official' ? 'ok' : 'est'}">${c.source === 'official' ? '官方价' : '估算'}</span>` : '<span class="tag est">未定价</span>')}
      ${row('费用', `<b>${fmtMoney(c.total)}</b> <span class="muted">(${fmtUSD(c.total)})</span>`)}
      ${row('项目', esc(r.project_dir || '—'))}
      ${row('会话', `${esc(r.session_title || '—')}<div class="muted" style="font-size:11.5px">${esc(r.session_id || '')}</div>`)}
      ${row('Agent / 来源', `${esc(r.agent || '—')} · ${esc(r.query_source || '')}`)}
      ${row('耗时 / 首字', `${fmtMs(r.duration_ms)} / ${fmtMs(r.ttft_ms)}`)}
      ${row('尝试 / 重试', `${r.attempt_index + 1} 次调用 · 重试 ${r.retry_count}`)}
      ${row('工具调用数', fmtInt(r.tool_call_count))}
      ${row('turn / trace', `<span class="muted" style="font-size:11.5px">${esc(r.turn_id || '')}<br>${esc(r.trace_id || '')}</span>`)}
      ${row('请求 ID', `<span class="muted" style="font-size:11.5px">${esc(d.logical_request_id || r.id)}</span>`)}
      ${r.error_message ? row('错误', `<span style="color:#d92d20">${esc(r.error_type || '')} ${esc(r.error_code || '')}<br>${esc(r.error_message)}</span>`) : ''}
    </dl>
    <p class="section-title">TOKEN 明细</p>
    <table class="data" style="margin-bottom:16px"><tbody>${tokenRows}</tbody></table>
    <p class="section-title">费用分解</p>
    <table class="data" style="margin-bottom:16px"><tbody>${costRows}
      <tr><td class="l"><b>合计</b></td><td class="num"><b>${fmtMoney(c.total)}</b></td></tr></tbody></table>
    <p class="section-title">原始 usage（来自 provider）</p>
    <pre class="json">${esc(JSON.stringify(d.raw_usage, null, 2) || '—')}</pre>
    <p class="section-title">provider 元数据</p>
    <pre class="json">${esc(JSON.stringify(d.provider_metadata, null, 2) || '—')}</pre>`;
}

/* ------------------------------------------------------------------ *
 * 过滤器与主流程
 * ------------------------------------------------------------------ */
function fillSelect(id, options, selected, placeholder = '全部') {
  const node = el(id);
  const current = selected ?? node.value;
  node.innerHTML = `<option value="">${placeholder}</option>` + options.map((o) => {
    const value = typeof o === 'string' ? o : o.value;
    const label = typeof o === 'string' ? o : (o.label || o.value);
    const count = typeof o === 'string' ? '' : (o.requests != null ? ` (${o.requests})` : '');
    return `<option value="${esc(value)}">${esc(label)}${count}</option>`;
  }).join('');
  if ([...node.options].some((o) => o.value === current)) node.value = current;
}

function buildFilters() {
  const b = state.bootstrap;
  if (!b) return;
  fillSelect('f-model', b.models.map((m) => ({ value: m.model_id, label: m.model_id, requests: m.requests })));
  fillSelect('f-provider', b.facets.providers);
  fillSelect('f-project', b.facets.projects.map((p) => ({ value: p.value, label: (p.label || p.value).replace(/^[A-Z]:\\/, ''), requests: p.requests })));
  fillSelect('f-agent', b.facets.agents);
  fillSelect('f-source', b.facets.sources);
  fillSelect('f-status', b.facets.statuses);
  Object.keys(state.filters).forEach((k) => {
    const node = el('f-' + k);
    if (node) node.value = state.filters[k] || '';
  });
}

function bindFilters() {
  ['model', 'provider', 'project', 'agent', 'source', 'status', 'priced', 'q'].forEach((k) => {
    const node = el('f-' + k);
    const handler = () => {
      state.filters[k] = node.value;
      state.page = 1;
      refresh();
    };
    node.addEventListener('change', handler);
    if (k === 'q') {
      let timer;
      node.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(handler, 450);
      });
    }
  });
  ['from', 'to'].forEach((k) => {
    el('f-' + k).addEventListener('change', () => {
      state.filters[k] = el('f-' + k).value;
      state.range = 'custom';
      [...el('range-group').children].forEach((b) => b.classList.remove('on'));
      state.page = 1;
      refresh();
    });
  });
  [...el('range-group').children].forEach((btn) => {
    btn.onclick = () => {
      state.range = btn.dataset.range;
      [...el('range-group').children].forEach((b) => b.classList.toggle('on', b === btn));
      el('f-from').value = '';
      el('f-to').value = '';
      state.filters.from = '';
      state.filters.to = '';
      state.page = 1;
      refresh();
    };
  });
  el('btn-reset').onclick = () => {
    state.filters = { from: '', to: '', model: '', provider: '', project: '', agent: '', source: '', status: '', priced: '', q: '' };
    state.range = '30d';
    [...el('range-group').children].forEach((b) => b.classList.toggle('on', b.dataset.range === '30d'));
    el('f-from').value = '';
    el('f-to').value = '';
    Object.keys(state.filters).forEach((k) => { const n = el('f-' + k); if (n) n.value = ''; });
    state.page = 1;
    refresh();
  };
}

function captureScroll() {
  const saved = {};
  document.querySelectorAll('.table-wrap').forEach((w, i) => { saved[i] = w.scrollTop; });
  return saved;
}

function restoreScroll(saved) {
  document.querySelectorAll('.table-wrap').forEach((w, i) => {
    if (saved[i]) w.scrollTop = saved[i];
  });
}

const TABS = ['overview', 'analytics', 'events', 'settings'];

function openTab(tab, { updateHash = true } = {}) {
  if (!TABS.includes(tab)) tab = 'overview';
  state.tab = tab;
  document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  TABS.forEach((t) => el('tab-' + t).classList.toggle('hidden', t !== tab));
  if (updateHash && location.hash.slice(1) !== tab) history.replaceState(null, '', '#' + tab);
  // 切回来立刻用已有数据渲染，别让用户对着空卡片等下一次自动刷新
  if (tab === 'events') loadEvents();
  else if (tab === 'settings') loadModels();
  else if (state.summary) (tab === 'overview' ? renderOverview : renderAnalytics)();
}

async function refresh({ reloadBootstrap = false } = {}) {
  try {
    if (reloadBootstrap || !state.bootstrap) state.bootstrap = await fetchJSON('/api/bootstrap');
    const [summary, live] = await Promise.all([
      fetchJSON('/api/summary?' + queryString()),
      fetchJSON('/api/live'),
    ]);
    state.summary = summary;
    state.live = live;

    el('db-info').textContent =
      `${state.bootstrap.total_rows} 条记录 · ${fmtTime(state.bootstrap.range.min)} ~ ${fmtTime(state.bootstrap.range.max)} · ${state.bootstrap.db_path}`;
    el('pricing-pill').textContent =
      `${state.bootstrap.pricing.display_currency} · 汇率 ${state.bootstrap.pricing.usd_to_cny} · ${state.bootstrap.pricing.rules.length} 条单价规则`;
    const unpriced = summary.kpi.unpriced_requests;
    el('unpriced-pill').classList.toggle('hidden', !unpriced);
    el('unpriced-pill').textContent = `${unpriced} 条未定价`;
    el('btn-export').href = '/api/export.csv?' + queryString();

    // index.html 给每个下拉框预置了一个「全部」选项，options.length 恒为 1，
    // 拿它判断是否已填充会让 buildFilters 永远不执行。改用 facet 指纹比较。
    const b = state.bootstrap;
    const facetSig = JSON.stringify([
      b.models.map((m) => m.model_id), b.facets.providers,
      b.facets.projects.map((p) => p.value), b.facets.agents,
      b.facets.sources, b.facets.statuses,
    ]);
    if (facetSig !== state.facetSig) {
      buildFilters();
      state.facetSig = facetSig;
    }

    // 自动刷新时别把已经滚到一半的表格弹回顶部
    const scroll = captureScroll();
    if (state.tab === 'settings') { /* 不动：避免覆盖未保存的编辑 */ }
    else if (state.tab === 'overview') renderOverview();
    else if (state.tab === 'analytics') renderAnalytics();
    else await loadEvents();
    restoreScroll(scroll);
    if (state.drawerId && !el('drawer').classList.contains('hidden')) openDrawer(state.drawerId);
  } catch (e) {
    el('ov-notice').innerHTML = `<div class="notice">连不上本地服务，可能已经自动退出（标签关闭后服务会自己停）。
      重新打开：输入 <code>/usage</code>。原始错误：${esc(e.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ *
 * 渲染：设置（逐模型的勾选 / 别名 / 价格）
 *
 * 表格本身就是编辑态：保存时直接读 DOM，不额外维护一份可失同步的状态。
 * 只有筛选导致重绘时，才先把当前输入收进 state.modelEdits 以免丢失。
 * ------------------------------------------------------------------ */
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

/** 把表格里当前的输入收成一份 {model_id: {...}}，用于重绘前暂存或提交 */
function collectModelRows() {
  const out = {};
  document.querySelectorAll('#st-table tr[data-mid]').forEach((tr) => {
    const mid = tr.dataset.mid;
    const num = (k) => {
      const node = tr.querySelector(`.st-num[data-k="${k}"]`);
      const v = node ? node.value.trim() : '';
      return v === '' ? null : Number(v);
    };
    out[mid] = {
      alias: (tr.querySelector('.st-alias')?.value || '').trim(),
      enabled: !!tr.querySelector('.st-on')?.checked,
      price: {
        currency: tr.querySelector('.st-cur')?.value || 'USD',
        input: num('input'),
        cache_read: num('cache_read'),
        cache_write: num('cache_write'),
        output: num('output'),
      },
    };
  });
  return out;
}

function stashModelEdits() {
  if (!el('st-table')?.querySelector('tr[data-mid]')) return;
  Object.assign(state.modelEdits, collectModelRows());
}

function stNum(v) {
  return v === null || v === undefined ? '' : String(v);
}

function stSourceTag(m) {
  if (m.ignored) return '<span class="tag err">已忽略</span>';
  if (m.source === 'manual') return '<span class="tag ok">手填</span>';
  if (m.source === 'unmatched') return '<span class="tag warn">未匹配</span>';
  const src = m.source.startsWith('rule:') ? m.source.slice(5) : m.source;
  const cls = src === 'official' ? 'ok' : (src === 'models.dev' ? 'mut' : 'est');
  return `<span class="tag ${cls}">规则·${esc(src)}</span>`;
}

/** 有没有可用价格（来自规则或 models.dev）。目录未就绪时不能拿 models_dev 当判据，
 *  否则「取消未匹配」会把所有行一起取消。 */
function stIsMatched(m) {
  if (!m) return false;
  return !!(m.price || m.models_dev) && m.source !== 'unmatched';
}

function stModelsDevCell(m) {
  const md = m.models_dev;
  if (!md) return '<span class="muted">—</span>';
  const p = md.price || {};
  const money = (v) => (v === null || v === undefined ? '—' : v);
  return `<div class="md-cell">
    <div><code>${esc(md.id || '')}</code> <span class="muted">${esc(md.provider || '')}</span>
      <span class="tag mut">${esc(md.how || '')}</span></div>
    <div class="muted">输入 ${money(p.input)} · 缓存读 ${money(p.cache_read)} · 缓存写 ${money(p.cache_write)} · 输出 ${money(p.output)}</div>
    <button class="st-adopt" data-mid="${esc(m.model_id)}">采用这组价格</button>
  </div>`;
}

function renderModels() {
  const data = state.models;
  if (!data) return;
  const q = (state.stFilter || '').trim().toLowerCase();
  const all = data.models || [];
  const rows = all.filter((m) => {
    if (!q) return true;
    const md = m.models_dev || {};
    return [m.model_id, m.alias, md.id, md.provider]
      .filter(Boolean).join(' ').toLowerCase().includes(q);
  });

  const s = data.summary || {};
  el('st-hint').textContent =
    `勾选的模型才计入统计 · 共 ${s.total} 个，已选 ${s.enabled}，未匹配 ${s.unmatched} · 改完点保存`;
  el('st-note').innerHTML =
    '别名留空则用记录到的原始模型名。<b>多个模型填同一个别名会被合并成一行</b>'
    + '（例如 <code>glm-5.3-flash</code> 与 <code>GLM-5.3-Flash</code>）。'
    + '价格与规则表中的值一致时不会写进配置，该行继续跟随 <code>prices.json</code> 的规则；'
    + '改过才会钉成手动价。';

  const body = rows.map((m) => {
    const e = state.modelEdits[m.model_id] || {};
    const alias = e.alias !== undefined ? e.alias : (m.alias || '');
    const enabled = e.enabled !== undefined ? e.enabled : m.enabled;
    const price = e.price || m.price || {};
    const cur = price.currency || 'USD';
    const prov = (m.providers || []).join(', ');
    return `<tr data-mid="${esc(m.model_id)}" class="${enabled ? '' : 'st-off'}">
      <td><input type="checkbox" class="st-on" ${enabled ? 'checked' : ''}
        ${m.ignored ? 'disabled title="在 ignored_models 里，需先从忽略列表移除"' : ''}></td>
      <td class="l"><code>${esc(m.model_id)}</code>
        ${m.ignored ? '<span class="tag err">忽略中</span>' : ''}
        <div class="muted st-sub">${esc(prov)}</div></td>
      <td><input class="st-alias" value="${esc(alias)}"
        placeholder="${esc((m.models_dev && m.models_dev.id) || '别名')}"></td>
      <td><input class="st-num" data-k="input" value="${stNum(price.input)}"></td>
      <td><input class="st-num" data-k="cache_read" value="${stNum(price.cache_read)}"></td>
      <td><input class="st-num" data-k="cache_write" value="${stNum(price.cache_write)}"></td>
      <td><input class="st-num" data-k="output" value="${stNum(price.output)}"></td>
      <td><select class="st-cur">
        <option value="USD"${cur === 'USD' ? ' selected' : ''}>USD</option>
        <option value="CNY"${cur === 'CNY' ? ' selected' : ''}>CNY</option>
      </select></td>
      <td class="l">${stSourceTag(m)}
        <div class="muted st-sub">${esc(m.rule_match || '')}</div></td>
      <td class="num">${fmtInt(m.requests)}</td>
      <td class="l">${stModelsDevCell(m)}</td>
    </tr>`;
  }).join('');

  el('st-table').innerHTML = `<div class="table-wrap"><table class="data st-table">
    <thead><tr>
      <th>启用</th><th class="l">记录的模型名</th><th class="l">别名</th>
      <th>输入</th><th>缓存读</th><th>缓存写</th><th>输出</th><th>币种</th>
      <th class="l">价格来源</th><th>请求</th><th class="l">models.dev 参考（每百万 token）</th>
    </tr></thead>
    <tbody>${body || '<tr><td colspan="11"><div class="empty">没有匹配的模型</div></td></tr>'}</tbody>
  </table></div>`;

  el('st-table').querySelectorAll('.st-adopt').forEach((btn) => {
    btn.onclick = () => {
      const mid = btn.dataset.mid;
      const m = all.find((x) => x.model_id === mid);
      if (!m || !m.models_dev) return;
      const tr = [...el('st-table').querySelectorAll('tr[data-mid]')]
        .find((r) => r.dataset.mid === mid);
      if (!tr) return;
      const p = m.models_dev.price || {};
      ['input', 'cache_read', 'cache_write', 'output'].forEach((k) => {
        const node = tr.querySelector(`.st-num[data-k="${k}"]`);
        if (node) node.value = p[k] === null || p[k] === undefined ? '' : p[k];
      });
      const cur = tr.querySelector('.st-cur');
      if (cur) cur.value = p.currency || 'USD';
      el('st-status').textContent = '已填入 models.dev 价格，记得保存';
    };
  });

  el('st-table').querySelectorAll('.st-on').forEach((cb) => {
    cb.onchange = () => {
      const tr = cb.closest('tr');
      tr.classList.toggle('st-off', !cb.checked);
    };
  });
}

function stAgeText(seconds) {
  if (seconds == null) return '';
  if (seconds < 90) return '刚刚';
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时`;
  return `${Math.round(seconds / 86400)} 天`;
}

/** 忽略列表：已选项做成可删的 chip，新增项从下拉里挑（也允许直接敲通配符） */
function renderIgnored() {
  const pats = state.ignoredDraft || [];
  const allModels = (state.models?.models || []).map((m) => m.model_id);
  const taken = new Set(pats.map((p) => p.toLowerCase()));
  const options = allModels
    .filter((m) => !taken.has(m.toLowerCase()))
    .map((m) => `<option value="${esc(m)}"></option>`).join('');

  const chips = pats.length
    ? pats.map((p, i) => `<span class="chip">${esc(p)}<button data-i="${i}" title="移除">×</button></span>`).join('')
    : '<span class="muted">还没有忽略任何模型</span>';

  el('st-ignored').innerHTML = `
    <div class="ignored-chips" id="st-ignored-chips">${chips}</div>
    <div class="ignored-edit">
      <input type="text" id="st-ignored-input" list="st-ignored-list" autocomplete="off"
        placeholder="从下拉里选一个已记录的模型，或直接输入通配符（如 *test*）">
      <datalist id="st-ignored-list">${options}</datalist>
      <button id="st-ignored-add">添加</button>
      <button id="st-ignored-save" class="primary">保存忽略列表</button>
    </div>
    <p class="muted st-note">
      支持 <code>*</code> 通配、大小写不敏感，命中的模型<b>完全不出现在看板里</b>（含筛选下拉与导出），
      比在表格里取消勾选更彻底。共 ${pats.length} 条。
    </p>`;

  const input = el('st-ignored-input');
  const add = () => {
    const v = (input.value || '').trim();
    if (!v) return;
    if (!pats.some((p) => p.toLowerCase() === v.toLowerCase())) pats.push(v);
    input.value = '';
    renderIgnored();
    el('st-ignored-input')?.focus();
  };
  el('st-ignored-add').onclick = add;
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
  // datalist 选中后多数浏览器不触发 change，这里补一个 input 事件兜底
  input.addEventListener('change', () => { if (input.value.trim()) add(); });

  el('st-ignored-chips').querySelectorAll('button[data-i]').forEach((btn) => {
    btn.onclick = () => {
      pats.splice(Number(btn.dataset.i), 1);
      renderIgnored();
    };
  });

  el('st-ignored-save').onclick = async () => {
    el('st-status').textContent = '保存中…';
    try {
      // 只提交这一个字段：GET /api/prices 是裁剪视图，整体回写会丢 peak / defaults
      await postJSON('/api/models', { ignored_models: pats });
      state.bootstrap = await fetchJSON('/api/bootstrap');
      await refresh();
      renderIgnored();
      el('st-status').textContent = `忽略列表已保存（${pats.length} 条）`;
    } catch (e) {
      el('st-status').textContent = '保存失败：' + e.message;
    }
  };
}

async function loadModels({ retries = 6 } = {}) {
  el('st-status').textContent = '加载中…';
  try {
    state.models = await fetchJSON('/api/models');
  } catch (e) {
    el('st-status').textContent = '加载失败：' + e.message;
    return;
  }
  if (state.ignoredDraft === null) {
    state.ignoredDraft = [...((state.bootstrap?.pricing?.ignored_models) || [])];
  }
  renderModels();
  renderIgnored();
  const c = state.models.catalog || {};
  if (c.ready) {
    // 有数据就用，哪怕是旧缓存 —— 网络不通不该让参考价整体消失
    el('st-status').textContent = c.error
      ? `参考价取自 ${stAgeText(c.age_seconds)}前的缓存（models.dev 暂时不可达）`
      : (c.stale ? `参考价取自 ${stAgeText(c.age_seconds)}前的缓存，正在后台更新` : '已就绪');
  } else if (c.loading && retries > 0) {
    // 首次拉取要 8~10 秒，稍后自己再来一次，别让首屏干等
    el('st-status').textContent = '正在拉取 models.dev 目录（首次约 10 秒）…';
    setTimeout(() => { if (state.tab === 'settings') loadModels({ retries: retries - 1 }); }, 3000);
  } else {
    el('st-status').textContent = c.error
      ? `models.dev 不可达，参考价暂缺：${c.error}`
      : '目录未就绪，可点「重新匹配」';
  }
}

async function saveModels() {
  const rows = collectModelRows();
  const count = Object.keys(rows).length;
  if (!count) { el('st-status').textContent = '没有可保存的行'; return; }
  el('st-status').textContent = `保存 ${count} 行…`;
  try {
    await postJSON('/api/models', { models: rows });
    state.modelEdits = {};
    state.bootstrap = await fetchJSON('/api/bootstrap');  // 名字/价格都变了，重取
    await refresh({ reloadBootstrap: false });
    await loadModels();
    el('st-status').textContent = `已保存 ${count} 行，统计已按新的勾选重算`;
  } catch (e) {
    el('st-status').textContent = '保存失败：' + e.message;
  }
}

function bindSettings() {
  el('st-save').onclick = saveModels;
  el('st-filter').addEventListener('input', () => {
    stashModelEdits();          // 重绘前先把输入收好，免得筛选把它们冲掉
    state.stFilter = el('st-filter').value;
    renderModels();
  });
  el('st-match').onclick = async () => {
    el('st-status').textContent = '正在重新拉取 models.dev 目录…';
    try {
      await postJSON('/api/models/match', {});
      for (let i = 0; i < 24; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const d = await fetchJSON('/api/models');
        if (d.catalog && d.catalog.ready && !d.catalog.loading) {
          state.models = d;
          renderModels();
          renderIgnored();
          el('st-status').textContent = `目录已更新（${d.catalog.size} 个带报价的模型）`;
          return;
        }
      }
      el('st-status').textContent = '目录拉取超时，请稍后重试';
    } catch (e) {
      el('st-status').textContent = '拉取失败：' + e.message;
    }
  };
  el('st-check-matched').onclick = () => {
    el('st-table').querySelectorAll('tr[data-mid]').forEach((tr) => {
      const row = (state.models?.models || []).find((m) => m.model_id === tr.dataset.mid);
      const cb = tr.querySelector('.st-on');
      if (cb && !cb.disabled && stIsMatched(row)) { cb.checked = true; tr.classList.remove('st-off'); }
    });
  };
  el('st-uncheck-unmatched').onclick = () => {
    el('st-table').querySelectorAll('tr[data-mid]').forEach((tr) => {
      const row = (state.models?.models || []).find((m) => m.model_id === tr.dataset.mid);
      const cb = tr.querySelector('.st-on');
      if (cb && !cb.disabled && row && !stIsMatched(row)) { cb.checked = false; tr.classList.add('st-off'); }
    });
  };
}

function init() {
  document.querySelectorAll('.tabs button').forEach((b) => b.onclick = () => openTab(b.dataset.tab));
  el('btn-refresh').onclick = () => refresh({ reloadBootstrap: true });
  el('drawer-close').onclick = () => { el('drawer').classList.add('hidden'); state.drawerId = null; };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') el('drawer-close').onclick(); });
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('tr.clickable');
    if (tr && tr.dataset.id) openDrawer(tr.dataset.id);
  });
  // 心跳与告别：服务端用 --auto-shutdown 时据此判断标签是否已关闭
  const beat = () => fetch('/api/ping?sid=' + encodeURIComponent(state.sid), { cache: 'no-store' }).catch(() => {});
  beat();
  setInterval(beat, 4000);
  window.addEventListener('pagehide', () => {
    try { navigator.sendBeacon('/api/bye?sid=' + encodeURIComponent(state.sid)); } catch (e) { /* 关不掉就算了，靠心跳超时兜底 */ }
  });

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.summary && state.tab !== 'events') refresh(); }, 250);
  });
  setInterval(() => {
    if (el('auto-refresh').checked && !state.drawerId) refresh();
  }, 15000);

  window.addEventListener('hashchange', () => {
    const tab = location.hash.slice(1);
    if (tab !== state.tab) openTab(tab, { updateHash: false });
  });

  bindFilters();
  bindSettings();
  openTab(location.hash.slice(1) || 'overview');
  refresh({ reloadBootstrap: true });
}

init();
