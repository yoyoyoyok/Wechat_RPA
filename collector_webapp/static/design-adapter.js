(function () {
  'use strict';
  document.documentElement.classList.add('adapter-loading');
  let page = location.pathname.split('/').pop().replace('.html', '') || 'accounts';
  const statusText = { pending: '等待中', collecting: '收藏中', extracting: '提取链接', done: '成功', stopped: '已停止', failed: '失败' };
  const statusClass = { done: 'status-success', collecting: 'status-running', extracting: 'status-running', failed: 'status-failed', stopped: 'status-muted', pending: 'status-muted' };
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const formatTime = value => value ? String(value).replace('T', ' ').slice(0, 16) : '-';
  const localDate = value => { const d = value || new Date(); const pad = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  const api = (url, options) => fetch(url, options || {}).then(r => r.ok ? (r.status === 204 ? null : r.json()) : r.json().catch(() => ({})).then(x => Promise.reject(x)));
  const json = (method, url, body) => api(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  function clearDemoContent() {
    document.querySelectorAll('[data-count]').forEach(node => { node.textContent = '0'; node.setAttribute('data-count', '0'); });
    document.querySelectorAll('.task-sub-card').forEach(node => { node.style.display = 'none'; });
    document.querySelectorAll('.chart-bar').forEach(node => { node.style.height = '0%'; node.setAttribute('data-height', '0'); });
    document.querySelectorAll('.progress-fill').forEach(node => { node.style.width = '0%'; node.setAttribute('data-width', '0%'); });
    const distribution = document.getElementById('distributionList'); if (distribution) distribution.innerHTML = '';
    document.querySelectorAll('.article-item').forEach(node => { node.style.display = 'none'; });
    document.querySelectorAll('table.data tbody, .task-table tbody').forEach(node => { node.innerHTML = ''; });
    const pager = document.querySelector('.pager-info'); if (pager) pager.textContent = '正在加载文章数据...';
  }
  function notify(message) { let n = document.getElementById('adapterToast'); if (!n) { n = document.createElement('div'); n.id = 'adapterToast'; n.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:9999;padding:10px 16px;border-radius:999px;background:#111827;color:#fff;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.18)'; document.body.appendChild(n); } n.textContent = message; n.style.display = 'block'; clearTimeout(n._timer); n._timer = setTimeout(() => n.style.display = 'none', 1800); }
  function customSelect(button, options, selected, onChange) {
    document.querySelectorAll('.select-menu').forEach(x => x.remove());
    const menu = document.createElement('div'); menu.className = 'select-menu';
    menu.innerHTML = options.map(x => `<button type="button" data-value="${esc(x[0])}" aria-selected="${x[0] === selected}">${esc(x[1])}</button>`).join('');
    document.body.appendChild(menu); const r = button.getBoundingClientRect(); menu.style.left = `${Math.max(8, Math.min(window.innerWidth - menu.offsetWidth - 8, r.left))}px`; menu.style.top = `${Math.min(window.innerHeight - menu.offsetHeight - 8, r.bottom + 6)}px`;
    menu.querySelectorAll('button').forEach(item => item.onclick = () => { onChange(item.dataset.value, item.textContent); menu.remove(); });
    const close = e => { if (!menu.contains(e.target) && e.target !== button) { menu.remove(); document.removeEventListener('click', close, true); } }; setTimeout(() => document.addEventListener('click', close, true), 0);
  }
  function ensureArticleSelect(select, options, value, onChange) {
    if (!select) return null;
    select.style.display = 'none';
    let button = select.parentElement.querySelector('.adapter-filter-trigger');
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'select adapter-filter-trigger';
      select.parentElement.insertBefore(button, select);
    }
    const current = options.find(item => String(item[0]) === String(value)) || options[0];
    button.textContent = current[1];
    button.onclick = event => {
      event.preventDefault();
      customSelect(button, options, String(select.value || ''), (next, label) => {
        select.value = next;
        button.textContent = label;
        onChange(next);
      });
    };
    return button;
  }
  const articleSelectedIds = new Set();
  function updateSelectionUi(selector, headerSelector, actionSelector) {
    const rows = [...document.querySelectorAll(selector)];
    const selected = rows.filter(row => row.checked).length;
    const header = document.querySelector(headerSelector);
    if (header) {
      const all = rows.length > 0 && selected === rows.length;
      header.setAttribute('data-checked', String(all));
      header.classList.toggle('checked', all);
      header.setAttribute('aria-checked', all ? 'true' : (selected ? 'mixed' : 'false'));
    }
    const action = actionSelector && document.querySelector(actionSelector);
    if (action) {
      action.classList.toggle('is-disabled', selected === 0);
      action.disabled = selected === 0;
    }
  }
  function bindStableSelectAll(root) {
    const scope = root || document;
    scope.querySelectorAll('.adapter-account-check, .adapter-article-check, .adapter-task-check').forEach(row => {
      if (row._stableBound) return;
      row._stableBound = true;
      row.addEventListener('change', () => {
        if (row.matches('.adapter-article-check')) {
          const id = Number(row.value);
          if (row.checked) articleSelectedIds.add(id); else articleSelectedIds.delete(id);
        }
        const selector = row.matches('.adapter-article-check') ? '.adapter-article-check' : row.matches('.adapter-account-check') ? '.adapter-account-check' : '.adapter-task-check';
        updateSelectionUi(selector, row.matches('.adapter-article-check') ? '.ck-all' : '.ck.select-all', row.matches('.adapter-article-check') ? '#batchDelBtn' : null);
      });
    });
    scope.querySelectorAll('.ck.select-all, .ck-all').forEach(header => {
      if (header._stableBound) return;
      header._stableBound = true;
      header.addEventListener('click', event => {
        event.preventDefault();
        event.stopImmediatePropagation();
        const selector = document.querySelector('table.data tbody .adapter-article-check') ? '.adapter-article-check' : document.querySelector('.adapter-account-check') ? '.adapter-account-check' : '.adapter-task-check';
        const rows = [...document.querySelectorAll(selector)];
        const checked = !(rows.length && rows.every(row => row.checked));
        rows.forEach(row => { row.checked = checked; row.dispatchEvent(new Event('change', { bubbles: true })); });
        if (selector === '.adapter-article-check') updateSelectionUi(selector, '.ck-all', '#batchDelBtn');
      }, true);
    });
  }
  function modal(title, body, actions) {
    const old = document.getElementById('adapterModal'); if (old) old.remove();
    const shell = document.createElement('div'); shell.id = 'adapterModal'; shell.className = 'adapter-modal-backdrop';
    shell.innerHTML = `<div role="dialog" aria-modal="true" class="adapter-modal-panel"><div class="adapter-modal-header"><h2 class="adapter-modal-title">${esc(title)}</h2><button type="button" data-modal-close aria-label="关闭" class="adapter-modal-close" title="关闭">×</button></div><div data-modal-body class="adapter-modal-body"></div><div data-modal-actions class="adapter-modal-actions"></div></div>`;
    shell.querySelector('[data-modal-body]').innerHTML = body;
    const actionRoot = shell.querySelector('[data-modal-actions]'); (actions || []).forEach(a => { const b = document.createElement('button'); b.type = 'button'; b.className = a.primary ? 'btn btn-primary adapter-modal-primary' : 'btn btn-secondary adapter-modal-secondary'; b.textContent = a.label; b.onclick = a.onclick; actionRoot.appendChild(b); });
    shell.querySelector('[data-modal-close]').onclick = () => shell.remove(); shell.addEventListener('click', e => { if (e.target === shell) shell.remove(); }); document.body.appendChild(shell); return shell;
  }
  let batchPollTimer = null;
  let activityPollTimer = null;
  function showBatchProgress(batchId, accounts) {
    const shell = document.getElementById('adapterModal'); if (!shell) return;
    const body = shell.querySelector('[data-modal-body]'); const actions = shell.querySelector('[data-modal-actions]'); actions.innerHTML = '';
    const stop = document.createElement('button'); stop.className = 'btn btn-secondary'; stop.textContent = '关闭'; stop.onclick = () => shell.remove(); actions.appendChild(stop);
    body.innerHTML = `<div style="color:var(--muted-foreground);font-size:13px;margin-bottom:14px">批次 #${batchId} · ${accounts.length} 个公众号</div><div style="height:8px;border-radius:99px;background:var(--muted,#f2f2f7);overflow:hidden"><div data-batch-fill style="height:100%;width:0;background:var(--primary,#007aff);transition:width .25s"></div></div><div data-batch-summary style="margin:14px 0;font-size:14px">正在准备任务...</div><div data-batch-failed style="color:var(--destructive,#ff3b30);font-size:13px"></div>`;
    const poll = async () => { try { const data = await api(`/api/task-batches/${batchId}`); const fill = body.querySelector('[data-batch-fill]'); fill.style.width = `${data.progress || 0}%`; body.querySelector('[data-batch-summary]').textContent = data.current_account ? `当前：${data.current_account} · ${data.current_phase || ''} · 已完成 ${data.completed_count}/${data.total_count} · 成功 ${data.success_count} · 失败 ${data.failed_count}` : `已完成 ${data.completed_count}/${data.total_count} · 成功 ${data.success_count} · 失败 ${data.failed_count}`; body.querySelector('[data-batch-failed]').textContent = (data.failed_accounts || []).map(x => `${x.account}: ${x.error || '失败'}`).join('\n'); if (['completed','partial_failed','failed','stopped'].includes(data.status)) { clearInterval(batchPollTimer); batchPollTimer = null; const retry = document.createElement('button'); retry.className = 'btn btn-secondary'; retry.textContent = '重试失败任务'; retry.onclick = async () => { await json('POST', `/api/task-batches/${batchId}/retry`); showBatchProgress(batchId, accounts); }; actions.insertBefore(retry, actions.firstChild); body.querySelector('[data-batch-summary]').textContent += ` · ${data.status === 'completed' ? '批次完成' : '批次结束'}`; } } catch (_) {} };
    clearInterval(batchPollTimer); batchPollTimer = setInterval(poll, 1000); poll();
  }
  function openTaskBatchModal(selected) {
    const names = selected.map(a => a.name); const today = localDate(); const initial = Math.max(...selected.map(a => a.default_number || 10));
    const body = `<div style="font-size:13px;color:var(--muted-foreground);margin-bottom:14px">已选 ${names.length} 个公众号：${esc(names.join('、'))}</div><label style="display:block;font-size:13px;margin:12px 0 6px">采集篇数</label><input id="batchNumber" type="number" min="1" max="200" value="${initial}" class="input" style="width:100%"><label style="display:block;font-size:13px;margin:16px 0 6px">文章发布日期</label><select id="batchRange" class="select" style="width:100%"><option value="none">不限时间</option><option value="week">最近一周</option><option value="half">最近半个月</option><option value="custom">自定义开始日期</option></select><input id="batchStart" type="date" class="input" style="display:none;width:100%;margin-top:8px"><div style="font-size:12px;color:var(--muted-foreground);margin-top:8px">结束日期：${today}</div><label style="display:flex;gap:8px;align-items:center;margin-top:16px;font-size:13px"><input id="batchRemove" type="checkbox"> 复制链接后移除收藏</label><label style="display:flex;gap:8px;align-items:center;margin-top:10px;font-size:13px"><input id="batchSkip" type="checkbox" checked> 失败后继续其他公众号</label>`;
    const shell = modal('创建采集任务', body, [{ label: '取消', onclick: () => shell.remove() }, { label: '开始采集', primary: true, onclick: async () => { const range = shell.querySelector('#batchRange').value; const now = new Date(); let start = null; if (range === 'week') { now.setDate(now.getDate() - 6); start = localDate(now); } else if (range === 'half') { now.setDate(now.getDate() - 14); start = localDate(now); } else if (range === 'custom') start = shell.querySelector('#batchStart').value || null; const result = await json('POST', '/api/task-batches', { accounts: names, number: Math.max(1, Number(shell.querySelector('#batchNumber').value) || 10), remove_favorite: shell.querySelector('#batchRemove').checked, collection_start_date: start, collection_end_date: start ? today : null, skip_failed: shell.querySelector('#batchSkip').checked }); showBatchProgress(result.batch_id, names); } }]);
    shell.querySelector('#batchRange').onchange = e => { shell.querySelector('#batchStart').style.display = e.target.value === 'custom' ? 'block' : 'none'; };
  }
  function openAddAccountModal(existingAccounts, groups, onDone) {
    const groupOptions = ['<option value="">未分组</option>'].concat((groups || []).map(group => `<option value="${esc(group.id)}">${esc(group.name)}</option>`)).join('');
    const body = `<div class="adapter-form"><div class="adapter-field"><label for="newAccountName" class="adapter-field-label">公众号名称<span class="adapter-required">必填</span></label><input id="newAccountName" class="adapter-field-control" type="text" maxlength="80" autocomplete="off" placeholder="请输入公众号名称"><div class="adapter-field-hint">名称用于识别公众号，不能与已有配置重复。</div></div><div class="adapter-field"><label for="newAccountGroup" class="adapter-field-label">所属分组<span class="adapter-field-optional">可选</span></label><div class="adapter-select-wrap"><select id="newAccountGroup" class="adapter-field-control adapter-select">${groupOptions}</select></div></div><div class="adapter-field"><label for="newAccountNumber" class="adapter-field-label">默认最大采集篇数<span class="adapter-required">必填</span></label><div class="adapter-number-control"><button type="button" data-number-step="-1" aria-label="减少采集篇数" title="减少">-</button><input id="newAccountNumber" class="adapter-number-input" type="number" min="1" max="200" step="1" value="10" inputmode="numeric"><button type="button" data-number-step="1" aria-label="增加采集篇数" title="增加">+</button></div><div class="adapter-field-hint">每次任务最多默认采集的文章数量，可在公众号列表中修改。</div></div></div>`;
    let shell;
    shell = modal('添加公众号', body, [
      { label: '取消', onclick: () => shell.remove() },
      { label: '添加', primary: true, onclick: async event => {
        const nameInput = shell.querySelector('#newAccountName');
        const groupInput = shell.querySelector('#newAccountGroup');
        const numberInput = shell.querySelector('#newAccountNumber');
        const name = (nameInput?.value || '').trim();
        const number = Math.max(1, Math.min(200, Number(numberInput?.value) || 10));
        if (!name) { notify('请输入公众号名称'); nameInput?.focus(); return; }
        if (existingAccounts.some(account => String(account.name || '').trim() === name)) { notify('该公众号已存在'); nameInput?.focus(); return; }
        const button = event?.currentTarget;
        if (button) { button.disabled = true; button.textContent = '添加中...'; }
        try {
          await json('POST', '/api/accounts', { name, default_number: number, group_id: groupInput?.value ? Number(groupInput.value) : null });
          shell.remove();
          notify('公众号已添加');
          if (onDone) await onDone();
        } catch (error) {
          if (button) { button.disabled = false; button.textContent = '添加'; }
          notify(error?.detail || error?.error || '添加公众号失败');
        }
      } }
    ]);
    const nameInput = shell.querySelector('#newAccountName');
    nameInput?.focus();
    shell.querySelectorAll('[data-number-step]').forEach(button => { button.onclick = () => { const input = shell.querySelector('#newAccountNumber'); const next = Math.max(1, Math.min(200, (Number(input.value) || 10) + Number(button.dataset.numberStep))); input.value = String(next); input.dispatchEvent(new Event('input', { bubbles: true })); }; });
    shell.querySelector('#newAccountNumber')?.addEventListener('blur', event => { event.target.value = String(Math.max(1, Math.min(200, Number(event.target.value) || 10))); });
    nameInput?.addEventListener('keydown', event => { if (event.key === 'Enter') shell.querySelector('[data-modal-actions] button:last-child')?.click(); });
  }
  function openGroupModal(onDone) {
    let shell;
    shell = modal('新建分组', '<div class="adapter-form"><div class="adapter-field"><label for="newGroupName" class="adapter-field-label">分组名称<span class="adapter-required">必填</span></label><input id="newGroupName" class="adapter-field-control" type="text" maxlength="40" placeholder="例如：重点关注"><div class="adapter-field-hint">创建后可从公众号列表批量移入或移出公众号。</div></div></div>', [
      { label: '取消', onclick: () => shell.remove() },
      { label: '创建', primary: true, onclick: async event => {
        const input = shell.querySelector('#newGroupName'); const name = (input?.value || '').trim();
        if (!name) { notify('请输入分组名称'); input?.focus(); return; }
        const button = event?.currentTarget; if (button) { button.disabled = true; button.textContent = '创建中...'; }
        try { await json('POST', '/api/account-groups', { name }); shell.remove(); notify('分组已创建'); if (onDone) await onDone(); }
        catch (error) { if (button) { button.disabled = false; button.textContent = '创建'; } notify(error?.detail || '创建分组失败'); }
      } }
    ]);
    shell.querySelector('#newGroupName')?.focus();
  }
  function openMoveGroupModal(groups, ids, onDone) {
    let shell;
    const options = '<option value="">移出分组</option>' + (groups || []).map(group => `<option value="${esc(group.id)}">${esc(group.name)}</option>`).join('');
    shell = modal('调整公众号分组', `<div class="adapter-form"><div class="adapter-field"><label for="moveAccountGroup" class="adapter-field-label">目标分组</label><select id="moveAccountGroup" class="adapter-field-control adapter-select">${options}</select><div class="adapter-field-hint">已选择 ${ids.length} 个公众号，选择“移出分组”即可清空分组。</div></div></div>`, [
      { label: '取消', onclick: () => shell.remove() },
      { label: '确定调整', primary: true, onclick: async event => {
        const button = event?.currentTarget; if (button) { button.disabled = true; button.textContent = '保存中...'; }
        try { await json('POST', '/api/accounts/batch-group', { ids, group_id: shell.querySelector('#moveAccountGroup').value || null }); shell.remove(); notify('公众号分组已更新'); if (onDone) await onDone(); }
        catch (error) { if (button) { button.disabled = false; button.textContent = '确定调整'; } notify(error?.detail || '分组调整失败'); }
      } }
    ]);
  }
  function bindNavigation() {
    const routes = { '公众号管理': 'accounts.html', '任务中心': 'task-center.html', '文章库': 'articles.html', '运行概览': 'overview.html', '设置': 'settings.html' };
    document.querySelectorAll('a.nav-item, .nav-list a, .sidebar-nav a').forEach(a => { const label = (a.textContent || '').replace(/\s+/g, '').trim(); if (routes[label]) a.setAttribute('href', routes[label]); });
    document.querySelectorAll('a[href="tasks.html"]').forEach(a => a.setAttribute('href', 'task-center.html'));
    document.addEventListener('click', event => {
      const link = event.target.closest('a.nav-item, .nav-list a, .sidebar-nav a');
      if (!link) return;
      const href = link.getAttribute('href');
      if (!href || href === '#') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      navigatePage(href, false).catch(() => location.assign(href));
    }, true);
    document.querySelectorAll('a.nav-item, .nav-list a, .sidebar-nav a').forEach(link => { const href = link.getAttribute('href'); if (href && href !== '#') { const prefetch = document.createElement('link'); prefetch.rel = 'prefetch'; prefetch.href = href; document.head.appendChild(prefetch); } });
    window.addEventListener('popstate', () => navigatePage(location.href, true).catch(() => {}));
  }
  async function serviceStatus() { try { const s = await api('/api/status'); const label = document.querySelector('.status-text'); if (label) label.textContent = s.worker_alive ? (s.current ? '采集运行中' : '服务运行中') : '服务异常'; const dot = document.querySelector('.status-dot'); if (dot) dot.style.background = s.worker_alive ? (s.current ? 'var(--primary)' : 'var(--success)') : 'var(--destructive)'; return s; } catch (_) { return null; } }
  function buttonByText(text, root) { return [...(root || document).querySelectorAll('button')].find(b => (b.textContent || '').replace(/\s+/g, '').includes(text)); }
  function selectedIds(selector) { return [...document.querySelectorAll(selector)].filter(x => x.checked || x.getAttribute('data-checked') === 'true' || x.classList.contains('checked')).map(x => Number(x.value)).filter(Number.isFinite); }
  let articlePage = 1;
  let articlePageSize = 10;
  const pageCache = new Map();

  function pageNameFromHref(href) {
    const name = new URL(href, location.href).pathname.split('/').pop().replace('.html', '');
    return name === 'tasks' ? 'task-center' : (name || 'accounts');
  }

  async function getPageDocument(href) {
    const url = new URL(href, location.href);
    const key = url.pathname + url.search;
    if (!pageCache.has(key)) pageCache.set(key, fetch(url.href, { credentials: 'same-origin' }).then(r => r.ok ? r.text() : Promise.reject(new Error('页面加载失败'))));
    return new DOMParser().parseFromString(await pageCache.get(key), 'text/html');
  }

  function swapPageStyles(targetDoc, targetPage) {
    const previous = [...document.head.querySelectorAll('style[data-design-page-style]')];
    const added = [];
    targetDoc.head.querySelectorAll('style').forEach(style => {
      const clone = document.createElement('style');
      clone.dataset.designPageStyle = targetPage;
      clone.textContent = style.textContent || '';
      document.head.appendChild(clone);
      added.push(clone);
    });
    const shell = document.querySelector('link[href="/design-shell.css"]');
    if (shell) document.head.appendChild(shell);
    return () => previous.forEach(node => node.remove());
  }

  async function navigatePage(href, replace) {
    const targetPage = pageNameFromHref(href);
    if (targetPage === page && !new URL(href, location.href).search) return;
    // Each design page owns its markup, inline styles and demo scripts. Reusing
    // only the content root leaves old page handlers/styles attached and can
    // produce mojibake or a half-rendered view until a hard refresh. Native
    // navigation gets the same clean UTF-8 document as a refresh; link prefetch
    // keeps this local transition fast.
    location.assign(href);
    return;
    /* SPA path retained below for reference and cache compatibility. */
    const currentRoot = document.getElementById('app-view') || document.querySelector('.content-area, .app-main, .main-content');
    if (!currentRoot) { location.assign(href); return; }
      currentRoot.classList.add('is-loading');
    document.documentElement.classList.add('adapter-loading');
    try {
      const targetDoc = await getPageDocument(href);
      const targetRoot = targetDoc.querySelector('.content-area, .app-main, .main-content');
      if (!targetRoot) throw new Error('页面内容区域不存在');
      targetRoot.id = 'app-view';
      const removePreviousStyles = swapPageStyles(targetDoc, targetPage);
      currentRoot.outerHTML = targetRoot.outerHTML;
      removePreviousStyles();
      page = targetPage;
      articlePage = 1;
      window._settingsReady = false;
      document.title = targetDoc.title || document.title;
      document.querySelectorAll('.nav-item').forEach(link => link.classList.toggle('active', pageNameFromHref(link.getAttribute('href') || '') === page));
      if (replace) history.replaceState({}, '', href); else history.pushState({}, '', href);
      clearDemoContent();
      await refresh();
    } catch (_) {
      location.assign(href);
    }
  }

  async function renderAccounts() {
    const [accounts, groups] = await Promise.all([api('/api/accounts'), api('/api/account-groups')]); const tbody = document.querySelector('.data-table tbody'); if (!tbody) return;
    const search = document.querySelector('.search-box input'); const filterButtons = [...document.querySelectorAll('.select-btn')].slice(0, 3); const groupFilter = filterButtons[0]; const activeGroup = groupFilter?._adapterValue || 'all'; const term = (search?.value || '').trim().toLowerCase();
    const stateFilter = filterButtons[1]?._adapterValue || 'all'; const sortMode = filterButtons[2]?._adapterValue || 'recent';
    let visible = accounts.filter(a => (!term || a.name.toLowerCase().includes(term)) && (activeGroup === 'all' || (activeGroup === 'ungrouped' && !a.group_id) || String(a.group_id) === activeGroup) && (stateFilter === 'all' || (stateFilter === 'enabled' && !!a.enabled) || (stateFilter === 'disabled' && !a.enabled)));
    visible.sort((a, b) => sortMode === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : String(b.last_collected_at || '').localeCompare(String(a.last_collected_at || '')));
    tbody.innerHTML = visible.map((a, i) => `<tr data-account-id="${a.id}"><td class="col-check"><input type="checkbox" class="adapter-account-check" value="${a.id}"></td><td class="col-idx">${i + 1}</td><td class="col-name"><span class="name-cell" title="${esc(a.name)}">${esc(a.name)}</span></td><td class="col-status"><span class="tag ${a.enabled ? 'tag-green' : 'tag-gray'}"><span class="dot ${a.enabled ? 'dot-green' : 'dot-gray'}"></span>${a.enabled ? '启用' : '停用'}</span></td><td class="col-count">${a.default_number || 10}</td><td class="col-articles">${a.article_count || 0}</td><td class="col-time">${formatTime(a.last_collected_at)}</td><td class="col-task">${a.last_task_status ? `<span class="tag ${statusClass[a.last_task_status] || 'tag-gray'}">${statusText[a.last_task_status] || a.last_task_status}</span>` : '-'}</td><td class="col-actions"><button class="icon-btn adapter-account-edit" title="编辑">编辑</button><button class="icon-btn adapter-account-articles" title="查看文章">文章</button><button class="icon-btn adapter-account-tasks" title="查看任务">任务</button><button class="icon-btn adapter-account-toggle" title="切换启用状态">${a.enabled ? '停用' : '启用'}</button><button class="icon-btn icon-btn-danger adapter-account-delete" title="删除">删除</button></td></tr>`).join('') || '<tr><td colspan="9" class="empty-cell">暂无公众号配置</td></tr>';
    tbody.querySelectorAll('.adapter-account-edit').forEach((b, i) => b.onclick = async () => { const a = visible[i]; const name = prompt('公众号名称', a.name); if (name == null) return; const number = prompt('默认最大采集篇数', a.default_number || 10); if (number == null) return; await json('PATCH', `/api/accounts/${a.id}`, { name: name.trim(), enabled: !!a.enabled, default_number: Math.max(1, Number(number) || 10), group_id: a.group_id }); notify('公众号已更新'); renderAccounts(); });
    tbody.querySelectorAll('.adapter-account-toggle').forEach((b, i) => b.onclick = () => json('PATCH', `/api/accounts/${visible[i].id}`, { name: visible[i].name, enabled: !visible[i].enabled, default_number: visible[i].default_number, group_id: visible[i].group_id }).then(renderAccounts));
    tbody.querySelectorAll('.adapter-account-delete').forEach((b, i) => b.onclick = async () => { if (!confirm('确定删除这个公众号配置？')) return; await api(`/api/accounts/${visible[i].id}`, { method: 'DELETE' }); notify('公众号已删除'); renderAccounts(); });
    tbody.querySelectorAll('.adapter-account-articles').forEach((b, i) => b.onclick = () => { location.href = 'articles.html?account=' + encodeURIComponent(visible[i].name); });
    tbody.querySelectorAll('.adapter-account-tasks').forEach((b, i) => { b.onclick = () => { location.href = 'task-center.html?account=' + encodeURIComponent(visible[i].name); }; });
    if (search && !search._adapterBound) { search._adapterBound = true; search.oninput = renderAccounts; }
    filterButtons.forEach((button, index) => { if (!button._adapterBound) { button._adapterBound = true; button.onclick = e => { e.preventDefault(); const options = index === 0 ? [['all','全部分组'], ...groups.map(g => [String(g.id), g.name]), ['ungrouped','未分组']] : index === 1 ? [['all','全部状态'], ['enabled','已启用'], ['disabled','已停用']] : [['recent','最近采集'], ['name','名称排序']]; const current = index === 0 ? (button._adapterValue || 'all') : (button._adapterValue || 'all'); customSelect(button, options, current, (value, label) => { button._adapterValue = value; button.childNodes[0].nodeValue = label; renderAccounts(); }); }; } });
    const add = buttonByText('添加公众号'); if (add && !add._adapterBound) { add._adapterBound = true; add.onclick = () => openAddAccountModal(accounts, groups, renderAccounts); }
    const addGroup = buttonByText('新建分组'); if (!addGroup && add?.parentElement) { const groupButton = document.createElement('button'); groupButton.type = 'button'; groupButton.className = 'btn btn-secondary adapter-create-group'; groupButton.textContent = '新建分组'; add.parentElement.insertBefore(groupButton, add.nextSibling); groupButton.onclick = () => openGroupModal(renderAccounts); }
    const checks = () => selectedIds('.adapter-account-check'); const del = buttonByText('批量删除'); const enable = buttonByText('批量启用'); const disable = buttonByText('批量停用'); const create = buttonByText('创建采集任务');
    if (del && !del._adapterBound) { del._adapterBound = true; del.onclick = async () => { const ids = checks(); if (!ids.length) return notify('请先勾选公众号'); if (!confirm(`确定删除 ${ids.length} 个公众号配置？`)) return; await json('POST', '/api/accounts/batch-delete', ids); notify('已批量删除'); renderAccounts(); }; }
    if (enable && !enable._adapterBound) { enable._adapterBound = true; enable.onclick = async () => { const ids = checks(); if (!ids.length) return notify('请先勾选公众号'); await json('POST', '/api/accounts/batch-enable', ids); renderAccounts(); }; }
    if (disable && !disable._adapterBound) { disable._adapterBound = true; disable.onclick = async () => { const ids = checks(); if (!ids.length) return notify('请先勾选公众号'); await json('POST', '/api/accounts/batch-disable', ids); renderAccounts(); }; }
    const moveGroup = buttonByText('移入/移出分组'); if (!moveGroup && add?.parentElement) { const groupAction = document.createElement('button'); groupAction.type = 'button'; groupAction.className = 'btn btn-secondary adapter-group-action'; groupAction.textContent = '移入/移出分组'; add.parentElement.appendChild(groupAction); groupAction.onclick = () => { const ids = checks(); if (!ids.length) return notify('请先勾选公众号'); openMoveGroupModal(groups, ids, renderAccounts); }; }
    if (create && !create._adapterBound) { create._adapterBound = true; create.onclick = () => { const selected = accounts.filter(a => checks().includes(a.id) && a.enabled); if (!selected.length) return notify('请先勾选启用的公众号'); openTaskBatchModal(selected); }; }
    bindStableSelectAll(document);
    const accountInfo = document.querySelector('.page-info'); if (accountInfo) accountInfo.textContent = `共 ${accounts.length} 个公众号`;
  }

  async function renderArticles() {
    const search = document.querySelector('input[placeholder*="搜索"]'); const selects = document.querySelectorAll('select'); const accountSelect = selects[0]; const timeSelect = selects[1]; const pageSizeSelect = selects[2];
    if (accountSelect && !accountSelect._adapterBound) { const accounts = await api('/api/accounts'); accountSelect.innerHTML = '<option value="">全部公众号</option>' + accounts.map(a => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join(''); accountSelect._adapterBound = true; accountSelect.onchange = renderArticles; const initial = new URLSearchParams(location.search).get('account'); if (initial) accountSelect.value = initial; }
    if (accountSelect) ensureArticleSelect(accountSelect, [...accountSelect.options].map(option => [option.value, option.textContent]), accountSelect.value, value => { accountSelect.value = value; articlePage = 1; renderArticles(); });
    if (timeSelect && !timeSelect._adapterOptionsReady) { timeSelect.innerHTML = '<option value="">全部</option><option value="1">今天</option><option value="7">最近7天</option><option value="30">最近30天</option>'; timeSelect._adapterOptionsReady = true; }
    if (timeSelect) ensureArticleSelect(timeSelect, [['', '全部'], ['1', '今天'], ['7', '最近7天'], ['30', '最近30天']], timeSelect.value, value => { timeSelect.value = value; articlePage = 1; renderArticles(); });
    const days = timeSelect?.value ? Number(timeSelect.value) : null;
    if (!document.getElementById('articleDateFrom') && timeSelect?.parentElement?.parentElement) {
      const holder = document.createElement('div'); holder.style.cssText = 'display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap';
      holder.innerHTML = '<input id="articleDateFrom" type="date" class="input" aria-label="开始日期"><span style="color:var(--muted-foreground);font-size:12px">至</span><input id="articleDateTo" type="date" class="input" aria-label="结束日期" readonly><button id="articleDateApply" class="btn btn-secondary">应用</button><button id="articleDateClear" class="btn btn-secondary">清除</button>';
      timeSelect.parentElement.parentElement.appendChild(holder); holder.querySelector('#articleDateTo').value = localDate();
      holder.querySelector('#articleDateApply').onclick = () => { articlePage = 1; renderArticles(); }; holder.querySelector('#articleDateClear').onclick = () => { holder.querySelector('#articleDateFrom').value = ''; articlePage = 1; renderArticles(); };
    }
    const dateFrom = document.getElementById('articleDateFrom')?.value || '';
    const dateTo = document.getElementById('articleDateTo')?.value || localDate();
    if (pageSizeSelect && !pageSizeSelect._adapterBound) { pageSizeSelect._adapterBound = true; pageSizeSelect.onchange = () => { articlePageSize = parseInt(pageSizeSelect.value, 10) || 10; articlePage = 1; renderArticles(); }; }
    if (pageSizeSelect) { const option = [...pageSizeSelect.options].find(x => /^(\d+)/.test(x.value || x.textContent)); if (option) pageSizeSelect.value = `${articlePageSize}条/页`; }
    const query = new URLSearchParams({ paged: 'true', page: String(articlePage), page_size: String(articlePageSize) }); if (search?.value) query.set('search', search.value); if (accountSelect?.value) query.set('account', accountSelect.value); if (days) query.set('days', String(days)); if (dateFrom) { query.set('date_from', dateFrom); query.set('date_to', dateTo); }
    const result = await api('/api/articles?' + query.toString()); const rows = result.items || [];
    const tbody = document.querySelector('table.data tbody'); if (!tbody) return; tbody.innerHTML = rows.map(a => `<tr><td><input type="checkbox" class="adapter-article-check" value="${a.id}" ${articleSelectedIds.has(Number(a.id)) ? 'checked' : ''}></td><td><span class="cell-title" title="${esc(a.title)}">${esc(a.title)}</span></td><td><a class="cell-link" title="${esc(a.url)}" href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.url)}</a></td><td><span class="chip">${esc(a.account)}</span></td><td>${esc(a.published_at || (a.collected_at || '').slice(0, 10) || '-')}</td><td>${formatTime(a.collected_at)}</td><td><span class="task-tag">${a.task_id ? '#' + a.task_id : '-'}</span></td><td><button class="act-btn adapter-open" data-url="${esc(a.url)}" title="打开">打开</button><button class="act-btn adapter-copy" data-url="${esc(a.url)}" title="复制链接">复制</button><button class="act-btn danger adapter-article-delete" data-id="${a.id}" title="删除">删除</button></td></tr>`).join('') || '<tr><td colspan="8">暂无文章数据</td></tr>';
    const emptyState = document.getElementById('emptyState'); if (emptyState) emptyState.style.display = rows.length ? 'none' : 'flex';
    tbody.querySelectorAll('.adapter-open').forEach(b => b.onclick = () => window.open(b.dataset.url, '_blank', 'noopener')); tbody.querySelectorAll('.adapter-copy').forEach(b => b.onclick = async () => { try { await navigator.clipboard.writeText(b.dataset.url); } catch (_) { const t = document.createElement('textarea'); t.value = b.dataset.url; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); } notify('链接已复制'); }); tbody.querySelectorAll('.adapter-article-delete').forEach(b => b.onclick = async () => { if (confirm('确定删除这篇文章记录？')) { await api('/api/articles?ids=' + b.dataset.id, { method: 'DELETE' }); articleSelectedIds.delete(Number(b.dataset.id)); renderArticles(); } });
    bindStableSelectAll(document);
    const batch = document.getElementById('batchDelBtn'); if (batch && !batch._adapterBound) { batch._adapterBound = true; batch.onclick = async () => { const ids = [...articleSelectedIds].filter(Number.isFinite); if (!ids.length) return notify('请先勾选文章'); if (confirm(`确定删除 ${ids.length} 篇文章记录？`)) { await api('/api/articles?ids=' + ids.join(','), { method: 'DELETE' }); ids.forEach(id => articleSelectedIds.delete(id)); notify('文章已删除'); articlePage = 1; renderArticles(); } }; }
    // The design template also registers a click listener for this button. Replace
    // the node once so the adapter owns the toggle behavior and cannot double-toggle
    // the menu (which made it look like the menu opened and closed immediately).
    let exportBtn = document.getElementById('exportBtn');
    if (exportBtn && !exportBtn._adapterBound) {
      const replacement = exportBtn.cloneNode(true);
      exportBtn.replaceWith(replacement);
      exportBtn = replacement;
      exportBtn._adapterBound = true;
      exportBtn.onclick = e => {
        e.preventDefault();
        e.stopPropagation();
        document.getElementById('exportDropdown')?.classList.toggle('open');
      };
    }
    document.querySelectorAll('.tbar-left .btn-secondary').forEach(button => { if (!button._adapterBound && /按公众号导出|全量导出/.test(button.textContent || '')) { button._adapterBound = true; button.onclick = () => { const account = /按公众号导出/.test(button.textContent || '') ? accountSelect?.value : ''; if (/按公众号导出/.test(button.textContent || '') && !account) return notify('请先选择公众号'); const q = new URLSearchParams(); if (account) q.set('account', account); if (dateFrom) { q.set('date_from', dateFrom); q.set('date_to', dateTo); } location.href = '/api/articles/export?' + q.toString(); }; } });
    const articleAll = document.querySelector('.ck-all'); if (articleAll && !articleAll._adapterFilterBound) { const old = articleAll.cloneNode(true); articleAll.replaceWith(old); old._adapterFilterBound = true; old.onclick = async e => { e.preventDefault(); e.stopImmediatePropagation(); const rows = [...document.querySelectorAll('.adapter-article-check')]; const checked = !(rows.length && rows.every(x => x.checked)); if (checked) { rows.forEach(x => { x.checked = true; articleSelectedIds.add(Number(x.value)); }); } else { rows.forEach(x => { x.checked = false; articleSelectedIds.delete(Number(x.value)); }); } updateSelectionUi('.adapter-article-check', '.ck-all', '#batchDelBtn'); }; }
    let selectFiltered = document.getElementById('articleSelectFiltered'); if (!selectFiltered) { const target = document.querySelector('.tbar-left'); if (target) { selectFiltered = document.createElement('button'); selectFiltered.id = 'articleSelectFiltered'; selectFiltered.type = 'button'; selectFiltered.className = 'btn btn-secondary'; selectFiltered.textContent = '全选筛选结果'; target.appendChild(selectFiltered); } }
    if (selectFiltered && !selectFiltered._adapterBound) { selectFiltered._adapterBound = true; selectFiltered.onclick = async () => { const q = new URLSearchParams(); if (accountSelect?.value) q.set('account', accountSelect.value); if (search?.value) q.set('search', search.value); if (days) q.set('days', String(days)); if (dateFrom) { q.set('date_from', dateFrom); q.set('date_to', dateTo); } const resultIds = await api('/api/articles/ids?' + q.toString()); resultIds.ids.forEach(id => articleSelectedIds.add(Number(id))); [...document.querySelectorAll('.adapter-article-check')].forEach(row => { row.checked = true; }); updateSelectionUi('.adapter-article-check', '.ck-all', '#batchDelBtn'); notify(`已选中当前筛选条件下 ${resultIds.total} 篇文章`); }; }
    const pagerInfo = document.querySelector('.pager-info'); if (pagerInfo) pagerInfo.innerHTML = `共 <b>${result.total}</b> 篇文章 &nbsp;|&nbsp; 第 <b>${result.page}</b> 页 / 共 <b>${result.pages}</b> 页`;
    const pager = document.querySelector('.pager-controls'); if (pager) { const pages = []; const start = Math.max(1, result.page - 2); const end = Math.min(result.pages, start + 4); for (let p = start; p <= end; p++) pages.push(`<button class="page-btn ${p === result.page ? 'active' : ''}" data-page="${p}">${p}</button>`); pager.innerHTML = `<button class="page-btn is-icon" data-page="${Math.max(1, result.page - 1)}" ${result.page <= 1 ? 'disabled' : ''} title="上一页">‹</button>${pages.join('')}<button class="page-btn is-icon" data-page="${Math.min(result.pages, result.page + 1)}" ${result.page >= result.pages ? 'disabled' : ''} title="下一页">›</button>`; pager.querySelectorAll('.page-btn:not(:disabled)').forEach(button => button.onclick = () => { articlePage = Number(button.dataset.page); renderArticles(); }); }
    document.querySelectorAll('#exportDropdown .dropdown-item').forEach((item, index) => { if (!item._adapterBound) { item._adapterBound = true; item.onclick = e => { e.preventDefault(); e.stopPropagation(); const text = item.textContent || ''; const selected = [...articleSelectedIds]; if (text.includes('已选中') && !selected.length) return notify('请先勾选文章'); const q = new URLSearchParams(); if (text.includes('当前页')) { (accountSelect?.value) && q.set('account', accountSelect.value); q.set('page', String(articlePage)); q.set('page_size', String(articlePageSize)); if (dateFrom) { q.set('date_from', dateFrom); q.set('date_to', dateTo); } } else if (text.includes('已选中')) q.set('ids', selected.join(',')); if (accountSelect?.value && !text.includes('已选中')) q.set('account', accountSelect.value); if (dateFrom && !text.includes('已选中')) { q.set('date_from', dateFrom); q.set('date_to', dateTo); } location.href = '/api/articles/export?' + q.toString(); document.getElementById('exportDropdown')?.classList.remove('open'); }; } });
    if (search && !search._adapterBound) { search._adapterBound = true; let timer; search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { articleSelectedIds.clear(); articlePage = 1; renderArticles(); }, 250); }; } if (timeSelect && !timeSelect._adapterBound) { timeSelect._adapterBound = true; timeSelect.onchange = () => { articleSelectedIds.clear(); articlePage = 1; renderArticles(); }; }
    bindStableSelectAll(document); updateSelectionUi('.adapter-article-check', '.ck-all', '#batchDelBtn');
  }

  async function renderTasks() {
    const accountFilter = new URLSearchParams(location.search).get('account');
    const data = await api('/api/task-center' + (accountFilter ? '?account=' + encodeURIComponent(accountFilter) : ''));
    const signature = taskSignature(data);
    if (signature === taskCenterSignature) return;
    taskCenterSignature = signature;
    const tasks = data.tasks || [];
    const stats = data.stats || {};
    updateTaskStats(stats);
    taskCenterActive = (stats.running_count || 0) > 0 || (stats.pending_count || 0) > 0;
    const running = tasks.filter(t => ['collecting', 'extracting'].includes(t.status));
    document.querySelectorAll('.task-sub-card').forEach((card, i) => { const t = running[i]; card.style.display = t ? '' : 'none'; if (t) { const text = card.querySelector('div[style*="font-weight:600"]'); if (text) text.textContent = `${t.account}采集`; const sub = card.querySelector('div[style*="font-size:0.75rem"]'); if (sub) sub.textContent = `公众号: ${t.account} | 任务ID: #${t.id}`; const badges = card.querySelectorAll('.badge'); if (badges[0]) badges[0].textContent = statusText[t.status]; if (badges[1]) badges[1].textContent = `复制后移除收藏: ${t.remove_favorite ? '已启用' : '未启用'}`; const fills = card.querySelectorAll('.progress-fill'); const p1 = Math.min(100, Math.round((t.collected || 0) / Math.max(1, t.number) * 100)); const p2 = Math.min(100, Math.round((t.extracted || 0) / Math.max(1, t.collected || t.number) * 100)); [p1, p2].forEach((p, n) => { if (fills[n]) { fills[n].style.setProperty('--target', p + '%'); fills[n].style.width = p + '%'; fills[n].setAttribute('data-width', p + '%'); const label = fills[n].closest('div[style*="flex-direction:column"]')?.querySelector('span[style*="font-weight:600"]'); if (label) label.textContent = `${p}%`; } }); } });
    const runningHost = document.querySelector('.task-sub-card')?.parentElement; if (runningHost) { let empty = runningHost.querySelector('[data-adapter-empty-running]'); if (!running.length) { if (!empty) { empty = document.createElement('div'); empty.dataset.adapterEmptyRunning = 'true'; empty.style.cssText = 'padding:24px;text-align:center;color:var(--apple-muted-foreground);font-size:13px'; empty.textContent = '当前没有运行中的任务'; runningHost.appendChild(empty); } } else if (empty) empty.remove(); }
    const tbody = document.querySelector('.task-table tbody'); if (!tbody) return; tbody.innerHTML = tasks.map(t => `<tr><td><input type="checkbox" class="adapter-task-check" value="${t.id}"></td><td style="font-weight:500">#${t.id}</td><td>${esc(t.account)}</td><td><span class="status-badge ${statusClass[t.status] || 'status-muted'}">${statusText[t.status] || t.status}</span></td><td>${formatTime(t.created_at)}</td><td>${formatTime(t.started_at)}</td><td style="text-align:right"><button class="icon-btn primary adapter-task-retry" data-id="${t.id}" ${['failed','stopped'].includes(t.status) ? '' : 'disabled'} title="重试">重试</button><button class="icon-btn danger adapter-task-delete" data-id="${t.id}" title="删除">删除</button></td></tr>`).join('') || '<tr><td colspan="7">暂无任务</td></tr>'; bindStableSelectAll(document);
    tbody.querySelectorAll('.adapter-task-retry').forEach(b => b.onclick = () => json('POST', `/api/tasks/${b.dataset.id}/retry`).then(() => { notify('任务已重试'); renderTasks(); })); tbody.querySelectorAll('.adapter-task-delete').forEach(b => b.onclick = async () => { if (confirm('确定删除该任务记录？')) { await api(`/api/tasks/${b.dataset.id}`, { method: 'DELETE' }); renderTasks(); } });
    const clear = buttonByText('清空已完成'); if (clear && !clear._adapterBound) { clear._adapterBound = true; clear.onclick = () => api('/api/tasks/clear-finished', { method: 'POST' }).then(() => { notify('已清理结束任务'); renderTasks(); }); } const batch = buttonByText('批量删除已结束'); if (batch && !batch._adapterBound) { batch._adapterBound = true; batch.onclick = () => { const ids = selectedIds('.adapter-task-check'); if (!ids.length) return notify('请先勾选任务'); return json('POST', '/api/tasks/batch-delete', ids).then(() => { notify('已删除结束任务'); renderTasks(); }); }; }
    document.querySelectorAll('button[title="暂停"]').forEach(b => { if (!b._adapterBound) { b._adapterBound = true; b.onclick = () => json('POST', '/api/pause').then(() => notify('已请求暂停')); } }); document.querySelectorAll('button[title="停止"]').forEach(b => { if (!b._adapterBound) { b._adapterBound = true; b.onclick = () => json('POST', '/api/stop').then(() => notify('已请求停止')); } });
    document.querySelectorAll('button[title="继续"]').forEach(b => { if (!b._adapterBound) { b._adapterBound = true; b.onclick = () => json('POST', '/api/resume').then(() => notify('已继续运行')); } });
    const refreshButton = buttonByText('刷新'); if (refreshButton && !refreshButton._adapterBound) { refreshButton._adapterBound = true; refreshButton.onclick = () => renderTasks(); }
  }

  async function renderOverview() {
    const data = await api('/api/overview');
    const stats = document.querySelectorAll('.stat-value[data-count]');
    [data.account_count, data.article_count, data.today_article_count, data.success_task_count, data.failed_task_count, data.queued_task_count, data.success_rate].forEach((v, i) => { if (stats[i] && v != null) { stats[i].textContent = i === 6 ? `${v}%` : v; stats[i].setAttribute('data-count', String(v)); stats[i].setAttribute('data-suffix', i === 6 ? '%' : ''); } });
    const body = document.querySelector('.data-table tbody'); if (body) body.innerHTML = (data.recent_tasks || []).map(t => `<tr><td>#${t.id}</td><td>${esc(t.account)}</td><td><span class="status-badge ${statusClass[t.status] || 'status-muted'}">${statusText[t.status] || t.status}</span></td><td>${formatTime(t.finished_at || t.created_at)}</td><td>${t.extracted || 0}</td></tr>`).join('') || '<tr><td colspan="5">暂无任务记录</td></tr>';
    const distribution = document.getElementById('distributionList'); if (distribution) { const max = Math.max(1, ...(data.distribution || []).map(x => x.count)); distribution.innerHTML = (data.distribution || []).map(x => `<div class="flex items-center gap-3"><span class="text-sm font-medium w-20 flex-shrink-0" style="color:var(--card-foreground)" title="${esc(x.account)}">${esc(x.account)}</span><div class="progress-bar"><div class="progress-fill" style="width:${Math.round(x.count / max * 100)}%" data-width="${Math.round(x.count / max * 100)}%"></div></div><span class="text-sm font-semibold w-14 text-right" style="color:var(--muted-foreground)">${x.count}</span></div>`).join('') || '<div style="color:var(--muted-foreground);font-size:13px">暂无采集分布</div>'; }
    const trend = data.trend || []; const maxTrend = Math.max(1, ...trend.map(x => x.count)); const bars = document.querySelectorAll('.chart-bar'); bars.forEach((bar, i) => { const item = trend[i]; if (!item) { bar.style.height = '0%'; bar.setAttribute('data-height', '0'); return; } const height = Math.max(item.count ? 6 : 0, Math.round(item.count / maxTrend * 100)); bar.style.height = `${height}%`; bar.setAttribute('data-height', String(height)); bar.dataset.value = item.count; const tip = bar.querySelector('.chart-tooltip'); if (tip) tip.textContent = `${item.count} 篇`; const label = bar.parentElement?.querySelector('.chart-label'); if (label) label.textContent = item.date.slice(5); });
    const articleItems = document.querySelectorAll('.article-item'); const recent = data.recent_articles || []; articleItems.forEach((item, i) => { const article = recent[i]; if (!article) { item.style.display = 'none'; return; } item.style.display = ''; item.onclick = () => article.url && window.open(article.url, '_blank', 'noopener'); item.style.cursor = article.url ? 'pointer' : ''; const title = item.querySelector('.article-title'); const tag = item.querySelector('.article-tag'); const time = item.querySelector('.article-time'); if (title) { title.textContent = article.title || '(无标题)'; title.title = article.title || ''; } if (tag) tag.textContent = article.account || '-'; if (time) time.textContent = formatTime(article.collected_at); });
    document.querySelectorAll('.view-all-link').forEach((a, i) => { a.href = i === 0 ? 'task-center.html' : 'articles.html'; });
  }
  async function renderSettings(initial) { const data = await api('/api/settings'); const inputs = [...document.querySelectorAll('input')]; const map = ['default_number', 'remove_favorite', 'log_limit', 'refresh_interval']; inputs.forEach((input, i) => { if (!input.dataset.setting && map[i]) input.dataset.setting = map[i]; const key = input.dataset.setting; if (key && data[key] != null && (initial || document.activeElement !== input)) input.type === 'checkbox' ? input.checked = data[key] === 'true' : input.value = data[key]; }); const info = document.querySelectorAll('.info-value'); if (info[0]) info[0].textContent = location.origin; if (info[1]) info[1].textContent = location.port || '80'; if (info[2]) info[2].textContent = 'collector_webapp/collector.db'; const save = document.getElementById('saveBtn'); if (save && !save._adapterBound) { save._adapterBound = true; save.onclick = async () => { const payload = {}; document.querySelectorAll('[data-setting]').forEach(input => payload[input.dataset.setting] = input.type === 'checkbox' ? String(input.checked) : input.value); await json('PUT', '/api/settings', payload); notify('设置已保存'); }; } const reset = document.getElementById('resetBtn'); if (reset && !reset._adapterBound) { reset._adapterBound = true; reset.onclick = () => { inputs.slice(0, 4).forEach((input, i) => input.type === 'checkbox' ? input.checked = i === 1 : input.value = [10, '', 200, 5][i]); notify('已恢复默认值'); }; } const exportBtn = buttonByText('导出数据'); if (exportBtn && !exportBtn._adapterBound) { exportBtn._adapterBound = true; exportBtn.onclick = () => { location.href = '/api/articles/export'; }; } const backup = buttonByText('立即备份'); if (backup && !backup._adapterBound) { backup._adapterBound = true; backup.onclick = () => { location.href = '/api/backup'; notify('正在下载数据库备份'); }; } const clean = buttonByText('清理数据'); if (clean && !clean._adapterBound) { clean._adapterBound = true; clean.onclick = () => confirm('确定清理已结束的任务记录？') && api('/api/tasks/clear-finished', { method: 'POST' }).then(() => notify('已清理任务记录')); } }
  let refreshing = false;
  let taskCenterSignature = '';
  let taskCenterActive = false;
  let liveTaskStats = null;
  let statsGuardTimer = null;
  function taskSignature(data) {
    const tasks = (data.tasks || []).map(t => [t.id, t.status, t.collected, t.extracted, t.error, t.finished_at, t.started_at]);
    const current = data.current ? [data.current.id, data.current.phase, data.current.progress] : null;
    return JSON.stringify([data.stats, current, data.queue_halted, data.paused, tasks]);
  }
  function applyTaskStats() {
    if (!liveTaskStats) return;
    const values = [liveTaskStats.running_count || 0, liveTaskStats.pending_count || 0, liveTaskStats.today_done_count || 0, liveTaskStats.today_failed_count || 0];
    document.querySelectorAll('[data-count]').forEach((node, index) => {
      if (index >= values.length) return;
      const value = String(values[index]);
      if (node.textContent !== value) node.textContent = value;
      node.setAttribute('data-count', value);
    });
  }
  function updateTaskStats(stats) {
    liveTaskStats = stats || {};
    applyTaskStats();
    // Protect the first live render from any delayed animation in a cached
    // design template.  This is intentionally short-lived; subsequent
    // refreshes restart the guard with the latest API response.
    clearInterval(statsGuardTimer);
    let remaining = 20;
    statsGuardTimer = window.setInterval(() => {
      applyTaskStats();
      if (--remaining <= 0) {
        clearInterval(statsGuardTimer);
        statsGuardTimer = null;
      }
    }, 100);
  }
  async function refresh() { if (refreshing) return; refreshing = true; try { await serviceStatus(); if (page === 'accounts') await renderAccounts(); else if (page === 'articles') await renderArticles(); else if (page === 'task-center' || page === 'tasks') await renderTasks(); else if (page === 'overview') await renderOverview(); else if (page === 'settings') await renderSettings(!window._settingsReady); if (page === 'settings') window._settingsReady = true; } finally { refreshing = false; document.documentElement.classList.remove('adapter-loading'); } }
  clearDemoContent();
  document.head.querySelectorAll('style').forEach(style => { style.dataset.designPageStyle = page; });
  const initialRoot = document.querySelector('.content-area, .app-main, .main-content');
  if (initialRoot) initialRoot.id = 'app-view';
  document.addEventListener('DOMContentLoaded', () => {
    bindNavigation();
    refresh().then(() => {
      // Database-backed values are applied once; activity polling is handled below.
    }).catch(() => notify('页面数据加载失败'));
    if (page === 'task-center' || page === 'tasks') {
      const pollActivity = async () => {
        if (document.hidden || refreshing || !taskCenterActive) return;
        try {
          await renderTasks();
          if (!taskCenterActive && activityPollTimer) {
            clearInterval(activityPollTimer);
            activityPollTimer = null;
          }
        } catch (_) {}
      };
      activityPollTimer = window.setInterval(pollActivity, 2000);
    }
  });
})();


