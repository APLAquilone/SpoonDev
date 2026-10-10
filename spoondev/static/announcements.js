/* Administrator announcements and the shared notice area for actual user roles. */
(() => {
  'use strict';
  const actor = window.spoondevAccount;
  const recipient = document.getElementById('announcements');
  const panel = document.getElementById('announcements-admin-panel');
  if (!actor || !recipient || !panel) return;

  const importanceLabels = {info: 'お知らせ', important: '重要', urgent: '緊急'};
  const today = () => {
    const parts = new Intl.DateTimeFormat('en', {
      timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit'
    }).formatToParts(new Date());
    const part = kind => parts.find(item => item.type === kind)?.value;
    return [part('year'), part('month'), part('day')].join('-');
  };
  function notices(result) {
    const values = Array.isArray(result) ? result : result?.announcements;
    if (!Array.isArray(values)) throw Error('お知らせを読み込めませんでした。');
    return values;
  }
  function dateLabel(value) {
    return /^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))
      ? value.replaceAll('-', '/') : '日付未設定';
  }
  function badge(importance) {
    const kind = Object.hasOwn(importanceLabels, importance) ? importance : 'info';
    return node('span', importanceLabels[kind], 'announcement-badge announcement-' + kind);
  }
  function noticeCard(item, managed = false) {
    const article = node('article', undefined, 'announcement-item');
    const meta = node('div', undefined, 'announcement-meta');
    const time = node('time', dateLabel(item.date));
    time.dateTime = item.date || '';
    meta.append(time, badge(item.importance));
    if (managed && item.date > today()) meta.append(node('span', '予約', 'announcement-scheduled'));
    const body = node('p', item.content || '', 'announcement-content');
    article.append(meta, body);
    if (managed) {
      const actions = node('div', undefined, 'announcement-actions');
      const edit = node('button', '編集');
      edit.type = 'button';
      edit.setAttribute('aria-label', dateLabel(item.date) + 'のお知らせを編集');
      edit.onclick = () => editNotice(item);
      const remove = node('button', '削除', 'danger');
      remove.type = 'button';
      remove.setAttribute('aria-label', dateLabel(item.date) + 'のお知らせを削除');
      remove.onclick = async () => {
        if (!confirm(dateLabel(item.date) + 'のお知らせを削除しますか？利用者画面からも削除されます。')) return;
        remove.disabled = true;
        edit.disabled = true;
        try {
          await postAccount('/api/admin/announcements/delete', {id: item.id, confirm: true});
          setStatus('お知らせを削除しました。');
          if (String(editingId) === String(item.id)) clearEditor();
          await loadAdmin();
        } catch (error) {
          setStatus(error.message, true);
        } finally {
          remove.disabled = false;
          edit.disabled = false;
        }
      };
      actions.append(edit, remove);
      article.append(actions);
    }
    return article;
  }

  let recipientBusy = false;
  let recipientVersion = '';
  async function loadRecipient() {
    if (actor.role !== 'user' || recipientBusy) return;
    recipientBusy = true;
    try {
      const items = notices(await api('/api/announcements'));
      const version = JSON.stringify(items);
      if (version === recipientVersion) return;
      recipientVersion = version;
      recipient.replaceChildren();
      recipient.hidden = items.length === 0;
      if (!items.length) return;
      const heading = node('div', undefined, 'announcements-heading');
      heading.append(node('h2', 'お知らせ'), node('span', '運営から', 'announcement-from'));
      recipient.append(heading);
      for (const item of items.slice(0, 2)) recipient.append(noticeCard(item));
      if (items.length > 2) {
        const older = node('details', undefined, 'announcements-older');
        older.append(node('summary', 'ほかのお知らせ（' + (items.length - 2) + '件）'));
        for (const item of items.slice(2)) older.append(noticeCard(item));
        recipient.append(older);
      }
    } catch (error) {
      // Keep already rendered notices during a temporary connection failure.
      let status = document.getElementById('announcement-load-error');
      if (!status) {
        status = node('p', undefined, 'announcement-load-error');
        status.id = 'announcement-load-error';
        status.setAttribute('role', 'status');
        const retry = node('button', '再読み込み', 'home-action');
        retry.type = 'button';
        retry.onclick = loadRecipient;
        status.append(node('span', 'お知らせを取得できませんでした。 '), retry);
        recipient.append(status);
      }
      recipient.hidden = false;
      recipientVersion = '';
    } finally {
      recipientBusy = false;
    }
  }

  let editingId = null;
  let adminSeq = 0;
  let adminLoaded = false;
  const form = document.getElementById('announcement-form');
  const dateInput = document.getElementById('announcement-date');
  const importanceInput = document.getElementById('announcement-importance');
  const contentInput = document.getElementById('announcement-content');
  const saveButton = document.getElementById('announcement-save');
  const cancelButton = document.getElementById('announcement-cancel');
  const list = document.getElementById('announcement-admin-list');
  const status = document.getElementById('announcement-admin-status');
  const editorTitle = document.getElementById('announcement-editor-title');

  function setStatus(message, error = false) {
    status.textContent = message;
    status.className = error ? 'error' : 'setting-status';
    status.setAttribute('role', error ? 'alert' : 'status');
  }
  function updateSaveLabel() {
    saveButton.textContent = editingId !== null ? '変更を保存' : dateInput.value > today() ? '予約する' : '公開する';
  }
  function clearEditor() {
    editingId = null;
    editorTitle.textContent = '新しいお知らせ';
    dateInput.value = today();
    importanceInput.value = 'info';
    contentInput.value = '';
    cancelButton.hidden = true;
    updateSaveLabel();
  }
  function editNotice(item) {
    editingId = item.id;
    editorTitle.textContent = 'お知らせを編集';
    dateInput.value = item.date;
    importanceInput.value = Object.hasOwn(importanceLabels, item.importance) ? item.importance : 'info';
    contentInput.value = item.content || '';
    cancelButton.hidden = false;
    updateSaveLabel();
    setStatus('日付・重要度・内容を変更して保存してください。');
    editorTitle.scrollIntoView({block: 'start', behavior: 'smooth'});
    contentInput.focus({preventScroll: true});
  }
  async function loadAdmin() {
    if (actor.role !== 'admin') return;
    const seq = ++adminSeq;
    if (!adminLoaded) list.replaceChildren(node('p', 'お知らせを読み込み中…', 'hint'));
    list.setAttribute('aria-busy', 'true');
    try {
      const items = notices(await api('/api/admin/announcements'));
      if (seq !== adminSeq) return;
      adminLoaded = true;
      list.replaceChildren();
      if (!items.length) list.append(node('p', 'お知らせはまだありません。上のフォームから作成できます。', 'empty'));
      else for (const item of items) list.append(noticeCard(item, true));
    } catch (error) {
      if (seq !== adminSeq) return;
      setStatus(error.message, true);
      if (!adminLoaded) list.replaceChildren(node('p', '一覧を取得できませんでした。「一覧を更新」で再試行できます。', 'hint'));
    } finally {
      if (seq === adminSeq) list.setAttribute('aria-busy', 'false');
    }
  }

  window.spoonAnnouncements = {loadAdmin, loadRecipient};
  if (actor.role === 'admin') {
    clearEditor();
    document.getElementById('announcements-admin-nav').hidden = false;
    document.getElementById('announcements-admin-nav').onclick = () => switchView('announcements-admin');
    document.getElementById('announcement-admin-refresh').onclick = loadAdmin;
    dateInput.onchange = updateSaveLabel;
    cancelButton.onclick = () => {clearEditor(); setStatus(''); dateInput.focus();};
    form.onsubmit = async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const payload = {date: dateInput.value, importance: importanceInput.value, content: contentInput.value.trim()};
      if (!payload.content) {setStatus('内容を入力してください。', true); contentInput.focus(); return;}
      if (editingId !== null) payload.id = editingId;
      const action = editingId !== null ? '変更を保存しました。' : payload.date > today() ? 'お知らせを予約しました。' : 'お知らせを公開しました。';
      const controls = [...form.querySelectorAll('input,select,textarea,button')];
      for (const control of controls) control.disabled = true;
      setStatus('保存しています…');
      try {
        await postAccount('/api/admin/announcements/save', payload);
        clearEditor();
        setStatus(action);
        await loadAdmin();
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        for (const control of controls) control.disabled = false;
      }
    };
    if (location.hash === '#announcements-admin') loadAdmin();
  } else if (actor.role === 'user') {
    loadRecipient();
    document.addEventListener('visibilitychange', () => {if (!document.hidden) loadRecipient();});
    setInterval(() => {if (!document.hidden) loadRecipient();}, 60000);
  }
})();
