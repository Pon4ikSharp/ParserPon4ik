const token = document.querySelector('meta[name="ponchik-token"]')?.content || '';

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {'X-Ponchik-Token': token, 'Content-Type': 'application/json', ...(options.headers || {})}
  });
  const data = await response.json().catch(() => ({error: 'Сервер повернув порожню відповідь'}));
  if (!response.ok) throw new Error(data.detail || data.error || 'Не вдалося виконати дію');
  return data;
}

function toast(message, error = false) {
  const node = document.createElement('div');
  node.className = `toast ${error ? 'toast-error' : ''}`;
  node.textContent = message;
  document.body.appendChild(node);
  requestAnimationFrame(() => node.classList.add('show'));
  setTimeout(() => { node.classList.remove('show'); setTimeout(() => node.remove(), 250); }, 3200);
}

async function runAction(button) {
  const confirmText = button.dataset.confirm;
  if (confirmText && !confirm(confirmText)) return;
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = 'Виконується…';
  try {
    const result = await api(button.dataset.action, {method: button.dataset.method || 'POST'});
    toast(result.message || button.dataset.success || 'Готово');
    setTimeout(() => location.reload(), 350);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = oldText;
  }
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (button) runAction(button);
  const sourceCheck = event.target.closest('[data-source-check]');
  if (sourceCheck) {
    const oldText = sourceCheck.textContent;
    sourceCheck.disabled = true; sourceCheck.textContent = 'Перевіряю…';
    api('/api/sources/check', {method: 'POST', body: JSON.stringify({endpoint: sourceCheck.dataset.sourceCheck})})
      .then(result => { toast(result.message); setTimeout(() => location.reload(), 450); })
      .catch(error => { toast(error.message, true); setTimeout(() => location.reload(), 900); })
      .finally(() => { sourceCheck.disabled = false; sourceCheck.textContent = oldText; });
  }
});

document.addEventListener('change', async event => {
  const input = event.target.closest('[data-source-toggle]');
  if (!input) return;
  input.disabled = true;
  try {
    await api(`/api/sources/${input.dataset.sourceId}`, {
      method: 'PATCH', body: JSON.stringify({[input.dataset.sourceToggle]: input.checked})
    });
    toast('Налаштування джерела збережено');
  } catch (error) {
    input.checked = !input.checked;
    toast(error.message, true);
  } finally { input.disabled = false; }
});

const analysisForm = document.querySelector('#analysis-form');
if (analysisForm) {
  const typeInput = analysisForm.querySelector('[name="job_type"]');
  const enhancedInput = analysisForm.querySelector('[data-research-only]');
  const syncEnhanced = () => {
    const allowed = typeInput?.value === 'research';
    if (enhancedInput) {
      enhancedInput.disabled = !allowed;
      if (!allowed) enhancedInput.checked = false;
    }
  };
  typeInput?.addEventListener('change', syncEnhanced);
  syncEnhanced();
  analysisForm.addEventListener('submit', async event => {
    event.preventDefault();
    const submit = analysisForm.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const raw = Object.fromEntries(new FormData(analysisForm));
      const payload = {...raw, web: Boolean(raw.web), enhanced: Boolean(raw.enhanced), priority: Number(raw.priority || 100)};
      await api('/api/analysis', {method: 'POST', body: JSON.stringify(payload)});
      toast('Завдання поставлено в чергу');
      setTimeout(() => location.reload(), 400);
    } catch (error) {
      toast(error.message, true);
      submit.disabled = false;
    }
  });
}

