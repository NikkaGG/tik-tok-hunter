/* Account-manager behavior; authorization for new accounts stays in account-auth.js. */
let accountEditorId = null;
let accountEditorTab = 'general';
let accountEditorLoading = false;
let accountEditorReturnFocus = null;
let accountSetsLoaded = false;
let accountListGeneration = 0;

function sessionCheckedText(value) {
  if (!value) return 'Ещё не проверялась';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Время проверки неизвестно';
  const time = date.toLocaleTimeString('ru-RU', { hour:'2-digit', minute:'2-digit' });
  return date.toDateString() === new Date().toDateString() ? `Проверена сегодня, ${time}` : `Проверена ${date.toLocaleDateString('ru-RU', { day:'numeric', month:'short' })}, ${time}`;
}
function accountSessionCell(account) {
  let label, tone, caption;
  if (account.session_task_id || accountCheckRequests.has(account.id)) {
    label = account.session_task_kind === 'login' ? 'Вход в аккаунт' : account.session_task_status === 'queued' ? 'Проверка в очереди' : 'Проверяем…';
    tone = 'checking';
    caption = account.session_task_kind === 'login' ? 'Завершите вход в окне TikTok' : account.session_task_status === 'queued' ? 'Начнётся после текущих задач' : 'Получаем ответ TikTok';
  } else {
    const states = {
      active: ['Активна', 'active'], expired: ['Сессия истекла', 'error'],
      error: ['Ошибка проверки', 'warning'], unchecked: ['Не проверена', 'warning'], missing: ['Не подключена', 'missing'],
    };
    [label, tone] = states[account.session_status] || states.unchecked;
    caption = account.session_status === 'missing' ? 'Подключите в настройках аккаунта' : sessionCheckedText(account.session_checked_at);
    if (account.session_status === 'error') caption = caption.replace('Проверена', 'Попытка проверки');
  }
  const error = !account.session_task_id && ['error', 'expired'].includes(account.session_status) && account.session_error;
  return `<div class="acc-session-badge acc-session-${tone}" role="status">${esc(label)}</div><div class="acc-session-time" title="${esc(fmtDate(account.session_checked_at))}">${esc(caption)}</div>${error ? `<span class="acc-session-error-text">${esc(account.session_error)}</span>` : ''}`;
}
function accountSetsCell(account) {
  const sets = accountSetsCache.filter(set => account.text_set_ids.includes(set.id));
  if (!sets.length) return `<button type="button" data-acc-sets="${account.id}" class="acc-assign-sets">Назначить наборы</button>`;
  return `<button type="button" data-acc-sets="${account.id}" class="acc-set-chips" aria-label="Настроить наборы @${esc(account.username)}">${sets.slice(0, 2).map(set => `<span class="acc-set-chip" title="${esc(set.name)}">${esc(set.name)}</span>`).join('')}${sets.length > 2 ? `<span class="acc-set-chip">+${sets.length - 2}</span>` : ''}</button>`;
}
function renderAccountOverview() {
  const checking = accountsCache.filter(a => a.session_task_id || accountCheckRequests.has(a.id)).length;
  const active = accountsCache.filter(a => a.session_status === 'active' && !a.session_task_id && !accountCheckRequests.has(a.id)).length;
  const attention = accountsCache.filter(a => ['error', 'expired', 'unchecked', 'missing'].includes(a.session_status) && !a.session_task_id && !accountCheckRequests.has(a.id)).length;
  const n = accountsCache.length;
  const noun = n % 100 >= 11 && n % 100 <= 14 ? 'аккаунтов' : n % 10 === 1 ? 'аккаунт' : [2,3,4].includes(n % 10) ? 'аккаунта' : 'аккаунтов';
  $('#acc-overview').innerHTML = `<div><strong>${n}</strong><span>${noun}</span></div><div><strong>${active}</strong><span>с активной сессией</span></div><div><strong>${attention}</strong><span>требуют внимания</span></div>${checking ? `<div><strong>${checking}</strong><span>проверяются</span></div>` : ''}`;
  const filterCount = Number(accStateFilter !== 'all') + Number(Boolean(accGroupFilter)) + Number(Boolean(accProxyFilter)) + Number(Boolean(accLimitFilter)) + Number(accOnlySession);
  $('#acc-filter-count').textContent = filterCount ? ` · ${filterCount}` : '';
}
loaders.accounts = async (opts = {}) => {
  const generation = ++accountListGeneration;
  const [accounts, sets] = await Promise.all([
    api('/api/accounts'), (!opts.silent || !accountSetsLoaded) ? api('/api/text-sets') : Promise.resolve(accountSetsCache),
  ]);
  if (generation !== accountListGeneration) return;
  const previousHealth = JSON.stringify(accountsCache.map(a => [a.id,a.session_status,a.session_checked_at,a.session_task_id,a.is_current_session]));
  const nextHealth = JSON.stringify(accounts.map(a => [a.id,a.session_status,a.session_checked_at,a.session_task_id,a.is_current_session]));
  accountsCache = accounts;
  accountSetsCache = sets;
  accountSetsLoaded = true;
  const focus = document.activeElement;
  const row = focus?.closest('#accounts-body [data-id]');
  const attribute = ['data-acc-check-session', 'data-acc-edit', 'data-acc-sets', 'data-acc-check'].find(a => focus?.hasAttribute(a));
  const id = row?.dataset.id;
  renderAccountsView();
  if (id && attribute) $(`#accounts-body tr[data-id="${id}"] [${attribute}]`)?.focus({ preventScroll:true });
  refreshAccountEditorSession();
  if (previousHealth !== nextHealth || !opts.silent) await refreshGlobal();
};
async function checkAccountSessions(ids) {
  const unique = [...new Set(ids)].filter(id => !accountCheckRequests.has(id) && !accountsCache.find(a => a.id === id)?.session_task_id);
  if (!unique.length) return;
  unique.forEach(id => accountCheckRequests.add(id));
  renderAccountsView();
  const failed = [];
  try {
    for (const id of unique) {
      try { await api(`/api/accounts/${id}/session/check`, { method:'POST' }); }
      catch (error) { failed.push(`@${accountsCache.find(a => a.id === id)?.username || id}: ${error.message}`); }
    }
  } finally {
    unique.forEach(id => accountCheckRequests.delete(id));
    await loaders.accounts({ silent:true });
  }
  if (failed.length) toast(failed.join('; '), true);
}
$('#accounts-body').addEventListener('click', safe(async e => {
  const check = e.target.closest('[data-acc-check-session]');
  if (check && !check.disabled) return checkAccountSessions([Number(check.dataset.accCheckSession)]);
  const sets = e.target.closest('[data-acc-sets]');
  if (sets) {
    const account = accountsCache.find(a => a.id === Number(sets.dataset.accSets));
    openAccountModal(account);
    selectAccountEditorTab('sets');
  }
  if (e.target.closest('[data-acc-add-empty]')) openConnectModal();
  if (e.target.closest('[data-acc-reset]')) $('#btn-acc-reset-filters').click();
}));
$('#acc-session-filter').onchange = e => { accSessionFilter = e.target.value; accPage = 1; renderAccountsView(); };
$('#btn-acc-check-all').onclick = safe(() => checkAccountSessions(accountsCache.map(a => a.id)));
$('#acc-clear-selection').onclick = () => { accSelectedIds.clear(); renderAccountsView(); };
$('#acc-bulk-action').onchange = e => {
  const action = e.target.value;
  e.target.value = '';
  if (!action) return;
  // Reuse the existing bulk endpoint handlers without a permanent row of action buttons.
  const button = document.createElement('button');
  button.type = 'button';
  button.hidden = true;
  button.dataset.accBulk = action;
  $('#acc-bulk-bar').append(button);
  button.click();
  button.remove();
};

