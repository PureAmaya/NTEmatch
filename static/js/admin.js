/* 管理端：门禁 + 配置编辑面板 + 诊断。
 * 只负责渲染与拉取诊断；表单提交/按钮动作由 actions.js 处理。
 */

import {
  App,
  api,
  canManageEvents,
  esc,
  fmtFull,
  hooks,
  isFinished,
  isServerAdmin,
  log,
  normScoring,
  qs,
  qsa,
  stateKey,
  VALUE_TYPE_OPTIONS,
  BETTER_OPTIONS,
  LABEL_PRESETS,
} from './core.js';
import {
  avaHtml,
  fieldArea,
  fieldDateTime,
  fieldNum,
  fieldSelect,
  fieldSwitch,
  fieldText,
  panelHtml,
  rulebookBodyHtml,
} from './ui.js';
import { mount as mountTeams } from './teams.js';
import { qqbotPushPanelHtml, refreshQqbotStatusBox } from './members.js';
import { icon } from './icons.js';
import { renderNoticeBoard } from './notices.js';

/* --------------------------- 面板渲染 ---------------------------
 *
 * 管理页整页都是表单，而 renderAdmin 会被很多事件触发（保存后、WS 推送、
 * 切换届次、直播信号轮询…）。直接 innerHTML 重建会把「正在填、还没保存」
 * 的内容冲掉——表现就是「界面隔一会儿弹一下，刚写的没了」。所以这里：
 *
 *   1. 状态指纹没变就**不重建**（force 可强制）；
 *   2. 真要重建时，把用户改过（dirty）的输入、焦点与光标位置、滚动位置还回去。
 *
 * 只认「用户自己改过」的字段（input/change 事件打标记），程序写入的值不算，
 * 免得跟服务端刚下发的新数据打架。
 */
let adminRenderedKey = '';
let adminBound = false;

const adminKey = () => `${App.eventId}|${App.token ? 'in' : 'out'}|${stateKey(App.state)}`;

function bindDirtyTracking() {
  if (adminBound) return;
  adminBound = true;
  ['#adminPanel', '#adminGate'].forEach((sel) => {
    const host = qs(sel);
    if (!host) return;
    const mark = (e) => {
      if (e.target?.name && e.target.dataset) e.target.dataset.dirty = '1';
    };
    host.addEventListener('input', mark);
    host.addEventListener('change', mark);
  });
}

function collectEdits(root) {
  const out = [];
  qsa('[data-dirty][name]', root).forEach((el) => {
    if (el.type === 'file') return; // 文件控件的值还原不了，也不需要还原
    out.push([el.name, el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value, el.type]);
  });
  return out;
}

function applyEdits(root, edits) {
  edits.forEach(([name, value, type]) => {
    const el = qs(`[name="${name}"]`, root);
    if (!el) return;
    if (type === 'checkbox' || type === 'radio') el.checked = Boolean(value);
    else el.value = value;
    el.dataset.dirty = '1'; // 下一次重建还要保住它
  });
}

/** 光标所在输入框（重绘后把焦点与光标位置还回去，别让人输一半跳到别处）。 */
function captureFocus() {
  const el = document.activeElement;
  if (!el?.name) return null;
  return { name: el.name, start: el.selectionStart ?? null, end: el.selectionEnd ?? null };
}

function restoreFocus(info) {
  if (!info) return;
  const el = qs(`[name="${info.name}"]`);
  if (!el || el.disabled) return;
  el.focus({ preventScroll: true });
  if (info.start == null || !el.setSelectionRange) return;
  try {
    el.setSelectionRange(info.start, info.end ?? info.start);
  } catch {
    /* 某些 input 类型不支持选区，忽略 */
  }
}

/**
 * 当前这一届是否归我管。
 *
 * 服务器管理员放行任意届；赛事管理员与届的 ``ownerUid`` 比对成员 uid。
 * **拿不到届列表时不拦**：界面上的判断只是「提前把话说清楚」，真正的门禁在后端
 * （``security.require_event_owned``）。宁可让他看到面板后收到一次 403，
 * 也不要因为前端数据还没到位就把有权限的人挡在门外。
 */
function canManageThisEvent() {
  if (isServerAdmin()) return true;
  const event = (App.events || []).find((e) => e.id === App.eventId);
  if (!event) return true;
  return Boolean(event.ownerUid && event.ownerUid === App.me?.uid);
}

/** 成员权限：能看不能管，给出出路。 */
const memberNoPowerHtml = () =>
  `<div class="panel"><div class="panel__head"><h2>赛事管理</h2>` +
  `<span class="panel__hint">当前权限：成员</span></div><div class="panel__body">` +
  `<div class="notice notice--warn">你是以<b>成员</b>身份登录的，不能管理赛事。` +
  `可在「我的」页修改个人资料、直播间名字与凭据；` +
  `如需创建 / 管理赛事，请联系服务器管理员把你的权限提升为「赛事管理员」。</div>` +
  `<div class="tool-group" style="margin-top:10px">` +
  `<button class="btn btn--sm btn--primary" type="button" data-act="route-user">前往「我的」</button>` +
  `</div></div></div>`;