const sourceCreateForm = document.querySelector('#source-create-form');
if (sourceCreateForm) {
  const verifyButton = sourceCreateForm.querySelector('[data-source-verify]');
  const saveButton = sourceCreateForm.querySelector('[data-source-save]');
  const resultNode = sourceCreateForm.querySelector('[data-source-result]');
  let verifiedAddress = '';
  const payload = save => {
    const raw = Object.fromEntries(new FormData(sourceCreateForm));
    return {...raw, for_br: Boolean(raw.for_br), for_info: Boolean(raw.for_info), save};
  };
  const verify = async save => {
    const address = sourceCreateForm.elements.address.value.trim();
    if (!address) throw new Error('Вставте адресу джерела');
    const result = await api('/api/sources/verify', {method: 'POST', body: JSON.stringify(payload(save))});
    resultNode.textContent = result.message;
    resultNode.classList.add('verified');
    verifiedAddress = address;
    saveButton.disabled = false;
    return result;
  };
  sourceCreateForm.elements.address.addEventListener('input', () => {
    if (sourceCreateForm.elements.address.value.trim() !== verifiedAddress) {
      saveButton.disabled = true; resultNode.classList.remove('verified');
    }
  });
  verifyButton.addEventListener('click', async () => {
    verifyButton.disabled = true;
    try { const result = await verify(false); toast(result.message); }
    catch (error) { resultNode.textContent = error.message; resultNode.classList.remove('verified'); toast(error.message, true); }
    finally { verifyButton.disabled = false; }
  });
  sourceCreateForm.addEventListener('submit', async event => {
    event.preventDefault(); saveButton.disabled = true;
    try {
      if (sourceCreateForm.elements.address.value.trim() !== verifiedAddress) await verify(false);
      const result = await verify(true); toast(result.message); setTimeout(() => location.reload(), 500);
    } catch (error) { resultNode.textContent = error.message; resultNode.classList.remove('verified'); toast(error.message, true); saveButton.disabled = false; }
  });
}

const eventEnrichmentForm = document.querySelector('[data-event-enrichment]');
if (eventEnrichmentForm) {
  eventEnrichmentForm.querySelector('[data-check-all]')?.addEventListener('change', event => {
    eventEnrichmentForm.querySelectorAll('[name="event_id"]').forEach(input => { input.checked = event.target.checked; });
  });
  eventEnrichmentForm.addEventListener('submit', async event => {
    event.preventDefault();
    const ids = [...eventEnrichmentForm.querySelectorAll('[name="event_id"]:checked')].map(x => Number(x.value));
    if (!ids.length) { toast('Спочатку позначте хоча б одну подію', true); return; }
    const button = eventEnrichmentForm.querySelector('button[type="submit"]'); button.disabled = true;
    try {
      const result = await api('/api/calendar/enrich', {method: 'POST', body: JSON.stringify({event_ids: ids})});
      toast(result.message); setTimeout(() => location.reload(), 500);
    } catch (error) { toast(error.message, true); button.disabled = false; }
  });
}

const mobileMenuButton = document.querySelector('[data-mobile-menu]');
const mobileSidebar = document.querySelector('.sidebar');
const mobileScrim = document.querySelector('[data-mobile-scrim]');
function setMobileMenu(open) {
  if (!mobileSidebar) return;
  mobileSidebar.classList.toggle('open', open);
  document.body.classList.toggle('mobile-nav-open', open);
  mobileMenuButton?.setAttribute('aria-expanded', String(open));
}
mobileMenuButton?.addEventListener('click', () => setMobileMenu(!mobileSidebar?.classList.contains('open')));
mobileScrim?.addEventListener('click', () => setMobileMenu(false));
mobileSidebar?.querySelectorAll('nav a').forEach(link => link.addEventListener('click', () => { if (innerWidth <= 760) setMobileMenu(false); }));
document.addEventListener('keydown', event => { if (event.key === 'Escape') setMobileMenu(false); });
window.addEventListener('resize', () => { if (innerWidth > 760) setMobileMenu(false); });

const liveDashboard = document.querySelector('[data-live-dashboard]');
let liveDashboardBusy = false;

function safeText(value) {
  return document.createTextNode(value == null ? '' : String(value));
}

function replaceChildren(node, children) {
  if (node) node.replaceChildren(...children);
}

