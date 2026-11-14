/* Aggregate account health in the sidebar. No browser jobs are started by rendering. */
let accountMonitorData = null;
let accountMonitorExpanded = false;
let accountMonitorSignature = '';
let monitorSettingsLoaded = false;

function monitorTime(value) {
  if (!value) return 'Ещё не проверялся';
  const date = new Date(value);
  return `Проверка: ${date.toLocaleString('ru-RU', { day:'numeric', month:'short', hour:'2-digit', minute:'2-digit' })}`;
}
function renderAccountMonitor(data) {
  accountMonitorData = data;
  const signature = JSON.stringify([data.accounts, data.interval_minutes, accountMonitorExpanded]);
  if (signature === accountMonitorSignature) return;
  accountMonitorSignature = signature;
  const all = data.accounts;
  const shown = accountMonitorExpanded ? all : all.slice(0, 5);
  const side = $('#side-session');
  const focusId = document.activeElement?.dataset?.monitorAccount;
  const scrollTop = $('.monitor-list', side)?.scrollTop || 0;
  side.innerHTML = `<div class="monitor-heading"><h2>TikTok</h2><span class="monitor-total" title="Всего аккаунтов">${all.length}</span></div>
    ${all.length ? `<div class="monitor-list">${shown.map(account => {
      const description = account.tone !== 'green' ? account.reason : account.activity || monitorTime(account.checked_at);
      const title = [`@${account.username}`, account.reason, account.detail && account.detail !== account.reason ? account.detail : '', monitorTime(account.checked_at), account.activity || '', account.paused ? 'Автоотправка на паузе' : ''].filter(Boolean).join('. ');
      return `<button type="button" class="monitor-account" data-monitor-account="${account.id}" data-tone="${account.tone}" title="${esc(title)}" aria-label="${esc(title)}"><span class="monitor-dot" aria-hidden="true"></span><span class="monitor-account-copy"><span class="monitor-username">@${esc(account.username)}</span><span class="monitor-description">${esc(description)}</span></span></button>`;
    }).join('')}</div>` : '<p class="monitor-empty">Нет подключённых аккаунтов</p><button type="button" data-monitor-add class="monitor-add">Добавить аккаунт</button>'}
    <div class="monitor-footer">${all.length > 5 ? `<button type="button" data-monitor-expand class="monitor-expand" aria-expanded="${accountMonitorExpanded}">${accountMonitorExpanded ? 'Свернуть список' : `Все аккаунты (${all.length})`}</button>` : ''}<button type="button" data-monitor-settings title="Настроить фоновую проверку">${data.interval_minutes ? `Автопроверка · каждые ${data.interval_minutes} мин` : 'Автопроверка выключена'}</button></div>`;
  if ($('.monitor-list', side)) $('.monitor-list', side).scrollTop = scrollTop;
  if (focusId) $(`[data-monitor-account="${focusId}"]`, side)?.focus({ preventScroll:true });
  const tone = data.counts.red ? 'red' : data.counts.yellow ? 'yellow' : 'green';
  const hdr = $('#hdr-session');
  hdr.className = 'monitor-mobile';
  hdr.dataset.tone = all.length ? tone : 'yellow';
  hdr.innerHTML = `<span class="monitor-dot" aria-hidden="true"></span>TikTok · ${data.counts.green}/${all.length}`;
  hdr.title = all.length ? `В порядке: ${data.counts.green} из ${all.length}. Нажмите, чтобы открыть аккаунты` : 'Добавить аккаунт TikTok';
  hdr.setAttribute('aria-label', hdr.title);
  hdr.onclick = () => all.length ? showTab('accounts') : openConnectModal();
}
$('#side-session').addEventListener('click', safe(async e => {
  const accountButton = e.target.closest('[data-monitor-account]');
  if (accountButton) {
    const id = Number(accountButton.dataset.monitorAccount);
    showTab('accounts');
    await loaders.accounts();
    const account = accountsCache.find(a => a.id === id);
    if (!account) return toast('Аккаунт уже удалён', true);
    openAccountModal(account);
    selectAccountEditorTab('session');
    $('#account-editor-tab-session').focus();
  }
  if (e.target.closest('[data-monitor-add]')) openConnectModal();
  if (e.target.closest('[data-monitor-expand]')) {
    accountMonitorExpanded = !accountMonitorExpanded;
    renderAccountMonitor(accountMonitorData);
    $('[data-monitor-expand]')?.focus({ preventScroll:true });
  }
  if (e.target.closest('[data-monitor-settings]')) {
    showTab('settings');
    $('#monitor-settings').scrollIntoView({ behavior:'smooth', block:'center' });
  }
}));

$('#mx-execution').insertAdjacentHTML('afterend', `<form id="monitor-settings" class="${mxCard}"><h2 class="font-semibold">Монитор аккаунтов</h2><p class="monitor-settings-copy">Зелёный — сессия подтверждена, жёлтый — нужна проверка, красный — сессия истекла или отключена. Подробности и время последней проверки — при наведении на аккаунт внизу меню.</p><label class="flex gap-2 items-center text-sm"><input id="monitor-enabled" type="checkbox" checked>Проверять сессии в фоне</label><label class="block text-sm">Интервал в минутах<input id="monitor-interval" type="number" min="1" max="1440" value="15" required class="${mxField} mt-1 max-w-xs"></label><p class="monitor-settings-copy">Фоновые проверки работают, пока запущено приложение. Они ждут текущие задачи аккаунта и не прерывают отправку. Если подтверждение старше двух интервалов, индикатор становится жёлтым. На паузе сессия тоже проверяется. Без автопроверки результат считается устаревшим через 30 минут.</p><button class="${mxButton}">Сохранить проверку</button><span id="monitor-saved" class="text-sm text-slate-500 ml-2" role="status"></span></form>`);
const monitorOriginalSettings = loaders.settings;
loaders.settings = async () => {
  await monitorOriginalSettings();
  if (!monitorSettingsLoaded) {
    const cfg = await api('/api/settings');
    $('#monitor-enabled').checked = cfg.monitor_interval_minutes > 0;
    $('#monitor-interval').disabled = !$('#monitor-enabled').checked;
    $('#monitor-interval').value = cfg.monitor_interval_minutes || 15;
    monitorSettingsLoaded = true;
  }
};
$('#monitor-enabled').onchange = e => {
  $('#monitor-interval').disabled = !e.target.checked;
  $('#monitor-saved').textContent = '';
};
$('#monitor-interval').oninput = () => { $('#monitor-saved').textContent = ''; };
$('#monitor-settings').onsubmit = safe(async e => {
  e.preventDefault();
  const interval_minutes = $('#monitor-enabled').checked ? Number($('#monitor-interval').value) : 0;
  await api('/api/settings/monitor', { method:'PUT', body:{ interval_minutes } });
  $('#monitor-saved').textContent = 'Сохранено';
  await refreshGlobal();
});
safe(refreshGlobal)();
