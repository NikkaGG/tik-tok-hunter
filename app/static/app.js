// TikTok Lead Hunter: фронтенд на Vanilla JS без сборки.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtDate = (s) => (s ? new Date(s).toLocaleString('ru-RU') : '—');
const fmtNum = (n) => (n == null ? '—' : Number(n).toLocaleString('ru-RU'));

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === 'string' ? j.detail : (j.detail || []).map((d) => d.msg).join('; ');
    } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $('#toast');
  if (!el) return;
  el.textContent = msg;
  el.className = `fixed bottom-4 right-4 px-4 py-2.5 rounded-xl shadow-2xl text-sm font-medium text-white z-50 transition-all ${isError ? 'bg-rose-600' : 'bg-slate-900'}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 4000);
}

// Обёртка для обработчиков: ошибки показываем в тосте.
const safe = (fn) => async (...args) => {
  try { await fn(...args); } catch (e) { toast(e.message, true); }
};

const BADGE = {
  active: 'bg-emerald-100 text-emerald-700', working: 'bg-sky-100 text-sky-700', paused: 'bg-slate-200 text-slate-600',
  warning: 'bg-amber-100 text-amber-700', banned: 'bg-rose-100 text-rose-700',
  new: 'bg-sky-100 text-sky-700', relevant: 'bg-emerald-100 text-emerald-700',
  irrelevant: 'bg-slate-200 text-slate-500', replied: 'bg-violet-100 text-violet-700',
  queued: 'bg-slate-200 text-slate-600', running: 'bg-sky-100 text-sky-700',
  done: 'bg-emerald-100 text-emerald-700', partial: 'bg-amber-100 text-amber-800', failed: 'bg-rose-100 text-rose-700',
  cancelled: 'bg-slate-200 text-slate-600',
  draft: 'bg-sky-100 text-sky-700', sent: 'bg-emerald-100 text-emerald-700', skipped: 'bg-slate-200 text-slate-500',
  INFO: 'text-slate-500', WARNING: 'text-amber-600', ERROR: 'text-rose-600',
};
const LABEL = {
  active: 'активен', working: 'в работе', paused: 'пауза', warning: 'предупреждение', banned: 'заблокирован',
  new: 'новый', relevant: 'подходит', irrelevant: 'не подходит', replied: 'отвечено',
  queued: 'в очереди', running: 'выполняется', done: 'готово', partial: 'с ошибками', failed: 'ошибка', cancelled: 'отменено',
  draft: 'черновик', sent: 'отправлен', skipped: 'пропущен',
  search: 'поиск', login: 'вход', check_session: 'проверка сессии', test_proxy: 'проверка прокси',
  auto_comment: 'автокомментарии',
};
const badge = (v) => `<span class="text-xs px-2 py-0.5 rounded-full ${BADGE[v] || 'bg-slate-100'}">${esc(LABEL[v] || v)}</span>`;

// Итог проверки сессии: задача может завершиться успешно, а сессия при этом оказаться недействительной.
function checkLine(lc) {
  if (!lc) return '—';
  const failed = lc.status === 'failed' || (lc.kind === 'check_session' && lc.status === 'done' && !lc.found);
  const mark = lc.status !== 'done' && lc.status !== 'failed' ? badge(lc.status)
    : failed ? '<span class="text-xs px-2 py-0.5 rounded-full bg-rose-100 text-rose-700">не прошла</span>'
    : '<span class="text-xs px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">ок</span>';
  return `${mark} <span class="${failed ? 'text-rose-700' : ''}">${esc(lc.result || lc.error || '')}</span>`;
}

// ---------- Вкладки ----------
let currentTab = 'dashboard';
const loaders = {};

// [заголовок, подзаголовок, шаг в работе с лидами, следующая вкладка]
const PAGES = {
  dashboard: ['Главная', 'Что происходит сейчас и с чего начать'],
  search: ['Поиск роликов', 'Введите хэштеги или ключевые слова: найденные ролики попадут в «Отбор роликов»', 1, 'videos'],
  videos: ['Отбор роликов', 'Отметьте подходящие ролики ✓ и создайте для них черновики ответов по шаблону', 2, 'drafts'],
  drafts: ['Отправка ответов', 'Черновики ждут публикации: вручную через «Конвейер» или автоматически ботом', 3],
  settings: ['Настройки', 'Общие настройки поиска, браузеров и прокси'],
  accounts: ['Аккаунты TikTok', 'Проверяйте сессии и управляйте аккаунтами в одном месте'],
  templates: ['Наборы текстов', 'Тематические варианты с общей историей использования для всех аккаунтов'],
  logs: ['Журнал событий', 'Все действия и ошибки — пригодится, если что-то пошло не так'],
};

function showTab(tab) {
  if (!loaders[tab]) tab = 'dashboard';
  currentTab = tab;
  $$('[data-panel]').forEach((p) => p.classList.toggle('hidden', p.dataset.panel !== tab));
  $$('.tab-btn').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  const [title, sub, step, next] = PAGES[tab];
  $('#page-title').textContent = title;
  $('#page-sub').textContent = sub;
  $('#page-step').classList.add('hidden');
  $$('.workflow-path [data-goto]').forEach(b => {
    if (b.dataset.goto === tab) b.setAttribute('aria-current', 'step');
    else b.removeAttribute('aria-current');
  });
  const nextBtn = $('#page-next');
  nextBtn.classList.toggle('hidden', !next);
  if (next) {
    nextBtn.dataset.goto = next;
    nextBtn.textContent = `Дальше: ${PAGES[next][0]} →`;
  }
  const accHdr = $('#acc-header-actions');
  accHdr.classList.toggle('hidden', tab !== 'accounts');
  accHdr.classList.toggle('flex', tab === 'accounts');
  try { localStorage.setItem('ttlh-tab', tab); } catch {}
  safe(loaders[tab])();
}
$$('.tab-btn').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));

// Любая кнопка с data-goto переключает вкладку.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-goto]');
  if (btn) showTab(btn.dataset.goto);
});

// Автообновление вкладок, где состояние меняется в фоне.
setInterval(() => {
  if (document.hidden) return;
  if (currentTab === 'dashboard') return safe(loaders.dashboard)();
  if (['search', 'logs', 'settings'].includes(currentTab)) safe(loaders[currentTab])();
  if (currentTab === 'drafts' && !cv.on) safe(() => loaders.drafts({ silent: true }))();
  safe(refreshGlobal)();
}, 5000);

// ---------- Статус сессии ----------
// active | missing | expired | checking | unknown
function sessionState(session) {
  const lc = session.last_check;
  if (lc && ['queued', 'running'].includes(lc.status)) return 'checking';
  if (!session.state_saved) return 'missing';
  if (session.session_status === 'expired') return 'expired';
  if (session.session_status === 'error') return 'error';
  if (session.session_status === 'active' && !['queued', 'running'].includes(lc?.status)) return 'active';
  if (!lc || lc.status !== 'done') return 'unknown';
  if (lc.kind === 'check_session') return lc.found ? 'active' : 'expired';
  return 'active'; // успешный вход
}

const SESSION_UI = {
  active:   { label: 'Сессия активна',  card: 'border-emerald-400 bg-emerald-50', pill: 'bg-emerald-600 text-white', dot: 'bg-emerald-200 animate-pulse' },
  missing:  { label: 'Нет авторизации', card: 'border-rose-400 bg-rose-50',       pill: 'bg-rose-600 text-white',    dot: 'bg-rose-200' },
  expired:  { label: 'Сессия истекла',  card: 'border-rose-400 bg-rose-50',       pill: 'bg-rose-600 text-white',    dot: 'bg-rose-200' },
  checking: { label: 'Проверяется…',    card: 'border-sky-300 bg-sky-50',         pill: 'bg-sky-600 text-white',     dot: 'bg-sky-200 animate-pulse' },
  error:    { label: 'Ошибка проверки', card: 'border-amber-300 bg-amber-50', pill: 'bg-amber-500 text-white', dot: 'bg-amber-100' },
  unknown:  { label: 'Не проверена',    card: 'border-amber-300 bg-amber-50',     pill: 'bg-amber-500 text-white',   dot: 'bg-amber-100' },
};
const sessionPill = (st, extra = '') => {
  const ui = SESSION_UI[st];
  return `<span class="inline-flex items-center gap-2 text-xs font-medium px-3 py-1 rounded-full ${ui.pill} ${extra}">
    <span class="w-2 h-2 rounded-full ${ui.dot}"></span>${ui.label}</span>`;
};

// Данные для всех вкладок: счётчик очереди и статус сессии в шапке.
async function refreshGlobal() {
  const [s, session, monitor] = await Promise.all([api('/api/stats'), api('/api/session'), api('/api/account-monitor')]);
  const badgeEl = $('#badge-drafts');
  badgeEl.textContent = s.drafts_pending;
  badgeEl.classList.toggle('hidden', !s.drafts_pending);
  const newVideos = s.videos_by_status.new || 0;
  $('#badge-videos').textContent = newVideos;
  $('#badge-videos').classList.toggle('hidden', !newVideos);
  const searching = s.last_task?.kind === 'search' && ['queued', 'running'].includes(s.last_task.status);
  $('#badge-search').classList.toggle('hidden', !searching);

  const st = sessionState(session);
  if (typeof renderAccountMonitor === 'function') renderAccountMonitor(monitor);
  return { s, session, st };
}
const updateDraftBadge = async () => (await refreshGlobal()).s;

// ---------- Дашборд ----------
const ACC_DOT = { active: 'bg-emerald-500', working: 'bg-sky-500', paused: 'bg-slate-400', warning: 'bg-amber-500', banned: 'bg-rose-500' };
const ACC_PILL = {
  active: 'bg-emerald-100 text-emerald-800 ring-emerald-300', working: 'bg-sky-100 text-sky-800 ring-sky-300',
  paused: 'bg-slate-100 text-slate-600 ring-slate-300',
  warning: 'bg-amber-100 text-amber-800 ring-amber-300', banned: 'bg-rose-100 text-rose-800 ring-rose-300',
};

loaders.dashboard = async () => {
  const [{ s, session, st }, accounts, logs, templates, tasksList] = await Promise.all([
    refreshGlobal(), api('/api/accounts'), api('/api/logs?limit=12'), api('/api/text-sets'), api('/api/tasks?limit=20'),
  ]);

  renderOnboarding({ s, session, st, templates });
  fillQuickTemplates([]);
  renderQuickStatus(tasksList.find((t) => t.kind === 'search'), s.drafts_pending);
  renderSessionCard(session, st, s);
  renderDashAccounts(accounts);
  $('#dash-logs').innerHTML = logs.map(logLine).join('') || '<p class="text-slate-400">Пусто</p>';

  // Карточки кликабельны: ведут в раздел, где с этими цифрами работают.
  const cards = [
    ['Роликов найдено', s.videos_total, 'videos'], ['Ждут отбора', s.videos_by_status.new || 0, 'videos'],
    ['Подходят', s.videos_by_status.relevant || 0, 'videos'], ['Черновиков в очереди', s.drafts_pending, 'drafts'],
    ['Ответов сегодня', s.replies_sent_today, 'drafts'], ['Ответов всего', s.replies_sent_total, 'drafts'],
  ];
  $('#stat-cards').innerHTML = cards.map(([k, v, tab]) => `
    <button data-goto="${tab}" class="text-left bg-white rounded-2xl border border-slate-200/80 shadow-sm p-4 hover:border-indigo-300 hover:shadow transition">
      <div class="text-xs text-slate-500">${k}</div><div class="text-2xl font-bold mt-1">${fmtNum(v)}</div>
    </button>`).join('');
};

// ---------- Быстрый старт ----------
const ONB_KEY = 'ttlh-onboarding-hidden';
const onbHidden = () => { try { return localStorage.getItem(ONB_KEY) === '1'; } catch { return false; } };
function setOnbHidden(v) {
  try { v ? localStorage.setItem(ONB_KEY, '1') : localStorage.removeItem(ONB_KEY); } catch {}
  $('#onboarding').classList.toggle('hidden', v);
  $('#onb-show').classList.toggle('hidden', !v);
}
$('#onb-hide').addEventListener('click', () => setOnbHidden(true));
$('#onb-show').addEventListener('click', () => setOnbHidden(false));

function renderOnboarding({ s, session, st, templates }) {
  setOnbHidden(onbHidden());
  const steps = [
    {
      done: st === 'active',
      title: 'Подключите TikTok',
      text: st === 'checking' ? 'Идёт проверка или вход, подождите…'
        : st === 'expired' ? 'Сессия истекла. Переподключите аккаунт в аккаунт-менеджере.'
        : 'Добавьте аккаунт через вход в TikTok или импорт готовой сессии.',
      action: st === 'active' ? null : { label: 'Добавить аккаунт', goto: 'accounts', primary: true },
    },
    {
      done: s.accounts_total > 0,
      title: 'Добавьте аккаунты',
      text: `Ваши TikTok-аккаунты и дневной лимит ответов для каждого. Прокси: ${session.proxy_configured
        ? '<b class="text-emerald-700">включён</b>' : 'не задан'} (по желанию, в разделе «Настройки»).`,
      action: { label: s.accounts_total ? `Аккаунтов: ${s.accounts_total}` : 'Добавить аккаунт', goto: 'accounts' },
    },
    {
      done: templates.length > 0,
      title: 'Создайте набор текстов',
      text: 'Добавьте варианты по тематике и назначьте набор аккаунтам. История помогает реже повторять тексты.',
      action: { label: templates.length ? `Наборов: ${templates.length}` : 'Создать набор', goto: 'templates' },
    },
    {
      done: s.replies_sent_total > 0,
      title: 'Запустите поиск и обработайте очередь',
      text: !s.videos_total ? 'Введите хэштег в «Быстрый поиск» ниже, затем отберите подходящие ролики и отправьте ответы.'
        : `Найдено роликов: ${fmtNum(s.videos_total)}, в очереди: ${s.drafts_pending}, отправлено: ${s.replies_sent_total}.`,
      action: s.drafts_pending ? { label: `Открыть очередь (${s.drafts_pending})`, goto: 'drafts', primary: true }
        : s.videos_total ? { label: 'Отобрать ролики', goto: 'videos' }
        : { label: 'К быстрому поиску', act: 'focus-search' },
    },
  ];
  const doneCount = steps.filter((x) => x.done).length;
  const current = steps.findIndex((x) => !x.done);

  $('#onb-progress').textContent = `${doneCount} из ${steps.length}`;
  $('#onb-bar').style.width = `${(doneCount / steps.length) * 100}%`;
  $('#onb-subtitle').textContent = doneCount === steps.length
    ? 'Всё настроено 🎉 Гид можно скрыть, он всегда доступен по ссылке.'
    : 'Четыре шага до первого обработанного лида';

  $('#onb-steps').innerHTML = steps.map((x, i) => {
    const isCurrent = i === current;
    const box = x.done ? 'border-emerald-200 bg-emerald-50'
      : isCurrent ? 'border-slate-900 ring-2 ring-slate-900/10 bg-white' : 'border-slate-200 bg-slate-50';
    const num = x.done ? '<span class="w-7 h-7 rounded-full bg-emerald-500 text-white flex items-center justify-center text-sm">✓</span>'
      : `<span class="w-7 h-7 rounded-full ${isCurrent ? 'bg-slate-900 text-white' : 'bg-slate-200 text-slate-500'} flex items-center justify-center text-sm font-semibold">${i + 1}</span>`;
    const a = x.action;
    const btnCls = a && a.primary && !x.done ? 'bg-slate-900 text-white hover:bg-slate-700'
      : 'border border-slate-300 bg-white hover:bg-slate-100';
    return `
      <li class="rounded-xl border p-4 flex flex-col gap-3 ${box}">
        <div class="flex items-center gap-3">${num}<h3 class="font-medium leading-tight">${x.title}</h3></div>
        <p class="text-sm text-slate-600 flex-1">${x.text}</p>
        ${a ? `<button class="self-start px-3 py-1.5 rounded-lg text-sm ${btnCls}"
          ${a.goto ? `data-goto="${a.goto}"` : `data-onb-act="${a.act}"`}>${a.label}</button>` : ''}
      </li>`;
  }).join('');
}

$('#onb-steps').addEventListener('click', safe(async (e) => {
  const act = e.target.closest('[data-onb-act]')?.dataset.onbAct;
  if (act === 'login') await startLogin();
  if (act === 'focus-search') {
    const input = $('#quick-search [name=q]');
    input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    input.focus();
  }
}));

// ---------- Карточки дашборда ----------
function renderSessionCard(session, st, s) {
  $('#session-card').className = `rounded-xl border-2 p-4 transition-colors ${SESSION_UI[st].card}`;
  $('#session-pill').innerHTML = sessionPill(st);
  const lc = session.last_check;
  const hint = {
    missing: '<div class="text-rose-700 font-medium">Без подключения TikTok почти всегда показывает капчу. Нажмите «Подключить TikTok».</div>',
    expired: '<div class="text-rose-700 font-medium">Сессия больше не действует. Подключитесь заново.</div>',
    unknown: '<div class="text-amber-700">Сессия сохранена, но не проверена. Нажмите «Проверить».</div>',
  }[st] || '';
  $('#session-info').innerHTML = `
    ${hint}
    <div>Файл сессии: ${session.state_saved ? `сохранён ${fmtDate(session.saved_at)}` : 'нет'}</div>
    <div>Прокси парсера: ${session.proxy_configured ? 'задан' : 'не используется'}</div>
    <div>Последняя проверка: ${checkLine(lc)}</div>
    ${s.last_task ? `<div class="text-slate-500">Последняя задача: #${s.last_task.id} ${esc(LABEL[s.last_task.kind] || s.last_task.kind)} ${badge(s.last_task.status)}</div>` : ''}`;
  $('#btn-login').textContent = st === 'active' ? 'Переподключить' : 'Подключить TikTok';
  $('#btn-login').className = `px-3 py-1.5 rounded-lg text-sm text-white ${['missing', 'expired'].includes(st) ? 'bg-rose-600 hover:bg-rose-700' : 'bg-slate-900 hover:bg-slate-700'}`;
}

function renderDashAccounts(accounts) {
  const counts = accounts.reduce((m, a) => ({ ...m, [a.status]: (m[a.status] || 0) + 1 }), {});
  $('#dash-acc-summary').innerHTML = ['active', 'working', 'warning', 'banned', 'paused']
    .filter((k) => counts[k])
    .map((k) => `<span class="inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full ring-1 ${ACC_PILL[k]}">
      <span class="w-2 h-2 rounded-full ${ACC_DOT[k]}"></span>${LABEL[k]}: ${counts[k]}</span>`).join('');

  $('#dash-accounts').innerHTML = accounts.length ? accounts.map((a) => {
    const pct = a.daily_reply_limit ? Math.min(100, (a.replies_today / a.daily_reply_limit) * 100) : 100;
    const full = a.daily_reply_limit > 0 && a.replies_today >= a.daily_reply_limit;
    return `
      <div class="flex items-center gap-3 py-2 border-b border-slate-100 last:border-0">
        <span class="w-2.5 h-2.5 rounded-full shrink-0 ${ACC_DOT[a.status]}" title="${LABEL[a.status]}"></span>
        <span class="flex-1 min-w-0 truncate">@${esc(a.username)}</span>
        <div class="hidden sm:block w-24 h-1.5 bg-slate-100 rounded-full overflow-hidden" title="Ответов сегодня">
          <div class="h-full ${full ? 'bg-amber-500' : 'bg-emerald-500'}" style="width:${pct}%"></div>
        </div>
        <span class="w-14 text-right tabular-nums ${full ? 'text-amber-600 font-medium' : 'text-slate-500'}">${a.replies_today}/${a.daily_reply_limit}</span>
        <span class="hidden sm:inline">${badge(a.status)}</span>
      </div>`;
  }).join('') : `<div class="text-center py-4">
      <p class="text-slate-400 mb-2">Аккаунтов пока нет</p>
      <button data-goto="accounts" class="px-3 py-1.5 rounded-lg bg-slate-900 text-white text-sm">Добавить аккаунт</button>
    </div>`;
}

// ---------- Быстрый поиск ----------
const QS_TPL_KEY = 'ttlh-quick-template';

function fillQuickTemplates(templates) {
  const sel = $('#quick-search').elements.template;
  let saved = sel.value;
  if (!sel.dataset.loaded) {
    try { saved = localStorage.getItem(QS_TPL_KEY) || ''; } catch {}
    sel.dataset.loaded = '1';
  }
  sel.innerHTML = '<option value="">Только найти</option>' +
    templates.map((t) => `<option value="${t.id}">+ черновики: ${esc(t.name)}</option>`).join('');
  sel.value = templates.some((t) => String(t.id) === saved) ? saved : '';
  updateQuickButton();
}

function updateQuickButton() {
  $('#qs-btn').textContent = $('#quick-search').elements.template.value ? 'Найти и подготовить' : 'Найти';
}

$('#quick-search').elements.template.addEventListener('change', (e) => {
  try { localStorage.setItem(QS_TPL_KEY, e.target.value); } catch {}
  updateQuickButton();
});

function renderQuickStatus(t, draftsPending = 0) {
  if (!t) {
    $('#quick-status').innerHTML = '<p class="text-slate-400">Поиск ещё не запускался.</p>';
    return;
  }
  const busy = ['queued', 'running'].includes(t.status);
  const tone = t.status === 'failed' ? 'border-rose-200 bg-rose-50' : busy ? 'border-sky-200 bg-sky-50' : 'border-emerald-200 bg-emerald-50';
  $('#quick-status').innerHTML = `
    <div class="rounded-lg border px-3 py-2 ${tone}">
      <div class="flex flex-wrap items-center gap-2">
        ${busy ? '<span class="w-3 h-3 rounded-full border-2 border-sky-500 border-t-transparent animate-spin"></span>' : ''}
        <span class="font-medium">Задача #${t.id}</span> ${badge(t.status)}
        <span class="text-slate-500 truncate">${esc(t.params.queries.join(', '))}</span>
      </div>
      <div class="mt-1 ${t.error ? 'text-rose-700' : 'text-slate-700'}">
        ${esc(t.error || t.result || (busy ? `Ищем… новых роликов: ${t.found}` : ''))}
        ${t.status === 'done' && t.found && !t.params.template_id ? '<button data-goto="videos" class="ml-2 text-indigo-600 hover:underline">Отобрать ролики →</button>' : ''}
      </div>
      ${t.status === 'done' && t.params.template_id && draftsPending ? `
        <button data-qs="conveyor" class="mt-2 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-medium">▶ Открыть конвейер (${draftsPending})</button>` : ''}
      <div>
      </div>
    </div>`;
}

$('#quick-search').addEventListener('submit', safe(async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const queries = f.q.value.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
  if (!queries.length) return;
  await api('/api/tasks/search', {
    method: 'POST',
    body: {
      queries,
      limit_per_query: Number(f.limit.value),
      visible: f.visible.checked,
      template_id: Number(f.template.value) || null,
    },
  });
  f.q.value = '';
  toast(`Поиск запущен: ${queries.join(', ')}`);
  await loaders.dashboard();
}));

$('#quick-status').addEventListener('click', safe(async (e) => {
  if (!e.target.closest('[data-qs="conveyor"]')) return;
  showTab('drafts');
  await startConveyor();
}));

async function startLogin() {
  openConnectModal();
}

const logLine = (l) => `<div class="${BADGE[l.level] || ''}"><span class="text-slate-400">${fmtDate(l.ts)}</span> [${esc(l.source)}] ${esc(l.message)}</div>`;

$('#btn-login').addEventListener('click', () => openConnectModal());
$('#btn-check').addEventListener('click', safe(async () => {
  await api('/api/tasks/check-session', { method: 'POST' });
  toast('Проверка запущена');
  await loaders.dashboard();
}));

// ---------- Аккаунт-менеджер ----------
let accountsCache = [];
let accStateFilter = 'all';
let accSearchQuery = '';
let accGroupFilter = '';
let accProxyFilter = '';
let accLimitFilter = '';
let accOnlySession = false;
let accSessionFilter = '';
let accountSetsCache = [];
const accountCheckRequests = new Set();
let accPage = 1;
let accPageSize = 8;
let accSelectedIds = new Set();
let accMenuAccountId = null;

const ACC_STATE_TABS = [
  ['all', 'Все'],
  ['ready', 'Готовы'],
  ['working', 'В работе'],
  ['resting', 'Отдыхают'],
  ['warning', 'Требуют внимания'],
  ['banned', 'Заблокированы'],
];

const ACC_STATE_PILL = {
  ready:   { label: 'Готов',            cls: 'bg-indigo-50 text-indigo-700',   dot: 'bg-indigo-500',   bar: 'bg-indigo-500' },
  working: { label: 'В работе',         cls: 'bg-find-50 text-find-700',   dot: 'bg-find-500',   bar: 'bg-find-500' },
  resting: { label: 'Отдыхает',         cls: 'bg-slate-100 text-slate-600', dot: 'bg-slate-500', bar: 'bg-indigo-500' },
  warning: { label: 'Требует внимания', cls: 'bg-queue-50 text-queue-700', dot: 'bg-queue-500',  bar: 'bg-rose-500' },
  banned:  { label: 'Заблокирован',     cls: 'bg-rose-50 text-rose-700',   dot: 'bg-rose-500',   bar: 'bg-slate-300' },
};

const ACC_AVATAR_THEMES = [
  { match: /дом|уют|интерьер/i, bg: 'from-amber-100 to-orange-100 text-amber-800', icon: '🏠' },
  { match: /техник|ноут|комп|it/i, bg: 'from-sky-100 to-blue-100 text-blue-800', icon: '💻' },
  { match: /косметик|бьюти|beauty/i, bg: 'from-pink-100 to-rose-100 text-rose-800', icon: '💄' },
  { match: /дет|игруш|kids/i, bg: 'from-yellow-100 to-amber-100 text-amber-800', icon: '🧸' },
  { match: /одежд|стил|мод|fashion/i, bg: 'from-purple-100 to-violet-100 text-violet-800', icon: '👗' },
  { match: /гаджет|телефон|смарт/i, bg: 'from-slate-200 to-zinc-200 text-slate-800', icon: '🎮' },
  { match: /кафе|кофе|ресторан|еда/i, bg: 'from-stone-200 to-amber-100 text-stone-800', icon: '☕' },
];
const ACC_FALLBACK_THEMES = [
  'from-indigo-100 to-purple-100 text-indigo-800',
  'from-blue-100 to-indigo-100 text-blue-800',
  'from-violet-100 to-purple-100 text-violet-800',
  'from-amber-100 to-orange-100 text-amber-800',
  'from-rose-100 to-pink-100 text-rose-800',
];

function accAvatarHtml(a) {
  const grp = a.display_name || '';
  const theme = ACC_AVATAR_THEMES.find((t) => t.match.test(grp) || t.match.test(a.username));
  if (theme) {
    return `<div class="w-10 h-10 rounded-xl bg-gradient-to-br ${theme.bg} border border-black/5 flex items-center justify-center text-lg shrink-0 shadow-sm" aria-hidden="true">${theme.icon}</div>`;
  }
  const bg = ACC_FALLBACK_THEMES[(a.id || 0) % ACC_FALLBACK_THEMES.length];
  const initials = (a.username || 'TT').replace(/[^a-z0-9]/gi, '').slice(0, 2).toUpperCase() || 'TT';
  return `<div class="w-10 h-10 rounded-xl bg-gradient-to-br ${bg} border border-black/5 flex items-center justify-center text-xs font-bold shrink-0 shadow-sm" aria-hidden="true">${esc(initials)}</div>`;
}

function accStatePillHtml(state) {
  const st = ACC_STATE_PILL[state] || ACC_STATE_PILL.ready;
  return `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold whitespace-nowrap ${st.cls}">
    <span class="w-2 h-2 rounded-full ${st.dot}"></span>${st.label}
  </span>`;
}

function accProxyCellHtml(a) {
  const host = a.proxy_host;
  if (!host) {
    return `
      <div class="font-medium text-muted text-sm">Без прокси</div>
      <div class="flex items-center gap-1.5 text-xs text-muted mt-0.5">
        <span class="w-2 h-2 rounded-full bg-slate-300 shrink-0"></span>
        <span class="truncate max-w-[12rem]">${esc(a.proxy_label || 'Не подключен')}</span>
      </div>`;
  }
  const isErr = a.proxy_status === 'error' || /недоступ|ошибк/i.test(a.proxy_label || '');
  const dotCls = isErr ? 'bg-rose-500' : 'bg-indigo-500';
  const defaultLabel = isErr ? 'недоступен' : (a.effective_state === 'working' ? 'активно' : 'стабильно');
  return `
    <div class="font-medium text-ink text-sm tabular-nums">${esc(host)}</div>
    <div class="flex items-center gap-1.5 text-xs text-muted mt-0.5">
      <span class="w-2 h-2 rounded-full ${dotCls} shrink-0"></span>
      <span class="truncate max-w-[13rem]">${esc(a.proxy_label || defaultLabel)}</span>
    </div>`;
}

function accRepliesCellHtml(a) {
  const limit = a.daily_reply_limit || 0;
  const pct = limit > 0 ? Math.min(100, Math.round((a.replies_today / limit) * 100)) : 0;
  const st = ACC_STATE_PILL[a.effective_state] || ACC_STATE_PILL.ready;
  return `
    <div class="font-semibold text-ink text-sm tabular-nums">${a.replies_today} / ${limit}</div>
    <div class="w-28 h-1.5 bg-slate-100 rounded-full overflow-hidden mt-1.5">
      <div class="h-full rounded-full transition-all ${st.bar}" style="width:${pct}%"></div>
    </div>`;
}

function accNoteText(a) {
  if (a.notes && a.notes.trim()) return a.notes.trim();
  if (a.proxy_status === 'error') return 'Прокси недоступен';
  if (a.daily_reply_limit > 0 && a.replies_today >= a.daily_reply_limit) return 'Лимит на сегодня';
  return '—';
}

function getFilteredAccounts() {
  const q = accSearchQuery.trim().toLowerCase();
  return accountsCache.filter((a) => {
    if (accStateFilter !== 'all' && a.effective_state !== accStateFilter) return false;
    if (accGroupFilter && (a.display_name || '') !== accGroupFilter) return false;
    if (accProxyFilter === 'ok' && (!a.proxy_host || a.proxy_status === 'error')) return false;
    if (accProxyFilter === 'error' && a.proxy_status !== 'error') return false;
    if (accProxyFilter === 'none' && a.proxy_host) return false;
    if (accLimitFilter === 'available' && a.daily_reply_limit > 0 && a.replies_today >= a.daily_reply_limit) return false;
    if (accLimitFilter === 'exhausted' && (!a.daily_reply_limit || a.replies_today < a.daily_reply_limit)) return false;
    if (accOnlySession && !a.is_current_session) return false;
    const checking = a.session_task_id || accountCheckRequests.has(a.id);
    if (accSessionFilter === 'active' && (a.session_status !== 'active' || checking)) return false;
    if (accSessionFilter === 'attention' && (!['unchecked', 'expired', 'error'].includes(a.session_status) || checking)) return false;
    if (accSessionFilter === 'missing' && a.session_status !== 'missing') return false;
    if (accSessionFilter === 'checking' && !checking) return false;
    if (q) {
      const hay = `${a.username} ${a.display_name || ''} ${a.proxy_host || ''} ${a.proxy_label || ''} ${a.notes || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderAccStateTabs() {
  const counts = { all: accountsCache.length, ready: 0, working: 0, resting: 0, warning: 0, banned: 0 };
  for (const a of accountsCache) {
    if (counts[a.effective_state] !== undefined) counts[a.effective_state] += 1;
  }
  $('#acc-state-tabs').innerHTML = ACC_STATE_TABS.map(([key, label]) => {
    const active = key === accStateFilter;
    const cls = active
      ? 'border-2 border-indigo-600 bg-indigo-50/50 text-ink font-semibold shadow-sm'
      : 'border border-line bg-white hover:bg-paper text-ink font-medium';
    const badgeCls = active
      ? 'bg-indigo-100 text-indigo-700 font-bold'
      : 'bg-paper text-muted font-semibold';
    return `<button type="button" role="tab" aria-selected="${active}" data-acc-tab="${key}" class="inline-flex items-center gap-2 px-3.5 py-2 rounded-xl text-sm transition-colors ${cls}">
      <span>${label}</span>
      <span class="px-2 py-0.5 rounded-md text-xs tabular-nums ${badgeCls}">${counts[key] || 0}</span>
    </button>`;
  }).join('');
}

function renderAccGroupOptions() {
  const sel = $('#acc-group');
  const current = accGroupFilter;
  const groups = [...new Set(accountsCache.map((a) => (a.display_name || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'));
  sel.innerHTML = `<option value="">Все группы</option>` +
    groups.map((g) => `<option value="${esc(g)}" ${g === current ? 'selected' : ''}>${esc(g)}</option>`).join('');
}

function updateAccBulkBar() {
  // Удаляем из выделения удалённые аккаунты
  const validIds = new Set(accountsCache.map((a) => a.id));
  for (const id of [...accSelectedIds]) {
    if (!validIds.has(id)) accSelectedIds.delete(id);
  }
  const n = accSelectedIds.size;
  $('#acc-bulk-bar').classList.toggle('hidden', n === 0);
  $('#acc-selected-count').textContent = n;
}

function renderAccountsView() {
  renderAccStateTabs();
  renderAccGroupOptions();
  updateAccBulkBar();

  const filtered = getFilteredAccounts();
  const total = filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / accPageSize));
  if (accPage > totalPages) accPage = totalPages;
  if (accPage < 1) accPage = 1;

  const startIdx = (accPage - 1) * accPageSize;
  const pageItems = filtered.slice(startIdx, startIdx + accPageSize);

  const allOnPageSelected = pageItems.length > 0 && pageItems.every((a) => accSelectedIds.has(a.id));
  $('#acc-select-all').checked = allOnPageSelected;

  if (typeof renderAccountOverview === 'function') renderAccountOverview();
  $('#accounts-body').innerHTML = pageItems.length ? pageItems.map((a) => `
    <tr data-id="${a.id}" class="${accSelectedIds.has(a.id) ? 'acc-row-selected' : ''}">
      <td class="acc-checkbox-cell"><input type="checkbox" data-acc-check value="${a.id}" ${accSelectedIds.has(a.id) ? 'checked' : ''} aria-label="Выбрать @${esc(a.username)}"></td>
      <td><div class="acc-identity">${accAvatarHtml(a)}<div class="min-w-0"><div class="acc-name"><a href="https://www.tiktok.com/@${esc(a.username)}" target="_blank" rel="noopener">@${esc(a.username)}</a>${a.is_current_session ? '<span class="acc-search-tag">Для поиска</span>' : ''}</div><div class="acc-row-caption">${esc(a.display_name || 'Без группы')} · ${esc(a.effective_state === 'ready' ? 'Активен' : a.status === 'paused' ? 'На паузе' : a.effective_state === 'resting' ? 'Лимит исчерпан' : ACC_STATE_PILL[a.effective_state]?.label || 'Активен')}</div></div></div></td>
      <td>${typeof accountSessionCell === 'function' ? accountSessionCell(a) : 'Загрузка…'}</td>
      <td>${typeof accountSetsCell === 'function' ? accountSetsCell(a) : '—'}</td>
      <td>${accRepliesCellHtml(a)}</td>
      <td><div class="acc-row-actions"><button type="button" data-acc-check-session="${a.id}" class="btn btn-sm btn-ghost" ${a.session_task_id || accountCheckRequests.has(a.id) ? 'disabled' : ''}>${a.session_task_id || accountCheckRequests.has(a.id) ? 'Проверяется…' : 'Проверить'}</button><button type="button" data-acc-edit="${a.id}" class="btn btn-sm acc-settings-btn">Настроить</button><button type="button" data-acc-menu-btn="${a.id}" class="acc-more" aria-label="Ещё действия для @${esc(a.username)}">⋯</button></div></td>
    </tr>`).join('') : `<tr><td colspan="6"><div class="acc-empty"><strong>${accountsCache.length ? 'Аккаунты не найдены' : 'Подключите первый аккаунт'}</strong><p>${accountsCache.length ? 'Измените поиск или сбросьте фильтры.' : 'Войдите в TikTok или перенесите готовую сессию.'}</p><button type="button" ${accountsCache.length ? 'data-acc-reset' : 'data-acc-add-empty'} class="btn btn-ghost">${accountsCache.length ? 'Сбросить фильтры' : 'Добавить аккаунт'}</button></div></td></tr>`;

  // Подвал пагинации
  const shownFrom = total ? startIdx + 1 : 0;
  const shownTo = Math.min(total, startIdx + pageItems.length);
  $('#acc-page-info').textContent = total ? `Показано ${shownFrom}–${shownTo} из ${total}` : 'Показано 0 из 0';

  const pages = [];
  for (let p = 1; p <= totalPages; p++) pages.push(p);
  $('#acc-pagination').innerHTML = `
    <button type="button" data-acc-page="${accPage - 1}" ${accPage <= 1 ? 'disabled' : ''} aria-label="Предыдущая страница" class="w-8 h-8 rounded-lg border border-line bg-white hover:bg-paper disabled:opacity-40 flex items-center justify-center text-muted">‹</button>
    ${pages.map((p) => `<button type="button" data-acc-page="${p}" class="w-8 h-8 rounded-lg border text-xs font-semibold tabular-nums transition-colors ${p === accPage ? 'border-indigo-500 bg-indigo-50 text-indigo-700' : 'border-line bg-white hover:bg-paper text-ink'}">${p}</button>`).join('')}
    <button type="button" data-acc-page="${accPage + 1}" ${accPage >= totalPages ? 'disabled' : ''} aria-label="Следующая страница" class="w-8 h-8 rounded-lg border border-line bg-white hover:bg-paper disabled:opacity-40 flex items-center justify-center text-muted">›</button>`;
}

loaders.accounts = async () => {
  const [accounts] = await Promise.all([api('/api/accounts'), refreshGlobal()]);
  accountsCache = accounts;
  renderAccountsView();
};

// Переключение вкладок состояний
$('#acc-state-tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-acc-tab]');
  if (!btn) return;
  accStateFilter = btn.dataset.accTab;
  accPage = 1;
  renderAccountsView();
});

// Поиск и фильтры
$('#acc-search').addEventListener('input', (e) => {
  accSearchQuery = e.target.value;
  accPage = 1;
  renderAccountsView();
});
$('#acc-group').addEventListener('change', (e) => {
  accGroupFilter = e.target.value;
  accPage = 1;
  renderAccountsView();
});
$('#btn-acc-filters').addEventListener('click', () => {
  const hidden = $('#acc-extra-filters').classList.toggle('hidden');
  $('#btn-acc-filters').setAttribute('aria-expanded', String(!hidden));
});
$('#acc-proxy-filter').addEventListener('change', (e) => {
  accProxyFilter = e.target.value;
  accPage = 1;
  renderAccountsView();
});
$('#acc-limit-filter').addEventListener('change', (e) => {
  accLimitFilter = e.target.value;
  accPage = 1;
  renderAccountsView();
});
$('#acc-only-session').addEventListener('change', (e) => {
  accOnlySession = e.target.checked;
  accPage = 1;
  renderAccountsView();
});
$('#btn-acc-reset-filters').addEventListener('click', () => {
  accStateFilter = 'all';
  accSearchQuery = '';
  accGroupFilter = '';
  accProxyFilter = '';
  accLimitFilter = '';
  accOnlySession = false;
  accSessionFilter = '';
  $('#acc-session-filter').value = '';
  $('#acc-search').value = '';
  $('#acc-group').value = '';
  $('#acc-proxy-filter').value = '';
  $('#acc-limit-filter').value = '';
  $('#acc-only-session').checked = false;
  accPage = 1;
  renderAccountsView();
});

// Кнопки в шапке Аккаунт-менеджера
$('#btn-acc-refresh')?.addEventListener('click', safe(async () => {
  await loaders.accounts();
  toast('Список аккаунтов обновлён');
}));
$('#btn-acc-add')?.addEventListener('click', () => openAccountModal(null));

// Пагинация
$('#acc-page-size').addEventListener('change', (e) => {
  accPageSize = Number(e.target.value) || 8;
  accPage = 1;
  renderAccountsView();
});
$('#acc-pagination').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-acc-page]');
  if (!btn || btn.disabled) return;
  accPage = Number(btn.dataset.accPage) || 1;
  renderAccountsView();
});

