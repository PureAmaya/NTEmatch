/* 展示层基础件：头像 / 选手行 / 徽标 / 表单字段。
 * 只做 HTML 字符串生成，不绑定事件（事件统一由 app.js 委托）。
 */

import { App, esc, qsa, toLocalInput } from './core.js';
import { icon, panelIcon } from './icons.js';

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
 * 某个机位的推流地址集合（仅管理端有值）。
 *
 * * 选手机位 → 该选手自己的流名那一套；
 * * 主直播间 → 直播配置里「默认流名」那一套（``/api/private → defaultPush``）。
 */
export const pushEndpointsOf = (pid) =>
  (isMainRoom(pid) ? App.private?.defaultPush : privateOf(pid).endpoints) || {};

/**
 * 某个机位的推流地址（只有 WHIP 一种）。
 *
 * 地址只由该机位自己的流名决定（整届固定），与在哪场比赛无关。
 */
export const pushUrlOf = (pid) => pushEndpointsOf(pid).whipPush || '';

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
  '推流只有 <b>WHIP</b> 一种（WebRTC / UDP）：延迟最低、弱网下表现最好，OBS 30+ 原生支持；',
  'OBS 里把 <b>B 帧 / B-frames 设为 0</b>，关键帧间隔（Keyframe Interval）设 2 秒，编码器用 H.264；',
  '开了 B 帧会让 WebRTC 推流花屏、卡顿甚至连不上（HLS 观看侧同样受影响）。',
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

/**
 * 主直播间的机位 ID。
 *
 * 它不是某位选手，而是「默认流名」那一路**总机位**（``直播配置 → 默认流名``，
 * 见 :class:`models.StreamConfig`）。用常量而不是流名当 ID，免得与选手 ID 撞车；
 * 真正的流名在机位对象的 ``key`` 里。
 */
export const MAIN_ROOM_ID = '__main__';
export const isMainRoom = (pid) => pid === MAIN_ROOM_ID;

/**
 * 主直播间当前有没有人在推流。
 *
 * 探测不到（媒体服务器 API 不可达）或还没探测过 → ``false``：
 * 宁可少显示这一路，也不让观众点进一个空流。
 */
export const isMainLive = () => {
  const known = App.liveHealth ? App.liveMain : App.state?.live?.streaming;
  return known === true;
};

/** 主直播间的观看地址集合（取自公开的源地址）；连地址都没有就没有这一路。 */
export function mainRoom() {
  const e = App.liveInfo || App.state?.live || {};
  if (!e.originWebrtc && !e.originHls) return null;
  return {
    key: e.key || 'stream',
    main: true,
    webrtc: e.originWebrtc || '',
    hls: e.originHls || '',
  };
}

/** 取一个机位的直播间地址集合（选手机位，或主直播间）。 */
export function roomFor(pid) {
  if (!pid) return null;
  if (isMainRoom(pid)) return mainRoom();
  return (App.state?.streams || {})[pid] || null;
}

/* --------------------------- 成员频道（日常直播） --------------------------- */
/**
 * **真的在推流**的成员频道 ID 集合（与选手机位同一套判断）。
 *
 * 探测不到（媒体服务器 API 不可达）时是空集——宁可少显示「直播中」，
 * 也不给观众一个假的直播标记。
 */
export const liveChannels = () =>
  App.liveChannelsNow instanceof Set
    ? App.liveChannelsNow
    : new Set(App.state?.liveChannels || []);

export const isChannelLive = (id) => Boolean(id) && liveChannels().has(id);

/* --------------------------- 成员直播间（全局） --------------------------- */
/**
 * **真的在推流**的成员 uid 集合（按推流 ID 判定，与选手机位同一套）。
 *
 * 探测不到（媒体服务器 API 不可达）时是空集——宁可少显示「直播中」，
 * 也不给观众一个假的直播标记。
 */
export const liveMembers = () =>
  App.liveMembersNow instanceof Set
    ? App.liveMembersNow
    : new Set((App.state?.members || []).filter((m) => m.live).map((m) => m.uid));

export const isMemberLive = (uid) => Boolean(uid) && liveMembers().has(uid);

/** 成员直播间头像：统一走 ``/api/avatar/m/<uid>``，请求里不出现 QQ 号。 */
export function memberAvatarUrl(member) {
  if (!member) return '';
  if (member.avatar) return member.avatar;
  const ui = App.state?.ui;
  if (ui && ui.showAvatar === false) return '';
  if (!member.uid || member.hasAvatar === false) return '';
  const bust = App.avatarBust?.[`m:${member.uid}`];
  return (
    `/api/avatar/m/${encodeURIComponent(member.uid)}?size=100` +
    (bust ? `&t=${encodeURIComponent(bust)}` : '')
  );
}