/** 「这一届不是你举办的」：写清归属 + 给出路，而不是让保存失败去解释。 */
function notMyEventHtml() {
  const event = (App.events || []).find((e) => e.id === App.eventId);
  const name = event?.name || App.eventId || '这一届';
  const owner = event?.ownerName || '';
  return (
    `<div class="panel"><div class="panel__head"><h2>赛事管理</h2>` +
    `<span class="panel__hint">当前权限：赛事管理员</span></div><div class="panel__body">` +
    `<div class="notice notice--warn">「<b>${esc(name)}</b>」不是你举办的，所以这里不能改。` +
    (owner
      ? `这一届由 <b>${esc(owner)}</b> 举办。`
      : '这一届没有登记举办者（历史数据），只有服务器管理员能管。') +
    `</div>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="route-events">看我举办的届</button>` +
    `<button class="btn btn--sm" type="button" data-act="route-home">回主页</button>` +
    `</div></div></div>`
  );
}

export function renderAdmin({ force = false } = {}) {
  const gate = qs('#adminGate');
  const panel = qs('#adminPanel');
  if (!gate || !panel) return;
  const key = adminKey();
  // 指纹没变就不重建：一次多余的重绘比「少刷新一次」代价大得多
  if (!force && key === adminRenderedKey) return;
  adminRenderedKey = key;

  bindDirtyTracking();
  const edits = [...collectEdits(panel), ...collectEdits(gate)];
  const focus = captureFocus();
  const scrollY = window.scrollY;

  if (!App.token) {
    panel.innerHTML = '';
    gate.innerHTML = gateHtml();
  } else if (!canManageEvents()) {
    gate.innerHTML = '';
    panel.innerHTML = memberNoPowerHtml();
  } else if (!App.state) {
    return; // 登录了但状态还没到：先不动，等状态到位指纹会变、再画
  } else if (!canManageThisEvent()) {
    gate.innerHTML = '';
    panel.innerHTML = notMyEventHtml();
  } else {
    gate.innerHTML = '';
    panel.innerHTML = adminPanelHtml(App.state);
    mountTeams(qs('#teamHost'));
    // 赛事通知（卡片列表 + 分页）：异步拉取，所以先占位再填
    renderNoticeBoard('event', qs('#adminNotices'), {
      manage: true,
      hint: 'Markdown · 打开本届即弹窗',
    });
  }

  applyEdits(panel, edits);
  applyEdits(gate, edits);
  restoreFocus(focus);
  if (window.scrollY !== scrollY) window.scrollTo(0, scrollY);
  refreshDiagnostics();
  void refreshQqbotStatusBox();
}

/** 当前赛制是否为积分制。 */
const isLeague = (s = App.state) => (s?.rules?.format || 'tournament') === 'league';

const gateHtml = () =>
  `<div class="panel"><div class="gate">` +
  `<div class="gate__title">登录后管理赛事</div>` +
  `<p class="gate__desc">用你的<b>成员密钥</b>登录就行——能管哪几届由<b>身份</b>决定：` +
  `服务器管理员可管全部届，赛事管理员只能管<b>自己举办的</b>届，普通成员只能改自己的资料。</p>` +
  `<div class="field"><label for="adminKey">成员密钥</label>` +
  `<input id="adminKey" type="password" autocomplete="current-password" placeholder="请输入成员密钥"></div>` +
  `<button class="btn btn--primary btn--block" type="button" data-act="admin-login">登录</button>` +
  `<p class="gate__hint">成员密钥由服务器管理员在「服务器 → 成员管理」里生成。</p>` +
  `</div></div>`;

const EVENT_STATE_TEXT = {
  draft: '筹备中',
  upcoming: '未开赛',
  running: '进行中',
  finished: '已结束',
};

/** 赛事时间编辑器：当前状态 + 「现在」快捷填充（只改输入框，点保存才落库）。 */
function eventTimeEditorHtml(t) {
  const state = t.state || 'draft';
  const parts = [
    t.startAt ? `开始 ${t.startAt.replace('T', ' ')}` : '开始时间未登记',
    t.endAt ? `结束 ${t.endAt.replace('T', ' ')}` : '结束时间待定',
    t.durationMinutes != null ? `用时 ${t.durationMinutes} 分钟` : '',
    t.total ? `赛程 ${t.done} / ${t.total} 场` : '尚未生成赛程',
  ].filter(Boolean);
  return (
    `<div class="etime etime--edit">` +
    `<span class="etime__pill etime__pill--${esc(state)}">${esc(EVENT_STATE_TEXT[state] || '')}</span>` +
    `<div class="etime__cells"><div class="etime__cell"><span>当前</span>` +
    `<b>${esc(parts.join(' · '))}</b></div></div>` +
    `<div class="tool-group etime__ops">` +
    `<button class="btn btn--sm" type="button" data-act="event-now" data-field="startTime">开始时间=现在</button>` +
    `<button class="btn btn--sm" type="button" data-act="event-now" data-field="endTime">结束时间=现在</button>` +
    `<button class="btn btn--sm" type="button" data-act="event-now-clear" data-field="endTime">清空结束时间</button>` +
    `</div>` +
    (t.state !== 'finished' && t.scheduleDone
      ? `<div class="etime__note">赛程已全部结束，可点「结束时间=现在」登记收尾时间。</div>`
      : '') +
    `</div>`
  );
}

