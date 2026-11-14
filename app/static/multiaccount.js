/* Multi-account controls use the existing UI styles and shared API helper. */
const mxButton = 'px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm hover:bg-slate-50';
const mxField = 'border rounded-lg px-3 py-2 w-full';
const mxCard = 'bg-white rounded-2xl border border-slate-200 p-5 space-y-4';
let mxSets = [];
let mxAccounts = [];
let mxEditing = null;
let mxPrepareVideos = null;

PAGES.videos[1] = 'Выберите ролики, аккаунты и наборы: готовые комментарии появятся в очереди для просмотра';
PAGES.settings[1] = 'Аккаунт поиска, число браузеров и общие настройки прокси';
LABEL.review = 'Требует проверки';
BADGE.review = 'bg-amber-100 text-amber-800';
const tplTab = $('[data-tab="templates"]');
if (tplTab) tplTab.querySelector('span:last-child')?.replaceChildren(document.createTextNode('Наборы текстов'));
$('#template-form').hidden = true;
$('#templates-list').hidden = true;
$('#v-template').classList.add('hidden');
$('#search-form [name="template"]').closest('label')?.classList.add('hidden');
$('#quick-search [name="template"]')?.classList.add('hidden');
$('#btn-generate').textContent = 'Подготовить комментарии';

$('[data-panel="templates"]').insertAdjacentHTML('beforeend', `
  <div class="${mxCard}"><h2 class="font-semibold">Тематический набор</h2>
    <p class="text-sm text-slate-500">Каждый вариант — отдельный текст. Внутри доступны {вариант1|вариант2}, [author] и [query]. История общая для всех аккаунтов.</p>
    <form id="mx-set-form" class="space-y-4"><label class="block">Название<input id="mx-set-name" required maxlength="128" class="${mxField}"></label>
      <div id="mx-variants" class="space-y-3"></div>
      <div class="flex gap-2 flex-wrap"><button type="button" id="mx-add-variant" class="${mxButton}">+ Текст</button><button class="${mxButton}">Сохранить набор</button><button type="button" id="mx-reset-set" class="${mxButton}">Новый набор</button></div>
    </form></div><div id="mx-set-list" class="grid md:grid-cols-2 gap-4"></div>`);

function mxAddVariant(item = {}) {
  const row = document.createElement('div');
  row.className = 'space-y-1';
  row.dataset.variantId = item.id || '';
  row.innerHTML = `<textarea required maxlength="2000" rows="3" aria-label="Вариант текста" class="${mxField}">${esc(item.body || '')}</textarea><button type="button" class="text-sm text-rose-600">Удалить вариант</button>`;
  $('button', row).onclick = () => row.remove();
  $('#mx-variants').append(row);
}
function mxResetSet() {
  mxEditing = null;
  $('#mx-set-name').value = '';
  $('#mx-variants').innerHTML = '';
  mxAddVariant();
}
$('#mx-add-variant').onclick = () => mxAddVariant();
$('#mx-reset-set').onclick = mxResetSet;
mxResetSet();
loaders.templates = async () => {
  mxSets = await api('/api/text-sets');
  $('#mx-set-list').innerHTML = mxSets.map(s => `<article class="${mxCard}" data-set="${s.id}"><h3 class="font-semibold">${esc(s.name)}</h3><p class="text-sm text-slate-500">Вариантов: ${s.variants.length}</p><p class="text-sm whitespace-pre-wrap">${esc(s.variants[0]?.body || '')}</p><div class="flex gap-2"><button data-edit class="${mxButton}">Редактировать</button><button data-archive class="${mxButton}">В архив</button></div></article>`).join('') || '<p>Наборов пока нет. Создайте первый выше.</p>';
};
$('#mx-set-list').onclick = safe(async e => {
  const card = e.target.closest('[data-set]');
  if (!card) return;
  const set = mxSets.find(s => s.id === Number(card.dataset.set));
  if (e.target.closest('[data-edit]')) {
    mxEditing = set.id;
    $('#mx-set-name').value = set.name;
    $('#mx-variants').innerHTML = '';
    set.variants.forEach(mxAddVariant);
    $('#mx-set-form').scrollIntoView({ behavior: 'smooth' });
  } else if (e.target.closest('[data-archive]') && confirm('Архивировать набор? Подготовленные тексты и история сохранятся.')) {
    await api(`/api/text-sets/${set.id}`, { method: 'DELETE' });
    await loaders.templates();
  }
});
$('#mx-set-form').onsubmit = safe(async e => {
  e.preventDefault();
  const variants = $$('#mx-variants > div').map(row => ({ id: Number(row.dataset.variantId) || null, body: $('textarea', row).value.trim() }));
  if (!variants.length) return toast('Добавьте хотя бы один текст', true);
  await api(mxEditing ? `/api/text-sets/${mxEditing}` : '/api/text-sets', { method: mxEditing ? 'PUT' : 'POST', body: { name: $('#mx-set-name').value.trim(), variants } });
  mxResetSet();
  toast('Набор сохранён');
  await loaders.templates();
});

function mxOptions(select, items, empty, chosen) {
  const previous = chosen === undefined ? select.value : String(chosen ?? '');
  const html = (empty == null ? '' : `<option value="">${esc(empty)}</option>`) + items.map(i => `<option value="${i.id}">${esc(i.name || '@' + i.username)}</option>`).join('');
  if (select.innerHTML !== html) select.innerHTML = html;
  if ([...select.options].some(o => o.value === previous)) select.value = previous;
}

