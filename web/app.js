/* Order window metrics console - plain JS, no dependencies. */
'use strict';

const state = {
  data: null,          // /api/state snapshot
  demos: [],
  asOf: null,          // selected epoch ms
  selectedCustomer: null,
  preview: null,       // last preview/commit result
  impactResult: null,  // result currently rendered in the impact section
  impactCommitted: false,
  showAllCells: false,
  selectedBatch: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* --------------------------------------------------------------- formatting */
const money = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
function yuan(cents) { return money.format(cents / 100); }
function yuanShort(cents) {
  const value = cents / 100;
  if (Math.abs(value) >= 1e8) return (value / 1e8).toFixed(2) + ' 亿';
  if (Math.abs(value) >= 1e4) return (value / 1e4).toFixed(2) + ' 万';
  return money.format(value);
}
function shortTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
function dayTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}
function longTime(iso) {
  if (!iso) return '—';
  return iso.replace('T', ' ');
}
/* Compact timestamp for dense tables; full ISO stays in the title attribute. */
function stamp(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
    + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function badge(text, kind) { return el('span', `badge badge-${kind}`, text); }

/* ------------------------------------------------------------------ network */
async function api(path, options) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await resp.json().catch(() => ({ error: '响应不是有效 JSON' }));
  if (!resp.ok) {
    const err = new Error(body.error || `HTTP ${resp.status}`);
    err.payload = body;
    err.status = resp.status;
    throw err;
  }
  return body;
}

let toastTimer = null;
function toast(message, bad = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = 'toast' + (bad ? ' bad' : '');
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, bad ? 6000 : 3500);
}

/* ------------------------------------------------------------------- loading */
async function loadState() {
  if (state.asOf && state.data
      && !state.data.evaluation_instants.some(item => item.ms === state.asOf)) {
    state.asOf = null;   // dataset changed; fall back to the latest anchor
  }
  const query = state.asOf
    ? `?as_of=${encodeURIComponent(new Date(state.asOf).toISOString())}`
    : '';
  let data = await api('/api/state' + query);
  if (!state.asOf) {
    state.asOf = data.latest_event_ms;
    data = await api('/api/state?as_of=' + encodeURIComponent(new Date(state.asOf).toISOString()));
  }
  state.data = data;
  renderAll();
}

async function loadDemos() {
  const data = await api('/api/demo');
  state.demos = data.demos;
  renderChips();
}

function renderAll() {
  renderAsOfOptions();
  renderKpis();
  renderDefinitions();
  renderCustomers();
  renderHistory();
  renderAudit();
  renderCustomerDetail();
  $('#footer-asof').textContent = `评估时刻 ${longTime(state.data.as_of)} · 最新事件 ${longTime(state.data.latest_event)}`;
}

/* --------------------------------------------------------------------- KPIs */
function renderKpis() {
  const summary = state.data.summary;
  const kpis = [
    {
      label: '客户数',
      value: summary.customer_count,
      unit: '位',
      note: `${summary.order_count} 笔订单 · 有效 ${summary.active_order_count} 笔`,
    },
    {
      label: '滚动七日总金额',
      value: yuanShort(summary.rolling_total_cents),
      unit: '',
      note: `截至 ${longTime(state.data.as_of)} 的闭区间合计`,
      hero: true,
    },
    {
      label: '已提交批次',
      value: summary.batch_count,
      unit: '批',
      note: '每次提交都是一个原子事务',
    },
    {
      label: '已撤销订单',
      value: summary.voided_order_count,
      unit: '笔',
      note: '保留时间锚点，不计入金额',
    },
  ];
  const row = $('#kpis');
  row.replaceChildren();
  for (const kpi of kpis) {
    const card = el('div', 'kpi' + (kpi.hero ? ' kpi-hero' : ''));
    card.append(el('p', 'kpi-label', kpi.label));
    const value = el('p', 'kpi-value');
    value.textContent = kpi.value;
    if (kpi.unit) value.append(el('span', 'unit', kpi.unit));
    card.append(value, el('p', 'kpi-note', kpi.note));
    row.append(card);
  }
}