function processButtons(role, running) {
  const nodes = [];
  const button = document.createElement('button');
  button.className = `btn btn-small ${running ? 'btn-danger' : 'btn-primary'}`;
  button.dataset.action = `/api/process/${role}/${running ? 'stop' : 'start'}`;
  button.textContent = running ? 'Зупинити' : 'Запустити';
  nodes.push(button);
  if (running) {
    const restart = document.createElement('button');
    restart.className = 'btn btn-small';
    restart.dataset.action = `/api/process/${role}/restart`;
    restart.textContent = 'Перезапустити';
    nodes.push(restart);
  }
  const log = document.createElement('a');
  log.className = 'btn btn-small';
  log.href = `/logs?role=${role}`;
  log.textContent = 'Журнал';
  nodes.push(log);
  return nodes;
}

function renderLiveDashboard(data) {
  const counts = {...data.counts};
  document.querySelectorAll('[data-live-count]').forEach(node => {
    const key = node.dataset.liveCount;
    const prefix = key === 'originals_24h' ? '+' : '';
    node.textContent = `${prefix}${counts[key] ?? 0}`;
  });
  Object.entries(data.processes || {}).forEach(([role, item]) => {
    const card = document.querySelector(`[data-live-process="${role}"]`);
    if (!card) return;
    const status = card.querySelector('[data-live-process-status]');
    status.textContent = item.running ? 'працює' : 'зупинений';
    status.classList.toggle('running', Boolean(item.running));
    card.querySelector('[data-live-process-pid]').textContent = item.pid || '—';
    card.querySelector('[data-live-process-start]').textContent = item.started_at ? new Date(item.started_at).toLocaleString('uk-UA') : '—';
    const workload = data.workloads?.[role] || {};
    card.querySelector('[data-live-workload-activity]').textContent = workload.activity || '—';
    card.querySelector('[data-live-workload-backlog]').textContent = workload.backlog ?? 0;
    card.querySelector('[data-live-workload-calls]').textContent = workload.calls_24h ?? 0;
    replaceChildren(card.querySelector('[data-live-process-actions]'), processButtons(role, item.running));
  });
  const queueRows = (data.queues || []).map(row => {
    const line = document.createElement('div');
    line.className = 'process-title';
    const label = document.createElement('span'); label.textContent = `${row.label} · ${row.status}`;
    const total = document.createElement('strong'); total.textContent = row.total;
    line.append(label, total); return line;
  });
  if (!queueRows.length) {
    const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent = 'Необроблених повідомлень і подій немає'; queueRows.push(empty);
  }
  replaceChildren(document.querySelector('[data-live-queues]'), queueRows);
  const funnelRows = (data.funnel || []).map(row => {
    const item = document.createElement('div');
    const label = document.createElement('span'); label.textContent = row.stage;
    const value = document.createElement('strong'); value.textContent = row.total;
    item.append(label, value); return item;
  });
  replaceChildren(document.querySelector('[data-live-funnel]'), funnelRows);
  const maxDay = Math.max(1, ...(data.days || []).map(row => Number(row.total)));
  const bars = (data.days || []).map(row => {
    const column = document.createElement('div'); column.className = 'bar-column';
    const value = document.createElement('span'); value.className = 'bar-value'; value.textContent = row.total;
    const bar = document.createElement('div'); bar.className = 'bar'; bar.style.height = `${Math.round(row.total / maxDay * 125)}px`;
    const label = document.createElement('span'); label.className = 'bar-label'; label.textContent = row.label;
    column.append(value, bar, label); return column;
  });
  replaceChildren(document.querySelector('[data-live-chart]'), bars);
  const recentRows = (data.recent || []).map(row => {
    const tr = document.createElement('tr');
    const time = document.createElement('td'); time.className = 'nowrap'; time.textContent = row.published_display;
    const source = document.createElement('td'); source.textContent = row.source_title;
    const material = document.createElement('td');
    const link = document.createElement('a'); link.href = `/originals/${row.id}`; link.textContent = row.preview; material.append(link);
    const state = document.createElement('td'); const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = row.content_status; state.append(badge);
    tr.append(time, source, material, state); return tr;
  });
  replaceChildren(document.querySelector('[data-live-recent]'), recentRows);
  const refreshed = document.querySelector('[data-live-refreshed]');
  if (refreshed) refreshed.textContent = `Оновлено автоматично о ${data.refreshed_at}`;
}

