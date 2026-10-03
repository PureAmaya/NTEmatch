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
  qs,
  qsa,
  stateKey,
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
    panel.innerHTML =
      `<div class="panel"><div class="panel__head"><h2>赛事管理</h2>` +
      `<span class="panel__hint">当前权限：成员</span></div><div class="panel__body">` +
      `<div class="notice notice--warn">你是以<b>成员</b>身份登录的，不能管理赛事。` +
      `可在「我的」页修改个人资料、直播间名字与凭据；` +
      `如需创建 / 管理赛事，请联系服务器管理员把你的权限提升为「赛事管理员」。</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="route-user">前往「我的」</button>` +
      `</div></div></div>`;
  } else if (App.state) {
    gate.innerHTML = '';
    panel.innerHTML = adminPanelHtml(App.state);
    mountTeams(qs('#teamHost'));
  } else {
    return; // 登录了但状态还没到：先不动，等状态到位指纹会变、再画
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
  `<div class="gate__title">赛事管理登录</div>` +
  `<p class="gate__desc">用<b>成员密钥</b>或<b>服务器管理 KEY</b> 登录，以解锁比分录入、赛程生成与配置编辑。` +
  `（普通成员只能修改自己的资料，见「我的」页。）</p>` +
  `<div class="field"><label for="adminKey">密钥</label>` +
  `<input id="adminKey" type="password" autocomplete="current-password" placeholder="请输入成员密钥或管理 KEY"></div>` +
  `<button class="btn btn--primary btn--block" type="button" data-act="admin-login">登录</button>` +
  `<p class="gate__hint">成员密钥由服务器管理员在「服务器 → 成员管理」里生成。` +
  `<br>服务器主 KEY 见服务启动日志（默认 <b>NTE-ADMIN</b>，请登录后尽快修改）。` +
  `<br>在本机（localhost / 127.0.0.1）直接访问时无需登录。</p>` +
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

/** 锦标赛制规则表单。 */
function tournamentRulesForm(rules, s) {
  const maxSize = s?.format?.maxSize || 0;
  const perMatch = Number(rules.teamsPerMatch) || 2;
  const loser = rules.loserBracket !== false;
  return (
    `<form class="form form--2" data-form="rules">` +
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
      hint: '自动时约 4 队一组，并尽量让每组队数刚好排满整场',
    }) +
    fieldNum('knockoutSize', '淘汰赛规模 (0 自动)', rules.knockoutSize ?? 0, {
      hint: maxSize
        ? `2 的幂且不超过 ${maxSize}（当前 ${s?.format?.teams || 0} 支队）；改后需重新生成赛程`
        : '2 的幂，如 16 表示十六强；需先组队',
    }) +
    fieldNum('targetScore', '单局目标分 (0 不限)', rules.targetScore) +
    fieldSwitch('allowDraw', '小组赛允许平局', rules.allowDraw) +
    `<div class="notice" style="grid-column:1/-1">赛制：确定参与名单 → <b>随机组队</b>（队友随机、全程固定）→ ` +
    `小组赛轮转（每场 ${perMatch === 2 ? '组 vs 组' : `${perMatch} 队同场`}，按名次分排名）→ ` +
    `总排名前 N 名进入<b>${loser ? '双败淘汰（含败者组）' : '单败淘汰'}</b>` +
    `${loser ? '，胜者组冠军与败者组冠军争夺总冠军' : '，最后一轮即决赛'}。</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存规则</button></div></form>`
  );
}