// Выбор чекбоксами
$('#acc-select-all').addEventListener('change', (e) => {
  const boxes = $$('[data-acc-check]', $('#accounts-body'));
  boxes.forEach((b) => {
    b.checked = e.target.checked;
    const id = Number(b.value);
    if (e.target.checked) accSelectedIds.add(id);
    else accSelectedIds.delete(id);
  });
  updateAccBulkBar();
});

$('#accounts-body').addEventListener('change', (e) => {
  if (!e.target.matches('[data-acc-check]')) return;
  const id = Number(e.target.value);
  if (e.target.checked) accSelectedIds.add(id);
  else accSelectedIds.delete(id);
  const boxes = $$('[data-acc-check]', $('#accounts-body'));
  $('#acc-select-all').checked = boxes.length > 0 && boxes.every((b) => b.checked);
  updateAccBulkBar();
});

// Массовые действия
$('#acc-bulk-bar').addEventListener('click', safe(async (e) => {
  const act = e.target.closest('[data-acc-bulk]')?.dataset.accBulk;
  if (!act || !accSelectedIds.size) return;
  const ids = [...accSelectedIds];
  if (act === 'check') return checkAccountSessions(ids);
  if (act === 'delete') {
    if (!confirm(`Удалить выбранные аккаунты (${ids.length} шт.)?`)) return;
    await api('/api/accounts/bulk-delete', { method: 'POST', body: { ids } });
    accSelectedIds.clear();
    toast('Аккаунты удалены');
    return loaders.accounts();
  }
  if (['active', 'working', 'paused'].includes(act)) {
    await api('/api/accounts/bulk-update', { method: 'POST', body: { ids, status: act } });
    toast('Состояние обновлено');
    return loaders.accounts();
  }
  if (act === 'limit') {
    const val = prompt('Новый дневной лимит ответов для выбранных аккаунтов:', '20');
    if (val === null) return;
    const limit = Number(val);
    if (Number.isNaN(limit) || limit < 0 || limit > 500) return toast('Введите число от 0 до 500', true);
    await api('/api/accounts/bulk-update', { method: 'POST', body: { ids, daily_reply_limit: limit } });
    toast('Дневной лимит обновлён');
    return loaders.accounts();
  }
  if (act === 'group') {
    const grp = prompt('Укажите группу / категорию для выбранных аккаунтов (оставьте пустым для сброса):', '');
    if (grp === null) return;
    await api('/api/accounts/bulk-update', { method: 'POST', body: { ids, display_name: grp.trim() } });
    toast('Группа обновлена');
    return loaders.accounts();
  }
}));

