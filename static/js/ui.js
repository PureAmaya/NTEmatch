/* 展示层基础件：头像 / 选手行 / 徽标 / 表单字段。
 * 只做 HTML 字符串生成，不绑定事件（事件统一由 app.js 委托）。
 */

import { App, esc, qsa, toLocalInput } from './core.js';

const PLACEHOLDER = (name, size) =>
  `<span class="ava ava--${size} ava--placeholder" title="${esc(name)}">${esc(String(name).slice(0, 1))}</span>`;

/**
 * 头像地址。
 *
 * 用户端拿不到选手的 QQ，因此统一走 ``/api/avatar/p/<选手 ID>``，
 * 由服务端去查 QQ——请求里不会出现 QQ 号。
 *
 * ``App.avatarBust[选手 ID]`` 是「刷新头像」写入的时间戳：带上它 URL 就变了，
 * 浏览器的图片缓存才会失效（服务端缓存由 ``?refresh=1`` 负责）。
 */
export function avatarUrl(player) {
  if (player.avatar) return player.avatar;
  const ui = App.state?.ui;
  if (ui && ui.showAvatar === false) return '';
  if (!player.id || player.hasAvatar === false) return '';
  const bust = App.avatarBust?.[player.id];
  return (
    `/api/avatar/p/${encodeURIComponent(player.id)}?size=100` +
    (bust ? `&t=${encodeURIComponent(bust)}` : '')
  );
}

/* --------------------------- 私有数据（仅管理端） --------------------------- */
/** 选手的隐私字段（UUID / QQ / 推流流名 / 推流地址）；未登录时为空对象。 */
export const privateOf = (pid) => (pid && App.private?.players?.[pid]) || {};

/**
 * 选手个人的推流地址。
 *
 * ``proto``：``whip``（WebRTC 套，默认）/ ``rtmp`` / ``rtsp``（后两者为 TCP 套）。
 * 地址只由选手自己的流名决定（整届固定），与在哪场比赛无关。
 */
export const pushUrlOf = (pid, proto = 'whip') => {
  const endpoints = privateOf(pid).endpoints || {};
  if (proto === 'rtmp') return endpoints.rtmpPush || '';
  if (proto === 'rtsp') return endpoints.rtspPush || '';
  return endpoints.whipPush || '';
};

/**
 * 某场比赛的机位信息（本场出场的每位选手 + 他们各自的固定地址），仅管理端可见。
 *
 * 直播只按选手区分，所以这里没有「本场比赛」自己的推流地址。
 */
export const roundStreamsOf = (code) => (code && App.private?.rounds?.[code]) || null;

/* --------------------------- 推流建议 --------------------------- */
/**
 * 推流建议：**优先 WHIP**，并且**不要在 OBS 里开 B 帧**。
 *
 * B 帧要靠后续帧才能解码，而 WebRTC / WHIP 是逐帧实时发送的，
 * 开了 B 帧最容易出现花屏、抖动，甚至直接推不上去——这是选手侧最常见的坑，
 * 所以凡是出现推流地址的地方都要提醒一次。
 */
export const PUSH_TIPS = [
  '优先用 <b>WHIP</b>（WebRTC / UDP）：延迟最低、弱网下表现最好，OBS 30+ 原生支持；',
  '备选 <b>RTMP / RTSP</b>（TCP）：只在 WHIP 推不上去时再用，延迟略高但更稳；',
  'OBS 里把 <b>B 帧 / B-frames 设为 0</b>，关键帧间隔（Keyframe Interval）设 2 秒，编码器用 H.264；',
  '开了 B 帧会让 WebRTC 推流花屏、卡顿甚至连不上（HLS / RTMP 观看侧同样受益）。',
];

/** 一行版：复制按钮的 title、复制后的提醒。 */
export const PUSH_TIP_LINE = '优先用 WHIP（UDP，延迟最低）；OBS 请把 B 帧设为 0';

/** 推流建议块（放在弹窗 / 面板里）。 */
export const pushTipsHtml = (extraClass = '') =>
  `<div class="notice notice--warn push-tips${extraClass ? ` ${extraClass}` : ''}">` +
  `<b>推流建议（按优先级）</b>` +
  `<ol>${PUSH_TIPS.map((tip) => `<li>${tip}</li>`).join('')}</ol>` +
  `</div>`;

/**
 * 头像内层 HTML：有图则 img + 环，无图则首字占位。
 *
 * 图片下面垫一层「首字」兜底：头像接口 404（没配 QQ）或加载失败时，
 * app.js 会把坏图移除，露出兜底而不是浏览器默认的破图图标。
 */
export function avaInner(player) {
  const name = player.name || player.tag || player.id || '?';
  const url = avatarUrl(player);
  const fallback = esc(String(name).slice(0, 1));
  if (!url) return fallback;
  return (
    `<span class="ava__fb">${fallback}</span>` +
    `<img src="${esc(url)}" alt="${esc(name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer">` +
    `<span class="ava__ring"></span>`
  );
}