/** 积分制规则表单。 */
function leagueRulesForm(rules) {
  return (
    `<form class="form form--2" data-form="rules">` +
    fieldNum('teamSize', '每队人数', rules.teamSize, { hint: '2 即 2v2，每局自动从参与名单排阵' }) +
    fieldNum('totalRounds', '总轮次', rules.totalRounds) +
    fieldNum('pointsWin', '胜方积分', rules.pointsWin) +
    fieldNum('pointsLose', '负方积分', rules.pointsLose) +
    fieldNum('pointsDraw', '平局积分', rules.pointsDraw) +
    fieldNum('minRankPlayed', '参与排名最少场次', rules.minRankPlayed ?? 5, {
      hint: '不足该场次的选手列在榜尾、不参与名次（仍显示场次与得分）',
    }) +
    fieldSwitch('allowDraw', '允许平局', rules.allowDraw) +
    fieldSwitch('includeSubstitutes', '人数不足时启用替补', rules.includeSubstitutes) +
    fieldSwitch('fairRotation', '公平轮换（均衡出场）', rules.fairRotation) +
    `<div class="notice" style="grid-column:1/-1">赛制：每局从参与名单自动排 2v2 阵容 → 逐局独立结算 → ` +
    `按<b>均分（总得分 ÷ 场次）</b>排名，积分记在实际出场的选手名下。</div>` +
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
      `<b>仍然可用</b>：<b>直播开关</b>、<b>替补换人</b>（替补不在名单里会自动加入）、录分与重置、` +
      `时间登记、赛事信息与界面配置、新增选手档案。</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="team-sub">替补换人</button>` +
      `<button class="btn btn--sm btn--danger" type="button" data-act="event-unlock">解除锁定</button></div>`
    : `<div class="etime etime--slim">` +
      `<span class="etime__pill etime__pill--upcoming">尚未开赛</span>` +
      `<span class="etime__text">名单、赛制与赛程都可以自由调整</span></div>` +
      `<div class="notice" style="margin-top:10px">确认无误后点「开始比赛」：` +
      `<b>赛制与参赛名单会被冻结</b>，但<b>直播开关</b>与<b>替补换人</b>始终可用。` +
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
      `<button class="btn btn--sm" type="button" data-act="team-sub">替补换人</button></div>`;
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
  // 公开状态里的 stream 是脱敏白名单（根地址 / 流名 / 推流地址都被剥掉），
  // 用它渲染表单会出现空输入框、一保存就把地址清空——所以管理端必须取私有配置。
  const stream = App.private?.stream || s.stream || {};
  const ui = s.ui || {};

  const eventTime = s.eventTime || {};
  const eventForm = `<form class="form form--2" data-form="event">` +
    fieldText('title', '赛事标题', evt.title) +
    fieldText('subtitle', '副标题', evt.subtitle) +
    fieldSelect('status', '届状态', evt.status || 'active', [
      ['draft', '筹备中'],
      ['active', '进行中'],
      ['closed', '已结束'],
    ]) +
    fieldText('brief', '比赛简介', evt.brief, {
      ph: '一句话介绍这一届（可留空）',
      maxlength: 30,
      hint: '≤30 字。留空则总览与主页 / 全部赛事的卡片都不显示这一项',
    }) +
    fieldSelect(
      'sport',
      '比赛类型',
      evt.sport || 'volleyball',
      (s.sportPresets || []).map((p) => [p.key, p.label]),
      { hint: '只影响界面称呼（参赛者 / 成绩 / 场次），不影响数据与赛制' }
    ) +
    fieldSwitch('ranked', '排名模式（关闭 = 娱乐记录，不排名 / 不晋级）', evt.ranked !== false) +
    fieldText('venue', '场地', evt.venue) +
    fieldText('organizer', '主办方', evt.organizer) +
    fieldText('logoText', 'Logo 文字', evt.logoText) +
    fieldDateTime('startTime', '开始时间', evt.startTime, { hint: '留空表示尚未确定' }) +
    fieldDateTime('endTime', '结束时间', evt.endTime, {
      hint: '留空 = 尚未结束 / 待定；填了即视为已结束',
    }) +
    `<div style="grid-column:1/-1">${eventTimeEditorHtml(eventTime)}</div>` +
    `<div style="grid-column:1/-1">${fieldArea('rulesText', '补充说明', evt.rulesText, {
      hint: '选填：会附在用户端「比赛规则」面板的末尾（规则主体由赛制参数自动生成）',
    })}</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存赛事信息</button></div></form>`;

  const rulesForm = isLeague(s) ? leagueRulesForm(rules) : tournamentRulesForm(rules, s);

  const uiForm = `<form class="form form--2" data-form="ui">` +
    fieldSelect('accent', '主题色', ui.accent, [
      ['cyan', '青'], ['violet', '紫'], ['magenta', '品红'], ['amber', '琥珀'], ['lime', '青柠'],
    ]) +
    // 注：UUID / QQ 属于隐私字段，任何情况下都不下发用户端，因此不再提供开关
    fieldSwitch('showAvatar', '显示选手头像', ui.showAvatar) +
    fieldSwitch('revealResults', '公开展示结果', ui.revealResults) +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存界面配置</button></div></form>`;

  const streamForm = `<form class="form form--2" data-form="stream">` +
    fieldSwitch('enabled', '启用直播', stream.enabled) +
    fieldSelect(
      'mode',
      '默认播放线路',
      stream.mode,
      [
        ['auto', '自动（WebRTC，不通再退 HLS）'],
        ['webrtc', 'WebRTC（8889，UDP，低延迟）'],
        ['hls', 'HLS（8888，TCP，抗抖动）'],
      ],
      { hint: '观众还能在直播页自行切换线路，不必改这里' }
    ) +
    fieldText('provider', '服务类型', stream.provider) +
    fieldText('streamKey', '默认流名（兜底）', stream.streamKey, {
      hint: '仅在选手没有流名时使用；选手各自的地址由他的推流流名决定（整届固定不变）',
    }) +
    fieldSwitch('verifyTls', '校验源站 HTTPS 证书', stream.verifyTls !== false, {
      hint: '只影响「信号探测」；自签名证书时关掉。观众侧仍需浏览器信任的证书',
    }) +
    `<div class="notice" style="grid-column:1/-1"><b>推流只有 WHIP；观众看直播只有两个地址：</b>` +
    `<code>&lt;WebRTC 根地址&gt;/&lt;流名&gt;/</code>（8889）与 <code>&lt;HLS 根地址&gt;/&lt;流名&gt;/</code>（8888），` +
    `打开就能看，播放器用的也是这两个。地址都是<b>源站地址</b>（本站不做反代），` +
    `因此站点是 HTTPS 时源站也要 HTTPS。</div>` +
    `<div style="grid-column:1/-1">${fieldText('baseUrl', 'WebRTC 根地址', stream.baseUrl, {
      hint: '8889 端口：WHIP 推流 + 观众观看地址，例如 https://live.example.com:8889',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldText('apiBase', 'MediaMTX API 地址', stream.apiBase, {
      hint: '默认 http://live.example.com:9997（mediamtx.yml 里 api: yes）。' +
        '只有媒体服务器上报「正在推流」的机位才会显示「直播中」；留空则不显示任何直播标记',
    })}</div>` +
    // 控制 API 开了鉴权（mediamtx.yml 的 authInternalUsers）时必须填下面两项，
    // 否则查询回 401，界面上就是那句「媒体服务器 API 不可达」
    `<div style="grid-column:1/-1">${fieldText('apiUser', 'MediaMTX API 用户名', stream.apiUser, {
      hint: '媒体服务器配了 API 鉴权时必填（等价于 curl -u 用户名:密码）；留空 = 请求不带认证',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldText('apiPass', 'MediaMTX API 密码', stream.apiPass, {
      type: 'password',
      hint: '属于凭据：只存服务端、只在管理端下发，观众端拿不到',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldText('hlsBase', 'HLS 根地址', stream.hlsBase, {
      hint: '8888 端口：观众观看地址，例如 https://live.example.com:8888',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldText('whipPush', '默认流名的 WHIP 地址', stream.whipPush, {
      hint: '主直播间（默认流名）的推流地址；选手 / 频道各自的地址由他们自己的流名派生',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldText('poster', '封面图 URL', stream.poster)}</div>` +
    `<div style="grid-column:1/-1">${fieldArea('note', '备注', stream.note)}</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存直播配置</button></div></form>`;

  const keyForm =
    `<div class="notice">一律以<b>加盐 PBKDF2</b> 存储（明文与无盐哈希都不落库），` +
    `也不会出现在「导出配置」的文件里。</div>` +
    `<form class="form form--2" data-form="admin-key" style="margin-top:10px">` +
    fieldText('key', '新的管理 KEY', '', {
      type: 'password',
      hint: '至少 6 位。保存后所有管理会话立即失效，需用新 KEY 重新登录。',
    }) +
    `<div class="form-actions" style="grid-column:1/-1">` +
    `<button class="btn btn--primary" type="submit">更新管理 KEY</button></div></form>`;

  // 已完结的届只留只读信息：编辑面板整体上锁，只放行「恢复进行」
  const finished = isFinished(s);
  const editing =
    // 开赛状态放在最前：下面哪些面板会被冻结，一眼可见
    lockPanelHtml(s) +
    formatPanelHtml(s) +
    panelHtml('赛事信息', '公开展示', eventForm) +
    // 推送到群：赛事管理员（自己创建的届）与服务器管理员都能用
    (canManageEvents() && !finished ? qqbotPushPanelHtml(s) : '') +
    panelHtml(
      '比赛规则',
      isLeague(s) ? '积分制' : '双败淘汰制',
      lockedWrap(rulesForm, s.event?.locked, '比赛已开始，赛制与人数已锁定')
    ) +
    panelHtml('界面配置', '主题与展示', uiForm) +
    panelHtml('直播配置', 'MediaMTX', streamForm) +
    participantsPanelHtml(s) +
    panelHtml(
      '组队台',
      s.event?.locked ? '已锁定 · 用「替补换人」调整' : '固定分组 · 拖拽调整队友',
      `<div class="tool-group" style="margin-bottom:10px">` +
        `<button class="btn btn--sm${s.event?.locked ? ' btn--primary' : ''}" type="button" data-act="team-sub">` +
        `替补换人（不重排队伍）</button>` +
        `<span class="panel__hint">把某队的一位队员换成替补；替补不在参与名单里会自动加入</span>` +
        `</div>` +
        lockedWrap('<div id="teamHost"></div>', s.event?.locked, '比赛已开始，队伍已锁定')
    ) +
    teamsPanelHtml(s) +
    schedulePanelHtml(s);

  return (
    panelHtml('系统状态', '实时诊断', `<div id="diagBox" class="kv"><div class="kv__row"><dt>加载中</dt><dd>…</dd></div></div>`) +
    // 服务器主 KEY 属于服务器级：只有服务器管理员能改
    (isServerAdmin() ? panelHtml('管理 KEY', '服务器主密钥', keyForm) : '') +
    panelHtml(
      '赛事地址',
      `${esc(s.eventId || 'e000')} · 独立路由`,
      `<div class="notice">这一届有自己的独立地址 <code>/${esc(s.eventId || 'e000')}</code>：` +
        `可以刷新、收藏、分享；页签里的「主页」随时回到总入口。` +
        `全部届次在主页的「管理赛事」里。</div>` +
        `<div class="tool-group" style="margin-top:10px">` +
        `<button class="btn btn--sm" type="button" data-act="route-events">全部赛事</button>` +
        `<button class="btn btn--sm" type="button" data-act="route-home">回主页</button>` +
        `<button class="btn btn--sm" type="button" data-act="event-refresh">刷新届次列表</button>` +
        `</div>`
    ) +
    (finished
      ? `<div class="panel"><div class="panel__head"><h2>已结束</h2>` +
        `<span class="panel__hint">${esc(s.eventId || '')} · 只读</span></div>` +
        `<div class="panel__body"><div class="notice notice--warn">这一届已经结束（锦标赛制的总冠军决出后会自动结束），` +
        `下面的编辑面板全部停用（只读）。要赛后补改，点「恢复进行」再改即可。</div>` +
        `<div class="tool-group" style="margin-top:10px">` +
        `<button class="btn btn--sm btn--primary" type="button" data-act="event-reopen" ` +
        `data-id="${esc(s.eventId || '')}">恢复进行</button>` +
        `</div></div></div>`
      : '') +
    lockedWrap(editing, finished, '这一届已经结束：只读。要改动先点上面的「恢复进行」。')
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
    const meta = [
      p.tag || p.id,
      p.substitute ? (isLeague(s) ? '替补' : '替补（锦标赛制不参与）') : '',
      idle ? '停用' : '',
    ]
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
      ? `已手动指定本届参与名单：<b>${joined.size}</b> / ${players.length} 人。`
      : `尚未指定，默认<b>全员参与</b>（${players.length} 人）；保存后即成为显式名单。`) +
    `</div>` +
    `<div class="tool-group" style="margin-top:10px">` +
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
    locked ? '比赛已开始 · 名单已锁定' : '手动选择上场选手',
    lockedWrap(
      body +
        (locked
          ? `<div class="notice" style="margin-top:10px">名单已锁定。` +
            `<b>替补不受影响</b>：在对局里点选手换人，或在上方「替补换人」里选队伍，` +
            `换上的人若不在名单中会<b>自动加入</b>。</div>`
          : ''),
      locked,
      '比赛已开始，参赛名单已锁定（替补仍可用）'
    )
  );
}

function memberNames(s, ids) {
  const map = new Map((s.players || []).map((p) => [p.id, p]));
  return (ids || []).map((pid) => map.get(pid)?.name || pid).join(' / ');
}

function teamsPanelHtml(s) {
  const rows = (s.teams || [])
    .map(
      (t) =>
        `<div class="editor-row" data-team-row="${esc(t.id)}">` +
        `<div class="field"><label>队名</label><input name="name" value="${esc(t.name)}"></div>` +
        `<div class="field"><label>缩写</label><input name="short" value="${esc(t.short)}"></div>` +
        `<div class="field"><label>主题色</label><input name="color" type="color" value="${esc(t.color || '#22e0e8')}"></div>` +
        `<div class="field"><label>队员</label><div class="panel__hint">${esc(memberNames(s, t.playerIds)) || '—'} · ${
          t.group ? `${esc(t.group)} 组` : '未分组'
        }</div></div>` +
        `<div class="editor-row__ops"><input type="hidden" name="id" value="${esc(t.id)}">` +
        `<button class="btn btn--sm btn--danger" type="button" data-act="team-del" data-id="${esc(t.id)}">删除</button></div></div>`
    )
    .join('');
  const league = isLeague(s);
  const body =
    `<div class="notice">队员由上方「组队台」拖拽编排；这里只调整队名、缩写与配色。删除队伍会清空当前赛程。` +
    (league ? '积分制下只有在「固定队伍」模式生成赛程时才会用到这些队伍。' : '') +
    `</div>` +
    `<div style="margin-top:10px">${rows || '<div class="empty"><b>尚未组队</b>确定参与名单后执行「随机组队」</div>'}</div>` +
    `<div class="form-actions" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="team-save">保存队名与配色</button></div>`;
  const locked = Boolean(s.event?.locked);
  return panelHtml(
    '队伍信息',
    locked ? '已锁定 · 用「替补换人」' : '名称 / 缩写 / 配色',
    lockedWrap(
      body,
      locked,
      '比赛已开始，队伍与队员已锁定；换人请用「替补换人」（不影响赛程）'
    )
  );
}

/* 选手名单的编辑面板已移除：名单统一在「选手」页处理（卡片上可新增 / 编辑 / 删除） */

function schedulePanelHtml(s) {
  const league = isLeague(s);
  const locked = Boolean(s.event?.locked);
  // 结构性操作（重建赛程 / 重新组队 / 清空比赛）在开赛后冻结
  const clearBtn =
    `<button class="btn btn--sm btn--danger" type="button" data-act="rounds-clear">` +
    `清空全部比赛</button>`;
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
      ? `<div class="notice" style="margin-top:8px">比赛已开始：<b>录分、重置、时间、直播、弃权、替补换人</b>` +
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
      ['鉴权模式', d.adminKeyMode],
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