$('[data-panel="settings"]').insertAdjacentHTML('afterbegin', `<form id="mx-execution" class="${mxCard}"><h2 class="font-semibold">Поиск и параллельные браузеры</h2><label class="block">Аккаунт для поиска по умолчанию<select id="mx-search-account" class="${mxField}"></select></label><label class="block">Одновременно браузеров (1–8)<input id="mx-limit" type="number" min="1" max="8" required value="3" class="${mxField}"></label><button class="${mxButton}">Сохранить</button><p class="text-sm text-slate-500">Новые аккаунты добавляются кнопкой «Добавить аккаунт» в аккаунт-менеджере. Открытие ссылки в обычном браузере не переключает аккаунт TikTok.</p></form>`);
let mxExecutionLoaded = false;
const mxOriginalSettings = loaders.settings;
loaders.settings = async () => {
  await mxOriginalSettings();
  const [accounts, cfg] = await Promise.all([api('/api/accounts'), api('/api/settings')]);
  if (!mxExecutionLoaded) {
    mxOptions($('#mx-search-account'), accounts, 'Выберите аккаунт', cfg.search_account_id);
    $('#mx-limit').value = cfg.browser_limit;
    mxExecutionLoaded = true;
  } else mxOptions($('#mx-search-account'), accounts, 'Выберите аккаунт');
};
$('#mx-execution').onsubmit = safe(async e => {
  e.preventDefault();
  await api('/api/settings/execution', { method: 'PUT', body: { browser_limit: Number($('#mx-limit').value), search_account_id: Number($('#mx-search-account').value) || null } });
  toast('Настройки сохранены');
  await loaders.settings();
});
$('#search-form > div').insertAdjacentHTML('afterend', `<label class="block text-sm">Аккаунт поиска<select id="mx-search-select" class="${mxField}" name="account_id" required></select></label>`);
const mxOriginalSearch = loaders.search;
loaders.search = async () => {
  await mxOriginalSearch();
  const [accounts, cfg] = await Promise.all([api('/api/accounts'), api('/api/settings')]);
  const prior = $('#mx-search-select').value || cfg.search_account_id;
  mxOptions($('#mx-search-select'), accounts, 'Выберите аккаунт', prior);
  $('#mx-search-select').insertAdjacentHTML('beforeend', '<option value="all">Все доступные аккаунты — распределить запросы</option>');
  if (prior === 'all') $('#mx-search-select').value = 'all';
};

// Draft preparation lives in draft-preparation.js.

$('#d-status').insertAdjacentHTML('beforebegin', `<select id="d-account-filter" aria-label="Фильтр аккаунта" class="border rounded-lg px-2 py-1.5"><option value="">Все аккаунты</option></select><select id="d-set-filter" aria-label="Фильтр набора" class="border rounded-lg px-2 py-1.5"><option value="">Все наборы</option></select>`);
if (!$('#d-status option[value="review"]')) $('#d-status').insertAdjacentHTML('beforeend', '<option value="review">Требует проверки</option>');
$('#d-account-filter').onchange = safe(() => loaders.drafts());
$('#d-set-filter').onchange = safe(() => loaders.drafts());
const mxOriginalDrafts = loaders.drafts;
$('#ac-active-banner').insertAdjacentHTML('afterend', `<div id="mx-active-tasks" class="space-y-2"></div>`);
loaders.drafts = async opts => {
  await mxOriginalDrafts(opts);
  const [accounts, sets, jobs] = await Promise.all([api('/api/accounts'), api('/api/text-sets'), api('/api/tasks?limit=200')]);
  mxOptions($('#d-account-filter'), accounts, 'Все аккаунты');
  mxOptions($('#d-set-filter'), sets, 'Все наборы');
  $('#ac-active-banner').classList.add('hidden');
  const latest = new Map();
  for (const t of jobs.filter(t => t.kind === 'auto_comment').sort((a, b) => b.id - a.id)) {
    if (!latest.has(t.params.account_id)) latest.set(t.params.account_id, t);
  }
  $('#mx-active-tasks').innerHTML = [...latest.values()].map(t => {
    const account = accounts.find(a => a.id === t.params.account_id);
    const name = account ? `@${account.username}` : `Аккаунт #${t.params.account_id}`;
    const detail = [t.result, t.error].filter(Boolean).join(' · ');
    const stop = ['queued', 'running'].includes(t.status) ? `<button data-mx-cancel="${t.id}" class="${mxButton}">Остановить</button>` : '';
    return `<div class="${mxCard} text-sm">${esc(name)} · задача #${t.id} · ${esc(LABEL[t.status] || t.status)} · ${esc(detail)} ${stop}</div>`;
  }).join('');
};
$('#mx-active-tasks').onclick = safe(async e => {
  const btn = e.target.closest('[data-mx-cancel]');
  if (!btn) return;
  await api(`/api/tasks/${btn.dataset.mxCancel}/cancel`, { method: 'POST' });
  await loaders.drafts();
});
$('#drafts-list').addEventListener('click', safe(async e => {
  const btn = e.target.closest('[data-resolve]');
  if (!btn) return;
  if (!confirm('Подтверждаете результат после ручной проверки комментариев в TikTok?')) return;
  await api(`/api/drafts/${btn.closest('[data-id]').dataset.id}/resolve`, { method: 'POST', body: { published: btn.dataset.resolve === 'true' } });
  await loaders.drafts();
}));
// If this was the restored tab, load its new controls now.
safe(loaders[currentTab])();

$('#ac-form input[value="all"]').closest('label').insertAdjacentHTML('afterend', '<label class="flex items-center gap-2"><input type="radio" name="ac_scope" value="selected">Только отмеченные черновики</label>');
