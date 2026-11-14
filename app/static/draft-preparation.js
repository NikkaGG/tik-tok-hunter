/* Preparation selects and reserves texts; publishing remains a separate action. */
let prepareRevision = 0;
let prepareTimer;
let prepareBusy = false;
let prepareEstimate = null;
let prepareReturnFocus;
document.body.insertAdjacentHTML('beforeend', `<div id="mx-prepare" class="prepare-overlay hidden">
  <section class="prepare-dialog" role="dialog" aria-modal="true" aria-labelledby="prepare-title">
    <header class="prepare-header"><div><h2 id="prepare-title">Создать черновики</h2><p id="mx-prepare-scope" class="prepare-copy"></p></div><button id="mx-prepare-close" aria-label="Закрыть" class="prepare-btn">×</button></header>
    <div class="prepare-body">
      <div id="prepare-selection">
        <fieldset><legend>1. Какие тексты использовать</legend><p class="prepare-copy">Выберите один или несколько наборов.</p><div id="mx-prepare-sets" class="prepare-choices"></div></fieldset>
        <fieldset><legend>2. От каких аккаунтов</legend><p class="prepare-copy">Выбранные тексты будут использованы для каждого отмеченного аккаунта.</p><button type="button" id="prepare-select-accounts" class="prepare-btn">Выбрать все аккаунты</button><div id="mx-prepare-accounts" class="prepare-choices"></div></fieldset>
        <div id="prepare-estimate" role="status" aria-live="polite" class="prepare-note"></div>
      </div>
      <div id="mx-prepare-result" class="prepare-results" role="status" aria-live="polite"></div>
      <p id="prepare-error" role="alert" class="prepare-note warning" hidden></p>
    </div>
    <footer class="prepare-footer"><p>Сначала просмотрите черновики. Отправку вы запускаете отдельно.</p><button id="mx-prepare-submit" class="prepare-btn primary" disabled>Создать черновики</button><button id="mx-open-queue" class="prepare-btn primary hidden">Просмотреть очередь →</button></footer>
  </section>
</div>`);

