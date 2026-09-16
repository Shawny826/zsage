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
      ${k.unpriced_models.map(esc).join('、')}。到 <code>prices.json</code> 里加一条规则即可。</div>`);
  }
  const assumed = (state.bootstrap?.models || []).filter((m) => m.pricing && m.pricing.source !== 'official');
  if (assumed.length) {
    notices.push(`<div class="notice info">以下模型的单价是估算值（官方价目页取不到），请核对后改 <code>prices.json</code>：
      ${assumed.map((m) => esc(m.model_id)).join('、')}</div>`);
  }
  el('ov-notice').innerHTML = notices.join('');

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

function renderPriceTable() {
  const rules = state.bootstrap?.pricing?.rules || [];
  const used = new Map((state.bootstrap?.models || []).map((m) => [m.model_id, m]));
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
  el('an-prices').innerHTML = `<div class="table-wrap"><table class="data">
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

const TABS = ['overview', 'analytics', 'events'];

function openTab(tab, { updateHash = true } = {}) {
  if (!TABS.includes(tab)) tab = 'overview';
  state.tab = tab;
  document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  TABS.forEach((t) => el('tab-' + t).classList.toggle('hidden', t !== tab));
  if (updateHash && location.hash.slice(1) !== tab) history.replaceState(null, '', '#' + tab);
  // 切回来立刻用已有数据渲染，别让用户对着空卡片等下一次自动刷新
  if (tab === 'events') loadEvents();
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

    if (!el('f-model').options.length) buildFilters();

    // 自动刷新时别把已经滚到一半的表格弹回顶部
    const scroll = captureScroll();
    if (state.tab === 'overview') renderOverview();
    else if (state.tab === 'analytics') renderAnalytics();
    else await loadEvents();
    restoreScroll(scroll);
    if (state.drawerId && !el('drawer').classList.contains('hidden')) openDrawer(state.drawerId);
  } catch (e) {
    el('ov-notice').innerHTML = `<div class="notice">连不上本地服务，可能已经自动退出（标签关闭后服务会自己停）。
      重新打开：输入 <code>/usage</code>。原始错误：${esc(e.message)}</div>`;
  }
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
  openTab(location.hash.slice(1) || 'overview');
  refresh({ reloadBootstrap: true });
}

init();