/** 成员直播间头像（六边形 + 首字兜底 + 直播中光环）。 */
export function memberAvaHtml(member, size = 'sm') {
  const name = member?.name || member?.uid || '?';
  const url = memberAvatarUrl(member);
  const live = isMemberLive(member?.uid);
  const cls = `ava ava--${size}${url ? '' : ' ava--placeholder'}${live ? ' ava--live' : ''}`;
  const inner = url
    ? `<span class="ava__fb">${esc(String(name).slice(0, 1))}</span>` +
      `<img src="${esc(url)}" alt="${esc(name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer">` +
      `<span class="ava__ring"></span>`
    : esc(String(name).slice(0, 1));
  return (
    `<span class="${cls}">${inner}` +
    (live ? '<i class="ava__live" aria-hidden="true"></i><span class="sr-only">直播中</span>' : '') +
    `</span>`
  );
}

/**
 * 成员频道头像地址。
 *
 * 与选手一致：客户端不直接引用 QQ 头像域名，统一走 ``/api/avatar/c/<频道 ID>``，
 * 请求里不会出现 QQ 号；``App.avatarBust[频道 ID]`` 用于绕过浏览器缓存。
 */
export function channelAvatarUrl(channel) {
  if (!channel) return '';
  if (channel.avatar) return channel.avatar;
  const ui = App.state?.ui;
  if (ui && ui.showAvatar === false) return '';
  if (!channel.id || channel.hasAvatar === false) return '';
  const bust = App.avatarBust?.[channel.id];
  return (
    `/api/avatar/c/${encodeURIComponent(channel.id)}?size=100` +
    (bust ? `&t=${encodeURIComponent(bust)}` : '')
  );
}

/** 频道头像（六边形 + 首字兜底），与选手机位同一套视觉。
 *
 * 也兼容「成员直播间」的合成对象（带 ``member:true`` / ``uid``）：走成员头像接口。
 */
export function channelAvaHtml(channel, size = 'sm') {
  const name = channel?.name || channel?.id || '?';
  const url = channel?.member ? memberAvatarUrl(channel) : channelAvatarUrl(channel);
  const live = channel?.member ? isMemberLive(channel.uid) : isChannelLive(channel?.id);
  const cls = `ava ava--${size}${url ? '' : ' ava--placeholder'}${live ? ' ava--live' : ''}`;
  const inner = url
    ? `<span class="ava__fb">${esc(String(name).slice(0, 1))}</span>` +
      `<img src="${esc(url)}" alt="${esc(name)}" loading="lazy" decoding="async" referrerpolicy="no-referrer">` +
      `<span class="ava__ring"></span>`
    : esc(String(name).slice(0, 1));
  return (
    `<span class="${cls}">${inner}` +
    (live ? '<i class="ava__live" aria-hidden="true"></i><span class="sr-only">直播中</span>' : '') +
    `</span>`
  );
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

/**
 * 举办者徽标：头像 + 名字（没头像时自动回落成首字母）。
 *
 * 复用 ``avaHtml`` 的头像逻辑；``id`` 留空是故意的——举办者不是机位，
 * 不该被套上「直播中」光环。
 */
export const ownerHtml = (name, avatar = '') =>
  name
    ? `<span class="owner">${avaHtml({ id: '', name, avatar }, 'sm')}` +
      `<span class="owner__name">${esc(name)}</span></span>`
    : '';

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

/**
 * 这局是否已经有任何结果痕迹。
 *
 * 与后端 ``tournament.round_has_result`` 同一口径：动过比分 / 名次 / 弃权 / 状态
 * 就算「已经开打」。小组赛对阵只允许在**一场都没开打**时调整，前后端都用它判断。
 */
export const roundHasResult = (rnd) =>
  Boolean(rnd) &&
  (rnd.status !== 'pending' ||
    Boolean(rnd.winner) ||
    (rnd.sets || []).length > 0 ||
    (rnd.sides || []).some((s) => s.score || s.points || s.rank || s.forfeit));

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

/**
 * 一个面板：标题（带图标）+ 右上角提示 + 正文。
 *
 * 图标按标题关键词自动挑（见 icons.panelIcon）——面板头全站几十处，
 * 这样美化和以后新增面板都不用逐处配图标。
 */
export const panelHtml = (title, hint, body, extraClass = '') =>
  `<div class="panel${extraClass}"><div class="panel__head">` +
  `<h2>${icon(panelIcon(title))}${esc(title)}</h2>` +
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
    // 比法：计分制 / 用时制——它决定「哪种数值更好」（后端 app/metrics.py）
    ['比法', facts.metricLabel || ''],
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
    // 赛事信息是服务端渲染好的 Markdown（严格白名单，见 app/markdown.py），
    // 所以直接当 HTML 放进去；没有内容时整块不显示
    (rb.noteHtml ? `<div class="rules__note"><b>赛事说明</b><div class="md">${rb.noteHtml}</div></div>` : '') +
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
  const { ph = '', type = 'text', hint = '', maxlength = 0 } = opts;
  const limit = Number(maxlength) || 0;
  return (
    `<div class="field"><label for="f-${name}">${esc(label)}</label>` +
    `<input id="f-${name}" name="${name}" type="${type}" value="${esc(value ?? '')}"` +
    ` placeholder="${esc(ph)}"${limit ? ` maxlength="${limit}"` : ''}>` +
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