// Меню «три точки» (⋮) и быстрый клик по заметке
function closeAccRowMenu() {
  $('#acc-row-menu')?.classList.add('hidden');
  accMenuAccountId = null;
}

function openAccRowMenu(btn, acc) {
  const menu = $('#acc-row-menu');
  accMenuAccountId = acc.id;
  const isPaused = acc.effective_state === 'resting' || acc.status === 'paused';
  $('#acc-menu-pause-label').textContent = isPaused ? 'Включить в работу' : 'Поставить на паузу';
  menu.classList.remove('hidden');

  const rect = btn.getBoundingClientRect();
  const menuW = 224;
  const menuH = menu.offsetHeight || 210;
  let left = Math.min(window.innerWidth - menuW - 12, Math.max(12, rect.right - menuW));
  let top = rect.bottom + 6;
  if (top + menuH > window.innerHeight - 12) {
    top = Math.max(12, rect.top - menuH - 6);
  }
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
}

$('#accounts-body').addEventListener('click', (e) => {
  const menuBtn = e.target.closest('[data-acc-menu-btn]');
  if (menuBtn) {
    e.stopPropagation();
    const id = Number(menuBtn.dataset.accMenuBtn);
    if (accMenuAccountId === id && !$('#acc-row-menu').classList.contains('hidden')) {
      return closeAccRowMenu();
    }
    const acc = accountsCache.find((a) => a.id === id);
    if (acc) openAccRowMenu(menuBtn, acc);
    return;
  }
  const editBtn = e.target.closest('[data-acc-edit]');
  if (editBtn) {
    const acc = accountsCache.find((a) => a.id === Number(editBtn.dataset.accEdit));
    if (acc) openAccountModal(acc);
  }
});