/**
 * 计分口径三件套：**类型 → 怎么解析与显示**、**标签 → 怎么写**、**判断标准 → 谁赢**。
 *
 * 这是全套赛制里最底层的一组开关：判定、名次、名次分、晋级与积分榜排序都跟着它走
 * （后端见 app/metrics.py）。三件事分开，是因为它们本来就是独立的——
 * 「时间 + 用时 + 数值低胜」是最常见的一种组合，而不是唯一一种。
 */
function scoringFields(rules) {
  const sc = normScoring(rules);
  return (
    fieldSelect('valueType', '计分类型（记什么）', sc.valueType, VALUE_TYPE_OPTIONS, {
      hint:
        '决定怎么录入与怎么显示：自然数原样、小数保留三位、时间按时分秒。' +
        '改它等于改已有成绩的含义，赛中一般不要动',
    }) +
    `<div class="field"><label for="f-valueLabel">计分标签（怎么写）</label>` +
    `<input id="f-valueLabel" name="valueLabel" list="scoring-labels" value="${esc(
      rules.valueLabel || ''
    )}" placeholder="${esc(sc.label)}">` +
    `<datalist id="scoring-labels">${LABEL_PRESETS.map(
      (text) => `<option value="${esc(text)}"></option>`
    ).join('')}</datalist>` +
    `<span class="field__hint">展示时怎么称呼这项成绩：得分 / 评分 / 用时，也可以自己填（留空按类型给默认）</span></div>` +
    fieldSelect('better', '判断标准（谁赢）', sc.better, BETTER_OPTIONS, {
      hint: '数值高胜 = 分高者赢；数值低胜 = 成绩小者赢',
    })
  );
}

/** 锦标赛制规则表单。 */
function tournamentRulesForm(rules, s) {
  const maxSize = s?.format?.maxSize || 0;
  const perMatch = Number(rules.teamsPerMatch) || 2;
  const loser = rules.loserBracket !== false;
  const sc = normScoring(rules);
  const teams = Number(s?.format?.teams) || 0;
  // 「0 自动」时到底会分几组：由后端算好放在 rulebook.facts 里（同一套算法，界面不自己猜）
  const autoGroups = Number(s?.rulebook?.facts?.groups) || 0;
  return (
    `<form class="form form--2" data-form="rules">` +
    scoringFields(rules) +
    fieldSelect(
      'teamSize',
      '每个组的人数',
      rules.teamSize ?? 2,
      [['1', '1 人'], ['2', '2 人（默认）'], ['3', '3 人'], ['4', '4 人'], ['5', '5 人'], ['6', '6 人']],
      { hint: '每次「随机组队」按此人数分队；组队台里还能逐队微调' }
    ) +
    fieldSelect(
      'teamsPerMatch',
      '每场比赛',
      perMatch,
      [['2', '组 vs 组'], ['3', '组 vs 组 vs 组'], ['4', '组 vs 组 vs 组 vs 组']],
      { hint: '小组赛每场同场竞技的队伍数；淘汰赛恒为 2 队对阵' }
    ) +
    fieldSwitch(
      'loserBracket',
      '启用败者组（双败淘汰）',
      loser,
      { hint: '关闭 = 单败淘汰，输一场即淘汰；开启 = 输一场进败者组，输两场才淘汰' }
    ) +
    fieldNum('groupCount', '小组赛组数 (0 自动)', rules.groupCount ?? 0, {
      hint: `自动 = 在「每组排得满一场」的前提下尽量多分组（队伍越多组越多，小组赛更短、出线名额更多）；当前${teams ? ` ${teams} 支队会分 ${autoGroups} 组` : '还没有队伍'}。填了就按填的来`,
    }) +
    fieldNum('knockoutSize', '淘汰赛规模 (0 自动)', rules.knockoutSize ?? 0, {
      hint: maxSize
        ? `2 的幂且不超过 ${maxSize}（当前 ${s?.format?.teams || 0} 支队）；改后需重新生成赛程`
        : '2 的幂，如 16 表示十六强；需先组队',
    }) +
    // 单轮目标只对数值型有意义（时间型的「目标」是跑完而不是够分）
    (sc.timeBased
      ? ''
      : fieldNum('targetScore', '单轮目标 (0 不限)', rules.targetScore)) +
    fieldSwitch('allowDraw', '小组赛允许平局', rules.allowDraw) +
    // 这里刻意**不再复述一遍赛制**：那几句话是「通用说明」，与实际参数一旦不一致
    // （比如单败的届里写着双败怎么打）就是错的。规则由参数推导、显示在
    // 「总览 → 比赛规则」里，改完保存一看便知。
    `<div class="notice" style="grid-column:1/-1">保存后，<b>总览 → 比赛规则</b>` +
    `会按当前参数（每队人数 / 每场同场队伍数 / 小组数 / ${loser ? '双败' : '单败'}淘汰…）` +
    `自动重算并展示本届的完整规则。</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存规则</button></div></form>`
  );
}

