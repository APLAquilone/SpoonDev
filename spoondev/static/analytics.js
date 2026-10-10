/* Listener activity: account cohorts and individual observations across rooms. */
(() => {
  'use strict';
  const labels = {all: '対象の全員', fans: '登録ファン', visitors: '今月の自枠リスナー', monthly: '自枠の月間温度あり'};
  const palette = {all: ['--chart-all', '#92b6ff'], fans: ['--chart-fan', '#71e4c7'],
    visitors: ['--chart-new', '#ffd28a'], monthly: ['--chart-monthly', '#c4adff']};
  let aggregateController, aggregateGeneration = 0, aggregateData = null;
  let aggregateDays = 7, aggregateGroup = 'all', aggregateShell;
  let personalController, personalGeneration = 0, personalDays = 7, personalUser, personalShell, personalOpener;
  const el = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (cls) item.className = cls;
    return item;
  };
  const amount = value => value == null ? '未確認' : Number(value).toLocaleString('ja-JP');
  const stamp = value => value ? new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'
  }).format(new Date(value)) : '未観測';
  const color = key => `var(${palette[key][0]}, ${palette[key][1]})`;
  const observedValue = (bin, key) => bin?.all == null || bin[key] == null ? null : Number(bin[key]);
  const dateLabel = value => new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo', month: 'numeric', day: 'numeric', weekday: 'short'
  }).format(new Date(`${value}T00:00:00+09:00`));
  function addStyles() {
    if (document.getElementById('listener-activity-styles')) return;
    const style = el('style'); style.id = 'listener-activity-styles';
    style.textContent = `
      .listener-activity-context{display:flex;flex-wrap:wrap;gap:7px 18px;color:var(--muted,#b1c0d6);font-size:13px;margin:10px 0 16px}
      .listener-activity-context strong{color:var(--ink,#edf3fd);font-variant-numeric:tabular-nums}
      .listener-activity-toolbar .analytics-controls{flex:1}
      .listener-activity-toolbar .analytics-control{min-width:130px}
      .listener-activity-toolbar .listener-activity-group{min-width:190px}
      .listener-activity-refresh{font-size:24px;line-height:1;padding:7px 12px;min-width:44px}
      .listener-activity-scroll{overflow:auto;overscroll-behavior:contain;max-width:100%;padding:3px 1px 8px;border-radius:8px;scrollbar-color:#617b9d #131e30}
      .listener-activity-grid{min-width:585px;padding:0 2px 2px;color:var(--ink,#edf3fd)}
      .listener-activity-grid .analytics-heatmap-row{grid-template-columns:74px repeat(24,minmax(0,1fr));gap:3px;margin:5px 0;font-size:11px}
      .listener-activity-grid .analytics-heatmap-date{position:sticky;left:0;z-index:2;background:var(--panel,#131e30);padding-right:6px;white-space:nowrap;color:var(--muted,#b1c0d6)}
      .listener-activity-grid .analytics-heatmap-hours{margin-bottom:8px}
      .listener-activity-grid .analytics-heatmap-cell{height:23px;min-height:23px;width:100%;min-width:0;padding:0;border:1px solid transparent;border-radius:4px;box-shadow:none;cursor:pointer}
      .listener-activity-grid .analytics-heatmap-cell[aria-current=true]{outline:2px solid #f5f8ff;outline-offset:1px}
      .listener-activity-grid .analytics-heatmap-cell:focus-visible{outline:3px solid var(--live,#71e4c7);outline-offset:2px}
      .listener-activity-grid .analytics-heatmap-cell:hover{border-color:#f5f8ff}
      .listener-activity-selected{max-width:100%;margin:12px 0 8px;min-height:46px;padding:10px 12px;background:var(--panel-raised,#1b2a41);border:1px solid var(--line,#33465f);border-radius:8px;color:var(--ink,#edf3fd);font-size:13px;line-height:1.6}
      .listener-activity-scale{display:flex;align-items:center;gap:7px;flex-wrap:wrap;color:var(--muted,#b1c0d6);font-size:12px;margin-top:10px}
      .listener-activity-swatch{width:15px;height:15px;border-radius:3px;display:inline-block}
      .listener-activity-swatch.unknown{background:repeating-linear-gradient(135deg,#26344a 0,#26344a 3px,#172338 3px,#172338 6px)}
      .listener-activity-swatch.zero{background:#263850;border:1px solid #465c78}
      .listener-activity-card .analytics-data-details{margin-top:14px}
      #listener-activity-dialog{width:min(850px,calc(100vw - 28px));max-width:calc(100vw - 28px);max-height:calc(100dvh - 40px);padding:0;border:1px solid #526982;border-radius:16px;background:var(--panel,#131e30);color:var(--ink,#edf3fd);box-shadow:0 28px 90px #0009;overflow:auto}
      #listener-activity-dialog::backdrop{background:#060d1dcc;backdrop-filter:blur(3px)}
      .listener-activity-dialog-header{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:18px 22px;border-bottom:1px solid var(--line,#33465f)}
      .listener-activity-dialog-header h2{margin:3px 0 0;font-size:18px;overflow-wrap:anywhere;line-height:1.6}
      .listener-activity-dialog-header button{flex:0 0 auto}
      .listener-activity-dialog-body{padding:18px 22px}
      .listener-activity-dialog-body .analytics-chart-card{margin-bottom:0}
      @media(max-width:600px){
        .listener-activity-grid{min-width:550px}
        .listener-activity-grid .analytics-heatmap-row{grid-template-columns:70px repeat(24,minmax(0,1fr))}
        .listener-activity-dialog-header,.listener-activity-dialog-body{padding:15px}
        .listener-activity-dialog-header h2{font-size:16px}
        .listener-activity-toolbar .analytics-control{flex:1 1 120px;min-width:0}
        .listener-activity-toolbar .listener-activity-group{flex-basis:170px}
        .listener-activity-toolbar{gap:9px;padding:12px}
        .listener-activity-context{font-size:12px;gap:6px 14px}
      }
    `;
    document.head.append(style);
  }
  function selectControl(label, choices, selected, onChange, cls = '') {
    const field = el('label', undefined, `analytics-control ${cls}`.trim()); field.append(el('span', label));
    const input = el('select'); input.setAttribute('aria-label', label);
    for (const [value, text] of choices) {
      const option = el('option', text); option.value = value; option.selected = String(selected) === value; input.append(option);
    }
    input.onchange = () => onChange(input.value); field.append(input); return {field, input};
  }
  function refreshButton(label, action) {
    const button = el('button', '↺', 'listener-activity-refresh');
    button.type = 'button'; button.title = label; button.setAttribute('aria-label', label); button.onclick = action; return button;
  }
  async function fetchData(path, signal) {
    if (typeof api === 'function') return api(path, signal);
    const response = await fetch(path, {signal, headers: {Accept: 'application/json'}});
    if (!response.ok) throw new Error(`読み込みに失敗しました（${response.status}）`);
    return response.json();
  }
  function stateNotice(title, text, buttonLabel, action, error = false) {
    const notice = el('div', undefined, error ? 'analytics-error' : 'analytics-empty'); notice.append(el('h3', title), el('p', text));
    if (action) {
      const button = el('button', buttonLabel, error ? '' : 'primary'); button.type = 'button'; button.onclick = action; notice.append(button);
    }
    return notice;
  }
  function goSettings() {
    if (typeof switchView === 'function') switchView('settings'); else location.hash = 'settings';
  }
  function ensureAggregate() {
    const panel = document.getElementById('analytics-panel'); if (!panel) return null;
    addStyles(); if (aggregateShell && panel.contains(aggregateShell.root)) return aggregateShell;
    const root = el('div', undefined, 'analytics-shell'), header = el('div', undefined, 'analytics-header');
    header.append(el('h2', 'ファン・来訪者が活動する時間'),
      el('p', '自分のファンや今月自枠で確認した人が、他枠も含めてリスナーとして観測された時間を確認できます。', 'analytics-muted'));
    const owner = el('p', '', 'analytics-owner'), toolbar = el('div', undefined, 'analytics-toolbar listener-activity-toolbar');
    const controls = el('div', undefined, 'analytics-controls');
    const period = selectControl('表示期間', [['7', '直近7日'], ['28', '直近28日']], aggregateDays, value => {
      aggregateDays = Number(value); load();
    });
    const group = selectControl('対象のリスナー', Object.entries(labels), aggregateGroup, value => {
      aggregateGroup = value; if (aggregateData) renderAggregate(aggregateData);
    }, 'listener-activity-group');
    const refresh = refreshButton('活動時間を更新', load); controls.append(period.field, group.field); toolbar.append(controls, refresh);
    const status = el('p', '', 'analytics-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const body = el('div', undefined, 'analytics-body'); root.append(header, owner, toolbar, status, body); panel.replaceChildren(root);
    aggregateShell = {root, owner, status, body, refresh, period: period.input, group: group.input}; return aggregateShell;
  }
  function cancelAggregate() {
    aggregateGeneration++; aggregateController?.abort(); aggregateController = null;
    if (aggregateShell) {aggregateShell.root.removeAttribute('aria-busy'); aggregateShell.refresh.disabled = false;}
  }
  async function load() {
    const panel = document.getElementById('analytics-panel'); if (!panel || panel.hidden) return;
    const ui = ensureAggregate(); cancelAggregate(); const current = aggregateGeneration;
    aggregateController = new AbortController(); aggregateData = null; ui.root.setAttribute('aria-busy', 'true'); ui.refresh.disabled = true;
    ui.status.textContent = '他枠も含めたリスナー観測を集計しています…'; ui.body.replaceChildren();
    try {
      const result = await fetchData(`/api/analytics?days=${aggregateDays}`, aggregateController.signal);
      if (current !== aggregateGeneration || panel.hidden) return; aggregateData = result; renderAggregate(result);
    } catch (error) {
      if (error.name === 'AbortError' || current !== aggregateGeneration) return;
      ui.status.textContent = ''; ui.body.replaceChildren(stateNotice('活動時間を読み込めませんでした', error.message, 'もう一度試す', load, true));
    } finally {
      if (current === aggregateGeneration) {ui.root.removeAttribute('aria-busy'); ui.refresh.disabled = false; aggregateController = null;}
    }
  }
  function windowText(result) {
    const window = result.window || {};
    return `${window.start_date || ''}〜${window.end_date || ''} · 日本時間 · 表示更新 ${stamp(result.checked_at)}`;
  }
  function renderAggregate(result) {
    const ui = ensureAggregate(); if (!ui) return;
    ui.period.value = String(aggregateDays); ui.group.value = aggregateGroup; ui.body.replaceChildren();
    if (result.state === 'needs_profile') {
      ui.owner.textContent = ''; ui.status.textContent = '';
      ui.body.append(stateNotice('自分のSpoonプロフィールを設定してください',
        '登録した配信者のファン・今月の来訪者を対象に、他枠での活動時間も集計します。', 'アカウント設定を開く', goSettings)); return;
    }
    ui.owner.textContent = result.profile ? `${result.profile.name || '名前未取得'} · ID ${result.profile.id}` : '';
    ui.status.textContent = windowText(result);
    const cohorts = result.cohorts || {}, totals = result.totals || {}, context = el('div', undefined, 'listener-activity-context');
    for (const [value, text] of [[cohorts.cohort_count, '対象'], [totals.all, '期間内に観測'], [totals.observed_days, '観測した日数']]) {
      const item = el('span'); item.append(`${text} `, el('strong', value == null ? '未確認' : amount(value) + (text === '観測した日数' ? '日' : '人'))); context.append(item);
    }
    ui.body.append(context);
    for (const option of ui.group.options) {
      const counts = {all: cohorts.cohort_count, fans: cohorts.registered_fan_count, visitors: cohorts.monthly_visitor_count, monthly: cohorts.monthly_confirmed_count};
      option.textContent = labels[option.value] + (counts[option.value] == null ? '' : `（${amount(counts[option.value])}人）`);
    }
    if (result.state === 'not_collected' || result.state === 'not_observed') ui.body.append(stateNotice(
      result.state === 'not_collected' ? 'この期間のリスナー観測はまだありません' : '対象のリスナーの観測はまだありません',
      '自分の配信中に限らず、収集した他の枠で確認できた記録も表示します。未観測はSpoonを開いていなかった証拠ではありません。'));
    ui.body.append(heatmapCard(result, aggregateGroup, false));
    if (result.state === 'partial' || totals.partial_snapshot_count) ui.body.append(el('p', '部分取得を含むため、表示人数は確認できた範囲の人数です。', 'analytics-note'));
    if (result.monthly?.state === 'not_collected') ui.body.append(el('p', '自枠の今月の月間温度は未取得です。「月間温度あり」は未確認として表示します。', 'analytics-note'));
    else if (result.monthly?.state === 'partial') ui.body.append(el('p', '月間温度は部分取得です。見つからない人も温度がついている可能性があります。', 'analytics-note'));
    ui.body.append(definitions(result, false));
  }
  function cellDescription(day, bin, key, individual) {
    const value = observedValue(bin, key); let text = `${day.date} ${bin.hour}:00〜${bin.hour}:59 · `;
    if (value == null) text += 'リスナー観測なし（活動していなかったとは限りません）';
    else if (individual) text += 'リスナーとして観測'; else text += `${labels[key]} ${amount(value)}人`;
    if (value != null && bin.partial) text += ' · 部分取得を含む'; return text;
  }
  function heatmapCard(result, key, individual) {
    const card = el('section', undefined, 'analytics-chart-card listener-activity-card');
    card.append(el('h3', '日付 × 時間帯'), el('p', individual ? '他枠も含めて、リスナーとして確認できた時間です。'
      : `${labels[key]}が、他枠も含めて確認できた時間です。同じ日・同じ時間の同じ人は1人として集計します。`, 'analytics-note'));
    const scroll = el('div', undefined, 'listener-activity-scroll'); scroll.setAttribute('role', 'region');
    scroll.setAttribute('aria-label', '日付と時間帯のリスナー観測。横にスクロールできます。');
    const grid = el('div', undefined, 'analytics-heatmap listener-activity-grid'); grid.setAttribute('role', 'group');
    grid.setAttribute('aria-label', '日付と時間帯のリスナー観測。矢印キーで移動します。');
    const header = el('div', undefined, 'analytics-heatmap-row analytics-heatmap-hours');
    header.setAttribute('aria-hidden', 'true'); header.append(el('span', '日本時間', 'analytics-heatmap-date'));
    for (let hour = 0; hour < 24; hour++) header.append(el('span', hour % 3 === 0 ? `${hour}時` : '')); grid.append(header);
    const daily = result.daily || [];
    const max = Math.max(1, ...daily.flatMap(day => (day.hours || []).map(bin => observedValue(bin, key) || 0)));
    const selected = el('p', 'セルを押すと観測の内訳を確認できます。横にスクロールできます。', 'listener-activity-selected');
    selected.setAttribute('role', 'status'); selected.setAttribute('aria-live', 'polite');
    const cells = []; let activeCell = null;
    const pick = (cell, day, bin) => {
      if (activeCell) {activeCell.tabIndex = -1; activeCell.removeAttribute('aria-current');}
      activeCell = cell; cell.tabIndex = 0; cell.setAttribute('aria-current', 'true'); selected.textContent = cellDescription(day, bin, key, individual);
    };
    for (let dayIndex = 0; dayIndex < daily.length; dayIndex++) {
      const day = daily[dayIndex], row = el('div', undefined, 'analytics-heatmap-row');
      const date = el('span', dateLabel(day.date), 'analytics-heatmap-date'); date.setAttribute('aria-hidden', 'true'); row.append(date);
      const rowCells = [];
      for (let hour = 0; hour < 24; hour++) {
        const bin = day.hours?.find(item => item.hour === hour) || {hour, all: null};
        const value = observedValue(bin, key), cell = el('button', '', 'analytics-heatmap-cell');
        cell.type = 'button'; cell.tabIndex = -1; cell.dataset.date = day.date; cell.dataset.hour = String(hour);
        cell.setAttribute('aria-label', cellDescription(day, bin, key, individual)); cell.title = cellDescription(day, bin, key, individual);
        if (value == null) {cell.classList.add('unobserved'); cell.dataset.state = 'unobserved';}
        else if (value === 0) {cell.classList.add('zero'); cell.dataset.state = 'zero';}
        else {cell.dataset.state = 'observed'; cell.style.backgroundColor = `color-mix(in srgb, ${color(key)} ${Math.round(30 + 70 * value / max)}%, #16263c)`;}
        cell.onclick = () => pick(cell, day, bin); cell.onfocus = () => pick(cell, day, bin);
        cell.onkeydown = event => {
          if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return; event.preventDefault();
          let targetDay = dayIndex, targetHour = hour;
          if (event.key === 'ArrowLeft') targetHour = Math.max(0, hour - 1); if (event.key === 'ArrowRight') targetHour = Math.min(23, hour + 1);
          if (event.key === 'ArrowUp') targetDay = Math.max(0, dayIndex - 1); if (event.key === 'ArrowDown') targetDay = Math.min(daily.length - 1, dayIndex + 1);
          if (event.key === 'Home') {targetHour = 0; if (event.ctrlKey) targetDay = 0;} if (event.key === 'End') {targetHour = 23; if (event.ctrlKey) targetDay = daily.length - 1;}
          cells[targetDay]?.[targetHour]?.focus();
        };
        rowCells.push(cell); row.append(cell);
      }
      cells.push(rowCells); grid.append(row);
    }
    const latest = daily.map((day, index) => ({day, index})).reverse().find(({day}) => (day.hours || []).some(bin => Number(observedValue(bin, key)) > 0));
    const initial = latest ? cells[latest.index][latest.day.hours.find(bin => Number(observedValue(bin, key)) > 0).hour] : cells[0]?.[0];
    if (initial) {initial.tabIndex = 0; activeCell = initial;}
    scroll.append(grid); card.append(scroll, selected);
    const scale = el('div', undefined, 'listener-activity-scale'), unknown = el('span', '', 'listener-activity-swatch unknown');
    unknown.setAttribute('aria-hidden', 'true'); scale.append(unknown, el('span', '未観測'));
    if (!individual) {
      const zero = el('span', '', 'listener-activity-swatch zero'); zero.setAttribute('aria-hidden', 'true'); scale.append(zero, el('span', '該当する観測なし'), el('span', '少'));
      for (const ratio of [30, 65, 100]) {
        const swatch = el('span', '', 'listener-activity-swatch'); swatch.setAttribute('aria-hidden', 'true');
        swatch.style.backgroundColor = `color-mix(in srgb, ${color(key)} ${ratio}%, #16263c)`; scale.append(swatch);
      }
      scale.append(el('span', `多（最大${amount(max)}人）`));
    } else {
      const present = el('span', '', 'listener-activity-swatch'); present.setAttribute('aria-hidden', 'true'); present.style.backgroundColor = color(key);
      scale.append(present, el('span', 'リスナーとして観測'));
    }
    card.append(scale, el('p', '未観測の時間にも活動していた可能性があります。Spoonを開いた時刻や閉じた時刻、連続した滞在時間を示すものではありません。', 'analytics-note'), observationTable(result, key, individual));
    return card;
  }
  function observationTable(result, key, individual) {
    const details = el('details', undefined, 'analytics-data-details'); details.append(el('summary', '観測した時間の一覧')); let built = false;
    details.addEventListener('toggle', () => {
      if (!details.open || built) return; built = true;
      const scroll = el('div', undefined, 'analytics-table-scroll'); scroll.tabIndex = 0; scroll.setAttribute('role', 'region');
      scroll.setAttribute('aria-label', '観測した日付と時間帯の数値。横にスクロールできます。');
      const table = el('table', undefined, 'analytics-data-table'); table.append(el('caption', '日本時間。未観測の時間は一覧に含めません。'));
      const head = el('thead'), headRow = el('tr');
      for (const text of ['日付', '時間帯', individual ? '観測' : labels[key], '取得状態']) {const th = el('th', text); th.scope = 'col'; headRow.append(th);}
      head.append(headRow); table.append(head); const body = el('tbody'); let count = 0;
      for (const day of result.daily || []) for (const bin of day.hours || []) {
        const value = observedValue(bin, key); if (value == null) continue;
        const row = el('tr'); row.append(el('td', day.date), el('td', `${bin.hour}:00〜${bin.hour}:59`),
          el('td', individual ? 'リスナーとして観測' : `${amount(value)}人`), el('td', bin.partial ? '部分取得を含む' : '記録あり')); body.append(row); count++;
      }
      if (!count) {details.append(el('p', 'この対象のリスナー観測はまだありません。', 'analytics-note')); return;}
      table.append(body); scroll.append(table); details.append(scroll);
    });
    return details;
  }
  function definitions(result, individual) {
    const details = el('details', undefined, 'analytics-definitions'); details.append(el('summary', '集計の読み方'));
    const fallback = individual ? [
      '保存されたすべての枠で、このユーザーがリスナーとして確認できた時間を集計しています。',
      '時刻は日本時間です。枠を配信していた時間のグラフではありません。'
    ] : [
      '対象は、現在の登録ファン・今月自枠で観測したリスナー・自枠の今月の月間温度が0°より高いリスナーの合計です。同じ人は重複して数えません。',
      'その対象の人が、保存された他の枠も含めてリスナーとして観測された日付・時間帯を集計します。自分が配信していた時間だけのグラフではありません。',
      '登録ファン、今月の自枠リスナー、月間温度ありは重複する場合があります。',
      'ランキングの未取得や部分取得による空白は、温度0とは区別します。'
    ];
    const notes = result.notes || {}, descriptions = Object.keys(notes).length ? Object.values(notes) : fallback;
    for (const text of descriptions) if (typeof text === 'string') details.append(el('p', text, 'analytics-note'));
    if (!individual && result.monthly?.observed_at) details.append(el('p', `月間温度の取得：${stamp(result.monthly.observed_at)} · ${result.monthly.month || ''}`, 'analytics-note')); return details;
  }
  function ensurePersonal() {
    addStyles(); if (personalShell) return personalShell;
    const dialog = el('dialog'); dialog.id = 'listener-activity-dialog'; dialog.setAttribute('aria-labelledby', 'listener-activity-title');
    const header = el('div', undefined, 'listener-activity-dialog-header'), title = el('h2', '', 'listener-activity-title'); title.id = 'listener-activity-title';
    const close = el('button', '閉じる'); close.type = 'button'; close.onclick = () => dialog.close(); header.append(title, close);
    const root = el('div', undefined, 'listener-activity-dialog-body');
    const subtitle = el('p', '他枠も含めて、リスナーとして観測された出現時間です。', 'analytics-muted');
    const toolbar = el('div', undefined, 'analytics-toolbar listener-activity-toolbar'), controls = el('div', undefined, 'analytics-controls');
    const period = selectControl('表示期間', [['7', '直近7日'], ['28', '直近28日']], personalDays, value => {personalDays = Number(value); loadPersonal();});
    const refresh = refreshButton('このリスナーの出現時間を更新', loadPersonal); controls.append(period.field); toolbar.append(controls, refresh);
    const status = el('p', '', 'analytics-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const body = el('div', undefined, 'analytics-body'); root.append(subtitle, toolbar, status, body); dialog.append(header, root);
    dialog.addEventListener('close', () => {
      personalGeneration++; personalController?.abort(); personalController = null; root.removeAttribute('aria-busy'); refresh.disabled = false;
      if (personalOpener?.isConnected) personalOpener.focus(); personalOpener = null;
    });
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return; const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
    document.body.append(dialog); personalShell = {dialog, root, title, status, body, refresh, period: period.input}; return personalShell;
  }
  async function openUser(user, opener) {
    const id = typeof user === 'object' ? user?.id : user; if (!/^[1-9]\d*$/.test(String(id))) return;
    const ui = ensurePersonal(); if (!ui.dialog.open) personalOpener = opener || document.activeElement;
    personalUser = typeof user === 'object' ? user : {id}; ui.title.textContent = `${personalUser.name || `ID ${id}`}の出現時間`;
    ui.period.value = String(personalDays); if (!ui.dialog.open) ui.dialog.showModal(); await loadPersonal();
  }
  async function loadPersonal() {
    const ui = personalShell; if (!ui?.dialog.open || !personalUser) return;
    const current = ++personalGeneration; personalController?.abort(); personalController = new AbortController();
    ui.root.setAttribute('aria-busy', 'true'); ui.refresh.disabled = true; ui.status.textContent = 'この人のリスナー観測を集計しています…'; ui.body.replaceChildren();
    try {
      const result = await fetchData(`/api/users/${personalUser.id}/activity?days=${personalDays}`, personalController.signal);
      if (current !== personalGeneration || !ui.dialog.open) return;
      if (result.profile?.name) ui.title.textContent = `${result.profile.name}の出現時間`; ui.status.textContent = windowText(result);
      const totals = result.totals || {}, context = el('p', undefined, 'listener-activity-context');
      context.append(el('span', `観測した日数 ${amount(totals.observed_days)}日`), el('span', `最終リスナー観測 ${stamp(totals.last_observed_at)}`)); ui.body.append(context);
      if (totals.all == null || totals.all === 0) ui.body.append(stateNotice('この期間のリスナー観測はまだありません',
        '未観測でもSpoonで活動していた可能性があります。記録がない時間を、活動していない時間とは判定できません。'));
      ui.body.append(heatmapCard(result, 'all', true), definitions(result, true));
    } catch (error) {
      if (error.name === 'AbortError' || current !== personalGeneration) return;
      ui.status.textContent = ''; ui.body.replaceChildren(stateNotice('出現時間を読み込めませんでした', error.message, 'もう一度試す', loadPersonal, true));
    } finally {
      if (current === personalGeneration) {ui.root.removeAttribute('aria-busy'); ui.refresh.disabled = false; personalController = null;}
    }
  }
  window.spoonAnalytics = {load, cancel: cancelAggregate, openUser};
  const start = () => {const panel = document.getElementById('analytics-panel'); if (panel && !panel.hidden) load();};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true}); else start();
})();