document.addEventListener('click', (e) => {
  const menu = $('#acc-row-menu');
  if (menu && !menu.classList.contains('hidden') && !menu.contains(e.target)) {
    closeAccRowMenu();
  }
});
window.addEventListener('scroll', closeAccRowMenu, true);

$('#acc-row-menu').addEventListener('click', safe(async (e) => {
  const btn = e.target.closest('[data-acc-menu-act]');
  if (!btn || !accMenuAccountId) return;
  const act = btn.dataset.accMenuAct;
  const acc = accountsCache.find((a) => a.id === accMenuAccountId);
  closeAccRowMenu();
  if (!acc) return;

  if (act === 'edit') {
    openAccountModal(acc);
  } else if (act === 'toggle-pause') {
    const isPaused = acc.effective_state === 'resting' || acc.status === 'paused';
    const nextStatus = isPaused ? 'active' : 'paused';
    await api(`/api/accounts/${acc.id}`, { method: 'PATCH', body: { status: nextStatus } });
    toast(nextStatus === 'paused' ? `@${acc.username} поставлен на паузу` : `@${acc.username} готов к работе`);
    await loaders.accounts();
  } else if (act === 'check-proxy') {
    toast(`Проверяем прокси @${acc.username}…`);
    const updated = await api(`/api/accounts/${acc.id}/check-proxy`, { method: 'POST' });
    toast(updated.proxy_status === 'ok' ? `Прокси @${acc.username} работает стабильно` : `Прокси @${acc.username}: ${updated.proxy_label || 'недоступен'}`, updated.proxy_status === 'error');
    await loaders.accounts();
  } else if (act === 'open-profile') {
    window.open(`https://www.tiktok.com/@${encodeURIComponent(acc.username)}`, '_blank', 'noopener');
  } else if (act === 'delete') {
    if (!confirm(`Удалить @${acc.username}? Отправленные ответы останутся в статистике без привязки к аккаунту.`)) return;
    await api(`/api/accounts/${acc.id}`, { method: 'DELETE' });
    toast(`Аккаунт @${acc.username} удалён`);
    await loaders.accounts();
  }
}));