/** 积分制规则表单。 */
function leagueRulesForm(rules) {
  const sc = normScoring(rules);
  return (
    `<form class="form form--2" data-form="rules">` +
    scoringFields(rules) +
    fieldNum('teamSize', '每队人数', rules.teamSize, { hint: '2 即 2v2，每局自动从参与名单排阵' }) +
    fieldNum('totalRounds', '总轮次', rules.totalRounds) +
    fieldNum('pointsWin', '胜方积分', rules.pointsWin) +
    fieldNum('pointsLose', '负方积分', rules.pointsLose) +
    fieldNum('pointsDraw', '平局积分', rules.pointsDraw) +
    fieldNum('minRankPlayed', '参与排名最少场次', rules.minRankPlayed ?? 5, {
      hint: '不足该场次的选手列在榜尾、不参与名次（仍显示场次与成绩）',
    }) +
    fieldSwitch('allowDraw', '允许平局', rules.allowDraw) +
    fieldSwitch('fairRotation', '公平轮换（均衡出场）', rules.fairRotation) +
    `<div class="notice" style="grid-column:1/-1">赛制：每局从参与名单自动排 2v2 阵容 → 逐局独立结算 → ` +
    `按<b>均分（总${esc(sc.label)} ÷ 场次）</b>排名` +
    `${sc.lowWins ? `，同分再比完成场次与总${esc(sc.label)}` : ''}，` +
    `积分记在实际出场的选手名下。</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存规则</button></div></form>`
  );
}

/* ---------------------- 比赛状态：开赛与锁定 ---------------------- */

/**
 * 锁定包装：把结构性面板整体变成不可交互区域。
 *
 * ``<fieldset disabled>`` 会禁用其中所有表单控件，再叠加 ``pointer-events: none``
 * 连拖拽也一并失效；面板里的说明文字仍然可读（不会被藏起来）。
 */
function lockedWrap(html, locked, note) {
  if (!locked) return html;
  return (
    `<div class="locked"><div class="locked__bar"><b>已锁定</b>` +
    `<span>${esc(note || '比赛已开始，这里不能再调整；要改动请先在「比赛状态」里解除锁定。')}</span></div>` +
    `<fieldset class="locked__body" disabled>${html}</fieldset></div>`
  );
}

/** 开赛前的准备情况：给二次确认弹窗与面板用同一份判断。 */
export function startReadiness(s) {
  s = s || App.state || {};
  const evt = s.event || {};
  const fmt = s.format || {};
  const players = (s.players || []).length;
  const joined = (s.participants || []).length;
  const teams = fmt.teams || (s.teams || []).length || 0;
  const rounds = (s.rounds || []).length;
  const league = (s.rules?.format || 'tournament') === 'league';
  const warnings = [];
  if (!rounds) warnings.push('还没有生成赛程：锁定后赛程重建会被拒绝，建议先生成赛程。');
  if (!league && !teams) warnings.push('锦标赛制还没有组队：锁定后组队会被拒绝，建议先随机组队。');
  if (!joined && !players) warnings.push('还没有可用选手，请先录入选手并确认参与名单。');
  return {
    locked: Boolean(evt.locked),
    league,
    players,
    joined: joined || players,
    teams,
    rounds,
    format: league ? '积分制' : '锦标赛制',
    ready: warnings.length === 0,
    warnings,
  };
}

