/* Account creation owns its authorization flow and never needs an existing account. */
let connectTaskId = null;
let connectPollTimer = null;
let connectMethod = 'import';
let connectReturnFocus = null;
LABEL.connect = 'добавление аккаунта';

document.head.insertAdjacentHTML('beforeend', `<style>
.account-auth-overlay { position:fixed; inset:0; z-index:60; display:flex; align-items:center; justify-content:center; padding:1.25rem; background:rgb(15 23 42 / .42); backdrop-filter:blur(3px); }
.account-auth-overlay[hidden] { display:none; }
.account-auth-dialog { width:100%; max-width:36rem; max-height:calc(100dvh - 2.5rem); overflow-y:auto; background:#fff; border-radius:1.25rem; padding:1.75rem; box-shadow:0 24px 80px rgb(15 23 42 / .17); color:#1e293b; }
.account-auth-dialog h2 { font-size:1.4rem; line-height:1.3; font-weight:650; letter-spacing:-.025em; }
.account-auth-copy { color:#64748b; font-size:.875rem; line-height:1.6; margin-top:.6rem; max-width:56ch; }
.account-auth-header { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; }
.account-auth-close { border-radius:.5rem; padding:.35rem .6rem; color:#64748b; font-size:1.25rem; }
.account-auth-close:hover { background:#f1f5f9; }
.account-auth-tabs { display:flex; border-bottom:1px solid #e2e8f0; gap:1.5rem; margin:1.5rem 0 1.25rem; }
.account-auth-tabs button { padding:.75rem 0; color:#64748b; font-size:.875rem; border-bottom:2px solid transparent; }
.account-auth-tabs button[aria-selected=true] { border-color:#4f46e5; color:#4338ca; font-weight:600; }
.account-auth-label { display:block; font-size:.875rem; font-weight:550; margin-bottom:.5rem; }
.account-auth-input { width:100%; min-height:6.5rem; resize:vertical; border:1px solid #cbd5e1; border-radius:.65rem; padding:.8rem; font-family:ui-monospace,monospace; font-size:.8rem; line-height:1.5; }
.account-auth-dialog :is(button,textarea):focus-visible { outline:3px solid #c7d2fe; outline-offset:3px; }
.account-auth-help { font-size:.8rem; line-height:1.7; color:#64748b; margin:.75rem 0 1.5rem; }
.account-auth-help summary { cursor:pointer; color:#475569; }
.account-auth-help ol { list-style:decimal; margin:.6rem 0 .6rem 1.2rem; }
.account-auth-footer { display:flex; align-items:center; justify-content:flex-end; flex-wrap:wrap; gap:.65rem; margin-top:1.5rem; }
.account-auth-primary { padding:.7rem 1.1rem; color:white; background:#4f46e5; border-radius:.65rem; font-size:.875rem; font-weight:550; }
.account-auth-primary:hover { background:#4338ca; }
.account-auth-secondary { padding:.7rem 1rem; color:#475569; border:1px solid #e2e8f0; border-radius:.65rem; font-size:.875rem; }
.account-auth-dialog button:disabled { opacity:.5; cursor:wait; }
.account-auth-status { padding:1rem 0 .25rem; font-size:.95rem; line-height:1.6; }
.account-auth-error { margin-top:1rem; font-size:.875rem; color:#b91c1c; line-height:1.6; }
.account-auth-success { color:#047857; font-weight:550; }
</style>`);
document.body.insertAdjacentHTML('beforeend', `<div id="account-auth" class="account-auth-overlay" hidden>
  <section role="dialog" aria-modal="true" aria-labelledby="account-auth-title" aria-describedby="account-auth-intro" class="account-auth-dialog">
    <header class="account-auth-header"><div><h2 id="account-auth-title">Добавить аккаунт</h2><p id="account-auth-intro" class="account-auth-copy">Войдите в TikTok или перенесите готовую сессию. Имя аккаунта определим автоматически.</p></div><button id="account-auth-close" class="account-auth-close" aria-label="Закрыть окно">×</button></header>
    <form id="account-auth-form">
      <div role="tablist" aria-label="Способ авторизации" class="account-auth-tabs">
        <button type="button" id="account-auth-import-tab" role="tab" aria-selected="true" aria-controls="account-auth-import">Импорт сессии</button>
        <button type="button" id="account-auth-browser-tab" role="tab" aria-selected="false" aria-controls="account-auth-browser" tabindex="-1">Войти в браузере</button>
      </div>
      <div id="account-auth-import" role="tabpanel" aria-labelledby="account-auth-import-tab">
        <label for="account-auth-cookies" class="account-auth-label">Sessionid или экспорт cookies</label>
        <textarea id="account-auth-cookies" class="account-auth-input" placeholder="Вставьте сессию TikTok" autocomplete="off" spellcheck="false" maxlength="200000"></textarea>
        <p class="account-auth-copy">Можно вставить sessionid. Если TikTok не принимает его отдельно, перенесите все cookies — они сохранят данные сессии и её региона.</p>
        <details class="account-auth-help"><summary>Как перенести сессию из браузера</summary><ol><li>Войдите в нужный аккаунт на tiktok.com в обычном браузере.</li><li>Для одного sessionid: F12 → Application → Cookies → https://www.tiktok.com → скопируйте полное значение sessionid.</li><li>Для всех cookies: F12 → Network → обновите страницу → выберите запрос к www.tiktok.com → Headers → Request Headers → скопируйте целиком значение Cookie.</li><li>Вставьте скопированное в поле выше. Уберите фильтр sessionid, если делаете полный JSON-экспорт cookies.</li></ol><p>Также подходит JSON-экспорт cookies или файл cookies.txt. Данные сессии дают доступ к аккаунту — не отправляйте их в чат.</p></details>
      </div>
      <div id="account-auth-browser" role="tabpanel" aria-labelledby="account-auth-browser-tab" hidden>
        <p class="account-auth-copy">Откроется отдельное окно TikTok. Войдите в нужный аккаунт — после входа он появится в списке.</p>
        <p class="account-auth-help">Используйте почту или телефон. Для входа через Google перенесите сессию из обычного браузера на вкладке «Импорт сессии».</p>
      </div>
      <div class="account-auth-footer"><button type="button" id="account-auth-back" class="account-auth-secondary">Отмена</button><button type="submit" id="account-auth-submit" class="account-auth-primary">Проверить и добавить</button></div>
    </form>
    <div id="account-auth-progress" hidden><p id="account-auth-progress-text" role="status" class="account-auth-status"></p><p class="account-auth-copy">Это окно можно свернуть. Подключение продолжится в фоне.</p><div class="account-auth-footer"><button id="account-auth-cancel" class="account-auth-secondary">Отменить подключение</button></div></div>
    <div id="account-auth-success" hidden><p id="account-auth-success-text" role="status" class="account-auth-status account-auth-success"></p><div class="account-auth-footer"><button id="account-auth-another" class="account-auth-secondary">Добавить ещё</button><button id="account-auth-done" class="account-auth-primary">К аккаунтам</button></div></div>
    <p id="account-auth-error" role="alert" class="account-auth-error" hidden></p>
  </section>
</div>`);