// Модальное окно добавления и редактирования аккаунта
function closeAccountModal() {
  $('#modal-account').classList.add('hidden');
  if (typeof restoreAccountEditorFocus === 'function') restoreAccountEditorFocus();
}

function openAccountModal(acc = null) {
  if (!acc) return openConnectModal();
  const form = $('#account-modal-form');
  form.reset();
  if (acc) {
    $('#acc-modal-title').textContent = `Настроить @${acc.username}`;
    form.elements.id.value = acc.id;
    form.elements.username.value = `@${acc.username}`;
    form.elements.display_name.value = acc.display_name || '';
    form.elements.status.value = acc.status === 'working' ? 'active' : acc.status || 'active';
    form.elements.daily_reply_limit.value = acc.daily_reply_limit ?? 20;
    form.elements.proxy.value = acc.proxy || '';
    form.elements.proxy_label.value = acc.proxy_label || '';
    form.elements.proxy_status.value = acc.proxy_status || (acc.proxy_host ? 'ok' : 'none');
    form.elements.notes.value = acc.notes || '';
  } else {
    $('#acc-modal-title').textContent = 'Добавить аккаунт';
    form.elements.id.value = '';
    form.elements.status.value = 'active';
    form.elements.daily_reply_limit.value = 20;
    form.elements.proxy_status.value = 'ok';
  }
  $('#modal-account').classList.remove('hidden');
  setupAccountEditor(acc);
  $('#account-editor-tab-general').focus();
}

$('#btn-close-acc-modal').addEventListener('click', closeAccountModal);
$('#btn-cancel-acc-modal').addEventListener('click', closeAccountModal);
$('#modal-account').addEventListener('click', (e) => {
  if (e.target.id === 'modal-account') closeAccountModal();
});

$('#account-modal-form').addEventListener('submit', safe(async (e) => {
  e.preventDefault();
  if (accountEditorLoading || !accountSetsLoaded) return toast('Дождитесь загрузки наборов', true);
  const f = e.target.elements;
  const id = f.id.value;
  const existing = id ? accountsCache.find((a) => String(a.id) === String(id)) : null;
  let proxyVal = f.proxy.value.trim() || null;
  // Если при редактировании поле содержит тот же host:port, что уже был у аккаунта с логином/паролем, сохраняем полный URL прокси
  if (existing && proxyVal && proxyVal === existing.proxy_host && existing.proxy) {
    proxyVal = existing.proxy;
  }
  const body = {
    username: f.username.value.trim(),
    display_name: f.display_name.value.trim() || null,
    status: f.status.value,
    daily_reply_limit: Number(f.daily_reply_limit.value ?? 20),
    proxy: proxyVal,
    proxy_label: f.proxy_label.value.trim() || null,
    proxy_status: proxyVal ? f.proxy_status.value : 'none',
    notes: f.notes.value.trim() || null,
    text_set_ids: $$('[data-account-set]:checked', e.target).map(el => Number(el.value)),
  };
  if (id) {
    await api(`/api/accounts/${id}`, { method: 'PATCH', body });
    toast('Настройки аккаунта сохранены');
  } else {
    await api('/api/accounts', { method: 'POST', body });
    toast('Аккаунт добавлен');
  }
  closeAccountModal();
  await loaders.accounts();
}));

// ---------- Поиск и задачи ----------
loaders.search = async () => {
  const [tasks, templates] = await Promise.all([api('/api/tasks?limit=30'), api('/api/templates')]);
  const tplSel = $('#search-form').elements.template;
  const tplPrev = tplSel.value;
  tplSel.innerHTML = '<option value="">Только найти</option>' +
    templates.map((t) => `<option value="${t.id}">+ черновики: ${esc(t.name)}</option>`).join('');
  tplSel.value = templates.some((t) => String(t.id) === tplPrev) ? tplPrev : '';

  $('#tasks-body').innerHTML = tasks.map((t) => {
    let paramsSummary = '';
    if (t.kind === 'search') {
      paramsSummary = `Аккаунт #${t.params?.account_id || "—"} · ` + esc(t.params?.queries?.join(', ') || '') + ` · ${t.params?.limit_per_query || 30}/запрос${t.params?.visible ? ' · окно' : ''}`;
    } else if (t.kind === 'auto_comment') {
      const cnt = t.params?.draft_ids?.length ? `${t.params.draft_ids.length} шт.` : (t.params?.max_count ? `первые ${t.params.max_count}` : 'все в очереди');
      paramsSummary = `Аккаунт #${t.params?.account_id || "—"} · Черновики: ${cnt} · ${t.params?.delay_min_s || 20}-${t.params?.delay_max_s || 45}с${t.params?.visible ? ' · окно' : ''}`;
    }
    const canCancel = ['queued', 'running'].includes(t.status);
    return `
    <tr class="border-t border-slate-100">
      <td class="p-3">${t.id}</td>
      <td class="p-3 font-medium">${esc(LABEL[t.kind] || t.kind)}</td>
      <td class="p-3 text-slate-500">${paramsSummary}</td>
      <td class="p-3">${badge(t.status)}</td>
      <td class="p-3 ${t.error ? 'text-rose-600' : ''}">${esc(t.error || t.result || (t.status === 'running' ? (t.kind === 'search' ? `новых: ${t.found}…` : `отправлено: ${t.found}…`) : ''))}</td>
      <td class="p-3 text-slate-400 whitespace-nowrap">${fmtDate(t.created_at)}</td>
      <td class="p-3 text-right">
        ${canCancel ? `<button data-cancel-task="${t.id}" class="px-2.5 py-1 text-xs rounded border border-rose-200 bg-white text-rose-600 hover:bg-rose-50 font-medium">Отменить</button>` : ''}
      </td>
    </tr>`;
  }).join('') || '<tr><td class="p-3 text-slate-400" colspan="7">Задач ещё не было</td></tr>';
};

$('#tasks-body')?.addEventListener('click', safe(async (e) => {
  const btn = e.target.closest('[data-cancel-task]');
  if (!btn) return;
  const id = btn.dataset.cancelTask;
  await api(`/api/tasks/${id}/cancel`, { method: 'POST' });
  toast(`Задача #${id} отменена`);
  await loaders.search();
}));

$('#search-form').addEventListener('submit', safe(async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const queries = String(f.get('queries')).split('\n').map((s) => s.trim()).filter(Boolean);
  const allAccounts = f.get('account_id') === 'all';
  const response = await api(allAccounts ? '/api/tasks/search-all' : '/api/tasks/search', {
    method: 'POST',
    body: {
      queries,
      account_id: Number(f.get("account_id")) || null,
      limit_per_query: Number(f.get('limit')),
      visible: f.get('visible') === 'on',
      template_id: Number(f.get('template')) || null,
    },
  });
  toast(allAccounts ? `Поиск запущен: аккаунтов ${response.accounts_used} из ${response.available_accounts}. Запросы распределены между ними.` : 'Поиск запущен. Найденные ролики появятся в «Отборе роликов»');
  await loaders.search();
}));

// ---------- Ролики ----------
const VPAGE = 50;
let vOffset = 0;
const selectedVideos = new Set();

async function loadTemplateOptions(select) {
  const tpls = await api('/api/templates');
  const prev = select.value;
  select.innerHTML = tpls.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join('') || '<option value="">— нет шаблонов —</option>';
  if (tpls.some((t) => String(t.id) === prev)) select.value = prev;
}

function videoFilters() {
  const params = new URLSearchParams();
  if ($('#v-status').value) params.set('status', $('#v-status').value);
  if ($('#v-query').value) params.set('query', $('#v-query').value);
  if ($('#v-q').value.trim()) params.set('q', $('#v-q').value.trim());
  return params;
}

loaders.videos = async () => {
  const params = videoFilters();
  params.set('limit', VPAGE);
  params.set('offset', vOffset);

  const [page, queries] = await Promise.all([api(`/api/videos?${params}`), api('/api/videos/queries'), loadTemplateOptions($('#v-template'))]);
  const qSel = $('#v-query');
  const qPrev = qSel.value;
  qSel.innerHTML = '<option value="">Все</option>' + queries.map((q) => `<option ${q === qPrev ? 'selected' : ''}>${esc(q)}</option>`).join('');

  $('#v-total').textContent = page.total ? `${vOffset + 1}–${Math.min(vOffset + VPAGE, page.total)} из ${page.total}` : 'Ничего не найдено';
  $('#v-prev').disabled = vOffset === 0;
  $('#v-next').disabled = vOffset + VPAGE >= page.total;
  $('#v-all').checked = page.items.length > 0 && page.items.every(v => selectedVideos.has(v.id));

  $('#videos-body').innerHTML = page.items.map((v) => `
    <tr class="border-t border-slate-100 align-top" data-id="${v.id}">
      <td class="p-3"><input type="checkbox" data-pick ${selectedVideos.has(v.id) ? 'checked' : ''} aria-label="Выбрать ролик"></td>
      <td class="p-3 whitespace-nowrap"><a href="${esc(v.url)}" target="_blank" class="text-sky-700 hover:underline">@${esc(v.author)}</a></td>
      <td class="p-3 max-w-md"><div class="line-clamp-2">${esc(v.description) || '<span class="text-slate-400">—</span>'}</div></td>
      <td class="p-3">${fmtNum(v.views)}</td>
      <td class="p-3 text-slate-500">${esc(v.query)}</td>
      <td class="p-3">${badge(v.status)}</td>
      <td class="p-3 whitespace-nowrap text-right space-x-1">
        <button data-act="relevant" title="Подходит" class="px-2 py-1 rounded border border-emerald-500 text-emerald-700 hover:bg-emerald-50">✓</button>
        <button data-act="irrelevant" title="Не подходит" class="px-2 py-1 rounded border border-slate-300 text-slate-500 hover:bg-slate-50">✗</button>
        <button data-act="draft" title="Сразу создать черновик по выбранному шаблону" class="px-2 py-1 rounded bg-slate-900 text-white hover:bg-slate-700">Черновик</button>
        <button data-act="delete" title="Удалить ролик" class="px-2 py-1 rounded border border-rose-200 text-rose-600 hover:bg-rose-50">🗑</button>
      </td>
    </tr>`).join('') || `
    <tr><td colspan="7" class="p-6 text-center text-slate-500">
      ${$('#v-status').value === 'new' && !$('#v-q').value.trim() ? 'Новых роликов нет — все отобраны или поиск ещё не запускался.' : 'По этим фильтрам ничего нет.'}
      <button data-goto="search" class="ml-2 text-indigo-600 hover:underline">К поиску →</button>
    </td></tr>`;
};