/** 比赛状态面板：开赛（二次确认）/ 解除锁定（二次确认）。 */
function lockPanelHtml(s) {
  const r = startReadiness(s);
  const evt = s.event || {};
  const body = r.locked
    ? `<div class="etime etime--slim">` +
      `<span class="etime__pill etime__pill--running">比赛已开始</span>` +
      `<span class="etime__text">` +
      esc(evt.lockedAt ? `${fmtFull(evt.lockedAt)} 锁定` : '已锁定') +
      (evt.startTime ? ` · 开赛 ${esc(fmtFull(evt.startTime))}` : '') +
      `</span></div>` +
      `<div class="notice" style="margin-top:10px"><b>已冻结</b>：赛制、每队人数、每场同场队伍数、败者组开关、` +
      `参赛名单、重新组队、赛程重建 / 清空、删除选手。<br>` +
      `<b>仍然可用</b>：<b>直播开关</b>、<b>对局替补 / 队伍换人</b>（替上的人不在名单里会自动加入）、录分与重置、` +
      `时间登记、赛事信息、新增选手档案。</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="team-sub">队伍换人</button>` +
      `<button class="btn btn--sm btn--danger" type="button" data-act="event-unlock">解除锁定</button></div>`
    : `<div class="etime etime--slim">` +
      `<span class="etime__pill etime__pill--upcoming">尚未开赛</span>` +
      `<span class="etime__text">名单、赛制与赛程都可以自由调整</span></div>` +
      `<div class="notice" style="margin-top:10px">确认无误后点「开始比赛」：` +
      `<b>赛制与参赛名单会被冻结</b>，但<b>直播开关</b>与<b>对局替补 / 队伍换人</b>始终可用。` +
      `开始比赛需要二次确认。</div>` +
      `<div class="kv kv--inline" style="margin-top:10px">` +
      `<div class="kv__row"><dt>赛制</dt><dd>${esc(r.format)}</dd></div>` +
      `<div class="kv__row"><dt>参与选手</dt><dd>${r.joined} 人</dd></div>` +
      `<div class="kv__row"><dt>队伍</dt><dd>${r.league ? '—' : `${r.teams} 支`}</dd></div>` +
      `<div class="kv__row"><dt>赛程</dt><dd>${r.rounds} 场</dd></div></div>` +
      (r.warnings.length
        ? `<div class="notice notice--warn" style="margin-top:10px">${r.warnings.map(esc).join('<br>')}</div>`
        : '') +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="event-start">开始比赛（二次确认）</button>` +
      `<button class="btn btn--sm" type="button" data-act="team-sub">队伍换人</button></div>`;
  return panelHtml('比赛状态', r.locked ? '已开赛 · 赛制与名单已锁定' : '开赛前请确认名单与赛制', body);
}

/** 赛制切换面板：两套规则并存，随时可换；下方实时预览用户端看到的规则。 */
function formatPanelHtml(s) {
  const league = isLeague(s);
  const preview = rulebookBodyHtml(s);
  const body =
    `<div class="notice">当前赛制：<b>${league ? '积分制' : '锦标赛制'}</b> —— ` +
    (league
      ? '每局自动轮换排阵，按均分排名，不淘汰。'
      : '随机组队后固定队伍，小组赛按名次分排名，之后进入淘汰赛。') +
    `</div>` +
    lockedWrap(
      `<div class="form-actions" style="margin-top:10px">` +
        `<button class="btn btn--sm${league ? '' : ' btn--primary'}" type="button" data-act="format-set" data-format="tournament">切换到锦标赛制</button>` +
        `<button class="btn btn--sm${league ? ' btn--primary' : ''}" type="button" data-act="format-set" data-format="league">切换到积分制</button>` +
        `</div>`,
      s.event?.locked,
      '比赛已开始，赛制已锁定（切换赛制会清空赛程）'
    ) +
    `<div class="panel__hint" style="margin-top:8px">两套赛制的赛程互不通用，切换会清空现有对局与比分（报名池与队伍保留）。</div>` +
    (preview
      ? `<div class="rules rules--preview"><div class="rules__head">` +
        `用户端「比赛规则」面板当前会显示以下内容（随参数实时变化）</div>${preview}</div>`
      : '');
  return panelHtml('赛制', '积分制 / 锦标赛制', body);
}

export function adminPanelHtml(s) {
  if (!s) return `<div class="panel"><div class="empty"><b>状态未就绪</b>请稍候</div></div>`;
  // 只读查看（别人的届 / 未登录）：写接口都作用在「当前届」上，这里不给任何管理入口，
  // 否则一次手滑就写到别的届上去了。
  if (s.readOnly) {
    return (
      `<div class="panel"><div class="panel__head"><h2>只读查看</h2>` +
      `<span class="panel__hint">${esc(s.eventId || '')} · 不是你的届</span></div>` +
      `<div class="panel__body">` +
      `<div class="notice">现在看的是 <b>${esc(s.eventName || s.eventId || '')}</b>：` +
      `它不归你管（或你还未登录），所以整页都是只读的。` +
      `需要编辑就用自己的账号进入自己创建的届。</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm" type="button" data-act="route-events">全部赛事</button>` +
      `<button class="btn btn--sm" type="button" data-act="route-home">回主页</button>` +
      `</div></div></div>`
    );
  }
  const evt = s.event || {};
  const rules = s.rules || {};
  // 这里只用**公开**的 stream（脱敏白名单：enabled / mode / 文案）。
  // 媒体服务器地址与凭据不在这页的渲染范围里，所以不必去拿私有配置——
  // 拿不到值时渲染出空输入框、一保存就把地址清空，正是要避免的那种事故。
  // 界面配置（主题色 / 分享图 / 展示开关）同理：它按届保存，但**只有服务器管理员**能改，
  // 所以表单也搬去了「服务器」页（见 members.js 的 uiPanelHtml）。
  // 直播配置同理，而且更彻底：这一页**没有**任何直播表单了——直播没有总开关
  // （只要有赛事就允许直播），媒体服务器地址 / 账号 / 凭据全是站点级设置，
  // 统一在「服务器 → 直播配置」里维护（写接口也把关，见 main._apply_stream_patch）。

  const eventTime = s.eventTime || {};
  // 已结束的届：赛事信息转为只读（服务端同样会拒），但通知照旧可发
  const closed = (evt.status || 'active') === 'closed';
  const eventForm = `<form class="form form--2" data-form="event">` +
    fieldText('title', '赛事标题', evt.title) +
    fieldText('subtitle', '副标题', evt.subtitle) +
    fieldSelect('status', '届状态', evt.status || 'active', [
      ['draft', '筹备中'],
      ['active', '进行中'],
      ['closed', '已结束'],
    ]) +
    // 简介可以**换行**（地图 / 规则 / 注意事项分行写），所以是 textarea 不是单行输入框
    fieldArea('brief', '比赛简介', evt.brief, {
      rows: 4,
      ph: '介绍这一届（可留空；可以换行，最多 6 行 / 200 字）',
      maxlength: 200,
      hint: '可以换行（最多 6 行 / 200 字）。留空则总览与主页 / 全部赛事的卡片都不显示这一项；' +
        '「比赛信息」卡片里的「比赛简介」用的就是它',
    }) +
    // 「比赛类型」已退休：它只换称呼（选手 → 车手…），而组织者要的是规则跟着赛制走。
    // 界面上不再提供这一项；老数据里的取值会被忽略。
    fieldSwitch('ranked', '排名模式（关闭 = 娱乐记录，不排名 / 不晋级）', evt.ranked !== false) +
    fieldText('venue', '场地', evt.venue) +
    fieldText('organizer', '主办方', evt.organizer) +
    fieldText('logoText', 'Logo 文字', evt.logoText) +
    fieldDateTime('startTime', '开始时间', evt.startTime, { hint: '留空表示尚未确定' }) +
    fieldDateTime('endTime', '结束时间', evt.endTime, {
      hint: '留空 = 尚未结束 / 待定；填了即视为已结束',
    }) +
    `<div style="grid-column:1/-1">${eventTimeEditorHtml(eventTime)}</div>` +
    // 赛事信息（Markdown）改用 MD 编辑器：表单里**不再放 textarea**，
    // 否则「保存赛事信息」会把编辑器写好的内容用旧值覆盖回去。
    `<div style="grid-column:1/-1" class="infocard">` +
    `<div class="infocard__head"><b>赛事信息</b>` +
    `<span class="panel__hint">Markdown · 图片 · 显示在「比赛规则」末尾</span>` +
    (closed
      ? `<span class="badge badge--lose">已结束 · 只读</span>`
      : `<button class="btn btn--sm" type="button" data-act="event-info-edit">${icon('edit')}编辑</button>`) +
    `</div>` +
    (s.rulebook?.noteHtml
      ? `<div class="md infocard__body">${s.rulebook.noteHtml}</div>`
      : `<div class="infocard__body"><span class="panel__hint">还没有内容；点「编辑」写参赛须知、场地位置等。</span></div>`) +
    (closed
      ? `<div class="notice" style="margin-top:8px">本届已结束：赛事信息只能查看；` +
        `要继续发内容请用下面的「赛事通知」（赛后仍可发）。</div>`
      : '') +
    `</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存赛事信息</button></div></form>`;

  const rulesForm = isLeague(s) ? leagueRulesForm(rules) : tournamentRulesForm(rules, s);

  // 已完结的届：**不再整片锁死**。
  // 真正只读的只有「赛事信息」正文（服务端把关，见 main.api_config）；比分、名单、组队、
  // 赛程、通知、推送到群照旧可用——用户报的就是「一结束整页都动不了，连发通知、
  // 推比赛结果都找不到入口」。要收尾 / 继续都用得起，才谈得上「管得住」。
  const finished = isFinished(s);
  // 手动「恢复进行」过的届：不会再被自动结束（见 models.EventInfo.keep_open），这里明说
  const keepOpen = Boolean(s.event?.keepOpen);
  const editing =
    // 开赛状态放在最前：下面哪些面板会被冻结，一眼可见
    lockPanelHtml(s) +
    // 赛事通知：发布后打开本届的人会自动弹窗；**赛前赛后都能发**（它从来不进只读区）
    `<div class="panel" id="adminNotices"></div>` +
    formatPanelHtml(s) +
    panelHtml('赛事信息', '公开展示 · Markdown', eventForm) +
    // 推送到群（走机器人）：赛事管理员（自己创建的届）与服务器管理员都能用；
    // **赛后照样能用**——「比赛结果」这条恰恰是赛后最常推的
    (canManageEvents() ? qqbotPushPanelHtml(s) : '') +
    panelHtml(
      '比赛规则',
      isLeague(s) ? '积分制' : '双败淘汰制',
      lockedWrap(rulesForm, s.event?.locked, '比赛已开始，赛制与人数已锁定')
    ) +
    participantsPanelHtml(s) +
    panelHtml(
      '组队台',
      s.event?.locked ? '已锁定 · 用「队伍换人」调整' : '固定分组 · 拖拽调整队友',
      `<div class="tool-group" style="margin-bottom:10px">` +
        `<button class="btn btn--sm${s.event?.locked ? ' btn--primary' : ''}" type="button" data-act="team-sub">` +
        `队伍换人（不重排队伍）</button>` +
        // 删除组队：**名单与选手档案都留着**，只是把队伍清掉（顺带清赛程）——
        // 「先删组队再重排 / 再让人报名」就走这里（见 store.signup_blocked 的说明）
        `<button class="btn btn--sm btn--danger" type="button" data-act="teams-clear">` +
        `删除组队（保留名单）</button>` +
        `<span class="panel__hint">把某队的一位队员换成候选池里的其他人；不在参与名单里会自动加入。` +
        `「删除组队」只清队伍，名单、选手档案、参与状态都不动（赛程会一起清空，需重新生成）</span>` +
        `</div>` +
        lockedWrap('<div id="teamHost"></div>', s.event?.locked, '比赛已开始，队伍已锁定')
    ) +
    schedulePanelHtml(s);

  return (
    panelHtml('系统状态', '实时诊断', `<div id="diagBox" class="kv"><div class="kv__row"><dt>加载中</dt><dd>…</dd></div></div>`) +
    (keepOpen && !finished
      ? `<div class="panel"><div class="panel__body"><div class="notice">这一届是你手动<b>恢复进行</b>过的：` +
        `<b>不会再被自动结束</b>，下面的面板照旧可用。要收尾就点「标记结束」（或登记结束时间），` +
        `那会解除这个状态。</div></div></div>`
      : '') +
    (finished
      ? `<div class="panel"><div class="panel__head"><h2>已结束</h2>` +
        `<span class="panel__hint">${esc(s.eventId || '')}</span></div>` +
        `<div class="panel__body"><div class="notice notice--warn">这一届已标记结束（冠军决出 / ` +
        `积分制全部打完时会自动结束）。<b>下面的面板照旧能用</b>：比分、名单、组队、赛程、` +
        `赛事通知、推送到群；只有「赛事信息」正文是只读的。<br>` +
        `点「恢复进行」还会<b>顺手钉住它</b>——恢复之后不会再被自动结束关一次。</div>` +
        `<div class="tool-group" style="margin-top:10px">` +
        `<button class="btn btn--sm btn--primary" type="button" data-act="event-reopen" ` +
        `data-id="${esc(s.eventId || '')}">恢复进行（不再自动结束）</button>` +
        `</div></div></div>`
      : '') +
    editing
  );
}