// One account editor: general settings, text assignments and session controls.
const accountEditorForm = $('#account-modal-form');
const accountEditorDialog = $('#modal-account > div');
accountEditorDialog.classList.add('acc-editor-dialog');
$('#acc-modal-title').nextElementSibling.textContent = 'Параметры, наборы текстов и подключение этого аккаунта';
const accountEditorFooter = accountEditorForm.lastElementChild;
accountEditorFooter.id = 'account-editor-footer';
const generalSection = document.createElement('div');
generalSection.id = 'account-editor-general';
generalSection.className = 'acc-editor-section';
generalSection.setAttribute('role', 'tabpanel');
generalSection.setAttribute('aria-labelledby', 'account-editor-tab-general');
[...accountEditorForm.children].filter(el => el !== accountEditorFooter && el.tagName !== 'INPUT').forEach(el => generalSection.append(el));
accountEditorForm.insertBefore(generalSection, accountEditorFooter);
accountEditorForm.insertAdjacentHTML('beforebegin', `<div class="acc-editor-tabs" role="tablist" aria-label="Настройки аккаунта">${[['general','Общее'],['sets','Наборы текстов'],['session','Сессия']].map(([id,label]) => `<button type="button" id="account-editor-tab-${id}" role="tab" aria-controls="account-editor-${id}" aria-selected="${id === 'general'}" tabindex="${id === 'general' ? 0 : -1}" data-account-editor-tab="${id}">${label}</button>`).join('')}</div>`);
accountEditorFooter.insertAdjacentHTML('beforebegin', `
  <div id="account-editor-sets" class="acc-editor-section" role="tabpanel" aria-labelledby="account-editor-tab-sets" hidden><p class="acc-editor-help">Аккаунт будет брать тексты из выбранных наборов. Один набор можно назначить нескольким аккаунтам.</p><div id="account-editor-set-list" class="space-y-2"></div></div>
  <div id="account-editor-session" class="acc-editor-section" role="tabpanel" aria-labelledby="account-editor-tab-session" hidden>
    <div id="account-editor-session-status" class="acc-editor-session" aria-live="polite"></div>
    <div class="acc-editor-actions"><button type="button" data-account-session="check" class="btn btn-ghost">Проверить сессию</button><button type="button" data-account-session="login" class="btn btn-ghost">Войти заново</button></div>
    <p class="acc-editor-help">Проверка обращается к TikTok и подтверждает, что сессия действует и принадлежит этому аккаунту.</p>
    <details id="account-editor-import"><summary class="acc-assign-sets cursor-pointer">Заменить сессию вручную</summary><label for="account-editor-cookies" class="label mt-3">Sessionid или экспорт cookies</label><textarea id="account-editor-cookies" rows="3" class="field mt-2" autocomplete="off" spellcheck="false" maxlength="200000"></textarea><button type="button" data-account-session="import" class="btn btn-ghost mt-3">Заменить и проверить</button></details>
    <div class="acc-editor-danger"><button type="button" data-account-session="disconnect" class="btn btn-quiet !text-rose-600">Отключить аккаунт</button><p>Сохранённые тексты и история отправок останутся.</p></div>
  </div>
  <p id="account-editor-error" class="acc-editor-error" role="alert" hidden></p>`);