/* -------------------------------------------------------------- definitions */
function renderDefinitions() {
  const def = state.data.definition;
  $('#def-rolling').textContent = def.rolling_window;
  $('#def-cumulative').textContent = def.cumulative;
  $('#def-last3').textContent = def.last3;
  $('#def-voided').textContent = def.voided;
  $('#def-timeline').textContent = def.timeline;
}

/* ---------------------------------------------------------------- as-of pick */
function renderAsOfOptions() {
  const select = $('#asof');
  const previous = state.asOf;
  select.replaceChildren();
  for (const item of state.data.evaluation_instants) {
    const opt = el('option', '', `T = ${longTime(item.iso)}`);
    opt.value = String(item.ms);
    if (item.ms === previous) opt.selected = true;
    select.append(opt);
  }
}
$('#asof').addEventListener('change', async event => {
  state.asOf = Number(event.target.value);
  await loadState();
});

/* ---------------------------------------------------------------- customers */
function renderCustomers() {
  const tbody = $('#customer-rows');
  const asOf = state.data.as_of;
  $('#customers-meta').textContent = `评估时刻 ${longTime(asOf)}`;
  tbody.replaceChildren();
  if (!state.data.customers.length) {
    const tr = el('tr');
    const td = el('td', 'empty', '尚无客户数据');
    td.colSpan = 6;
    tr.append(td);
    tbody.append(tr);
    return;
  }
  for (const customer of state.data.customers) {
    const m = customer.metrics;
    const tr = el('tr');
    tr.dataset.customer = customer.customer_id;
    if (state.selectedCustomer === customer.customer_id) tr.className = 'selected';
    tr.setAttribute('role', 'button');
    tr.setAttribute('tabindex', '0');
    tr.setAttribute('aria-label', `查看 ${customer.name} 的指标明细`);

    const nameCell = el('td');
    nameCell.append(el('span', 'cust-name', customer.name));
    nameCell.append(el('span', 'cust-id', ` ${customer.customer_id}`));

    const rolling = el('td', 'num' + (m.rolling_7d_cents === 0 ? ' zero' : ''));
    rolling.textContent = yuan(m.rolling_7d_cents);
    const cumulative = el('td', 'num');
    cumulative.textContent = yuan(m.cumulative_cents);
    const avg = el('td', 'num');
    const wrap = el('span', 'avg-cell');
    wrap.textContent = m.last3_count ? yuan(Math.round(m.last3_sum_cents / m.last3_count)) : '—';
    if (m.last3_count) wrap.append(el('span', 'avg-count', `均值 / ${m.last3_count} 笔`));
    avg.append(wrap);

    const orders = el('td', 'num');
    orders.textContent = `${customer.active_orders} 有效`;
    if (customer.voided_orders) orders.textContent += ` / ${customer.voided_orders} 撤销`;
    const latest = el('td');
    latest.textContent = shortTime(customer.latest_event);

    tr.append(nameCell, rolling, cumulative, avg, orders, latest);
    tr.addEventListener('click', () => selectCustomer(customer.customer_id));
    tr.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectCustomer(customer.customer_id);
      }
    });
    tbody.append(tr);
  }
}

function selectCustomer(customerId) {
  state.selectedCustomer = state.selectedCustomer === customerId ? null : customerId;
  for (const tr of $$('#customer-rows tr[data-customer]')) {
    tr.classList.toggle('selected', tr.dataset.customer === state.selectedCustomer);
  }
  renderCustomerDetail();
}