/** 本届参与名单：报名池可以先录满，每届再勾选真正上场的人。 */
function participantsPanelHtml(s) {
  const players = s.players || [];
  const joined = new Set(s.participants || []);
  const explicit = Boolean(s.participantsSet);
  const card = (p) => {
    const on = joined.has(p.id);
    const idle = p.active === false;
    const meta = [p.tag || p.id, idle ? '停用' : '']
      .filter(Boolean)
      .join(' · ');
    return (
      `<label class="pick${on ? '' : ' pick--off'}${idle ? ' pick--disabled' : ''}" data-participant-row="${esc(p.id)}">` +
      `<input type="checkbox" data-role="participant" value="${esc(p.id)}"` +
      `${on ? ' checked' : ''}${idle ? ' disabled' : ''}>` +
      avaHtml(p, 'xs') +
      `<span class="pick__txt"><span class="who__name">${esc(p.name || p.id)}</span>` +
      `<span class="pick__meta">${esc(meta)}</span></span></label>`
    );
  };
  const body =
    `<div class="notice">` +
    (explicit
      ? joined.size
        ? `已手动指定本届参与名单：<b>${joined.size}</b> / ${players.length} 人。`
        : `本届参与名单是<b>空的</b>（没有人参与）：现有赛程按旧名单保留，` +
          `重新勾选并保存后才会按新名单重排。`
      : `尚未指定，默认<b>全员参与</b>（${players.length} 人）；保存后即成为显式名单。`) +
    `</div>` +
    `<div class="notice" style="margin-top:8px">` +
    `名单以<b>成员列表</b>为准：点「从成员列表选择」勾人参加本届——本届还没有档案的会` +
    `<b>按成员资料自动建好</b>（姓名 / QQ / 头像 / 游戏 UUID），以后改成员资料这里跟着更新。` +
    `下面的勾选框是本届已有的选手档案，需要时也能直接改。` +
    `</div>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="participants-members">` +
    `从成员列表选择</button>` +
    `<span class="tool-group__sep"></span>` +
    `<button class="btn btn--sm" type="button" data-act="participants-all">全选</button>` +
    `<button class="btn btn--sm" type="button" data-act="participants-none">全不选</button>` +
    `<button class="btn btn--sm" type="button" data-act="participants-invert">反选</button>` +
    `<span class="tool-group__sep"></span>` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="participants-save">保存参与名单</button>` +
    `</div>` +
    `<div class="notice" style="margin-top:10px">` +
    (isLeague(s)
      ? '保存后会按新名单<b>自动重排未结算的对局</b>（已结算的比分不受影响）；随后点「生成赛程」重新编排。'
      : '参与名单确定后：在「组队台」点<b>随机组队</b>分配队友（队友随机、全程固定），再<b>生成赛程</b>（小组赛 + 双败淘汰）。') +
    `</div>` +
    `<div class="pick-grid">${players.map(card).join('') || '<div class="empty"><b>暂无选手</b></div>'}</div>`;
  const locked = Boolean(s.event?.locked);
  return panelHtml(
    '本届参与名单',
    locked ? '比赛已开始 · 名单已锁定' : '来自成员列表 · 勾选参加本届',
    lockedWrap(
      body +
        (locked
          ? `<div class="notice" style="margin-top:10px">名单已锁定。` +
            `<b>替补不受影响</b>：在赛程里点某位选手安排替补（可只替一场），` +
            `替上的人若不在名单中会<b>自动加入</b>。</div>`
          : ''),
      locked,
      '比赛已开始，参赛名单已锁定（替补仍可用）'
    )
  );
}

/* 「队伍信息」面板已移除：队名 / 缩写 / 主题色 / 分组 / 成员全部在「组队台」上就地编辑
 * （见 static/js/teams.js），两处重复的表单只会各自漂移。 */

/* 选手名单的编辑面板已移除：名单统一在「选手」页处理（卡片上可新增 / 编辑 / 删除） */

function schedulePanelHtml(s) {
  const league = isLeague(s);
  const locked = Boolean(s.event?.locked);
  // 结构性操作（重建赛程 / 重新组队 / 删除赛程）在开赛后冻结
  const clearBtn =
    `<button class="btn btn--sm btn--danger" type="button" data-act="rounds-clear">` +
    `删除赛程（全部比赛）</button>`;
  const structural = league
    ? `<button class="btn btn--sm btn--primary" type="button" data-act="schedule-generate">生成赛程</button>` +
      clearBtn
    : `<button class="btn btn--sm btn--primary" type="button" data-act="quick-group">快速创建分组</button>` +
      `<button class="btn btn--sm" type="button" data-act="tournament-generate">生成赛程</button>` +
      `<button class="btn btn--sm" type="button" data-act="teams-auto">随机组队</button>` +
      clearBtn;
  // 运营性操作（补赛 / 追加空局 / 重载 / 导出）随时可用
  const ops = league
    ? `<button class="btn btn--sm" type="button" data-act="schedule-append">追加补赛</button>` +
      `<button class="btn btn--sm" type="button" data-act="round-append">追加空局</button>`
    : '';
  const body =
    lockedWrap(
      `<div class="tool-group">${structural}</div>`,
      locked,
      '比赛已开始，赛程重建 / 重新组队 / 清空赛程已锁定'
    ) +
    `<div class="tool-group"${locked ? ' style="margin-top:8px"' : ''}>${ops}` +
    `<button class="btn btn--sm" type="button" data-act="reload">从数据库重载</button>` +
    `<button class="btn btn--sm" type="button" data-act="export">导出配置</button>` +
    `<span class="tool-group__sep"></span>` +
    `<button class="btn btn--sm btn--danger" type="button" data-act="logout">退出登录</button></div>` +
    (league
      ? `<div class="notice" style="margin-top:10px"><b>生成赛程</b>按参与名单自动 2v2 轮换，` +
        `并<strong>覆盖现有全部对局与比分</strong>；<b>追加补赛</b>为出场最少的选手补局，不影响已有比分。</div>` +
        `<div class="notice" style="margin-top:8px">点击对局里的选手可换人 / 移出，空位可补人；` +
        `参与名单变化时会自动重排未结算的对局。</div>`
      : `<div class="notice" style="margin-top:10px"><b>快速创建分组</b>：比赛开始前（还没有任何结果时）` +
        `一键「重新随机组队 + 生成赛程」，弹窗里可调<b>每个组的人数 / 小组数 / 淘汰赛规模 / 每场同场队伍数 / 败者组开关</b>，` +
        `并实时预估结构（人少自动短赛程，人多则拉长）。</div>` +
        `<div class="notice" style="margin-top:8px">分步操作用 <b>随机组队</b> + <b>生成赛程</b>；` +
        `两者都会<strong>覆盖现有全部对局与比分</strong>。</div>` +
        `<div class="notice" style="margin-top:8px">录入比分后胜者自动晋级、败者进败者组；` +
        `队伍长期没人或人数不足时，在对局上点「<b>弃权</b>」即可让对方直接晋级。</div>`) +
    (locked
      ? `<div class="notice" style="margin-top:8px">比赛已开始：<b>录分、重置、时间、直播、弃权、队伍换人</b>` +
        `都照常可用，只有上方的结构性操作被锁定。</div>`
      : '');
  return panelHtml('赛程与系统', league ? '积分制操作' : '锦标赛操作', body);
}

const PHASE_TEXT = {
  idle: '尚未生成赛程',
  group: '小组赛进行中',
  knockout: '淘汰赛进行中',
  finished: '已结束',
};

function scheduleQualityText() {
  const s = App.state || {};
  const fmt = s.format || {};
  const prog = s.progress || [];
  const done = prog.reduce((n, p) => n + p.done, 0);
  const total = prog.reduce((n, p) => n + p.total, 0);
  return `${PHASE_TEXT[s.phase] || s.phase || '—'} · 队伍 ${fmt.teams || 0} 支 · 淘汰赛 ${
    fmt.size || 0
  } 强 · 已完成 ${done}/${total} 场`;
}

export async function refreshDiagnostics() {
  const box = qs('#diagBox');
  if (!box) return;
  try {
    const d = await api('/diagnostics', { auth: true });
    const kv = [
      ['数据库', d.databasePath],
      ['当前届', `${d.eventName || '—'}（${d.eventId || '—'}）· ${d.eventStatus || 'active'} · 共 ${d.eventCount ?? 1} 届`],
      ['版本', d.revision],
      ['更新时间', d.updatedAt || '—'],
      ['在线客户端', d.ws?.online ?? 0],
      ['广播次数', d.ws?.broadcasts ?? 0],
      ['头像缓存', `${d.avatarCache?.files ?? 0} 文件 / ${Math.round((d.avatarCache?.bytes || 0) / 1024)} KB`],
      [
        '上传图片',
        `${d.uploads?.files ?? 0} 个 / ${Math.round((d.uploads?.bytes || 0) / 1024)} KB` +
          `（通知 / 信息插图与本地头像，同图只存一份）`,
      ],
      ['待升级凭据', d.legacyCredentials ? `${d.legacyCredentials} 位成员仍是旧格式` : '无'],
      ['赛制状态', scheduleQualityText()],
    ]
      .map(([k, v]) => `<div class="kv__row"><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`)
      .join('');
    const issues = (d.issues || []).length
      ? d.issues.map((i) => `<div class="notice notice--warn" style="margin-top:8px">${esc(i)}</div>`).join('')
      : `<div class="notice" style="margin-top:8px">配置校验通过，无提示项。</div>`;
    box.innerHTML = kv + issues;
    log.debug('诊断数据已加载', d);
  } catch (err) {
    box.innerHTML = `<div class="kv__row"><dt>错误</dt><dd>${esc(err.message)}</dd></div>`;
  }
}