function formatLocal(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('uk-UA');
}

function renderParserDashboard(data) {
  const parser = data.parser || {};
  const stats = data.stats || {};
  const dot = document.querySelector('[data-parser-dot]');
  if (dot) {
    dot.classList.toggle('running', Boolean(parser.healthy));
    dot.classList.toggle('warning', Boolean(parser.running && !parser.healthy));
  }
  const state = document.querySelector('[data-parser-state]');
  if (state) state.textContent = parser.healthy ? 'працює' : (parser.running ? 'процес є, очікую heartbeat' : 'зупинений');
  const pid = document.querySelector('[data-parser-pid]');
  if (pid) pid.textContent = parser.pid || '—';
  const heartbeat = document.querySelector('[data-parser-heartbeat]');
  if (heartbeat) heartbeat.textContent = parser.heartbeat_age_seconds ?? '—';
  const tg = document.querySelector('[data-parser-tg]');
  if (tg) tg.textContent = parser.telegram_saved_session ?? 0;
  const rss = document.querySelector('[data-parser-rss]');
  if (rss) rss.textContent = parser.rss_saved_session ?? 0;
  const rssCycle = document.querySelector('[data-parser-rss-cycle]');
  if (rssCycle) rssCycle.textContent = formatLocal(parser.last_rss_cycle_at);
  const tgLast = document.querySelector('[data-parser-tg-last]');
  if (tgLast) tgLast.textContent = formatLocal(parser.last_telegram_at);
  document.querySelectorAll('[data-stat]').forEach(node => {
    node.textContent = stats[node.dataset.stat] ?? 0;
  });
  const actions = document.querySelector('[data-parser-actions]');
  if (actions) {
    const button = document.createElement('button');
    button.className = 'btn ' + (parser.running ? 'btn-danger' : 'btn-primary');
    button.dataset.action = '/api/process/collector/' + (parser.running ? 'stop' : 'start');
    if (parser.running) button.dataset.confirm = 'Зупинити парсер?';
    button.textContent = parser.running ? 'Вимкнути парсер' : 'Увімкнути парсер';
    const log = document.createElement('a');
    log.className = 'btn';
    log.href = '/logs?role=collector';
    log.textContent = 'Журнал';
    actions.replaceChildren(button, log);
  }
  const refreshed = document.querySelector('[data-live-refreshed]');
  if (refreshed) refreshed.textContent = 'Оновлено о ' + (data.refreshed_at || '—') + ' · легкий режим';
}

async function refreshLiveDashboard() {
  if (!liveDashboard || liveDashboardBusy || document.hidden) return;
  liveDashboardBusy = true;
  try {
    const data = await api('/api/live/dashboard');
    renderParserDashboard(data);
  } catch (error) {
    const refreshed = document.querySelector('[data-live-refreshed]');
    if (refreshed) refreshed.textContent = 'Немає зв’язку з локальною панеллю';
  } finally {
    liveDashboardBusy = false;
  }
}

if (liveDashboard) {
  refreshLiveDashboard();
  setInterval(refreshLiveDashboard, 10000);
}

const translateButton = document.querySelector('[data-translate-document]');
const translationPanel = document.querySelector('[data-translation-panel]');
let translationTimer = null;