['#v-status', '#v-query'].forEach((s) => $(s).addEventListener('change', () => { selectedVideos.clear(); vOffset = 0; safe(loaders.videos)(); }));
let vSearchTimer;
$('#v-q').addEventListener('input', () => {
  clearTimeout(vSearchTimer);
  vSearchTimer = setTimeout(() => { selectedVideos.clear(); vOffset = 0; safe(loaders.videos)(); }, 350);
});
$('#v-prev').addEventListener('click', () => { vOffset = Math.max(0, vOffset - VPAGE); safe(loaders.videos)(); });
$('#v-next').addEventListener('click', () => { vOffset += VPAGE; safe(loaders.videos)(); });
$('#v-all').addEventListener('change', (e) => $$('[data-pick]').forEach((c) => { c.checked = e.target.checked; const id = Number(c.closest('tr').dataset.id); e.target.checked ? selectedVideos.add(id) : selectedVideos.delete(id); }));
$('#videos-body').addEventListener('change', e => {
  if (!e.target.matches('[data-pick]')) return;
  const id = Number(e.target.closest('tr').dataset.id);
  e.target.checked ? selectedVideos.add(id) : selectedVideos.delete(id);
});
$('#v-select-everything').addEventListener('click', safe(async () => {
  const params = videoFilters();
  params.set('limit', '500');
  let offset = 0, page;
  do {
    params.set('offset', String(offset));
    page = await api(`/api/videos?${params}`);
    page.items.forEach(video => selectedVideos.add(video.id));
    offset += page.items.length;
  } while (page.items.length && offset < page.total);
  toast(`Выбрано роликов: ${selectedVideos.size}`);
  await loaders.videos();
}));

$('#videos-body').addEventListener('click', safe(async (e) => {
  const btn = e.target.closest('[data-act]');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  const tr = btn.closest('tr');
  const id = Number(tr.dataset.id);
  btn.disabled = true;
  try {
    if (act === 'delete') {
      tr.remove();
      await api(`/api/videos/${id}`, { method: 'DELETE' });
      toast('Ролик удалён');
    } else if (act === 'draft') {
      await openPrepare([id]);
    } else {
      const filterSt = $('#v-status').value;
      if (filterSt && filterSt !== act) tr.remove();
      await api(`/api/videos/${id}`, { method: 'PATCH', body: { status: act } });
    }
    await Promise.all([loaders.videos(), updateDraftBadge()]);
  } finally {
    btn.disabled = false;
  }
}));

async function bulkStatus(status) {
  const ids = [...selectedVideos];
  if (!ids.length) return toast('Отметьте ролики галочками слева', true);
  for (let i = 0; i < ids.length; i += 1000) {
    const chunk = ids.slice(i, i + 1000);
    await api('/api/videos/bulk-status', { method: 'POST', body: { ids: chunk, status } });
    chunk.forEach(id => selectedVideos.delete(id));
  }
  selectedVideos.clear();
  toast(`Обновлено роликов: ${ids.length}`);
  await Promise.all([loaders.videos(), updateDraftBadge()]);
}
$('#btn-bulk-rel').addEventListener('click', safe(() => bulkStatus('relevant')));
$('#btn-bulk-irr').addEventListener('click', safe(() => bulkStatus('irrelevant')));
$('#btn-bulk-del')?.addEventListener('click', safe(async () => {
  const ids = [...selectedVideos];
  if (!ids.length) return toast('Отметьте ролики галочками слева', true);
  if (!confirm(`Удалить отмеченные ролики (${ids.length} шт.) и связанные с ними черновики?`)) return;
  const r = await api('/api/videos/bulk-delete', { method: 'POST', body: { ids } });
  selectedVideos.clear();
  toast(`Удалено роликов: ${r.deleted}`);
  await Promise.all([loaders.videos(), updateDraftBadge()]);
}));

$('#btn-generate').addEventListener('click', safe(async () => {
  const ids = [...selectedVideos];
  await openPrepare(ids.length ? ids : null);
}));
$('#workflow-prepare').addEventListener('click', safe(() => openPrepare(null)));

// ---------- Шаблоны ----------
let templatesCache = [];

loaders.templates = async () => {
  templatesCache = await api('/api/templates');
  $('#templates-list').innerHTML = templatesCache.map((t) => `
    <div class="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-4" data-id="${t.id}">
      <div class="flex justify-between items-start gap-2">
        <h3 class="font-semibold">${esc(t.name)}</h3>
        <div class="text-sm space-x-2 whitespace-nowrap">
          <button data-edit class="text-sky-700 hover:underline">Изменить</button>
          <button data-del class="text-rose-600 hover:underline">Удалить</button>
        </div>
      </div>
      <pre class="mt-2 text-sm whitespace-pre-wrap font-mono text-slate-600">${esc(t.body)}</pre>
    </div>`).join('') || '<p class="text-slate-400 text-sm">Шаблонов пока нет</p>';
};

$('#templates-list').addEventListener('click', safe(async (e) => {
  const card = e.target.closest('[data-id]');
  if (!card) return;
  const t = templatesCache.find((x) => x.id === Number(card.dataset.id));
  const form = $('#template-form');
  if (e.target.matches('[data-edit]')) {
    form.elements.id.value = t.id; form.elements.name.value = t.name; form.elements.body.value = t.body;
    form.scrollIntoView({ behavior: 'smooth' });
  } else if (e.target.matches('[data-del]') && confirm(`Удалить шаблон «${t.name}»?`)) {
    await api(`/api/templates/${t.id}`, { method: 'DELETE' });
    await loaders.templates();
  }
}));

$('#template-form').addEventListener('submit', safe(async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = { name: form.elements.name.value.trim(), body: form.elements.body.value.trim() };
  const id = form.elements.id.value;
  await api(id ? `/api/templates/${id}` : '/api/templates', { method: id ? 'PUT' : 'POST', body });
  form.reset(); form.elements.id.value = '';
  $('#preview-box').classList.add('hidden');
  toast('Шаблон сохранён');
  await loaders.templates();
}));

$('#btn-tpl-reset').addEventListener('click', () => {
  const form = $('#template-form');
  form.reset(); form.elements.id.value = '';
  $('#preview-box').classList.add('hidden');
});

$('#btn-preview').addEventListener('click', safe(async () => {
  const bodyText = $('#template-form').elements.body.value.trim();
  if (!bodyText) return;
  const r = await api('/api/templates/preview', { method: 'POST', body: { body: bodyText, count: 5 } });
  const box = $('#preview-box');
  box.innerHTML = `<div class="text-slate-500">Комбинаций: ${fmtNum(r.total_combinations)}</div>` +
    [...new Set(r.variants)].map((v) => `<div>• ${esc(v)}</div>`).join('');
  box.classList.remove('hidden');
}));

// ---------- Очередь ответов ----------
let lastDraftsSig = '';

loaders.drafts = async (opts = {}) => {
  const silent = opts?.silent === true;
  const status = $('#d-status').value;
  const [drafts, accounts, tasks] = await Promise.all([
    api(`/api/drafts?status=${status}${$('#d-account-filter')?.value ? '&account_id=' + $('#d-account-filter').value : ''}${$('#d-set-filter')?.value ? '&text_set_id=' + $('#d-set-filter').value : ''}`),
    api('/api/accounts'),
    api('/api/tasks?limit=200'),
  ]);
  accountsCache = accounts;
  const active = accounts.filter((a) => a.status === 'active' || a.status === 'working');
  const editable = status === 'draft';
  $('#d-hint').classList.toggle('hidden', !editable || cv.on);

  // Проверка активной задачи автокомментирования
  const activeAc = tasks.find((t) => t.kind === 'auto_comment' && ['queued', 'running'].includes(t.status));
  const banner = $('#ac-active-banner');
  if (banner) {
    if (activeAc) {
      banner.classList.remove('hidden');
      $('#ac-active-status').textContent = activeAc.result || (activeAc.status === 'running' ? `Отправлено: ${activeAc.found}…` : 'В очереди…');
      $('#btn-cancel-ac-task').dataset.taskId = activeAc.id;
    } else {
      banner.classList.add('hidden');
    }
  }

  const sig = `${status}|` + drafts.map((d) => `${d.id}:${d.status}:${d.account_id}:${d.task_id}:${d.text}:${d.result}`).join(',');
  if (silent) {
    const hasFocus = $('#drafts-list')?.contains(document.activeElement);
    const hasChecked = $$('[data-d-pick]:checked').length > 0;
    if (hasFocus || hasChecked || sig === lastDraftsSig) return;
  }
  lastDraftsSig = sig;
  if ($('#d-all')) $('#d-all').checked = false;

  $('#drafts-list').innerHTML = drafts.map((d) => `
    <div class="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-4 space-y-3" data-id="${d.id}">
      <div class="flex flex-wrap justify-between gap-2 text-sm">
        <div class="flex items-center gap-2.5">
          <input type="checkbox" data-d-pick>
          <a href="${esc(d.video_url)}" target="_blank" class="text-sky-700 hover:underline font-medium">@${esc(d.video_author)}</a>
          <span class="text-slate-500 ml-1">${esc((d.video_description || '').slice(0, 140))}</span>
        </div>
        <div>${badge(d.status)} ${d.sent_at ? `<span class="text-slate-400">${fmtDate(d.sent_at)}</span>` : ''}</div>
      </div>
      ${d.repeated ? '<p class="text-sm text-amber-700">Повтор ранее использованного текста: новые варианты исчерпаны</p>' : ''}
      ${d.task_id ? `<p class="text-sm text-indigo-700">В задаче #${d.task_id}</p>` : ''}
      ${d.result ? `<p class="text-sm text-slate-600">${d.error_code ? `<span class="font-medium">${esc(d.error_code)}:</span> ` : ''}${esc(d.result)}</p>` : ''}
      ${d.status === 'review' ? `<div class="flex gap-2"><button data-resolve="true" class="px-3 py-2 border rounded-lg">Проверил: опубликовано</button><button data-resolve="false" class="px-3 py-2 border rounded-lg">Проверил: не опубликовано</button></div>` : ''}
      <textarea data-text rows="2" ${editable && !d.task_id ? '' : 'disabled'} class="w-full border rounded-lg px-2 py-1.5 disabled:bg-slate-50">${esc(d.text)}</textarea>
      <div class="flex flex-wrap items-center gap-2 text-sm">
        <select data-account ${editable && !d.task_id ? '' : 'disabled'} class="border rounded-lg px-2 py-1.5">
          <option value="">— аккаунт —</option>
          ${(editable ? active : accounts).map((a) => `<option value="${a.id}" ${a.id === d.account_id ? 'selected' : ''}>@${esc(a.username)} (${a.replies_today}/${a.daily_reply_limit})</option>`).join('')}
          ${d.account_id && !active.some((a) => a.id === d.account_id) && editable ? `<option value="${d.account_id}" selected>@${esc(d.account_username)} (неактивен)</option>` : ''}
        </select>
        ${editable && !d.task_id ? `
          <button data-act="open" class="px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white">Открыть и скопировать</button>
          <button data-act="sent" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white">Отправлено</button>
          <button data-act="skip" class="px-3 py-1.5 rounded-lg border border-slate-300 hover:bg-slate-50">Пропустить</button>
          <span class="text-slate-300">|</span>
          <button data-act="autocomment" title="Опубликовать этот комментарий ботом в браузере" class="px-3 py-1.5 rounded-lg border border-emerald-300 text-emerald-700 hover:bg-emerald-50">⚡ Ботом</button>` : ''}
        <button data-act="delete" class="px-3 py-1.5 rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50 ml-auto">🗑 Удалить</button>
      </div>
    </div>`).join('') || (editable ? `
      <div class="bg-white rounded-2xl border border-dashed border-slate-300 p-6 text-center text-sm space-y-2">
        <p class="text-slate-500">Очередь пуста. Черновики появляются, когда вы отбираете ролики и создаёте ответы по шаблону.</p>
        <button data-goto="videos" class="px-3 py-1.5 rounded-lg bg-slate-900 text-white">К отбору роликов</button>
      </div>` : '<p class="text-slate-400 text-sm">Пусто</p>');
};

$('#d-status').addEventListener('change', safe(() => loaders.drafts()));
$('#d-all')?.addEventListener('change', (e) => $$('[data-d-pick]').forEach((c) => { c.checked = e.target.checked; }));

$('#btn-d-del-selected')?.addEventListener('click', safe(async () => {
  const checked = $$('[data-d-pick]:checked');
  const ids = checked.map((c) => Number(c.closest('[data-id]').dataset.id));
  if (!ids.length) return toast('Отметьте черновики галочками', true);
  checked.forEach((c) => c.closest('[data-id]')?.remove());
  const r = await api('/api/drafts/bulk-delete', { method: 'POST', body: { ids } });
  toast(`Удалено из очереди: ${r.deleted}`);
  await Promise.all([loaders.drafts(), updateDraftBadge()]);
}));

$('#btn-d-clear')?.addEventListener('click', safe(async () => {
  const status = $('#d-status').value;
  const label = { draft: 'ждущие отправки', sent: 'отправленные', skipped: 'пропущенные' }[status] || '';
  if (!confirm(`Очистить все ${label} черновики в этом списке?`)) return;
  const r = await api('/api/drafts/bulk-delete', { method: 'POST', body: { status } });
  toast(`Очищено черновиков: ${r.deleted}`);
  await Promise.all([loaders.drafts(), updateDraftBadge()]);
}));

$('#drafts-list').addEventListener('change', safe(async (e) => {
  const card = e.target.closest('[data-id]');
  if (!card) return;
  const id = card.dataset.id;
  if (e.target.matches('[data-text]')) {
    const saved = await api(`/api/drafts/${id}`, { method: 'PATCH', body: { text: e.target.value.trim() } });
    toast(saved.repeated ? 'Сохранено. Этот текст уже использовался ранее' : 'Текст сохранён');
  } else if (e.target.matches('[data-account]')) {
    await api(`/api/drafts/${id}`, { method: 'PATCH', body: { account_id: e.target.value ? Number(e.target.value) : null } });
  }
}));

$('#drafts-list').addEventListener('click', safe(async (e) => {
  const btn = e.target.closest('[data-act]');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  const card = btn.closest('[data-id]');
  if (!card) return;
  const id = card.dataset.id;
  if (act === 'autocomment') {
    btn.disabled = true;
    try {
      await api(`/api/drafts/${id}/auto-comment`, { method: 'POST' });
      toast(`Бот запущен для отправки черновика #${id}`);
      await Promise.all([loaders.drafts(), updateDraftBadge()]);
    } catch (err) {
      btn.disabled = false;
      throw err;
    }
    return;
  }
  if (act === 'open') {
    await navigator.clipboard.writeText($('[data-text]', card).value);
    openVideo($('a', card).href);
    toast('Текст скопирован. Вставьте его в комментарий и нажмите «Отправлено»');
    return;
  }
  btn.disabled = true;
  if (act === 'delete' || act === 'skip') {
    card.remove();
  }
  const req = { sent: [`/api/drafts/${id}/sent`, 'POST'], skip: [`/api/drafts/${id}/skip`, 'POST'], delete: [`/api/drafts/${id}`, 'DELETE'] }[act];
  try {
    await api(req[0], { method: req[1] });
    if (act === 'sent') toast('Отмечено как отправленное');
    else if (act === 'delete') toast('Черновик удалён');
  } finally {
    await Promise.all([loaders.drafts(), updateDraftBadge()]);
  }
}));