async function renderCustomerDetail() {
  const host = $('#customer-detail');
  if (!state.selectedCustomer) {
    host.hidden = true;
    host.replaceChildren();
    return;
  }
  let data;
  try {
    data = await api(
      `/api/customers/${encodeURIComponent(state.selectedCustomer)}?as_of=${encodeURIComponent(new Date(state.asOf).toISOString())}`
    );
  } catch (err) {
    host.hidden = true;
    toast('客户明细加载失败：' + err.message, true);
    return;
  }
  host.hidden = false;
  host.replaceChildren();

  const head = el('div', 'detail-head');
  head.append(el('h3', '', `${data.customer.name} · 指标明细`));
  const facts = el('div', 'detail-facts');
  facts.innerHTML = `
    <span>滚动七日 <strong>¥${yuan(data.snapshot.rolling_sum_cents)}</strong></span>
    <span>累计 <strong>¥${yuan(data.metrics.cumulative_cents)}</strong></span>
    <span>最近三笔 <strong>${data.metrics.last3_count} 笔</strong> 合计 ¥${yuan(data.metrics.last3_sum_cents)}</span>
    <span>评估时刻 <strong>${longTime(data.as_of)}</strong></span>`;
  head.append(facts);
  host.append(head);

  const rollingBox = el('div');
  rollingBox.append(el('h3', '', `滚动七日窗口内的订单（${data.snapshot.rolling_orders.length}）`));
  if (data.snapshot.rolling_orders.length) {
    rollingBox.append(orderList(data.snapshot.rolling_orders, false));
  } else {
    rollingBox.append(el('p', 'hint', '该时刻前 7 天内没有有效订单，滚动金额为 0。'));
  }

  const last3Box = el('div');
  last3Box.append(el('h3', '', '最近三笔（按时间、订单号升序取末三笔）'));
  last3Box.append(orderList(data.snapshot.last3_orders, false));

  const allBox = el('div');
  allBox.append(el('h3', '', `全部订单（${data.orders.length}）`));
  allBox.append(orderList(data.orders, true));

  const grid = el('div', 'split');
  const left = el('div');
  left.style.display = 'grid';
  left.style.gap = '14px';
  left.append(rollingBox, last3Box);
  const right = el('div');
  right.append(allBox);
  grid.append(left, right);
  host.append(grid);
}

function orderList(orders, showStatus) {
  const list = el('div', 'order-list');
  if (!orders.length) {
    list.append(el('p', 'hint', '没有订单。'));
    return list;
  }
  for (const order of orders) {
    const row = el('div', 'order-row' + (order.status === 'voided' ? ' voided' : ''));
    row.append(el('span', 'order-time', shortTime(order.occurred_at)));
    const main = el('div', 'order-main');
    main.append(el('span', 'order-id', order.order_id));
    if (showStatus) {
      main.append(badge(order.status === 'voided' ? '已撤销' : '有效', order.status === 'voided' ? 'void' : 'active'));
    }
    row.append(main, el('span', 'order-amount', `¥${yuan(order.amount_cents)}`));
    list.append(row);
  }
  return list;
}