function renderTranslation(data) {
  if (!translationPanel) return;
  translationPanel.hidden = false;
  const status = translationPanel.querySelector('[data-translation-status]');
  const error = translationPanel.querySelector('[data-translation-error]');
  const textNode = translationPanel.querySelector('[data-translation-text]');
  status.textContent = data.status === 'completed' ? 'готово' :
    data.status === 'failed' ? 'помилка' : 'перекладається';
  status.className = `badge ${data.status === 'completed' ? 'badge-green' : data.status === 'failed' ? 'badge-red' : 'badge-amber'}`;
  error.textContent = data.error || '';
  if (data.status === 'completed') {
    textNode.textContent = data.translated_text || '';
    textNode.hidden = false;
    translateButton.disabled = false;
    translateButton.textContent = 'Перекласти повторно';
    if (translationTimer) clearInterval(translationTimer);
  } else if (data.status === 'failed') {
    textNode.hidden = true;
    translateButton.disabled = false;
    translateButton.textContent = 'Повторити переклад';
    if (translationTimer) clearInterval(translationTimer);
  } else {
    textNode.hidden = true;
    translateButton.disabled = true;
    translateButton.textContent = 'Перекладається…';
  }
}

async function pollTranslation() {
  if (!translationPanel || document.hidden) return;
  try {
    const data = await api(`/api/originals/${translationPanel.dataset.documentId}/translation`);
    renderTranslation(data);
  } catch (_) { /* The next polling cycle will retry. */ }
}

if (translateButton && translationPanel) {
  translateButton.addEventListener('click', async () => {
    translateButton.disabled = true;
    try {
      const result = await api(`/api/originals/${translateButton.dataset.translateDocument}/translate`, {method: 'POST'});
      toast(result.message); renderTranslation({status: 'queued', error: '', translated_text: ''});
      if (translationTimer) clearInterval(translationTimer);
      translationTimer = setInterval(pollTranslation, 3000);
    } catch (error) {
      translateButton.disabled = false; toast(error.message, true);
    }
  });
  const initialStatus = translationPanel.querySelector('[data-translation-status]')?.textContent.trim();
  if (['queued', 'processing'].includes(initialStatus)) translationTimer = setInterval(pollTranslation, 3000);
}


/* Originals v3: persistent selection, presets and exports */
const ORIGINAL_SELECTION_KEY = 'ponchik_original_selection_v1';

function loadOriginalSelection() {
  try {
    const raw = JSON.parse(localStorage.getItem(ORIGINAL_SELECTION_KEY) || '[]');
    return new Set((Array.isArray(raw) ? raw : []).map(Number).filter(Number.isFinite));
  } catch (_) {
    return new Set();
  }
}

function saveOriginalSelection(selection) {
  localStorage.setItem(ORIGINAL_SELECTION_KEY, JSON.stringify([...selection]));
}

function formatLocalDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function originalsExportPayload(mode) {
  const form = document.querySelector('#originals-filter-form');
  const data = new FormData(form);
  const selection = loadOriginalSelection();
  if (mode === 'weekly') {
    const today = new Date();
    const from = new Date(today);
    const daysSinceMonday = (today.getDay() + 6) % 7;
    from.setDate(today.getDate() - daysSinceMonday);
    return {
      mode: 'filtered',
      format: 'markdown',
      ids: [],
      q: '',
      date_from: formatLocalDate(from),
      date_to: formatLocalDate(today),
      source: '',
      processing_status: 'grok_processed',
      routes: ['MUST_ANALYZE', 'ANALYZE'],
      audiences: [],
      canonical_only: false,
    };
  }
  return {
    mode,
    format: document.querySelector('[data-original-export-format]')?.value || 'markdown',
    ids: mode === 'selected' ? [...selection] : [],
    q: data.get('q') || '',
    date_from: data.get('date_from') || '',
    date_to: data.get('date_to') || '',
    source: data.get('source') || '',
    processing_status: data.get('processing_status') || 'all',
    routes: data.getAll('routes'),
    audiences: data.getAll('audiences'),
    canonical_only: data.get('canonical_only') === 'true',
  };
}