function prepareSelected(kind) {
  return $$(`#mx-prepare-${kind} input:checked`).map(input => Number(input.value));
}
function prepareIssue(account, sets) {
  const matching = mxSets.filter(s => sets.includes(s.id));
  if (!sets.length) return 'Сначала выберите наборы текстов';
  return matching.map(s => `${s.name} · вариантов: ${s.variants.length}`).join('; ');
}
function prepareChanged() {
  const revision = ++prepareRevision;
  clearTimeout(prepareTimer);
  prepareEstimate = null;
  $('#mx-prepare-submit').disabled = true;
  const sets = prepareSelected('sets');
  const accounts = prepareSelected('accounts');
  for (const account of mxAccounts) {
    const row = $(`[data-prepare-account="${account.id}"]`);
    if (!row) continue;
    $('[data-prepare-note]', row).textContent = prepareIssue(account, sets);
    $('[data-prepare-assign]', row).hidden = true;
  }
  if (!sets.length || !accounts.length) {
    $('#prepare-estimate').className = 'prepare-note';
    $('#prepare-estimate').textContent = !sets.length ? 'Выберите наборы текстов, затем аккаунты.' : 'Выберите аккаунты, для которых создать черновики.';
    return;
  }
  $('#prepare-estimate').textContent = 'Проверяем выбранные ролики и аккаунты…';
  prepareTimer = setTimeout(() => prepareCheck(revision), 180);
}
async function prepareCheck(revision) {
  try {
    const estimate = await api('/api/drafts/preview', {method:'POST', body:{video_ids:mxPrepareVideos,
      account_ids:prepareSelected('accounts'), text_set_ids:prepareSelected('sets')}});
    if (revision !== prepareRevision) return;
    prepareEstimate = estimate;
    $('#mx-prepare-scope').textContent = mxPrepareVideos
      ? `Выбрано роликов: ${estimate.video_count}. По одному комментарию от каждого выбранного аккаунта.`
      : `Роликов со статусом «Подходят»: ${estimate.video_count}. Со всех страниц таблицы.`;
    const blocked = estimate.accounts.filter(a => a.reason);
    const duplicates = estimate.accounts.reduce((sum, a) => sum + a.duplicate_count, 0);
    const messages = [];
    if (blocked.length) messages.push(...blocked.map(a => `@${mxAccounts.find(x => x.id === a.account_id)?.username}: ${a.reason}.`));
    else if (!estimate.video_count) messages.push('Нет подходящих роликов. Отметьте нужные ролики в таблице или присвойте им статус «Подходят».');
    else if (!estimate.candidate_count) messages.push('Для всех выбранных пар ролик + аккаунт уже есть черновик или отправка. Проверьте очередь.');
    else {
      messages.push(`Роликов: ${estimate.video_count}. Аккаунтов: ${estimate.accounts.length}. Можно подготовить до ${estimate.maximum_drafts} черновиков.`);
      messages.push(`Выбрано вариантов: ${estimate.available_variants}. Формулировки распределяются между роликами; при необходимости текст может повториться.`);
      messages.push('Это оценка: изменения в очереди и некорректные тексты могут уменьшить результат.');
    }
    if (duplicates) messages.push(`Уже подготовлено или отправлено: ${duplicates}. Повторно создаваться не будут.`);
    $('#prepare-estimate').textContent = messages.join(' ');
    $('#prepare-estimate').className = 'prepare-note' + (blocked.length || estimate.maximum_drafts < estimate.candidate_count ? ' warning' : '');
    $('#mx-prepare-submit').disabled = prepareBusy || !!blocked.length || !estimate.maximum_drafts;
  } catch (error) {
    if (revision !== prepareRevision) return;
    $('#prepare-estimate').textContent = 'Не удалось проверить подготовку. Измените выбор, чтобы повторить.';
    $('#prepare-error').textContent = error.message;
    $('#prepare-error').hidden = false;
  }
}
async function openPrepare(videoIds) {
  [mxAccounts, mxSets] = await Promise.all([api('/api/accounts'), api('/api/text-sets')]);
  mxPrepareVideos = videoIds;
  prepareReturnFocus = document.activeElement;
  $('#mx-prepare-scope').textContent = videoIds ? `Выбрано роликов: ${videoIds.length}. По одному комментарию от каждого выбранного аккаунта.` : 'Будут использованы все ролики со статусом «Подходят», со всех страниц таблицы.';
  $('#mx-prepare-sets').innerHTML = mxSets.map(s => `<label class="prepare-choice"><input type="checkbox" value="${s.id}"><span class="prepare-choice-main"><strong>${esc(s.name)}</strong><small>Вариантов: ${s.variants.length}</small><small>${esc((s.variants[0]?.body || 'Нет текстов').slice(0, 110))}</small></span></label>`).join('') || '<p class="prepare-copy">Наборов пока нет. Создайте их в разделе «Наборы текстов».</p>';
  $('#mx-prepare-accounts').innerHTML = mxAccounts.filter(a => ['active', 'working'].includes(a.status)).map(a => `<div class="prepare-choice" data-prepare-account="${a.id}"><input id="prepare-account-${a.id}" type="checkbox" value="${a.id}"><div class="prepare-choice-main"><label for="prepare-account-${a.id}"><strong>@${esc(a.username)}</strong></label><small data-prepare-note></small><button data-prepare-assign="${a.id}" class="prepare-fix" hidden>Назначить выбранные наборы этому аккаунту</button></div></div>`).join('') || '<p class="prepare-copy">Нет доступных аккаунтов. Добавьте аккаунт или снимите паузу в аккаунт-менеджере.</p>';
  $('#mx-prepare-result').innerHTML = '';
  $('#prepare-error').hidden = true;
  $('#prepare-selection').hidden = false;
  $('#mx-prepare-submit').classList.remove('hidden');
  $('#mx-open-queue').classList.add('hidden');
  $('#mx-prepare').classList.remove('hidden');
  prepareChanged();
  $('#mx-prepare-close').focus();
}
function closePrepare() {
  if (prepareBusy) return;
  ++prepareRevision;
  clearTimeout(prepareTimer);
  $('#mx-prepare').classList.add('hidden');
  if (prepareReturnFocus?.isConnected) prepareReturnFocus.focus();
}
$('#mx-prepare-close').onclick = closePrepare;
$('#mx-open-queue').onclick = () => { closePrepare(); showTab('drafts'); };
$('#mx-prepare-sets').onchange = prepareChanged;
$('#mx-prepare-accounts').onchange = prepareChanged;
$('#prepare-select-accounts').onclick = () => { $$('#mx-prepare-accounts input').forEach(input => input.checked = true); prepareChanged(); };
$('#mx-prepare-accounts').onclick = async e => {
  const button = e.target.closest('[data-prepare-assign]');
  if (!button || prepareBusy) return;
  const id = Number(button.dataset.prepareAssign);
  const sets = prepareSelected('sets');
  if (!sets.length) return;
  prepareBusy = true;
  $('#prepare-error').hidden = true;
  $('#mx-prepare-submit').disabled = true;
  button.disabled = true;
  try {
    const fresh = (await api('/api/accounts')).find(a => a.id === id);
    const result = await api(`/api/text-sets/accounts/${id}`, {method:'PUT', body:{text_set_ids:[...new Set([...fresh.text_set_ids, ...sets])]}});
    mxAccounts.find(a => a.id === id).text_set_ids = result.text_set_ids;
    toast('Наборы назначены аккаунту');
  } catch (error) {
    $('#prepare-error').textContent = error.message;
    $('#prepare-error').hidden = false;
  } finally {
    prepareBusy = false;
    button.disabled = false;
    prepareChanged();
  }
};
function prepareResult(result) {
  const groups = new Map();
  for (const skip of result.skipped) {
    const key = `${skip.account_id}:${skip.reason}`;
    if (!groups.has(key)) groups.set(key, {...skip, count:0});
    groups.get(key).count++;
  }
  $('#mx-prepare-result').innerHTML = `<div class="prepare-note ${result.created.length ? 'success' : 'warning'}"><strong>Создано: ${result.created.length}. Пропущено: ${result.skipped.length}.</strong><p>${result.created.length ? 'Черновики сохранены в очереди. Просмотрите тексты перед запуском отправки.' : 'Черновики не созданы. Причины указаны ниже.'}</p></div>`
    + [...groups.values()].map(s => `<p class="prepare-note warning">@${esc(mxAccounts.find(a => a.id === s.account_id)?.username || 'удалённый аккаунт')} · пропущено: ${s.count}. ${esc(s.reason)}</p>`).join('')
    + (result.created.length ? '<h3>Подготовленные комментарии</h3>' : '')
    + result.created.slice(0, 8).map(d => `<article class="prepare-preview"><strong>@${esc(mxAccounts.find(a => a.id === d.account_id)?.username)}</strong> → <a href="${esc(d.video_url)}" target="_blank" rel="noopener noreferrer">ролик ${d.video_author ? '@' + esc(d.video_author) : ''}</a><small>Набор: ${esc(mxSets.find(s => s.id === d.text_set_id)?.name || '—')}${d.repeated ? ' · повтор ранее использованного текста' : ''}</small><p>${esc(d.text)}</p></article>`).join('')
    + (result.created.length > 8 ? `<p class="prepare-copy">Показаны первые 8 из ${result.created.length}. Все комментарии доступны в очереди.</p>` : '');
  $('#mx-open-queue').classList.toggle('hidden', !result.created.length);
  $('#prepare-selection').hidden = !!result.created.length;
  $('#mx-prepare-submit').classList.toggle('hidden', !!result.created.length);
}
$('#mx-prepare-submit').onclick = async () => {
  if (prepareBusy || !prepareEstimate || $('#mx-prepare-submit').disabled) return;
  prepareBusy = true;
  ++prepareRevision;
  $('#prepare-error').hidden = true;
  $('#mx-prepare-submit').disabled = true;
  $('#mx-prepare-submit').textContent = 'Создаём черновики…';
  $$('#prepare-selection input, #prepare-selection button').forEach(el => el.disabled = true);
  try {
    const result = await api('/api/drafts/generate', {method:'POST', body:{video_ids:mxPrepareVideos,
      account_ids:prepareSelected('accounts'), text_set_ids:prepareSelected('sets')}});
    prepareResult(result);
    await updateDraftBadge();
    if (result.created.length) $('#mx-open-queue').focus();
  } catch (error) {
    $('#prepare-error').textContent = error.message;
    $('#prepare-error').hidden = false;
  } finally {
    prepareBusy = false;
    $('#mx-prepare-submit').textContent = 'Создать черновики';
    $$('#prepare-selection input, #prepare-selection button').forEach(el => el.disabled = false);
    if (!$('#prepare-selection').hidden) prepareChanged();
  }
};
$('#mx-prepare').addEventListener('click', e => { if (e.target.id === 'mx-prepare') closePrepare(); });
$('#mx-prepare').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); closePrepare(); }
  if (e.key !== 'Tab') return;
  const items = $$('button:not(:disabled),input:not(:disabled),a[href],summary', $('#mx-prepare')).filter(el => el.getClientRects().length);
  if (e.shiftKey && document.activeElement === items[0]) { e.preventDefault(); items.at(-1)?.focus(); }
  else if (!e.shiftKey && document.activeElement === items.at(-1)) { e.preventDefault(); items[0]?.focus(); }
});
