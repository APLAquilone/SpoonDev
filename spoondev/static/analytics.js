/* Personal observation analytics. All cohorts are scoped by the server. */
(() => {
  'use strict';
  const labels = {
    all: 'すべての観測', fans: '自分の登録ファン', monthly: '自枠の月間温度あり',
    other: 'どちらにも未確認', first: '自枠で初観測した日', repeat: '以前にも自枠で観測'
  };
  const colors = {
    all: ['--chart-all', '#c2cede'], fans: ['--chart-fan', '#71e4c7'],
    monthly: ['--chart-monthly', '#c4adff'], other: ['--chart-other', '#92b6ff'],
    first: ['--chart-new', '#ffd28a'], repeat: ['--chart-repeat', '#85d8ff']
  };
  let controller, generation = 0, data = null, days = 7, view = 'relationship', unit = 'average';
  let heatGroup = 'fans', shell = null, active = new Set(['fans', 'monthly', 'other']);
  const el = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (cls) item.className = cls;
    return item;
  };
  const amount = value => value === null || value === undefined ? '未観測' : Number(value).toLocaleString('ja-JP');
  const plotted = (bin, key) => bin.all == null || bin[key] == null ? null
    : unit === 'average' ? Number(bin[key]) / Math.max(1, bin.known_days ?? bin.observed_days ?? 0) : Number(bin[key]);
  const graphAmount = value => value == null ? '未確認' : Number(value).toLocaleString('ja-JP', {maximumFractionDigits: 1});
  const stamp = value => value ? new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'
  }).format(new Date(value)) : '未取得';
  const color = key => `var(${colors[key][0]}, ${colors[key][1]})`;
  const keys = () => view === 'relationship' ? ['fans', 'monthly', 'other'] : ['first', 'repeat'];
  const svgEl = (tag, attributes = {}) => {
    const item = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, String(value));
    return item;
  };
  function selectControl(label, choices, selected, onChange) {
    const field = el('label', undefined, 'analytics-control');
    field.append(el('span', label));
    const input = el('select');
    input.setAttribute('aria-label', label);
    for (const [value, text] of choices) {
      const option = el('option', text);
      option.value = value;
      option.selected = String(selected) === value;
      input.append(option);
    }
    input.onchange = () => onChange(input.value);
    field.append(input);
    return {field, input};
  }
  function ensureShell() {
    const panel = document.getElementById('analytics-panel');
    if (!panel) return null;
    if (shell && panel.contains(shell.root)) return shell;
    const root = el('div', undefined, 'analytics-shell');
    const header = el('div', undefined, 'analytics-header');
    header.append(el('h2', 'リスナーが来やすい時間を探す'),
      el('p', '自分の枠で保存された観測から、ファンとの接点と新しい出会いの時間帯を確認できます。', 'analytics-muted'));
    const owner = el('p', '', 'analytics-owner');
    const toolbar = el('div', undefined, 'analytics-toolbar');
    const controls = el('div', undefined, 'analytics-controls');
    const period = selectControl('集計期間', [['7', '直近7日'], ['28', '直近28日']], days, value => {
      days = Number(value); load();
    });
    const grouping = selectControl('比べる属性', [['relationship', 'ファン・月間温度'], ['history', '初観測・継続観測']], view, value => {
      view = value; active = new Set(keys());
      heatGroup = view === 'relationship' ? 'fans' : 'first';
      if (data) render(data);
    });
    const measurement = selectControl('グラフの単位', [['average', '1観測日あたりの人数'], ['total', '延べ観測人数']], unit, value => {
      unit = value;
      if (data) render(data);
    });
    controls.append(period.field, grouping.field, measurement.field);
    const refresh = el('button', '更新');
    refresh.type = 'button'; refresh.onclick = load;
    toolbar.append(controls, refresh);
    const status = el('p', '', 'analytics-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const body = el('div', undefined, 'analytics-body');
    root.append(header, owner, toolbar, status, body);
    panel.replaceChildren(root);
    shell = {root, owner, toolbar, period: period.input, grouping: grouping.input, measurement: measurement.input, refresh, status, body};
    return shell;
  }
  function leave() {
    generation++;
    if (controller) controller.abort();
    controller = null;
    if (shell) {shell.root.removeAttribute('aria-busy'); shell.refresh.disabled = false;}
  }
  async function fetchData(path, signal) {
    if (typeof api === 'function') return api(path, signal);
    const response = await fetch(path, {signal, headers: {Accept: 'application/json'}});
    if (!response.ok) throw new Error(`読み込みに失敗しました（${response.status}）`);
    return response.json();
  }
  async function load() {
    const panel = document.getElementById('analytics-panel');
    if (!panel || panel.hidden) return;
    const ui = ensureShell();
    leave();
    const current = generation;
    controller = new AbortController();
    ui.root.setAttribute('aria-busy', 'true');
    ui.refresh.disabled = true;
    ui.status.textContent = '自分の枠の観測を集計しています…';
    data = null;
    ui.body.replaceChildren();
    try {
      const result = await fetchData(`/api/analytics?days=${days}`, controller.signal);
      if (current !== generation || panel.hidden) return;
      data = result;
      render(result);
    } catch (error) {
      if (error.name === 'AbortError' || current !== generation) return;
      data = null;
      ui.status.textContent = '';
      const notice = el('div', undefined, 'analytics-error');
      notice.append(el('h3', '集計を読み込めませんでした'), el('p', error.message));
      const retry = el('button', 'もう一度試す'); retry.type = 'button'; retry.onclick = load;
      notice.append(retry); ui.body.replaceChildren(notice);
    } finally {
      if (current === generation) {ui.root.removeAttribute('aria-busy'); ui.refresh.disabled = false; controller = null;}
    }
  }
  function goSettings() {
    if (typeof switchView === 'function') switchView('settings');
    else location.hash = 'settings';
  }
  function render(result) {
    const ui = ensureShell();
    if (!ui) return;
    ui.period.value = String(days); ui.grouping.value = view; ui.measurement.value = unit;
    ui.body.replaceChildren();
    if (result.state === 'needs_profile') {
      ui.owner.textContent = ''; ui.status.textContent = '';
      const empty = el('div', undefined, 'analytics-empty');
      empty.append(el('h3', '自分のSpoonプロフィールを設定してください'),
        el('p', '時間帯の集計は、設定した配信者の枠だけを対象にします。'));
      const button = el('button', 'アカウント設定を開く', 'primary');
      button.type = 'button'; button.onclick = goSettings;
      empty.append(button); ui.body.append(empty); return;
    }
    ui.owner.textContent = result.profile ? `${result.profile.name || '名前未取得'} · ID ${result.profile.id}` : '';
    const window = result.window || {};
    ui.status.textContent = `${window.start_date || ''}〜${window.end_date || ''} · 日本時間 · 表示更新 ${stamp(result.checked_at)}`;
    if (result.state === 'not_collected' || !result.totals?.snapshot_count) {
      const empty = el('div', undefined, 'analytics-empty');
      empty.append(el('h3', 'この期間のライブ観測はまだありません'),
        el('p', '収集処理で自分の枠が観測されると、時間帯ごとのグラフを表示します。未観測の時間は人数0として扱いません。'));
      ui.body.append(empty); return;
    }
    const totals = result.totals;
    const summary = el('div', undefined, 'analytics-summary');
    for (const [value, label] of [[totals.all, '期間内に観測した人'], [totals.fans, 'そのうち登録ファン'], [totals.monthly, 'そのうち自枠の月間温度あり']]) {
      const item = el('div', undefined, 'analytics-summary-item');
      item.append(el('strong', value == null ? '未確認' : amount(value) + '人'), el('span', label));
      summary.append(item);
    }
    ui.body.append(summary);
    if (result.state === 'partial' || totals.partial_snapshot_count) {
      ui.body.append(el('p', '部分取得を含みます。確認できた人数の下限で、実際に来た人数のすべてを表すものではありません。', 'analytics-note analytics-coverage-note'));
    }
    if (result.monthly?.state === 'not_collected') {
      ui.body.append(el('p', '自枠の今月の月間温度は未取得です。「月間温度あり」は未確認として表示します。', 'analytics-note'));
    } else if (result.monthly?.state === 'partial') {
      ui.body.append(el('p', '自枠の月間温度は部分取得です。一覧に見つからない人も、月間温度がついている可能性があります。', 'analytics-note'));
    }
    ui.body.append(hourlyCard(result), heatmapCard(result), definitions(result));
  }
  function groupDescription() {
    return view === 'relationship'
      ? '登録ファンと自枠の月間温度がある人は重複します。「どちらにも未確認」は非ファンという意味ではありません。分類は現在のファン一覧と今月の取得済みランキングを使います。'
      : '「初観測した日」は自分の枠の保存済み記録で初めて確認できた日です。Spoonでの初訪問や新規ユーザーとは限りません。翌日以降の観測を「以前にも観測」に分けます。';
  }
  function pointDescription(bin, selectedKeys = keys()) {
    if (!bin || bin.all == null) return `${bin?.hour ?? ''}時台：未観測、または部分取得だけで人数を確認できません。`;
    const parts = [`${bin.hour}時台`, ...selectedKeys.map(key => `${labels[key]} ${bin[key] == null ? '未確認' : graphAmount(plotted(bin, key)) + '人' + (unit === 'average' ? '/観測日' : '・延べ')}`)];
    parts.push(`観測がある日 ${bin.observed_days || 0}日`);
    if (unit === 'average') parts.push(`人数を確認できた日 ${bin.known_days ?? bin.observed_days ?? 0}日`);
    if (bin.partial) parts.push('部分取得を含む下限');
    return parts.join(' · ');
  }
  function hourlyCard(result) {
    const card = el('section', undefined, 'analytics-chart-card');
    card.append(el('h3', '時間帯ごとの観測人数'), el('p', unit === 'average'
      ? '同じ日・同じ時間帯の重複を除いた人数を、人数を確認できた日数で割っています。部分取得だけで人数不明の日は割り算に含めません。内訳では観測日数も確認できます。'
      : '延べ観測人数：同じ日・同じ時間帯に何度観測しても、同じ人は1回として集計します。', 'analytics-note'));
    const legend = el('div', undefined, 'analytics-legend');
    legend.setAttribute('aria-label', 'グラフに表示する属性');
    const hourly = result.hourly || [];
    const wrap = el('div', undefined, 'analytics-chart');
    const tooltip = el('p', 'グラフの時間帯を押すと内訳を確認できます。', 'analytics-tooltip');
    tooltip.setAttribute('role', 'status'); tooltip.setAttribute('aria-live', 'polite');
    const draw = () => {
      wrap.replaceChildren();
      const shown = keys().filter(key => active.has(key));
      const max = Math.max(1, ...hourly.flatMap(bin => shown.map(key => Number(plotted(bin, key) || 0))));
      const axis = el('div', undefined, 'analytics-y-axis');
      for (const ratio of [1, .5, 0]) axis.append(el('span', graphAmount(max * ratio)));
      const plot = el('div', undefined, 'analytics-plot');
      const svg = svgEl('svg', {viewBox: '0 0 1000 200', preserveAspectRatio: 'none', role: 'group', 'aria-label': '時間帯別の観測人数グラフ'});
      const bottom = 185, top = 10, x = hour => 20 + hour * (960 / 23), y = value => bottom - (value / max) * (bottom - top);
      for (const ratio of [0, .5, 1]) svg.append(svgEl('line', {x1: 0, x2: 1000, y1: y(max * ratio), y2: y(max * ratio), stroke: 'var(--line, #33465f)', 'vector-effect': 'non-scaling-stroke'}));
      for (const bin of hourly) if (bin.all == null) svg.append(svgEl('rect', {x: bin.hour * 1000 / 24, y: top, width: 1000 / 24, height: bottom - top, fill: 'var(--analytics-unobserved, #243047)', opacity: .55}));
      for (const key of shown) {
        let path = '', pen = false;
        for (const bin of hourly) {
          if (bin.all == null || bin[key] == null) {pen = false; continue;}
          path += `${pen ? ' L' : ' M'}${x(bin.hour)},${y(plotted(bin, key))}`; pen = true;
        }
        svg.append(svgEl('path', {d: path.trim(), fill: 'none', stroke: color(key), 'stroke-width': 2.5, 'vector-effect': 'non-scaling-stroke', 'stroke-linejoin': 'round'}));
        for (const bin of hourly) if (bin.all != null && bin[key] != null) {
          svg.append(svgEl('circle', {cx: x(bin.hour), cy: y(plotted(bin, key)), r: 3, fill: color(key), 'vector-effect': 'non-scaling-stroke'}));
        }
      }
      const focusPoints = [];
      for (const bin of hourly) {
        const hit = svgEl('rect', {x: bin.hour * 1000 / 24, y: 0, width: 1000 / 24, height: 200,
          fill: 'transparent', tabindex: '0', role: 'button', 'aria-label': pointDescription(bin, shown), class: 'analytics-hour-hit'});
        const show = () => {tooltip.textContent = pointDescription(bin, shown);};
        hit.addEventListener('pointerenter', show); hit.addEventListener('focus', show); hit.addEventListener('click', show);
        hit.addEventListener('keydown', event => {
          if (['Enter', ' '].includes(event.key)) {event.preventDefault(); show();}
          if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            event.preventDefault(); const index = event.key === 'Home' ? 0 : event.key === 'End' ? 23 : Math.min(23, Math.max(0, bin.hour + (event.key === 'ArrowLeft' ? -1 : 1)));
            focusPoints[index]?.focus();
          }
        });
        focusPoints.push(hit); svg.append(hit);
      }
      const hourAxis = el('div', undefined, 'analytics-hour-axis');
      for (let hour = 0; hour < 24; hour++) hourAxis.append(el('span', hour % 3 === 0 ? `${hour}時` : ''));
      plot.append(svg, hourAxis); wrap.append(axis, plot);
    };
    for (const key of keys()) {
      const button = el('button', undefined, 'analytics-legend-button');
      button.type = 'button'; button.setAttribute('aria-pressed', String(active.has(key)));
      const dot = el('span', '', 'analytics-legend-dot'); dot.style.backgroundColor = color(key);
      dot.setAttribute('aria-hidden', 'true'); button.append(dot, el('span', labels[key]));
      button.onclick = () => {
        if (active.has(key)) {if (active.size === 1) return; active.delete(key);} else active.add(key);
        button.setAttribute('aria-pressed', String(active.has(key))); draw();
      };
      legend.append(button);
    }
    card.append(legend, wrap, tooltip, el('p', groupDescription(), 'analytics-note'));
    draw();
    const peak = keys().filter(key => key === 'fans' || key === 'first').map(key => {
      const positive = hourly.filter(bin => Number(plotted(bin, key)) > 0).sort((a, b) => plotted(b, key) - plotted(a, key) || a.hour - b.hour);
      if (!positive.length) return null;
      const hours = positive.filter(bin => plotted(bin, key) === plotted(positive[0], key)).map(bin => `${bin.hour}時台`).slice(0, 3).join('・');
      return `${labels[key]}の観測が多い時間：${hours}（${graphAmount(plotted(positive[0], key))}人${unit === 'average' ? '/観測日' : '・延べ'}、人数確認${positive[0].known_days ?? positive[0].observed_days ?? 0}日）`;
    }).filter(Boolean);
    if (peak.length) card.append(el('p', peak.join(' / ') + '。観測できた時間に偏りがあるため、配信時刻の候補としてご利用ください。', 'analytics-peak analytics-note'));
    return card;
  }
  function heatmapCard(result) {
    const card = el('section', undefined, 'analytics-chart-card');
    const heading = el('div', undefined, 'analytics-heatmap-heading');
    heading.append(el('h3', '日付 × 時間帯'));
    const group = selectControl('表示する属性', [['all', labels.all], ...keys().map(key => [key, labels[key]])], heatGroup, value => {
      heatGroup = value; draw();
    });
    heading.append(group.field); card.append(heading);
    const map = el('div', undefined, 'analytics-heatmap');
    const tooltip = el('p', '色が明るいほど確認できた人数が多い時間です。', 'analytics-tooltip');
    tooltip.setAttribute('role', 'status'); tooltip.setAttribute('aria-live', 'polite');
    const draw = () => {
      map.replaceChildren();
      const selected = heatGroup, daily = result.daily || [];
      const max = Math.max(1, ...daily.flatMap(day => day.hours.map(bin => Number(bin[selected] || 0))));
      const header = el('div', undefined, 'analytics-heatmap-row analytics-heatmap-hours');
      header.append(el('span', '日本時間', 'analytics-heatmap-date'));
      for (let hour = 0; hour < 24; hour++) header.append(el('span', hour % 6 === 0 ? String(hour) : ''));
      map.append(header);
      for (const day of daily) {
        const row = el('div', undefined, 'analytics-heatmap-row');
        row.append(el('span', day.date.slice(5).replace('-', '/'), 'analytics-heatmap-date'));
        for (const bin of day.hours) {
          const value = bin.all == null ? null : bin[selected];
          const cell = el('span', '', 'analytics-heatmap-cell');
          cell.setAttribute('aria-hidden', 'true');
          let description = `${day.date} ${bin.hour}時台 · ${labels[selected]} ${value == null ? '未確認' : amount(value) + '人'}`;
          if (bin.all == null) description = `${day.date} ${bin.hour}時台 · 未観測、または部分取得だけで人数を確認できません。`;
          if (bin.partial && value != null) description += ' · 部分取得を含む下限';
          cell.title = description;
          if (value == null) cell.classList.add('unobserved');
          else if (value === 0) cell.classList.add('zero');
          else {
            cell.style.backgroundColor = color(selected);
            cell.style.opacity = String(.25 + .75 * value / max);
          }
          cell.onpointerenter = () => {tooltip.textContent = description;};
          cell.onclick = () => {tooltip.textContent = description;};
          row.append(cell);
        }
        map.append(row);
      }
    };
    card.append(map, tooltip, el('p', '明るさ＝人数 ／ 斜線＝未確認 ／ 暗いマス＝取得範囲で0人（部分取得では見落としがあります）。空白は配信していなかった証拠ではありません。', 'analytics-note'));
    draw(); card.append(dataTable(result)); return card;
  }
  function dataTable(result) {
    const details = el('details', undefined, 'analytics-data-details');
    details.append(el('summary', '数値の一覧を開く'));
    const scroll = el('div', undefined, 'analytics-table-scroll');
    scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', '時間帯別の観測人数、横にスクロールできます');
    const table = el('table', undefined, 'analytics-data-table');
    const caption = el('caption', '時間帯別の延べ観測人数（日本時間）。同じ人でも日が異なれば別に数えます。');
    table.append(caption);
    const thead = el('thead'), row = el('tr');
    for (const label of ['時間帯', 'すべて', ...keys().map(key => labels[key]), '観測した日', '人数を確認できた日', '取得状態']) {
      const th = el('th', label); th.scope = 'col'; row.append(th);
    }
    thead.append(row); table.append(thead);
    const tbody = el('tbody');
    for (const bin of result.hourly || []) {
      const tr = el('tr'), th = el('th', `${bin.hour}時台`); th.scope = 'row'; tr.append(th);
      for (const key of ['all', ...keys()]) tr.append(el('td', bin.all == null ? '未観測' : bin[key] == null ? '未確認' : amount(bin[key])));
      tr.append(el('td', `${bin.observed_days || 0}日`), el('td', `${bin.known_days ?? bin.observed_days ?? 0}日`), el('td', bin.partial ? '部分取得を含む' : bin.snapshot_count ? '取得済み' : '未観測'));
      tbody.append(tr);
    }
    table.append(tbody); scroll.append(table); details.append(scroll);
    const dailyDetails = el('details', undefined, 'analytics-daily-data-details');
    dailyDetails.append(el('summary', '日付ごとの時間帯の数値を開く'));
    const dailyScroll = el('div', undefined, 'analytics-table-scroll');
    dailyScroll.tabIndex = 0; dailyScroll.setAttribute('role', 'region'); dailyScroll.setAttribute('aria-label', '日付と時間帯別の観測人数、横にスクロールできます');
    const dailyTable = el('table', undefined, 'analytics-data-table');
    dailyTable.append(el('caption', '日付 × 時間帯の観測人数。表の各行で同じ人は1回だけ数えます。'));
    const dailyHead = el('thead'), dailyHeadRow = el('tr');
    for (const label of ['日付', '時間帯', 'すべて', ...keys().map(key => labels[key]), '取得状態']) {
      const th = el('th', label); th.scope = 'col'; dailyHeadRow.append(th);
    }
    dailyHead.append(dailyHeadRow); dailyTable.append(dailyHead);
    const dailyBody = el('tbody');
    for (const day of result.daily || []) for (const bin of day.hours || []) {
      const tr = el('tr'); tr.append(el('td', day.date), el('td', `${bin.hour}時台`));
      for (const key of ['all', ...keys()]) tr.append(el('td', bin.all == null ? '未観測' : bin[key] == null ? '未確認' : amount(bin[key])));
      tr.append(el('td', bin.partial ? '部分取得を含む' : bin.snapshot_count ? '取得済み' : '未観測')); dailyBody.append(tr);
    }
    dailyTable.append(dailyBody); dailyScroll.append(dailyTable); dailyDetails.append(dailyScroll); details.append(dailyDetails);
    return details;
  }
  function definitions(result) {
    const details = el('details', undefined, 'analytics-definitions');
    details.append(el('summary', '集計の読み方'));
    const notes = result.notes || {};
    const fallback = {
      counts: 'グラフは日と時間帯ごとの重複を除いた延べ観測人数です。期間の人数は同じ人を1回だけ数えます。',
      coverage: '観測は収集時の記録です。現在の視聴状態や正確な訪問回数、滞在時間を表しません。',
      cohorts: '現在の登録ファン一覧と今月の取得済み月間温度で分類します。両方に当てはまる人は重複して表示されます。',
      first_observed: '初観測は自分の枠の保存済み記録で初めて確認できた日です。初訪問ではありません。',
      window: '時刻は日本時間です。観測のない時間を0人として埋めません。',
      monthly: '自枠の今月のランキングで0°より大きい温度を確認できた人を分類します。ランキング未取得・部分取得による空白は、温度0とは区別します。'
    };
    const descriptions = Object.keys(notes).length ? Object.values(notes) : Object.values(fallback);
    for (const text of descriptions) if (typeof text === 'string') details.append(el('p', text, 'analytics-note'));
    if (result.monthly?.observed_at) details.append(el('p', `月間温度の取得：${stamp(result.monthly.observed_at)} · ${result.monthly.month || ''}`, 'analytics-note'));
    return details;
  }
  window.spoonAnalytics = {load, cancel: leave};
  const start = () => {
    const panel = document.getElementById('analytics-panel');
    if (panel && !panel.hidden) load();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