function updateOriginalSelectionUI() {
  const workspace = document.querySelector('.originals-v3');
  if (!workspace) return;
  const selection = loadOriginalSelection();
  document.querySelectorAll('[data-original-selected-count]').forEach(node => {
    node.textContent = selection.size.toLocaleString('uk-UA');
  });

  const visible = [...document.querySelectorAll('[data-original-select]')];
  let selectedVisible = 0;
  visible.forEach(input => {
    const id = Number(input.value);
    const checked = selection.has(id);
    input.checked = checked;
    if (checked) selectedVisible += 1;
    input.closest('tr')?.classList.toggle('is-selected', checked);
  });

  const selectPage = document.querySelector('[data-original-select-page]');
  if (selectPage) {
    selectPage.checked = visible.length > 0 && selectedVisible === visible.length;
    selectPage.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
  }

  document.querySelectorAll('[data-original-export="selected"]').forEach(button => {
    button.disabled = selection.size === 0;
  });
}

async function downloadOriginals(mode, button) {
  const payload = originalsExportPayload(mode);
  if (mode === 'selected' && payload.ids.length === 0) {
    toast('Спочатку виберіть хоча б один матеріал', true);
    return;
  }
  const originalText = button.textContent;
  const buttons = [...document.querySelectorAll('[data-original-export]')];
  buttons.forEach(node => node.disabled = true);
  button.textContent = 'Формую файл…';
  try {
    const response = await fetch('/api/originals/export', {
      method: 'POST',
      headers: {
        'X-Ponchik-Token': token,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({detail: 'Не вдалося сформувати файл'}));
      throw new Error(error.detail || error.error || 'Не вдалося сформувати файл');
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/i);
    const filename = match?.[1] || 'originals-export';
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    const count = response.headers.get('X-Export-Count');
    toast(`Файл сформовано${count ? `: ${count} матеріалів` : ''}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.textContent = originalText;
    updateOriginalSelectionUI();
  }
}

function initOriginalsWorkspace() {
  const workspace = document.querySelector('.originals-v3');
  if (!workspace) return;

  updateOriginalSelectionUI();

  document.querySelectorAll('[data-original-select]').forEach(input => {
    input.addEventListener('change', () => {
      const selection = loadOriginalSelection();
      const id = Number(input.value);
      if (input.checked) selection.add(id);
      else selection.delete(id);
      saveOriginalSelection(selection);
      updateOriginalSelectionUI();
    });
  });

  const selectPage = document.querySelector('[data-original-select-page]');
  if (selectPage) {
    selectPage.addEventListener('change', () => {
      const selection = loadOriginalSelection();
      document.querySelectorAll('[data-original-select]').forEach(input => {
        const id = Number(input.value);
        if (selectPage.checked) selection.add(id);
        else selection.delete(id);
      });
      saveOriginalSelection(selection);
      updateOriginalSelectionUI();
    });
  }

  document.querySelector('[data-original-clear-selection]')?.addEventListener('click', () => {
    localStorage.removeItem(ORIGINAL_SELECTION_KEY);
    updateOriginalSelectionUI();
    toast('Вибір очищено');
  });

  document.querySelectorAll('[data-original-export]').forEach(button => {
    button.addEventListener('click', () => downloadOriginals(button.dataset.originalExport, button));
  });

  document.querySelectorAll('[data-original-preset]').forEach(button => {
    button.addEventListener('click', () => {
      const form = document.querySelector('#originals-filter-form');
      if (!form) return;
      form.querySelectorAll('input[name="routes"]').forEach(input => { input.checked = false; });
      const today = new Date();
      const from = new Date(today);
      const processing = form.querySelector('select[name="processing_status"]');
      if (button.dataset.originalPreset === 'weekly-grok') {
        const daysSinceMonday = (today.getDay() + 6) % 7;
        from.setDate(today.getDate() - daysSinceMonday);
        ['MUST_ANALYZE', 'ANALYZE'].forEach(route => {
          const input = form.querySelector(`input[name="routes"][value="${route}"]`);
          if (input) input.checked = true;
        });
        if (processing) processing.value = 'grok_processed';
        form.querySelectorAll('input[name="audiences"]').forEach(input => { input.checked = false; });
        const canonical = form.querySelector('input[name="canonical_only"]');
        if (canonical) canonical.checked = false;
        const query = form.querySelector('input[name="q"]');
        const source = form.querySelector('input[name="source"]');
        if (query) query.value = '';
        if (source) source.value = '';
      } else {
        from.setDate(today.getDate() - 1);
        ['MUST_ANALYZE', 'ANALYZE'].forEach(route => {
          const input = form.querySelector(`input[name="routes"][value="${route}"]`);
          if (input) input.checked = true;
        });
        if (processing) processing.value = 'prefilter_processed';
      }
      const fromInput = form.querySelector('input[name="date_from"]');
      const toInput = form.querySelector('input[name="date_to"]');
      if (fromInput) fromInput.value = formatLocalDate(from);
      if (toInput) toInput.value = formatLocalDate(today);
      form.submit();
    });
  });
}

document.addEventListener('DOMContentLoaded', initOriginalsWorkspace);

/* Sources v2: background health check */
let sourceHealthTimer = null;
let sourceHealthWasRunning = false;

function renderSourceHealthState(state) {
  const button = document.querySelector('[data-source-check-all]');
  const progress = document.querySelector('[data-source-check-all-progress]');
  if (!button || !progress) return;
  const status = state.status || 'idle';
  const total = Number(state.total || 0);
  const done = Number(state.done || 0);
  const ok = Number(state.ok || 0);
  const failed = Number(state.failed || 0);
  const missing = Number(state.missing_total || 0);
  if (status === 'running') {
    sourceHealthWasRunning = true;
    button.disabled = true;
    button.textContent = total ? `Перевірка ${done}/${total}` : 'Запускаю перевірку…';
    progress.textContent = `Працює: ${ok} успішно · ${failed} помилок · можливих пропусків ${missing}${state.current ? ` · зараз: ${state.current}` : ''}`;
  } else {
    button.disabled = false;
    button.textContent = 'Перевірити всі';
    if (status === 'completed') progress.textContent = `Остання перевірка: ${ok} працюють · ${failed} з помилками · можливих пропусків ${missing}`;
    else if (status === 'error') progress.textContent = `Помилка перевірки: ${state.last_error || 'невідома'}`;
  }
}

async function pollSourceHealth() {
  try {
    const state = await api('/api/sources/check-all/status');
    const shouldReload = sourceHealthWasRunning && state.status === 'completed';
    renderSourceHealthState(state);
    if (state.status !== 'running' && sourceHealthTimer) {
      clearInterval(sourceHealthTimer);
      sourceHealthTimer = null;
    }
    if (shouldReload) {
      sourceHealthWasRunning = false;
      setTimeout(() => location.reload(), 700);
    }
  } catch (_) { /* next poll retries */ }
}

document.querySelector('[data-source-check-all]')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  if (!confirm('Перевірити всі активні джерела? Перевірка піде окремим процесом і може тривати кілька хвилин.')) return;
  button.disabled = true;
  try {
    const result = await api('/api/sources/check-all', {method: 'POST'});
    toast(result.message || 'Перевірку запущено');
    await pollSourceHealth();
    if (!sourceHealthTimer) sourceHealthTimer = setInterval(pollSourceHealth, 2000);
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
});

if (document.querySelector('[data-source-check-all]')) {
  pollSourceHealth();
  if (!sourceHealthTimer) sourceHealthTimer = setInterval(pollSourceHealth, 3000);
}

document.querySelectorAll('[data-source-endpoint-form]').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const oldEndpoint = form.elements.old_endpoint.value;
    const address = form.elements.address.value.trim();
    if (!address) { toast('Вкажіть нову адресу', true); return; }
    if (!confirm('Перевірити нову адресу і замінити поточну точку збору?')) return;
    button.disabled = true;
    button.textContent = 'Перевіряю…';
    try {
      const result = await api(`/api/sources/${form.dataset.sourceId}/endpoint`, {
        method: 'POST',
        body: JSON.stringify({old_endpoint: oldEndpoint, address})
      });
      toast(result.message || 'Точку джерела оновлено');
      setTimeout(() => location.reload(), 500);
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Перевірити й замінити';
      toast(error.message, true);
    }
  });
});