// Отмена автокомментирования из баннера
$('#btn-cancel-ac-task')?.addEventListener('click', safe(async (e) => {
  const taskId = e.target.dataset.taskId;
  if (!taskId) return;
  await api(`/api/tasks/${taskId}/cancel`, { method: 'POST' });
  toast('Автокомментирование останавливается…');
  await Promise.all([loaders.drafts(), updateDraftBadge()]);
}));

// Модальное окно автокомментирования
let acSummaryRevision = 0;
async function refreshAutoCommentSummary() {
  const revision = ++acSummaryRevision;
  const accountId = $('#ac-account').value;
  const summary = await api('/api/drafts/summary' + (accountId ? `?account_id=${accountId}` : ''));
  if (revision !== acSummaryRevision) return;
  $('#ac-drafts-count').textContent = summary.available;
  $('#ac-empty-alert')?.classList.toggle('hidden', summary.available > 0);
  const notes = [];
  if (summary.unassigned) notes.push(`Без назначенного аккаунта: ${summary.unassigned}. Эти черновики не будут отправлены; для них можно выбрать один аккаунт.`);
  if (summary.in_progress) notes.push(`Уже в работе: ${summary.in_progress}. Они не будут запущены повторно.`);
  $('#ac-scope-note').textContent = notes.join(' ');
  $('#ac-scope-note').classList.toggle('hidden', !notes.length);
}

$('#btn-open-ac-modal')?.addEventListener('click', safe(async () => {
  // Открываем модальное окно сразу, чтобы пользователь видел реакцию на клик
  $('#modal-autocomment').classList.remove('hidden');

  const accounts = await api('/api/accounts');
  const activeAccs = accounts.filter((a) => a.status === 'active' || a.status === 'working');
  $('#ac-account').innerHTML = '<option value="">Все назначенные аккаунты</option>' +
    activeAccs.map((a) => `<option value="${a.id}">@${esc(a.username)} (${a.replies_today}/${a.daily_reply_limit})</option>`).join('');
  await refreshAutoCommentSummary();
}));
$('#ac-account')?.addEventListener('change', safe(refreshAutoCommentSummary));

$('#modal-autocomment')?.addEventListener('click', (e) => {
  if (e.target.id === 'modal-autocomment') {
    $('#modal-autocomment').classList.add('hidden');
  }
});
$('#btn-close-ac-modal')?.addEventListener('click', () => {
  $('#modal-autocomment').classList.add('hidden');
});
$('#btn-cancel-ac-modal')?.addEventListener('click', () => {
  $('#modal-autocomment').classList.add('hidden');
});

$('#ac-form')?.addEventListener('submit', safe(async (e) => {
  e.preventDefault();
  const count = Number($('#ac-drafts-count').textContent) || 0;
  const scope = $('input[name="ac_scope"]:checked', e.target)?.value;
  if (count === 0 && ['all', 'limit'].includes(scope)) {
    return toast('Нет доступных черновиков с выбранными аккаунтами', true);
  }
  if (scope === 'selected' && !$$('[data-d-pick]:checked').length) return toast('Отметьте черновики в очереди', true);
  const maxCount = scope === 'limit' ? Number($('#ac-max-count').value) : null;
  const accountId = $('#ac-account').value ? Number($('#ac-account').value) : null;
  const delayMin = Number($('#ac-delay-min').value) || 20;
  const delayMax = Number($('#ac-delay-max').value) || 45;
  const visible = $('#ac-visible').checked;

  if (delayMin > delayMax) {
    return toast('Минимальная задержка не может быть больше максимальной', true);
  }

  let allVideoDraftIds = null;
  if (scope === 'all_videos') {
    const prepared = await api('/api/drafts/prepare-all-assigned', {method:'POST', body:{account_id:accountId}});
    toast(`Подготовлено новых черновиков: ${prepared.created}`);
    allVideoDraftIds = prepared.draft_ids;
    if (!allVideoDraftIds.length) return toast('Нет черновиков для выбранных роликов и аккаунтов', true);
  }

  const submitted = await api('/api/tasks/auto-comment', {
    method: 'POST',
    body: {
      draft_ids: scope === 'all_videos' ? allVideoDraftIds : scope === 'selected' ? $$('[data-d-pick]:checked').map(c => Number(c.closest('[data-id]').dataset.id)) : null,
      account_id: accountId,
      max_count: maxCount,
      delay_min_s: delayMin,
      delay_max_s: delayMax,
      visible,
    },
  });

  $('#modal-autocomment').classList.add('hidden');
  const skipped = submitted.skipped_unassigned ? ` Без аккаунта пропущено: ${submitted.skipped_unassigned}.` : '';
  toast(`Автокомментирование запущено в фоне.${skipped}`);
  await Promise.all([loaders.drafts(), updateDraftBadge()]);
}));

// ---------- Экспорт CSV ----------
function download(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

$('#v-export').addEventListener('click', () => download(`/api/export/leads.csv?${videoFilters()}`));

$('#btn-export-replies').addEventListener('click', () => {
  const from = $('#exp-from').value;
  const to = $('#exp-to').value;
  if (from && to && from > to) return toast('Дата «с» позже даты «по»', true);
  const params = new URLSearchParams();
  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);
  download(`/api/export/replies.csv?${params}`);
});

// ---------- Конвейер ----------
// Один черновик на экране. Ролики открываются в одной и той же вкладке,
// поэтому вкладки не копятся.
const VIDEO_WIN = 'ttlh-video';
const LAST_ACC_KEY = 'ttlh-last-account';
const cv = { on: false, items: [], idx: 0, sent: 0, skipped: 0, busy: false, accounts: [] };

function openVideo(url) {
  window.open(url, VIDEO_WIN);
}
function lastAccount() {
  try { return Number(localStorage.getItem(LAST_ACC_KEY)) || null; } catch { return null; }
}
function rememberAccount(id) {
  try { if (id) localStorage.setItem(LAST_ACC_KEY, String(id)); } catch {}
}

const cvCurrent = () => cv.items[cv.idx];
// Аккаунт черновика, иначе последний использованный (если он ещё активен).
function cvAccountId(d) {
  if (d.account_id) return d.account_id;
  const last = lastAccount();
  return cv.accounts.some((a) => a.id === last && a.status === 'active') ? last : null;
}

function toggleConveyorView(on) {
  $('#conveyor').classList.toggle('hidden', !on);
  ['#d-toolbar', '#d-hint', '#drafts-list'].forEach((sel) => $(sel).classList.toggle('hidden', on));
}

async function startConveyor() {
  const [drafts, accounts] = await Promise.all([api('/api/drafts?status=draft&limit=500'), api('/api/accounts')]);
  if (!drafts.length) return toast('Очередь пуста: сначала создайте черновики в разделе «Отбор роликов»', true);
  Object.assign(cv, { on: true, items: drafts, idx: 0, sent: 0, skipped: 0, accounts });
  toggleConveyorView(true);
  renderConveyor();
  await cvOpen(); // клик по кнопке позволяет сразу открыть первый ролик
}

function stopConveyor() {
  cv.on = false;
  toggleConveyorView(false);
  safe(loaders.drafts)();
}