/* ------------------------------------------------------------------- chips */
function renderChips() {
  const host = $('#demo-chips');
  host.replaceChildren();
  state.demos.forEach(demo => {
    const chip = el('button', 'chip', demo.title);
    chip.type = 'button';
    chip.title = demo.note;
    chip.addEventListener('click', () => {
      $$('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      $('#payload').value = JSON.stringify({ operations: demo.operations }, null, 2);
      clearPreview();
      $('#btn-commit').disabled = true;
    });
    host.append(chip);
  });
}

/* Drop a pending (not yet committed) preview. The rendered impact section is
   cleared too because it describes a payload that no longer matches the editor. */
function clearPreview() {
  state.preview = null;
  $('#impact').hidden = true;
  $('#btn-commit').disabled = true;
  clearErrors();
}

/* ---------------------------------------------------------------- preview */
$('#payload').addEventListener('input', () => {
  if (state.preview || !$('#btn-commit').disabled) {
    clearPreview();
    toast('批次内容已修改，请重新预览');
  }
});

$('#btn-preview').addEventListener('click', async () => {
  clearErrors();
  let payload;
  try {
    payload = JSON.parse($('#payload').value);
  } catch (err) {
    showErrors(['JSON 解析失败：' + err.message]);
    return;
  }
  setBusy(true);
  try {
    const result = await api('/api/preview', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.preview = result;
    state.showAllCells = false;
    renderImpact(result, false);
    $('#btn-commit').disabled = false;
    toast(result.explanation.headline);
  } catch (err) {
    handleBatchError(err);
  } finally {
    setBusy(false);
  }
});

$('#btn-commit').addEventListener('click', async () => {
  if (!state.preview) return;
  clearErrors();
  let payload;
  try {
    payload = JSON.parse($('#payload').value);
  } catch (err) {
    showErrors(['JSON 解析失败：' + err.message]);
    return;
  }
  const simulate = $('#simulate-failure').checked;
  setBusy(true);
  try {
    let result;
    try {
      result = await api('/api/commit', {
        method: 'POST',
        body: JSON.stringify({ ...payload, simulate_failure: simulate }),
      });
    } catch (err) {
      if (err.payload && err.payload.committed === false) {
        result = err.payload;
      } else {
        throw err;
      }
    }
    if (result.committed) {
      renderImpact(result, true);
      state.preview = null;
      $('#btn-commit').disabled = true;
      toast(`批次 ${result.batch_id} 已原子提交：${result.changed_cells} 个指标变化`);
      await loadState();
      await loadAudit();
    } else {
      state.preview = null;
      $('#btn-commit').disabled = true;
      $('#impact').hidden = true;
      showErrors([result.error || '提交失败', '批次、订单与全部指标均已回滚到提交前状态。'], true);
      toast('提交已回滚：没有任何部分指标被发布', true);
      await loadState();
    }
  } catch (err) {
    handleBatchError(err);
  } finally {
    setBusy(false);
  }
});

function setBusy(busy) {
  $('#btn-preview').disabled = busy;
  $('#btn-commit').disabled = busy || !state.preview;
}

function clearErrors() {
  const box = $('#errors');
  box.hidden = true;
  box.replaceChildren();
}

function showErrors(errors, rolledBack = false) {
  const box = $('#errors');
  box.hidden = false;
  box.replaceChildren();
  box.append(el('h3', '', rolledBack ? '提交失败，已完整回滚' : '批次校验失败'));
  const list = el('ul');
  for (const message of errors) list.append(el('li', '', message));
  box.append(list);
}

function handleBatchError(err) {
  state.preview = null;
  $('#impact').hidden = true;
  $('#btn-commit').disabled = true;
  const errors = err.payload && err.payload.errors;
  if (errors) {
    showErrors(errors);
    toast(`批次校验失败：${errors.length} 处问题`, true);
  } else {
    showErrors([err.message]);
    toast(err.message, true);
  }
  $('#btn-commit').disabled = true;
}

/* ------------------------------------------------------------------ impact */
function renderImpact(result, committed) {
  state.impactResult = result;
  state.impactCommitted = committed;
  const section = $('#impact');
  section.hidden = false;
  $('#impact-title').textContent = committed ? '已提交：影响范围与修改前后对照' : '预览：影响范围与修改前后对照';
  $('#impact-meta').textContent = committed
    ? `批次 ${result.batch_id} · 事务已提交`
    : '尚未写入；提交后才会发布指标';
  $('#impact-headline').textContent = result.explanation.headline;

  const sentences = $('#impact-sentences');
  sentences.replaceChildren();
  for (const sentence of result.explanation.sentences) sentences.append(el('li', '', sentence));

  const host = $('#impact-customers');
  host.replaceChildren();

  const summaryLine = el('div', 'detail-facts');
  summaryLine.innerHTML = `
    <span>受影响客户 <strong>${result.affected_customer_count}</strong> / ${result.all_customer_count}</span>
    <span>重算锚点 <strong>${result.recomputed_cells}</strong></span>
    <span>结果变化 <strong>${result.changed_cells}</strong></span>
    <span>新增锚点 <strong>${result.new_cells}</strong></span>
    <span>全局锚点 <strong>${result.all_cells}</strong></span>
    <span>未重算 <strong>${Math.max(0, result.all_cells - result.recomputed_cells)}</strong></span>`;
  host.append(summaryLine);

  const bar = el('div', 'cell-toggle');
  const toggleBtn = el('button', 'link-btn',
    state.showAllCells ? '只显示结果变化的锚点' : '显示全部重算锚点（含未变）');
  toggleBtn.type = 'button';
  toggleBtn.setAttribute('aria-expanded', String(state.showAllCells));
  toggleBtn.addEventListener('click', () => {
    state.showAllCells = !state.showAllCells;
    renderImpact(state.impactResult, state.impactCommitted);
  });
  bar.append(toggleBtn);
  host.append(bar);

  // Shared timeline across affected customers so range bars are comparable.
  // Rolling windows can extend past the last anchor; include their ends too.
  const allStarts = result.customers.map(c => c.first_affected_ms);
  const allEnds = result.customers.flatMap(c => [
    c.last_affected_ms,
    ...c.rolling_ranges.map(r => r.end_ms),
  ]);
  const tMin = Math.min(...allStarts);
  const tMax = Math.max(...allEnds);
  const span = Math.max(1, tMax - tMin);

  for (const customer of result.customers) {
    const card = el('div', 'impact-customer');
    const head = el('div', 'ic-head');
    head.append(el('h3', '', `${customer.customer_name}（${customer.customer_id}）`));
    head.append(badge(`${customer.changed_order_ids.length} 笔订单变更`, 'changed'));
    if (customer.new_cells) head.append(badge(`${customer.new_cells} 个新增锚点`, 'new'));
    card.append(head);

    const stats = el('div', 'ic-stats');
    stats.innerHTML = `
      <span>重算 <b>${customer.recomputed_cells}</b> / 客户锚点 ${customer.total_customer_cells}</span>
      <span>变化 <b>${customer.changed_cells}</b></span>
      <span>未变 <b>${customer.unchanged_cells}</b></span>
      <span>更早未重算 <b>${customer.skipped_cells}</b></span>
      <span>影响始于 <b>${longTime(customer.first_affected)}</b></span>
      <span>至 <b>${longTime(customer.last_affected)}</b></span>`;
    card.append(stats);

    card.append(renderRanges(customer, tMin, tMax, span));
    card.append(renderCells(customer));
    host.append(card);
  }
}

/* Range bars: rolling impact (aqua) vs recompute span (blue).
   Both are closed intervals; text labels carry exact values. */
function renderRanges(customer, tMin, tMax, span) {
  const wrap = el('div', 'ranges');

  const rows = [
    { key: 'rolling', label: '滚动窗口影响', ranges: customer.rolling_ranges },
    { key: 'recompute', label: '重算区间', ranges: customer.recompute_ranges },
  ];
  for (const row of rows) {
    const line = el('div', 'range-row');
    const label = el('div', 'range-label');
    label.append(el('span', `dot dot-${row.key}`), el('span', '', row.label));
    const track = el('div', 'range-track');
    for (const range of row.ranges) {
      const fill = el('div', `range-fill ${row.key}`);
      const left = ((range.start_ms - tMin) / span) * 100;
      const width = Math.max(0.6, ((range.end_ms - range.start_ms) / span) * 100);
      fill.style.left = `${Math.max(0, left)}%`;
      fill.style.width = `${Math.min(100 - Math.max(0, left), width)}%`;
      fill.dataset.tip = `${row.label}\n${longTime(range.start)}\n→ ${longTime(range.end)}（闭区间）`;
      track.append(fill);
    }
    line.append(label, track);
    wrap.append(line);
  }

  const axis = el('div', 'range-axis');
  axis.append(el('span', '', ''), el('span', '', `${dayTime(new Date(tMin).toISOString())} → ${dayTime(new Date(tMax).toISOString())}`));
  wrap.append(axis);
  return wrap;
}

/* Metric table: before/after per anchor, only changed cells by default. */
function renderCells(customer) {
  const cells = state.showAllCells
    ? customer.cells
    : customer.cells.filter(c => c.changed);
  const wrap = el('div');
  const title = el('div', 'detail-head');
  title.append(el('h3', '', state.showAllCells ? '全部重算锚点（修改前 → 修改后）' : `结果变化的锚点（${cells.length}）`));
  wrap.append(title);

  if (!cells.length) {
    wrap.append(el('p', 'hint', '没有结果变化的锚点：重算结果与修改前完全一致。'));
    return wrap;
  }

  const table = el('table', 'cell-table');
  table.append(headerRow([
    '锚点 T', '滚动七日', '累计金额', '最近三笔', '窗口内订单', '状态',
  ]));
  const tbody = el('tbody');
  for (const cell of cells) {
    const tr = el('tr', cell.changed ? 'changed' : 'unchanged');
    const t = el('td', '', longTime(cell.as_of));
    const rolling = numChange(cell.before ? cell.before.rolling_7d_cents : null, cell.after.rolling_7d_cents);
    const cumulative = numChange(cell.before ? cell.before.cumulative_cents : null, cell.after.cumulative_cents);
    const last3 = last3Change(cell.before, cell.after);
    const windowCell = el('td');
    if (cell.detail && cell.detail.after) {
      const after = cell.detail.after;
      const before = cell.detail.before;
      const diff = before
        ? after.rolling_ids.filter(id => !before.rolling_ids.includes(id)).length
          + before.rolling_ids.filter(id => !after.rolling_ids.includes(id)).length
        : after.rolling_ids.length;
      windowCell.textContent = `${after.rolling_ids.length} 笔${diff ? ` · 变动 ${diff}` : ''}`;
      windowCell.dataset.tip = '窗口构成（订单号）\n前：' + (before ? before.rolling_ids.join(', ') || '空' : '（无旧值）')
        + '\n后：' + (after.rolling_ids.join(', ') || '空');
    } else {
      windowCell.textContent = '—';
    }
    const status = el('td');
    if (cell.new_cell) status.append(badge('新增', 'new'));
    else if (cell.changed) status.append(badge('变化', 'changed'));
    else status.append(badge('未变', 'same'));
    tr.append(t, rolling, cumulative, last3, windowCell, status);
    tbody.append(tr);
  }
  table.append(tbody);
  wrap.append(table);
  return wrap;
}

function headerRow(labels) {
  const thead = el('thead');
  const tr = el('tr');
  labels.forEach((label, index) => {
    const th = el('th', index === 0 ? '' : 'num', label);
    th.scope = 'col';
    tr.append(th);
  });
  thead.append(tr);
  return thead;
}

function numChange(before, after) {
  const td = el('td', 'num');
  if (before === null || before === undefined) {
    td.append(el('span', 'now', yuan(after)));
    return td;
  }
  if (before === after) {
    td.append(el('span', '', yuan(after)));
    return td;
  }
  td.append(el('span', 'was', yuan(before)));
  td.append(el('span', 'arrow', '→'));
  td.append(el('span', 'now', yuan(after)));
  return td;
}

function last3Change(before, after) {
  const td = el('td', 'num');
  const text = c => c && c.last3_count
    ? `¥${yuan(Math.round(c.last3_sum_cents / c.last3_count))} · ${c.last3_count} 笔`
    : '—';
  if (!before) {
    td.append(el('span', 'now', text(after)));
    return td;
  }
  const same = before.last3_sum_cents === after.last3_sum_cents
    && before.last3_count === after.last3_count
    && JSON.stringify(before.last3_ids) === JSON.stringify(after.last3_ids);
  if (same) {
    td.append(el('span', '', text(after)));
  } else {
    td.append(el('span', 'was', text(before)));
    td.append(el('span', 'arrow', '→'));
    td.append(el('span', 'now', text(after)));
  }
  td.dataset.tip = `最近三笔订单\n前：${before.last3_ids.join(', ') || '无'}\n后：${after.last3_ids.join(', ') || '无'}`;
  return td;
}

/* ---------------------------------------------------------------- history */
function renderHistory() {
  const tbody = $('#history-rows');
  tbody.replaceChildren();
  $('#history-meta').textContent = `${state.data.summary.batch_count} 个已提交批次`;
  if (!state.data.batches.length) {
    const tr = el('tr');
    const td = el('td', 'empty', '暂无提交批次；预览不会写入任何数据。');
    td.colSpan = 6;
    tr.append(td);
    tbody.append(tr);
    return;
  }
  for (const batch of state.data.batches) {
    const tr = el('tr');
    tr.dataset.batch = batch.batch_id;
    if (state.selectedBatch === batch.batch_id) tr.className = 'selected';
    const id = el('td');
    id.append(el('span', 'cust-id', batch.batch_id));
    const created = el('td', '', stamp(batch.created_at));
    created.title = batch.created_at;
    const ops = el('td', 'num', String(batch.operation_count));
    const cust = el('td', 'num', String(batch.affected_customers));
    const recalc = el('td', 'num', `${batch.recomputed_cells} / ${batch.changed_cells}`);
    const action = el('td');
    const open = el('button', 'link-btn', state.selectedBatch === batch.batch_id ? '收起' : '对照');
    open.setAttribute('aria-label', state.selectedBatch === batch.batch_id ? '收起批次对照' : '查看批次完整对照');
    open.type = 'button';
    action.append(open);
    tr.append(id, created, ops, cust, recalc, action);
    tr.addEventListener('click', () => selectBatch(batch.batch_id));
    tbody.append(tr);
  }
}

async function selectBatch(batchId) {
  if (state.selectedBatch === batchId) {
    state.selectedBatch = null;
    $('#batch-detail').hidden = true;
    renderHistory();
    return;
  }
  state.selectedBatch = batchId;
  renderHistory();
  try {
    const data = await api(`/api/batches/${encodeURIComponent(batchId)}`);
    renderBatchDetail(data);
  } catch (err) {
    toast('批次对照加载失败：' + err.message, true);
  }
}

function renderBatchDetail(data) {
  const host = $('#batch-detail');
  host.hidden = false;
  host.replaceChildren();
  const batch = data.batch;
  const head = el('div', 'detail-head');
  head.append(el('h3', '', `批次 ${batch.batch_id} 的完整重算对照`));
  const facts = el('div', 'detail-facts');
  facts.innerHTML = `
    <span>操作 <strong>${batch.operation_count}</strong></span>
    <span>客户 <strong>${batch.affected_customers}</strong></span>
    <span>重算 <strong>${batch.recomputed_cells}</strong></span>
    <span>变化 <strong>${batch.changed_cells}</strong></span>
    <span>全局锚点 <strong>${batch.all_cells}</strong></span>`;
  head.append(facts);
  host.append(head);

  host.append(el('h3', '', '订单变更'));
  const changes = el('div', 'change-list');
  for (const change of data.changes) {
    const row = el('div', 'change-row');
    row.append(el('div', 'cust-id', `${change.seq}. ${change.op}`));
    const body = el('div', 'change-ops');
    const before = change.before
      ? `${change.before.order_id} · ¥${yuan(change.before.amount_cents)} · ${longTime(change.before.occurred_at)} · ${change.before.status}`
      : '（新订单）';
    const after = `${change.after.order_id} · ¥${yuan(change.after.amount_cents)} · ${longTime(change.after.occurred_at)} · ${change.after.status}`;
    body.append(el('span', 'change-before', before));
    body.append(el('span', 'change-after', after));
    row.append(body);
    changes.append(row);
  }
  host.append(changes);

  host.append(el('h3', '', `重算日志（${data.recalc.length} 个锚点，${data.recalc_changed} 个变化）`));
  const table = el('table', 'cell-table');
  table.append(headerRow(['客户', '锚点 T', '滚动七日 前 → 后', '累计 前 → 后', '最近三笔 前 → 后']));
  const tbody = el('tbody');
  for (const row of data.recalc) {
    const tr = el('tr', row.changed ? 'changed' : 'unchanged');
    const c = el('td', 'cust-id', row.customer_id);
    const t = el('td', '', longTime(row.as_of));
    const rolling = numChange(row.before ? row.before.rolling_7d_cents : null, row.after.rolling_7d_cents);
    const cumulative = numChange(row.before ? row.before.cumulative_cents : null, row.after.cumulative_cents);
    const last3 = last3Change(row.before, row.after);
    tr.append(c, t, rolling, cumulative, last3);
    tbody.append(tr);
  }
  table.append(tbody);
  host.append(table);
}

/* ------------------------------------------------------------------ audit */
function renderAudit() {
  // Audit is loaded separately so it can include the seed entry.
}

async function loadAudit() {
  const host = $('#audit-list');
  try {
    const events = await api('/api/audit');
    host.replaceChildren();
    if (!events.length) {
      host.append(el('li', '', '暂无事件'));
      return;
    }
    for (const event of events) {
      const li = el('li');
      const when = el('time', '', stamp(event.at));
      when.title = event.at;
      li.append(when);
      const body = el('span');
      body.append(el('span', 'kind', event.kind === 'seed' ? '样例初始化' : '批次提交'));
      const detail = event.detail || {};
      body.append(el('span', '', detail.batch_id ? `${detail.batch_id} · 重算 ${detail.recomputed_cells} · 变化 ${detail.changed_cells}` : `订单 ${detail.orders || 0} 笔`));
      li.append(body);
      host.append(li);
    }
  } catch (err) {
    host.replaceChildren(el('li', '', '日志加载失败：' + err.message));
  }
}

/* ---------------------------------------------------------------- verify */
async function verifyCache() {
  const note = $('#verify-note');
  try {
    const result = await api('/api/verify');
    if (result.ok) {
      const message = `缓存与订单事实一致：${result.checked_cells} 个锚点全部通过，${result.batch_count} 个批次。`;
      note.textContent = message;
      toast(message);
    } else {
      note.textContent = `发现 ${result.mismatches.length} 个不一致锚点。`;
      toast(`缓存校验失败：${result.mismatches.length} 个不一致`, true);
    }
  } catch (err) {
    note.textContent = '校验失败：' + err.message;
    toast(note.textContent, true);
  }
  await loadState();
}

$('#btn-verify').addEventListener('click', verifyCache);
$('#btn-verify-2').addEventListener('click', verifyCache);

$('#btn-reset').addEventListener('click', async () => {
  if (!confirm('重置样例会删除全部批次、订单修改与审计记录，恢复初始数据。继续？')) return;
  try {
    const data = await api('/api/reset', { method: 'POST', body: '{}' });
    state.preview = null;
    state.selectedCustomer = null;
    state.selectedBatch = null;
    state.asOf = null;
    clearPreview();
    $('#batch-detail').hidden = true;
    $('#payload').value = '';
    $('#simulate-failure').checked = false;
    state.data = data.state;
    state.asOf = data.state.latest_event_ms;
    renderAll();
    await loadAudit();
    toast('样例已重置');
  } catch (err) {
    toast('重置失败：' + err.message, true);
  }
});

$('#def-toggle').addEventListener('click', event => {
  const button = event.currentTarget;
  const open = button.getAttribute('aria-expanded') === 'true';
  button.setAttribute('aria-expanded', String(!open));
  button.textContent = open ? '展开细节' : '收起细节';
  $('.definitions').classList.toggle('collapsed', open);
});

/* ---------------------------------------------------------------- tooltip */
const tooltip = $('#tooltip');
function showTip(event, text) {
  tooltip.textContent = '';
  const lines = text.split('\n');
  const title = el('div', 'tt-title', lines[0]);
  tooltip.append(title);
  for (const line of lines.slice(1)) tooltip.append(el('div', 'tt-row', line));
  tooltip.hidden = false;
  moveTip(event);
}
function moveTip(event) {
  const pad = 14;
  const rect = tooltip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = event.clientY - rect.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}
document.addEventListener('mouseover', event => {
  const target = event.target.closest('[data-tip]');
  if (target) showTip(event, target.dataset.tip);
});
document.addEventListener('mousemove', event => {
  if (!tooltip.hidden) moveTip(event);
});
document.addEventListener('mouseout', event => {
  if (event.target.closest('[data-tip]')) tooltip.hidden = true;
});
document.addEventListener('focusin', event => {
  const target = event.target.closest('[data-tip]');
  if (target) {
    const rect = target.getBoundingClientRect();
    showTip({ clientX: rect.left, clientY: rect.bottom }, target.dataset.tip);
  }
});
document.addEventListener('focusout', () => { tooltip.hidden = true; });

/* -------------------------------------------------------------------- init */
(async function init() {
  try {
    $('#payload').value = JSON.stringify(
      { operations: [{ op: 'void', order_id: 'O-1001' }] }, null, 2);
  } catch (err) { /* noop */ }
  try {
    await loadDemos();
    await loadState();
    await loadAudit();
  } catch (err) {
    toast('初始化失败：' + err.message, true);
  }
})();