/* --------------------------- 直播状态（多机位） --------------------------- */
/**
 * **真的在推流**的选手 ID 集合。
 *
 * 以媒体服务器上报为准（``App.liveNow``，来自 ``/api/live/health`` 的 ``streaming``）；
 * 还没探测过（``null``）时退回状态里的兜底值。查询失败时是**空集**——
 * 宁可少显示「直播中」，也不给观众一个假的直播标记。
 */
export const livePlayers = () =>
  App.liveNow instanceof Set ? App.liveNow : new Set(App.state?.livePlayers || []);

export const isLivePlayer = (pid) => Boolean(pid) && livePlayers().has(pid);

/** 取某位选手的直播间地址集合（只有选手机位，无主直播间）。 */
export function roomFor(pid) {
  if (!pid) return null;
  return (App.state?.streams || {})[pid] || null;
}

export function liveTag(text = '直播中') {
  return `<span class="live-tag"><i class="live-tag__dot"></i>${esc(text)}</span>`;
}

export function avaHtml(player, size = 'sm') {
  const url = avatarUrl(player);
  const live = isLivePlayer(player.id);
  const cls = `ava ava--${size}${url ? '' : ' ava--placeholder'}${live ? ' ava--live' : ''}`;
  return (
    `<span class="${cls}">${avaInner(player)}` +
    (live ? '<i class="ava__live" aria-hidden="true"></i><span class="sr-only">直播中</span>' : '') +
    `</span>`
  );
}

export const playerById = (id) => (App.state?.players || []).find((p) => p.id === id);

/** 选手行：头像 + 姓名 + 编号等元信息（积分制榜表用；UUID / QQ 不下发）。 */
export function whoHtml(player, { size = 'sm', form = [] } = {}) {
  const name = player ? player.name || player.tag || player.id : '未知选手';
  const parts = [];
  if (player) {
    if (player.tag) parts.push(player.tag);
    if (player.substitute) parts.push('替补');
    if (player.active === false) parts.push('停用');
  }
  const chips = form && form.length ? formChips(form) : '';
  return (
    `<div class="who">${avaHtml(player || { name }, size)}` +
    `<div class="who__txt"><div class="who__name">${esc(name)}</div>` +
    `<div class="who__meta">${esc(parts.join(' · ')) || '—'}</div>${chips}</div></div>`
  );
}

export function formChips(form) {
  if (!form || !form.length) return '<span class="panel__hint">—</span>';
  return (
    `<div class="form-chips">` +
    form.slice(-6).map((r) => `<i data-r="${esc(r)}">${esc(r)}</i>`).join('') +
    `</div>`
  );
}

export const ROUND_BADGE = { pending: 'badge--pending', live: 'badge--live', done: 'badge--done' };
export const ROUND_TEXT = { pending: '待开始', live: '进行中', done: '已结束' };

export const roundBadge = (status) =>
  `<span class="badge ${ROUND_BADGE[status] || 'badge--pending'}">${ROUND_TEXT[status] || esc(status)}</span>`;

export function kpiCard(label, value, sub, barPercent) {
  const bar =
    barPercent === undefined
      ? ''
      : `<div class="kpi__bar"><i style="width:${Math.max(0, Math.min(100, barPercent))}%"></i></div>`;
  return (
    `<div class="kpi__card"><div class="kpi__label">${esc(label)}</div>` +
    `<div class="kpi__value">${value}</div>` +
    `<div class="kpi__sub">${esc(sub)}</div>${bar}</div>`
  );
}