function renderConveyor() {
  const total = cv.items.length;
  const done = cv.sent + cv.skipped;
  $('#cv-bar').style.width = `${(done / total) * 100}%`;
  $('#cv-progress').textContent = `${Math.min(done + 1, total)} из ${total}`;

  const d = cvCurrent();
  if (!d) {
    $('#cv-progress').textContent = `${total} из ${total}`;
    $('#cv-card').innerHTML = `
      <div class="bg-white rounded-xl border border-emerald-200 p-8 text-center space-y-3">
        <div class="text-4xl">🎉</div>
        <h2 class="text-lg font-semibold">Очередь разобрана</h2>
        <p class="text-slate-600">Отправлено: <b>${cv.sent}</b>, пропущено: <b>${cv.skipped}</b></p>
        <button data-cv="exit" class="px-4 py-2 rounded-lg bg-slate-900 text-white">Вернуться к очереди</button>
      </div>`;
    return;
  }

  const accId = cvAccountId(d);
  const options = cv.accounts.filter((a) => a.status === 'active' || a.id === accId);
  const acc = cv.accounts.find((a) => a.id === accId);
  const full = acc && acc.daily_reply_limit > 0 && acc.replies_today >= acc.daily_reply_limit;

  $('#cv-card').innerHTML = `
    <div class="bg-white rounded-2xl border border-slate-200/80 shadow-sm p-5 space-y-4">
      <div class="flex flex-wrap items-baseline justify-between gap-2">
        <a href="${esc(d.video_url)}" target="${VIDEO_WIN}" class="text-lg font-semibold text-sky-700 hover:underline">@${esc(d.video_author)}</a>
        <span class="text-xs text-slate-400">черновик #${d.id}</span>
      </div>
      ${d.video_description ? `<p class="text-sm text-slate-600 bg-slate-50 rounded-lg p-3 max-h-28 overflow-auto">${esc(d.video_description)}</p>` : ''}
      <label class="block text-sm text-slate-500">Текст ответа <span class="text-slate-400">(Esc — выйти из поля, Ctrl+Enter — отправлено)</span>
        <textarea id="cv-text" rows="3" class="mt-1 w-full border rounded-lg px-3 py-2 text-base text-slate-800">${esc(d.text)}</textarea>
      </label>
      <div class="flex flex-wrap items-center gap-3 text-sm">
        <label class="flex items-center gap-2">Аккаунт
          <select id="cv-account" class="border rounded-lg px-2 py-1.5">
            <option value="">— выберите —</option>
            ${options.map((a) => `<option value="${a.id}" ${a.id === accId ? 'selected' : ''}>@${esc(a.username)} (${a.replies_today}/${a.daily_reply_limit})</option>`).join('')}
          </select>
        </label>
        ${full ? '<span class="text-amber-600 font-medium">Дневной лимит этого аккаунта исчерпан</span>' : ''}
      </div>
      <div class="flex flex-wrap gap-2 pt-1">
        <button data-cv="autocomment" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-medium flex items-center gap-1.5 shadow-sm">⚡ Отправить ботом</button>
        <button data-cv="open" class="px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-700 text-white">Открыть и скопировать <kbd class="ml-1 opacity-70">O</kbd></button>
        <button data-cv="sent" class="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white">Отправлено → дальше <kbd class="ml-1 opacity-70">Enter</kbd></button>
        <button data-cv="skip" class="px-4 py-2 rounded-lg border border-slate-300 hover:bg-slate-50">Пропустить <kbd class="ml-1 opacity-70">S</kbd></button>
        <button data-cv="delete" class="px-4 py-2 rounded-lg border border-rose-200 text-rose-600 hover:bg-rose-50 ml-auto">🗑 Удалить</button>
      </div>
    </div>`;
}

async function cvOpen() {
  const d = cvCurrent();
  if (!d) return;
  try {
    await navigator.clipboard.writeText($('#cv-text').value);
    toast('Текст скопирован, ролик открыт');
  } catch {
    toast('Не удалось скопировать: скопируйте текст вручную', true);
  }
  openVideo(d.video_url);
}

// Сохраняет правки текста и выбор аккаунта перед отметкой.
async function cvSync(d) {
  const text = $('#cv-text').value.trim();
  const accId = Number($('#cv-account').value) || null;
  const body = {};
  if (text && text !== d.text) body.text = text;
  if (accId !== d.account_id) body.account_id = accId;
  if (Object.keys(body).length) Object.assign(d, await api(`/api/drafts/${d.id}`, { method: 'PATCH', body }));
  return accId;
}

async function cvAdvance(kind) {
  const d = cvCurrent();
  if (!d || cv.busy) return;
  cv.busy = true;
  try {
    if (kind === 'sent') {
      const accId = await cvSync(d);
      if (!accId) {
        toast('Выберите аккаунт, с которого отправлен ответ', true);
        $('#cv-account').focus();
        return;
      }
      await api(`/api/drafts/${d.id}/sent`, { method: 'POST' });
      rememberAccount(accId);
      const acc = cv.accounts.find((a) => a.id === accId);
      if (acc) acc.replies_today += 1;
      cv.sent += 1;
    } else if (kind === 'delete') {
      await api(`/api/drafts/${d.id}`, { method: 'DELETE' });
      cv.skipped += 1;
    } else {
      await api(`/api/drafts/${d.id}/skip`, { method: 'POST' });
      cv.skipped += 1;
    }
    cv.idx += 1;
    renderConveyor();
    safe(refreshGlobal)();
    if (cvCurrent()) await cvOpen();
  } finally {
    cv.busy = false;
  }
}

$('#btn-conveyor').addEventListener('click', safe(startConveyor));
$('#cv-exit').addEventListener('click', stopConveyor);

$('#cv-card').addEventListener('click', safe(async (e) => {
  const act = e.target.closest('[data-cv]')?.dataset.cv;
  if (act === 'open') await cvOpen();
  if (act === 'sent' || act === 'skip' || act === 'delete') await cvAdvance(act);
  if (act === 'autocomment') {
    const d = cvCurrent();
    if (!d) return;
    await cvSync(d);
    await api(`/api/drafts/${d.id}/auto-comment`, { method: 'POST' });
    toast(`Бот запущен для отправки комментария #${d.id}`);
    cv.idx += 1;
    cv.sent += 1;
    renderConveyor();
    safe(refreshGlobal)();
    if (cvCurrent()) await cvOpen();
  }
  if (act === 'exit') stopConveyor();
}));

// После выбора аккаунта убираем фокус с select, чтобы снова работали клавиши.
$('#cv-card').addEventListener('change', (e) => {
  if (e.target.id !== 'cv-account') return;
  rememberAccount(Number(e.target.value) || null);
  e.target.blur();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#modal-autocomment') && !$('#modal-autocomment').classList.contains('hidden')) {
    $('#modal-autocomment').classList.add('hidden');
    return;
  }
  if (!cv.on || currentTab !== 'drafts' || e.altKey || e.metaKey) return;
  const inField = e.target.matches('textarea, input, select');
  if (e.key === 'Escape') {
    e.preventDefault();
    if (inField) e.target.blur(); else stopConveyor();
    return;
  }
  if (inField) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      e.target.blur();
      safe(cvAdvance)('sent');
    }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.repeat) return;
  // e.code не зависит от раскладки: S сработает и на русской «Ы».
  const actions = {
    Enter: () => cvAdvance('sent'),
    NumpadEnter: () => cvAdvance('sent'),
    KeyS: () => cvAdvance('skip'),
    KeyO: cvOpen,
    KeyE: () => {
      const t = $('#cv-text');
      t.focus();
      t.setSelectionRange(t.value.length, t.value.length);
    },
  };
  const fn = actions[e.code];
  if (!fn) return;
  e.preventDefault();
  safe(fn)();
});

// ---------- Настройки: подключение TikTok и прокси ----------
let pxLoaded = false; // поле прокси заполняем один раз, чтобы автообновление не стирало ввод

loaders.settings = async () => {
  const cfg = await api('/api/settings');

  const pill = (cls, text) => `<span class="inline-flex items-center gap-2 text-xs font-medium px-3 py-1 rounded-full ${cls}">${text}</span>`;
  $('#px-pill').innerHTML = cfg.search_proxy
    ? pill('bg-emerald-600 text-white', cfg.proxy_source === 'env' ? 'Задан (переменная окружения)' : 'Прокси включён')
    : pill('bg-slate-200 text-slate-600', 'Без прокси');

  $('#use-chrome').checked = cfg.use_system_chrome;

  if (!pxLoaded) {
    const f = $('#proxy-form').elements;
    f.proxy.value = cfg.proxy_source === 'ui' ? cfg.search_proxy : '';
    f.scheme.value = (cfg.search_proxy || '').startsWith('socks5') ? 'socks5' : 'http';
    pxLoaded = true;
  }

  const t = cfg.last_proxy_test;
  const busy = t && ['queued', 'running'].includes(t.status);
  $('#px-status').innerHTML = !t ? '' : `
    <div class="rounded-lg border px-3 py-2 ${t.status === 'failed' ? 'border-rose-200 bg-rose-50 text-rose-800' : busy ? 'border-sky-200 bg-sky-50' : 'border-emerald-200 bg-emerald-50 text-emerald-900'}">
      ${busy ? 'Проверяем прокси…' : `Проверка ${fmtDate(t.finished_at)}: ${esc(t.result || t.error || '')}`}
    </div>`;
};

async function saveProxy(proxy, scheme) {
  await api('/api/settings/proxy', { method: 'PUT', body: { proxy, scheme } });
  pxLoaded = false;
  await loaders.settings();
}

$('#proxy-form').addEventListener('submit', safe(async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  await saveProxy(f.proxy.value.trim(), f.scheme.value);
  toast(f.proxy.value.trim() ? 'Прокси сохранён. Нажмите «Проверить»' : 'Прокси отключён');
}));
$('#btn-proxy-clear').addEventListener('click', safe(async () => {
  await saveProxy('', 'http');
  toast('Прокси отключён');
}));
$('#use-chrome').addEventListener('change', safe(async (e) => {
  await api('/api/settings/browser', { method: 'PUT', body: { use_system_chrome: e.target.checked } });
  toast(e.target.checked ? 'Теперь используется Google Chrome. Нажмите «Проверить»' : 'Используется встроенный браузер');
  await loaders.settings();
}));

$('#btn-proxy-test').addEventListener('click', safe(async () => {
  await api('/api/settings/proxy/test', { method: 'POST' });
  toast('Проверяем прокси…');
  await loaders.settings();
}));

// ---------- Логи ----------
loaders.logs = async () => {
  const params = new URLSearchParams({ limit: 300 });
  if ($('#l-level').value) params.set('level', $('#l-level').value);
  if ($('#l-source').value) params.set('source', $('#l-source').value);
  const logs = await api(`/api/logs?${params}`);
  $('#logs-body').innerHTML = logs.map((l) => `
    <tr class="border-t border-slate-100 ${BADGE[l.level] || ''}">
      <td class="p-2 text-slate-400 whitespace-nowrap">${fmtDate(l.ts)}</td>
      <td class="p-2">${esc(l.level)}</td><td class="p-2">${esc(l.source)}</td>
      <td class="p-2 text-slate-700">${esc(l.message)}</td>
    </tr>`).join('') || '<tr><td class="p-3 text-slate-400">Пусто</td></tr>';
};
['#l-level', '#l-source'].forEach((s) => $(s).addEventListener('change', safe(loaders.logs)));

// ---------- Старт ----------
let startTab = 'dashboard';
try { startTab = localStorage.getItem('ttlh-tab') || startTab; } catch {}
showTab(startTab);