// Proxy health is determined by its check, rather than typed as a status label.
accountEditorForm.elements.proxy_status.closest('label').hidden = true;
accountEditorForm.elements.proxy_status.closest('.grid').classList.remove('sm:grid-cols-2');
accountEditorForm.elements.proxy_label.placeholder = 'Необязательная подпись';
accountEditorForm.elements.proxy.placeholder = 'Пусто — использовать общий прокси';
accountEditorForm.elements.status.closest('label').firstChild.textContent = 'Режим аккаунта';
accountEditorForm.elements.status.querySelector('option[value=working]')?.remove();
for (const option of accountEditorForm.elements.status.options) {
  option.textContent = ({active:'Активен', working:'Активен (в работе)', paused:'На паузе', warning:'Требует внимания', banned:'Заблокирован'})[option.value];
}
generalSection.insertAdjacentHTML('beforeend', `<div class="acc-editor-actions"><button id="account-use-for-search" type="button" class="btn btn-ghost">Использовать для поиска</button><span id="account-search-description" class="acc-editor-help"></span></div>`);
function selectAccountEditorTab(tab) {
  accountEditorTab = tab;
  for (const key of ['general', 'sets', 'session']) {
    $(`#account-editor-${key}`).hidden = key !== tab;
    const button = $(`#account-editor-tab-${key}`);
    button.setAttribute('aria-selected', String(key === tab));
    button.tabIndex = key === tab ? 0 : -1;
  }
  accountEditorFooter.hidden = tab === 'session';
}
$('.acc-editor-tabs').addEventListener('click', e => {
  const button = e.target.closest('[data-account-editor-tab]');
  if (button) selectAccountEditorTab(button.dataset.accountEditorTab);
});
$('.acc-editor-tabs').addEventListener('keydown', e => {
  if (!['ArrowRight','ArrowLeft','Home','End'].includes(e.key)) return;
  e.preventDefault();
  const tabs = ['general','sets','session'];
  const index = tabs.indexOf(accountEditorTab);
  selectAccountEditorTab(e.key === 'Home' ? tabs[0] : e.key === 'End' ? tabs[2] : tabs[(index + (e.key === 'ArrowRight' ? 1 : 2)) % 3]);
  $(`#account-editor-tab-${accountEditorTab}`).focus();
});
function populateAccountEditorSets(account) {
  $('#account-editor-set-list').innerHTML = accountSetsCache.map(set => `<label class="acc-editor-set"><input type="checkbox" data-account-set value="${set.id}" ${account.text_set_ids.includes(set.id) ? 'checked' : ''}><span>${esc(set.name)}<small>Вариантов текста: ${set.variants.length}</small></span></label>`).join('') || '<p class="acc-editor-help">Наборов пока нет. Создайте их в разделе «Наборы текстов».</p>';
}
async function setupAccountEditor(account) {
  accountEditorReturnFocus = document.activeElement;
  accountEditorId = account.id;
  $('#account-editor-error').hidden = true;
  $('#account-editor-cookies').value = '';
  $('#account-editor-import').open = false;
  selectAccountEditorTab('general');
  refreshAccountEditorSession();
  accountEditorLoading = true;
  $('button[type=submit]', accountEditorForm).disabled = true;
  $('#account-editor-set-list').textContent = 'Загружаем наборы…';
  try {
    const sets = await api('/api/text-sets');
    if (accountEditorId !== account.id) return;
    accountSetsCache = sets;
    accountSetsLoaded = true;
    populateAccountEditorSets(account);
  } catch (error) {
    if (accountEditorId !== account.id) return;
    accountSetsLoaded = false;
    $('#account-editor-error').textContent = error.message;
    $('#account-editor-error').hidden = false;
  } finally {
    if (accountEditorId === account.id) {
      accountEditorLoading = false;
      $('button[type=submit]', accountEditorForm).disabled = !accountSetsLoaded;
    }
  }
}
function restoreAccountEditorFocus() {
  accountEditorId = null;
  if (accountEditorReturnFocus?.isConnected) accountEditorReturnFocus.focus();
}
function refreshAccountEditorSession() {
  const account = accountsCache.find(a => a.id === accountEditorId);
  if (!account) return;
  $('#account-editor-session-status').innerHTML = accountSessionCell(account);
  const busy = Boolean(account.current_task_id);
  $$('[data-account-session]', accountEditorForm).forEach(button => {
    button.disabled = button.dataset.accountSession === 'check' ? Boolean(account.session_task_id || accountCheckRequests.has(account.id)) : busy;
  });
  const current = account.is_current_session;
  $('#account-use-for-search').hidden = current;
  $('#account-search-description').textContent = current ? 'Этот аккаунт используется для поиска по умолчанию.' : 'Сейчас для поиска используется другой аккаунт.';
}
accountEditorForm.addEventListener('click', async e => {
  const button = e.target.closest('[data-account-session]');
  if (!button || button.disabled || !accountEditorId) return;
  const id = accountEditorId;
  const action = button.dataset.accountSession;
  $('#account-editor-error').hidden = true;
  button.disabled = true;
  try {
    if (action === 'check') await checkAccountSessions([id]);
    else if (action === 'disconnect') {
      if (!confirm('Отключить сессию этого аккаунта?')) return;
      await api(`/api/accounts/${id}/session`, { method:'DELETE' });
      await loaders.accounts({ silent:true });
    } else {
      const body = action === 'import' ? { data:$('#account-editor-cookies').value.trim() } : undefined;
      if (action === 'import' && !body.data) throw new Error('Вставьте sessionid или экспорт cookies');
      await api(`/api/accounts/${id}/session/${action}`, { method:'POST', body });
      if (action === 'import') $('#account-editor-cookies').value = '';
      await loaders.accounts({ silent:true });
    }
  } catch (error) {
    $('#account-editor-error').textContent = error.message;
    $('#account-editor-error').hidden = false;
  } finally { refreshAccountEditorSession(); }
});
$('#account-use-for-search').onclick = async () => {
  try {
    const cfg = await api('/api/settings');
    await api('/api/settings/execution', { method:'PUT', body:{ browser_limit:cfg.browser_limit, search_account_id:accountEditorId } });
    mxExecutionLoaded = false;
    await Promise.all([loaders.accounts({ silent:true }), refreshGlobal()]);
    toast('Аккаунт выбран для поиска');
  } catch (error) { toast(error.message, true); }
};
$('#modal-account').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); closeAccountModal(); }
  if (e.key === 'Tab') {
    const items = $$('button:not(:disabled),input:not([type=hidden]),textarea,select,summary', $('#modal-account')).filter(el => el.getClientRects().length && el.tabIndex >= 0);
    if (e.shiftKey && document.activeElement === items[0]) { e.preventDefault(); items.at(-1)?.focus(); }
    else if (!e.shiftKey && document.activeElement === items.at(-1)) { e.preventDefault(); items[0]?.focus(); }
  }
});
// Poll local metadata only. TikTok is contacted solely by an explicit check action.
let accountPollBusy = false;
setInterval(async () => {
  if (document.hidden || currentTab !== 'accounts' || accountPollBusy) return;
  accountPollBusy = true;
  try { await loaders.accounts({ silent:true }); } catch { /* Keep the last known state until the next refresh. */ }
  finally { accountPollBusy = false; }
}, 2000);
if (currentTab === 'accounts') safe(loaders.accounts)();