function selectConnectMethod(method) {
  connectMethod = method;
  $('#account-auth-import').hidden = method !== 'import';
  $('#account-auth-browser').hidden = method !== 'login';
  for (const [id, selected] of [['import', method === 'import'], ['browser', method === 'login']]) {
    const tab = $(`#account-auth-${id}-tab`);
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  $('#account-auth-submit').textContent = method === 'import' ? 'Проверить и добавить' : 'Открыть окно TikTok';
}
function resetConnectForm() {
  $('#account-auth-form').hidden = false;
  $('#account-auth-progress').hidden = true;
  $('#account-auth-success').hidden = true;
  $('#account-auth-error').hidden = true;
  $('#account-auth-cookies').value = '';
  $('#account-auth-submit').disabled = false;
  selectConnectMethod('import');
}
function openConnectModal() {
  connectReturnFocus = document.activeElement;
  if (!connectTaskId) resetConnectForm();
  $('#account-auth').hidden = false;
  (connectTaskId ? $('#account-auth-close') : $('#account-auth-cookies')).focus();
}
function closeConnectModal() {
  $('#account-auth').hidden = true;
  if (!connectTaskId) $('#account-auth-cookies').value = '';
  if (connectReturnFocus?.isConnected) connectReturnFocus.focus();
}
function connectError(message) {
  $('#account-auth-error').textContent = message;
  $('#account-auth-error').hidden = false;
}
$('#account-auth-import-tab').onclick = () => selectConnectMethod('import');
$('#account-auth-browser-tab').onclick = () => selectConnectMethod('login');
$('.account-auth-tabs').onkeydown = e => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  e.preventDefault();
  selectConnectMethod(e.key === 'Home' ? 'import' : e.key === 'End' ? 'login' : connectMethod === 'import' ? 'login' : 'import');
  $(`#account-auth-${connectMethod === 'import' ? 'import' : 'browser'}-tab`).focus();
};
$('#account-auth-close').onclick = closeConnectModal;
$('#account-auth-back').onclick = closeConnectModal;
$('#account-auth-another').onclick = () => { resetConnectForm(); $('#account-auth-cookies').focus(); };
$('#account-auth-done').onclick = () => { closeConnectModal(); showTab('accounts'); };
$('#account-auth').addEventListener('click', e => { if (e.target.id === 'account-auth') closeConnectModal(); });
$('#account-auth').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); closeConnectModal(); }
  if (e.key === 'Tab') {
    const focusable = $$('button:not(:disabled),textarea:not(:disabled),summary', $('#account-auth')).filter(el => el.getClientRects().length && el.tabIndex >= 0);
    if (!focusable.length) return;
    if (e.shiftKey && document.activeElement === focusable[0]) { e.preventDefault(); focusable.at(-1).focus(); }
    else if (!e.shiftKey && document.activeElement === focusable.at(-1)) { e.preventDefault(); focusable[0].focus(); }
  }
});
async function pollConnectTask() {
  if (!connectTaskId) return;
  try {
    const task = await api(`/api/tasks/${connectTaskId}`);
    if (['queued', 'running'].includes(task.status)) {
      $('#account-auth-progress-text').textContent = task.status === 'queued' ? 'Ожидаем свободное окно браузера…' : connectMethod === 'login' ? 'Войдите в TikTok в открывшемся окне…' : 'Проверяем сессию и определяем аккаунт…';
      connectPollTimer = setTimeout(pollConnectTask, 800);
      return;
    }
    connectTaskId = null;
    sessionStorage.removeItem('ttlh-connect-task');
    $('#account-auth-progress').hidden = true;
    $('#account-auth-error').hidden = true;
    if (task.status === 'done') {
      $('#account-auth-cookies').value = '';
      $('#account-auth-success-text').textContent = task.result || 'Аккаунт добавлен';
      $('#account-auth-success').hidden = false;
      if ($('#account-auth').hidden) toast(task.result || 'Аккаунт добавлен');
      await Promise.all([loaders.accounts(), refreshGlobal()]);
      if (currentTab === 'settings') { mxExecutionLoaded = false; await loaders.settings(); }
      if (!$('#account-auth').hidden) $('#account-auth-done').focus();
    } else {
      $('#account-auth-form').hidden = false;
      $('#account-auth-submit').disabled = false;
      connectError(task.status === 'cancelled' ? 'Подключение отменено. Можно попробовать ещё раз.' : task.error || 'Не удалось подключить аккаунт');
    }
  } catch {
    connectError('Не удалось получить результат. Повторяем проверку подключения…');
    if (connectTaskId) connectPollTimer = setTimeout(pollConnectTask, 1500);
  }
}
$('#account-auth-form').onsubmit = async e => {
  e.preventDefault();
  if (connectTaskId) return;
  $('#account-auth-error').hidden = true;
  const data = $('#account-auth-cookies').value.trim();
  if (connectMethod === 'import' && !data) {
    connectError('Вставьте sessionid или экспорт cookies');
    $('#account-auth-cookies').focus();
    return;
  }
  $('#account-auth-submit').disabled = true;
  try {
    const task = await api('/api/accounts/connect', { method: 'POST', body: { method: connectMethod, ...(connectMethod === 'import' ? { data } : {}) } });
    connectTaskId = task.id;
    sessionStorage.setItem('ttlh-connect-task', JSON.stringify({ id: task.id, method: connectMethod }));
    $('#account-auth-form').hidden = true;
    $('#account-auth-progress').hidden = false;
    $('#account-auth-progress-text').textContent = 'Подключаем аккаунт…';
    $('#account-auth-cancel').focus();
    await pollConnectTask();
  } catch (error) {
    connectError(error.message);
    $('#account-auth-submit').disabled = false;
  }
};
$('#account-auth-cancel').onclick = async () => {
  if (!connectTaskId) return;
  try {
    await api(`/api/tasks/${connectTaskId}/cancel`, { method: 'POST' });
    clearTimeout(connectPollTimer);
    await pollConnectTask();
  } catch (error) { connectError(error.message); }
};
try {
  const saved = JSON.parse(sessionStorage.getItem('ttlh-connect-task'));
  if (saved?.id) {
    connectTaskId = Number(saved.id);
    connectMethod = saved.method === 'login' ? 'login' : 'import';
    $('#account-auth-form').hidden = true;
    $('#account-auth-progress').hidden = false;
    pollConnectTask();
  }
} catch { sessionStorage.removeItem('ttlh-connect-task'); }
