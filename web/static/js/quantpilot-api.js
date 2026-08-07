(function () {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const api = async (url, options = {}) => {
    const response = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.success === false) throw new Error(payload.error || payload.message || `请求失败 (${response.status})`);
    return payload.data ?? payload;
  };
  const toast = message => {
    const node = $('#toast');
    if (!node) return;
    node.textContent = message;
    node.classList.add('show');
    clearTimeout(window.quantPilotToastTimer);
    window.quantPilotToastTimer = setTimeout(() => node.classList.remove('show'), 2400);
  };
  const refreshIcons = () => window.lucide && lucide.createIcons();
  const formatMoney = value => `¥ ${Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;
  const formatPercent = value => `${Number(value || 0) >= 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`;
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const state = {
    watchlist: [],
    groups: new Set(['核心关注', '短线策略', '待观察']),
    strategies: [],
    selectedStrategies: []
  };

  const replaceHandler = id => {
    const element = document.getElementById(id);
    if (!element) return null;
    const replacement = element.cloneNode(true);
    element.replaceWith(replacement);
    return replacement;
  };

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function renderStrategyList() {
    const list = $('.strategy-list');
    if (!list || !state.strategies.length) return;
    list.innerHTML = state.strategies.slice(0, 8).map((strategy, index) => {
      const name = strategy.display_name || strategy.name || strategy.strategy_name || `策略 ${index + 1}`;
      const winRate = Number(strategy.win_rate || strategy.metadata?.win_rate || 0);
      return `<label class="strategy-row"><input class="strategy-check" data-strategy="${escapeHtml(name)}" type="checkbox" checked><div><div class="strategy-name">${escapeHtml(name)}</div><div class="strategy-meta"><span>${winRate ? `胜率 ${winRate.toFixed(1)}%` : '可运行策略'}</span><span>${strategy.trade_count ? `${strategy.trade_count} 笔` : '待回测'}</span></div><div class="progress"><span style="width:${Math.min(winRate || 0, 100)}%"></span></div></div><div class="strategy-return">${strategy.total_return == null ? '--' : formatPercent(strategy.total_return)}<small>${strategy.sharpe_ratio ? `夏普 ${Number(strategy.sharpe_ratio).toFixed(2)}` : '待回测'}</small></div></label>`;
    }).join('');
    state.selectedStrategies = $$('.strategy-check:checked').map(node => node.dataset.strategy);
    setText('selectedStrategyCount', state.selectedStrategies.length);
    if (!list.dataset.quantPilotBound) {
      list.dataset.quantPilotBound = 'true';
      list.addEventListener('change', () => {
        state.selectedStrategies = $$('.strategy-check:checked').map(node => node.dataset.strategy);
        setText('selectedStrategyCount', state.selectedStrategies.length);
      });
    }
  }

  function renderSignals(data) {
    const signals = Array.isArray(data?.signals) ? data.signals : [];
    const list = $('.signal-list');
    if (!list) return;
    const grouped = new Map();
    signals.forEach(signal => {
      const name = signal.strategy_name || signal.signal_type || '策略信号';
      const entry = grouped.get(name) || { count: 0, strength: 0 };
      entry.count += 1;
      entry.strength = Math.max(entry.strength, Number(signal.confidence || signal.score || signal.signal_strength || 0));
      grouped.set(name, entry);
    });
    const entries = [...grouped.entries()].sort((a, b) => b[1].strength - a[1].strength).slice(0, 5);
    setText('signalMore', '查看全部信号');
    const total = $('.signal-total strong');
    if (total) total.textContent = signals.length;
    list.innerHTML = entries.length ? entries.map(([name, entry], index) => `<div class="signal-item"><div class="signal-icon ${index === 1 ? 'orange' : index === 2 ? 'blue' : 'green'}"><i data-lucide="${index === 1 ? 'triangle-alert' : index === 2 ? 'git-compare-arrows' : 'zap'}"></i></div><div><div class="signal-name">${escapeHtml(name)}</div><div class="signal-desc">${entry.count} 个标的 · 后端实时信号</div></div><div class="signal-num up">${entry.strength ? `${entry.strength.toFixed(0)}%` : '--'}</div></div>`).join('') : '<div class="stock-pool-empty">当前没有待确认信号</div>';
    refreshIcons();
  }

  function renderStats(portfolio, signals, stats) {
    const cards = $$('.stat-card');
    const assets = portfolio?.total_assets || portfolio?.available_cash || 0;
    const profit = portfolio?.total_profit_percent || 0;
    const signalCount = Array.isArray(signals?.signals) ? signals.signals.length : 0;
    if (cards[0]) $('.stat-value', cards[0]).textContent = formatMoney(assets);
    if (cards[0]) $('.stat-footer', cards[0]).innerHTML = `<span class="trend-up">${formatPercent(profit)}</span><span>策略账户累计</span>`;
    if (cards[1]) $('.stat-value', cards[1]).textContent = formatPercent(profit);
    if (cards[1]) $('.stat-footer', cards[1]).innerHTML = `<span>当前持仓 ${portfolio?.positions_count || 0} 只</span><span>数据日期 ${escapeHtml(portfolio?.date || stats?.latest_date || '--')}</span>`;
    if (cards[2]) $('.stat-value', cards[2]).textContent = `${signalCount}`;
    if (cards[2]) $('.stat-footer', cards[2]).innerHTML = `<span>待确认策略信号</span><span>${stats?.strategies || state.strategies.length} 个策略</span>`;
    if (cards[3]) $('.stat-value', cards[3]).textContent = portfolio?.run_mode === 'auto' ? '自动' : '手动';
    if (cards[3]) $('.stat-footer', cards[3]).innerHTML = `<span>运行模式</span><span class="tag ${portfolio?.run_mode === 'auto' ? 'green' : 'orange'}">${portfolio?.run_mode === 'auto' ? '实盘' : '手动确认'}</span>`;
  }

  function groupOptions(selected) {
    return [...state.groups].map(group => `<option value="${escapeHtml(group)}" ${group === selected ? 'selected' : ''}>${escapeHtml(group)}</option>`).join('');
  }

  function renderWatchlist() {
    const table = $('#stockTable');
    if (!table) return;
    const rows = state.watchlist.map(item => {
      const change = Number(item.change || 0);
      const strength = Number(item.signal_strength || 0);
      const favorite = item.favorite !== false;
      return `<tr data-code="${escapeHtml(item.code)}" data-favorite="${favorite}" data-group="${escapeHtml(item.group || '待观察')}" data-strategy="${escapeHtml(item.strategy || '')}" data-search="${escapeHtml(`${item.name || ''} ${item.code || ''} ${item.industry || ''}`)}"><td><div class="stock-cell"><button class="stock-star ${favorite ? 'active' : ''}" title="${favorite ? '取消自选' : '加入自选'}"><i data-lucide="star"></i></button><div><div class="stock-name">${escapeHtml(item.name || item.code)}</div><div class="stock-code">${escapeHtml(item.code)} · ${escapeHtml(item.industry || '未分类')}</div><span class="stock-group">${escapeHtml(item.group || '待观察')}</span></div></div></td><td class="price">${Number(item.price || 0).toFixed(2)}</td><td class="${change < 0 ? 'down' : 'up'}">${formatPercent(change)}</td><td><span class="tag ${String(item.strategy || '').includes('回踩') ? 'orange' : 'green'}">${escapeHtml(item.strategy || '观察中')}</span></td><td><span class="${strength >= 70 ? 'up' : ''}">${strength ? `${strength >= 85 ? '强' : '中'} · ${strength}` : '--'}</span></td><td>${item.win_rate ? `${Number(item.win_rate).toFixed(1)}%` : '--'}</td><td class="up">${item.expected_return == null ? '--' : formatPercent(item.expected_return)}</td><td><select class="row-group" aria-label="${escapeHtml(item.name || item.code)}分组">${groupOptions(item.group || '待观察')}</select></td><td><button class="icon-btn row-detail" title="查看详情"><i data-lucide="arrow-up-right"></i></button></td></tr>`;
    }).join('');
    table.innerHTML = rows || '<tr data-search=""><td colspan="10" class="stock-pool-empty">暂无自选股，请从筛选结果加入</td></tr>';
    setText('watchCount', state.watchlist.filter(item => item.favorite !== false).length);
    setText('tableResult', `显示 ${state.watchlist.length} / ${state.watchlist.length} 只股票 · 数据来自后端`);
    refreshIcons();
    bindWatchlistEvents();
  }

  async function loadWatchlist() {
    const data = await api('/api/quantpilot/watchlist');
    state.watchlist = data.items || [];
    (data.groups || []).forEach(group => state.groups.add(group));
    renderWatchlist();
    renderBacktestPool();
  }

  async function saveWatch(item) {
    await api('/api/quantpilot/watchlist', { method: 'POST', body: JSON.stringify(item) });
    await loadWatchlist();
  }

  function renderScanResults(data) {
    const results = [];
    Object.entries(data || {}).forEach(([strategy, stocks]) => {
      if (!Array.isArray(stocks)) return;
      stocks.forEach(stock => {
      const signal = (Array.isArray(stock.signals) ? stock.signals[0] : stock.signals) || {};
        results.push({
          code: stock.code,
          name: stock.name || stock.code,
          industry: stock.industry || '未分类',
          price: stock.price ?? stock.close ?? signal.close ?? 0,
          change: stock.change ?? signal.change ?? 0,
          strategy,
          strength: stock.score ?? stock.signal_strength ?? signal.score ?? 0,
          win: stock.win_rate ?? signal.win_rate,
          expected: stock.expected_return ?? signal.expected_return,
          reason: stock.reason || signal.reason || ''
        });
      });
    });
    const unique = [...new Map(results.map(item => [item.code, item])).values()].slice(0, 20);
    const container = $('#filterResults');
    if (!container) return;
    const heading = $('.result-heading', container);
    if (heading) heading.querySelector('strong').innerHTML = `筛选结果预览 <span class="tag blue">${unique.length} 条</span>`;
    const list = $('.result-list', container);
    if (!list) return;
    list.innerHTML = unique.length ? unique.map(item => `<div class="filter-result-row" data-name="${escapeHtml(item.name)}" data-code="${escapeHtml(item.code)}" data-sector="${escapeHtml(item.industry)}" data-search="${escapeHtml(`${item.name} ${item.code} ${item.industry}`)}" data-price="${Number(item.price || 0).toFixed(2)}" data-change="${Number(item.change || 0).toFixed(2)}" data-signal="${escapeHtml(item.strategy)}" data-strength="${Number(item.strength || 0)}" data-win="${item.win == null ? '' : Number(item.win).toFixed(1)}" data-return="${item.expected == null ? '' : Number(item.expected).toFixed(2)}"><div class="result-stock"><div><div class="stock-name">${escapeHtml(item.name)}</div><div class="stock-code">${escapeHtml(item.code)} · ${escapeHtml(item.industry)}</div>${item.reason ? `<div class="result-reason">${escapeHtml(item.reason)}</div>` : ''}</div></div><div class="result-signal">信号强度 <strong>${Number(item.strength || 0).toFixed(0)}</strong>${item.win == null ? '' : ` · 历史胜率 <strong>${Number(item.win).toFixed(1)}%</strong>`}</div><div class="result-actions"><select class="group-select result-group" aria-label="${escapeHtml(item.name)}分组">${groupOptions('待观察')}</select><button class="ghost-btn add-filter-stock"><i data-lucide="star"></i>加入自选</button></div></div>`).join('') : '<div class="stock-pool-empty">没有符合条件的股票</div>';
    refreshIcons();
  }

  async function bindFavorite(row) {
    const item = state.watchlist.find(entry => entry.code === row.dataset.code);
    if (!item) return;
    await api(`/api/quantpilot/watchlist/${item.code}`, { method: 'DELETE' });
    await loadWatchlist();
    toast(`${item.name || item.code} 已移出自选股`);
  }

  function bindWatchlistEvents() {
    const table = $('#stockTable');
    if (!table || table.dataset.quantPilotBound) return;
    table.dataset.quantPilotBound = 'true';
    table.addEventListener('change', async event => {
      const select = event.target.closest('.row-group');
      if (!select) return;
      const row = select.closest('tr');
      const item = state.watchlist.find(entry => entry.code === row.dataset.code);
      if (!item) return;
      try { await saveWatch({ ...item, group: select.value }); toast(`已将 ${item.name} 移入「${select.value}」`); } catch (error) { toast(error.message); }
    });
    table.addEventListener('click', async event => {
      const star = event.target.closest('.stock-star');
      if (!star) return;
      try { await bindFavorite(star.closest('tr')); } catch (error) { toast(error.message); }
    });
  }

  function renderBacktestPool() {
    const select = $('#backtestFavoriteSelect');
    if (!select) return;
    const favoriteItems = state.watchlist.filter(item => item.favorite !== false);
    select.innerHTML = `<option value="">选择自选股</option>${favoriteItems.map(item => `<option value="${escapeHtml(item.code)}">${escapeHtml(item.name || item.code)} (${escapeHtml(item.code)})</option>`).join('')}`;
    setText('backtestStockCount', `已选 ${favoriteItems.length} 只`);
  }

  async function runScan() {
    const names = state.selectedStrategies.length ? state.selectedStrategies : state.strategies.slice(0, 3).map(item => item.name || item.display_name);
    if (!names.length) return toast('暂无可运行策略');
    const button = $('#runScan');
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>正在筛选';
    refreshIcons();
    try {
      const result = await api('/api/select', { method: 'POST', body: JSON.stringify({ strategies: names, logic: 'or' }) });
      renderScanResults(result);
      const rows = Object.values(result || {}).filter(value => Array.isArray(value)).flat();
      toast(`筛选完成，后端返回 ${rows.length} 条结果`);
      $('#filterResults')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (error) { toast(`筛选失败：${error.message}`); }
    button.disabled = false;
    button.innerHTML = '<i data-lucide="scan-line"></i>运行筛选';
    refreshIcons();
  }

  async function runCodeScreen() {
    const source = $('#codeFilter')?.value.trim();
    if (!source) return toast('请先编写筛选代码');
    const button = $('#runCode');
    if (!button) return;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>执行中';
    refreshIcons();
    try {
      const result = await api('/api/quantpilot/code-screen', {
        method: 'POST',
        body: JSON.stringify({ source })
      });
      renderScanResults({ '代码筛选': result.results || [] });
      const incomplete = result.errors?.length ? `；${result.errors.length} 只因历史数据不足未纳入` : '';
      toast(`后端筛选完成，全部条件已执行，命中 ${result.count || 0} 只股票${incomplete}`);
    } catch (error) {
      toast(`代码筛选失败：${error.message}`);
    }
    button.disabled = false;
    button.innerHTML = '<i data-lucide="play"></i>运行代码';
    refreshIcons();
  }

  async function runBacktest() {
    const strategy = state.selectedStrategies[0] || state.strategies[0]?.name || state.strategies[0]?.display_name;
    const start = $('#backtestStart')?.value;
    const end = $('#backtestEnd')?.value;
    if (!strategy || !start || !end || start >= end) return toast('请检查策略和回测日期范围');
    const button = $('#runBacktest');
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>回测中';
    refreshIcons();
    setText('backtestRunState', '运行中');
    try {
      const result = await api('/api/trading/backtest/run', { method: 'POST', body: JSON.stringify({ strategy_name: strategy, start_date: start, end_date: end, timing_strategy: 'turtle' }) });
      setText('backtestRunState', '已完成');
      const fields = { backtestReturn: result.total_return, backtestWin: result.win_rate, backtestDrawdown: result.max_drawdown, backtestTrades: result.total_trades };
      Object.entries(fields).forEach(([id, value]) => { if (value != null) setText(id, id === 'backtestTrades' ? value : formatPercent(value)); });
      setText('backtestStatus', `最近运行：${result.created_at || new Date().toLocaleString('zh-CN')}`);
      toast('回测完成，结果已保存到后端');
    } catch (error) { setText('backtestRunState', '失败'); toast(`回测失败：${error.message}`); }
    button.disabled = false;
    button.innerHTML = '<i data-lucide="play"></i>运行回测';
    refreshIcons();
  }

  async function loadData() {
    try {
      const [stats, portfolio, signals, strategies] = await Promise.all([
        api('/api/stats'), api('/api/portfolio'), api('/api/signals'), api('/api/strategies')
      ]);
      state.strategies = Array.isArray(strategies) ? strategies : (strategies?.strategies || []);
      renderStrategyList();
      renderStats(portfolio, signals, stats);
      renderSignals(signals);
      setText('pageTitle', '市场总览');
      const statusText = $('.system-status div:last-child');
      if (statusText) statusText.textContent = `行情更新于 ${stats?.latest_date || '--'} · 策略 ${stats?.strategies || state.strategies.length} 个`;
      await loadWatchlist();
    } catch (error) {
      toast(`数据加载失败：${error.message}`);
    }
  }

  function bindActions() {
    const refresh = replaceHandler('refreshBtn');
    refresh?.addEventListener('click', async () => { refresh.classList.add('loading'); await loadData(); refresh.classList.remove('loading'); toast('数据已刷新'); });
    const scan = replaceHandler('runScan');
    scan?.addEventListener('click', runScan);
    const runCode = replaceHandler('runCode');
    runCode?.addEventListener('click', runCodeScreen);
    const backtest = replaceHandler('runBacktest');
    backtest?.addEventListener('click', runBacktest);
    const addStock = replaceHandler('addStockBtn');
    addStock?.addEventListener('click', () => { $('#stockSearch')?.focus(); toast('请从筛选结果中加入自选股'); });
    replaceHandler('stockTable');
    const filterResults = replaceHandler('filterResults');
    filterResults?.addEventListener('click', async event => {
      const button = event.target.closest('.add-filter-stock');
      if (!button) return;
      const row = button.closest('.filter-result-row');
      const parse = value => value === '' ? null : Number(String(value).replace('%', '').replace('+', ''));
      try {
        await saveWatch({
          code: row.dataset.code,
          name: row.dataset.name,
          industry: row.dataset.sector,
          group: $('.result-group', row).value,
          strategy: row.dataset.signal,
          signal_strength: parse(row.dataset.strength),
          win_rate: parse(row.dataset.win),
          expected_return: parse(row.dataset.return)
        });
        button.disabled = true;
        button.innerHTML = '<i data-lucide="check"></i>已加入';
        refreshIcons();
        toast(`${row.dataset.name} 已加入自选股`);
      } catch (error) { toast(`加入失败：${error.message}`); }
    });
    $$('.strategy-check').forEach(node => node.addEventListener('change', () => {
      state.selectedStrategies = $$('.strategy-check:checked').map(item => item.dataset.strategy);
      setText('selectedStrategyCount', state.selectedStrategies.length);
    }));
    const groupButton = replaceHandler('saveGroup');
    groupButton?.addEventListener('click', async () => {
      const name = $('#groupName')?.value.trim();
      if (!name) return toast('请输入分组名称');
      state.groups.add(name);
      await api('/api/quantpilot/groups', { method: 'POST', body: JSON.stringify({ name }) });
      $('#groupModalBackdrop')?.classList.remove('open');
      renderWatchlist();
      toast(`分组「${name}」已创建`);
    });
    const autoSwitch = replaceHandler('autoSwitch');
    autoSwitch?.addEventListener('click', async () => {
      try {
        const status = await api('/api/strategy/status');
        toast(`当前策略运行状态：${status?.status || status?.run_mode || '已连接'}`);
      } catch (error) { toast(`获取自动交易状态失败：${error.message}`); }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    bindActions();
    loadData();
    window.setInterval(loadWatchlist, 30000);
  });
})();