/** labels 元素可为字符串，或 [文本, 悬浮说明]。 */
export const boardHeadHtml = (labels) =>
  `<div class="board__head">${labels
    .map((item) => {
      const [text, title] = Array.isArray(item) ? item : [item, ''];
      return `<span${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
    })
    .join('')}</div>`;

/** rank 为 null 表示未达排名门槛（场次不足），展示为「—」且不参与名次。 */
export const rankCell = (rank) =>
  rank == null
    ? `<div class="rank rank--none" title="场次不足，不参与排名">—</div>`
    : `<div class="rank${rank <= 3 ? ` rank--${rank}` : ''}">${rank}</div>`;

export const panelHtml = (title, hint, body, extraClass = '') =>
  `<div class="panel${extraClass}"><div class="panel__head"><h2>${esc(title)}</h2>` +
  `<span class="panel__hint">${esc(hint)}</span></div><div class="panel__body">${body}</div></div>`;

/* ------------------------------ 比赛规则 ------------------------------ */
/**
 * 比赛规则的正文（参数速览 + 分区条目 + 赛事说明）。
 *
 * 内容由后端 ``rulebook`` 从当前赛制参数推导，因此切换赛制或改任何参数后
 * 用户端与「管理端预览」都会立刻同步。放在共享层是为了让
 * views / admin / events 都能复用，同时避免模块循环依赖。
 */
export function rulebookBodyHtml(s) {
  const rb = s?.rulebook;
  if (!rb || !(rb.sections || []).length) return '';
  const facts = rb.facts || {};
  const chips = [
    ['赛制', facts.formatLabel || ''],
    ['参赛', facts.format === 'league' ? `${facts.players || 0} 人` : `${facts.teams || 0} 支队`],
    ['每队', `${facts.teamSize || 0} 人`],
    facts.format === 'league'
      ? ['总轮次', `${facts.totalRounds || 0} 局`]
      : ['每场', facts.teamsPerMatch === 2 ? '组 vs 组' : `${facts.teamsPerMatch || 2} 队同场`],
    facts.format === 'league'
      ? ['排名', '均分']
      : ['淘汰', facts.loserBracket ? '双败' : '单败'],
    !facts.format || facts.format === 'tournament'
      ? ['晋级', facts.size ? `${facts.size} 强` : '待定']
      : ['平局', facts.allowDraw ? '允许' : '不允许'],
  ].filter(([, value]) => value !== '' && value != null);

  const sections = (rb.sections || [])
    .map(
      (sec) =>
        `<div class="rules__sec"><h3>${esc(sec.title)}</h3><ul>` +
        (sec.items || []).map((text) => `<li>${esc(text)}</li>`).join('') +
        `</ul></div>`
    )
    .join('');

  return (
    `<div class="rules">` +
    `<div class="rules__facts">${chips
      .map(
        ([key, value]) =>
          `<span class="rules__fact"><i>${esc(key)}</i><b>${esc(value)}</b></span>`
      )
      .join('')}</div>` +
    `<div class="rules__secs">${sections}</div>` +
    (rb.note ? `<div class="rules__note"><b>赛事说明</b>${esc(rb.note)}</div>` : '') +
    `</div>`
  );
}

/** 比赛规则面板（用户端总览底部）。 */
export function rulesPanelHtml(s) {
  const body = rulebookBodyHtml(s);
  if (!body) return '';
  return panelHtml('比赛规则', s?.rulebook?.headline || '', body);
}

/* ------------------------------ 表单字段 ------------------------------ */
export function fieldText(name, label, value, opts = {}) {
  const { ph = '', type = 'text', hint = '' } = opts;
  return (
    `<div class="field"><label for="f-${name}">${esc(label)}</label>` +
    `<input id="f-${name}" name="${name}" type="${type}" value="${esc(value ?? '')}" placeholder="${esc(ph)}">` +
    (hint ? `<span class="field__hint">${esc(hint)}</span>` : '') +
    `</div>`
  );
}

export const fieldNum = (name, label, value, opts = {}) =>
  fieldText(name, label, value, { ...opts, type: 'number' });

/** 日期时间输入：值需为 YYYY-MM-DDTHH:MM；留空表示未设置。 */
export const fieldDateTime = (name, label, value, opts = {}) =>
  fieldText(name, label, toLocalInput(value), { ...opts, type: 'datetime-local' });

export function fieldArea(name, label, value, opts = {}) {
  return (
    `<div class="field"><label for="f-${name}">${esc(label)}</label>` +
    `<textarea id="f-${name}" name="${name}" rows="3">${esc(value ?? '')}</textarea>` +
    (opts.hint ? `<span class="field__hint">${esc(opts.hint)}</span>` : '') +
    `</div>`
  );
}

export function fieldSelect(name, label, value, options, opts = {}) {
  return (
    `<div class="field"><label for="f-${name}">${esc(label)}</label>` +
    `<select id="f-${name}" name="${name}">` +
    options
      .map(
        ([v, t]) =>
          `<option value="${esc(v)}"${String(value) === String(v) ? ' selected' : ''}>${esc(t)}</option>`
      )
      .join('') +
    `</select>` +
    (opts.hint ? `<span class="field__hint">${esc(opts.hint)}</span>` : '') +
    `</div>`
  );
}

export function fieldSwitch(name, label, checked, opts = {}) {
  return (
    `<div class="field field--switch"><span class="switch">` +
    `<input id="f-${name}" name="${name}" type="checkbox"${checked ? ' checked' : ''}>` +
    `<i></i></span><label for="f-${name}">${esc(label)}</label>` +
    (opts.hint ? `<span class="field__hint">${esc(opts.hint)}</span>` : '') +
    `</div>`
  );
}

/** 收集表单值：复选框取布尔，数字输入取 number，其余取字符串。 */
export function collectForm(formEl) {
  const out = {};
  qsa('[name]', formEl).forEach((el) => {
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.type === 'number') out[el.name] = el.value === '' ? 0 : Number(el.value);
    else out[el.name] = el.value;
  });
  return out;
}
