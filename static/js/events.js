/* 主页（/）与「全部赛事」页（/events）：同一套分组板。
 *
 * 两页都按**状态优先级**分组（进行中 → 筹备中 → 已结束），组可折叠、可按
 * 名称 / 编号 / 简介搜索；区别只有权限：
 *
 * * 主页对所有人开放：卡片只用来「进入这一届」；
 * * 全部赛事页给有赛事管理权限的人：卡片上多出重命名 / 封存 / 删除等操作。
 *
 * 这里**没有「主赛事」**——谁都不特殊、没有高亮、也没有「设为主赛事」。
 * 点卡片就是进入 ``/e001``；有那一届的管理权限时会自动切过去编辑。
 */

import {
  App,
  api,
  canManageEvents,
  esc,
  fmtFull,
  fmtSpan,
  isServerAdmin,
  log,
  qs,
  routePath,
  siteName,
  toast,
} from './core.js';
import { isChannelLive, isMemberLive } from './ui.js';

/* ------------------------------ 届次数据 ------------------------------ */

/** 这一届是否归当前用户管（服务器管理员放行）。 */
const ownedByMe = (e) => isServerAdmin() || Boolean(e?.ownerUid && e.ownerUid === App.me?.uid);

const STATUS_LABEL = { draft: '筹备中', active: '进行中', closed: '已结束' };
const STATUS_CLASS = { draft: 'badge--pending', active: 'badge--live', closed: 'badge--done' };

export const statusBadge = (status) =>
  `<span class="badge ${STATUS_CLASS[status] || 'badge--pending'}">${STATUS_LABEL[status] || esc(status)}</span>`;

/** 拉取届列表（默认走缓存；主页 / 全部赛事页进入前会先失效缓存）。 */
export async function loadEvents(force = false) {
  if (App.events.length && !force) return App.events;
  try {
    const res = await api('/events');
    App.events = res.events || [];
    App.eventId = res.current || App.eventId;
    log.info('届次列表已加载', App.events.length);
  } catch (err) {
    log.warn('届次列表加载失败', err);
    if (force) toast(err.message, 'err');
  }
  return App.events;
}

/** 任何届次变更后调用，强制下次重新拉取。 */
export function invalidateEvents() {
  App.events = [];
}

/* ------------------------------ 分组 ------------------------------ */

/** 分组顺序 = 优先级：进行中 → 筹备中 → 已结束（都按创建时间倒序）。 */
const GROUPS = [
  { key: 'active', label: '进行中', match: (e) => e.status === 'active' },
  { key: 'draft', label: '筹备中', match: (e) => e.status !== 'active' && e.status !== 'closed' },
  { key: 'closed', label: '已结束', match: (e) => e.status === 'closed' },
];

/** 搜索：名称 / 编号 / 简介 / 榜首都能命中（大小写不敏感）。 */
function filterEvents(events, term) {
  const q = String(term || '').trim().toLowerCase();
  if (!q) return events;
  return events.filter((e) =>
    [e.name, e.id, e.brief, e.champion].some((v) => String(v || '').toLowerCase().includes(q))
  );
}

function groupHtml(group, events, admin) {
  const open = App.homeOpen[group.key] !== false;
  const body = events.length
    ? `<div class="evt-grid evt-grid--anim">${events.map((e, i) => eventCardHtml(e, admin, i)).join('')}</div>`
    : `<div class="empty"><b>这一组还没有赛事</b>${
        admin ? '点上面的「新建一届」开始筹备' : '换个关键词试试'
      }</div>`;
  return (
    `<section class="home-group${open ? ' is-open' : ''}" data-group="${group.key}">` +
    `<button class="home-group__head" type="button" data-act="group-toggle" ` +
    `data-group="${group.key}" aria-expanded="${open}">` +
    `<span class="home-group__caret" aria-hidden="true"></span>` +
    `<span class="home-group__label">${group.label}</span>` +
    `<span class="home-group__count">${events.length}</span>` +
    `</button>` +
    `<div class="home-group__body"><div class="home-group__inner">${body}</div></div>` +
    `</section>`
  );
}

/** 三个分组的 HTML（搜索过滤后按状态归组）。 */
export function eventsGroupsHtml(events, { admin = false, term = App.homeSearch } = {}) {
  const rows = filterEvents(events, term);
  return GROUPS.map((g) => groupHtml(g, rows.filter(g.match), admin)).join('');
}

/** 填充某个分组容器（主页 / 全部赛事页共用；搜索时只重绘它，不重建搜索框）。 */
export function renderGroupList(hostId, admin, s = App.state) {
  const host = qs(`#${hostId}`);
  if (!host) return;
  if (!App.events.length) {
    host.innerHTML =
      `<div class="empty"><b>还没有赛事</b>` +
      (admin ? '点上面的「新建一届」开始筹备' : '等管理员创建后再来看') +
      `</div>`;
    return;
  }
  host.innerHTML =
    eventsGroupsHtml(App.events, { admin }) ||
    `<div class="empty"><b>没有匹配的赛事</b>换个关键词试试，或清空搜索框。</div>`;
  void s;
}

/* ------------------------------ 卡片 ------------------------------ */

const POP_ICON =
  `<svg viewBox="0 0 24 24" aria-hidden="true">` +
  `<path d="M13.5 5H19v5.5M19 5l-7.2 7.2M17 14.5V18a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 4 18V7.5A1.5 1.5 0 0 1 5.5 6H9" ` +
  `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

/**
 * 一届 = 一张卡片。
 *
 * 整张卡可点（进这一届的总览）；角落 ↗ 在新窗口打开同一届，便于并排对比。
 * ``admin`` 为真且这一届归自己管时，卡片底部多出一排操作按钮。
 */
function eventCardHtml(e, admin = false, index = 0) {
  const path = routePath(e.id, 'overview');
  const tags = [
    statusBadge(e.status),
    e.ranked === false ? '<span class="badge badge--pending">娱乐记录</span>' : '',
    e.hidden ? '<span class="badge badge--pending">已隐藏</span>' : '',
  ]
    .filter(Boolean)
    .join('');
  const meta = [
    `${e.players || 0} 人`,
    e.rounds ? `已赛 ${e.played || 0} / ${e.rounds}` : '赛程未生成',
    e.champion ? `榜首 ${esc(e.champion)}` : '',
  ]
    .filter(Boolean)
    .join(' · ');
  const ops =
    admin && ownedByMe(e)
      ? `<div class="evt-card__ops">` +
        `<button class="btn btn--sm" type="button" data-act="event-rename" data-id="${esc(e.id)}">重命名</button>` +
        (e.status === 'closed'
          ? `<button class="btn btn--sm" type="button" data-act="event-reopen" data-id="${esc(e.id)}">恢复进行</button>`
          : `<button class="btn btn--sm" type="button" data-act="event-close" data-id="${esc(e.id)}">标记结束</button>`) +
        `<button class="btn btn--sm btn--danger" type="button" data-act="event-delete" data-id="${esc(e.id)}">删除</button>` +
        `</div>`
      : '';
  return (
    `<article class="evt-card evt-card--act" style="--i:${index}" ` +
    `data-act="event-view" data-id="${esc(e.id)}" data-page="overview" ` +
    `title="进入这一届：${esc(path)}">` +
    `<div class="evt-card__head">` +
    `<button class="evt-card__name" type="button" data-act="event-view" data-id="${esc(e.id)}" ` +
    `data-page="overview">${esc(e.name || e.id)}</button>` +
    tags +
    `<button class="evt-card__pop" type="button" data-act="event-open" data-id="${esc(e.id)}" ` +
    `data-page="overview" title="在新窗口打开 ${esc(path)}" aria-label="在新窗口打开">${POP_ICON}</button>` +
    `</div>` +
    (e.brief ? `<div class="evt-card__brief">${esc(e.brief)}</div>` : '') +
    `<div class="evt-card__meta">${meta}</div>` +
    `<div class="evt-card__meta" title="创建 ${esc(fmtFull(e.createdAt))} · 更新 ${esc(fmtFull(e.updatedAt))}">` +
    `起止 ${esc(fmtSpan(e.startTime, e.endTime))}` +
    (e.status === 'closed' ? ' · 已封存' : '') +
    `</div>` +
    ops +
    `</article>`
  );
}

/* ------------------------------ 主页 ------------------------------ */

/** 频道概览：成员直播间 + 手工建的传统频道（与频道页同一套「在播」判定）。 */
function channelStats(s) {
  const members = (s.members || []).filter((m) => m.streamId);
  const memberIds = new Set(members.map((m) => m.id));
  const extra = (s.channels || []).filter((c) => c.active !== false && !memberIds.has(c.id));
  return {
    total: members.length + extra.length,
    live:
      members.filter((m) => isMemberLive(m.uid)).length +
      extra.filter((c) => isChannelLive(c.id)).length,
  };
}

function searchBoxHtml(id) {
  return (
    `<div class="home-search">` +
    `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5" fill="none" stroke="currentColor" stroke-width="2.2"/><path d="M16 16l4.5 4.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>` +
    `<input id="${id}" type="search" autocomplete="off" placeholder="搜索赛事名称 / 编号 / 简介" ` +
    `value="${esc(App.homeSearch)}" aria-label="搜索赛事">` +
    `</div>`
  );
}

/**
 * 主页：全部赛事（按状态分组 · 可折叠 · 可搜索）+ 频道入口。
 *
 * 与比赛无关的页面（频道 / 全部赛事 / 我的 / 服务器）都由这里进入，
 * 它们自己只有「主页」一个页签——所以主页是唯一的总入口。
 */
export function renderHome(s = App.state) {
  const host = qs('#homeBody');
  if (!host) return;
  const events = App.events || [];
  const active = events.filter((e) => e.status === 'active').length;
  const ch = channelStats(s || {});
  host.innerHTML =
    `<div class="home">` +
    `<section class="home-hero">` +
    `<p class="home-hero__kicker">赛事平台</p>` +
    `<h1 class="home-hero__title">${esc(siteName(s))}</h1>` +
    `<p class="home-hero__desc">全部赛事与直播间的总入口：点开一届看赛程与战况，或进频道看日常直播。</p>` +
    `<div class="home-hero__stats">` +
    `<span class="home-stat"><b>${events.length}</b><span>届赛事</span></span>` +
    `<span class="home-stat home-stat--live"><b>${active}</b><span>进行中</span></span>` +
    `<span class="home-stat"><b>${ch.live}</b><span>直播间在播</span></span>` +
    `</div></section>` +
    `<div class="home-entries">` +
    `<button class="home-entry home-entry--channel" type="button" data-act="route-channels">` +
    `<span class="home-entry__icon" aria-hidden="true">` +
    `<svg viewBox="0 0 24 24"><path d="M12 3l7.5 4.2v9.6L12 21l-7.5-4.2V7.2z" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linejoin="round"/><path d="M9.7 9.2l4.9 2.8-4.9 2.8z" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linejoin="round"/></svg>` +
    `</span>` +
    `<span class="home-entry__text"><b>频道</b>` +
    `<span>${ch.total} 个直播间${ch.live ? ` · ${ch.live} 个在播` : ' · 暂时没人在播'}</span></span>` +
    `<span class="home-entry__go" aria-hidden="true">›</span>` +
    `</button>` +
    (canManageEvents()
      ? `<button class="home-entry home-entry--admin" type="button" data-act="route-events">` +
        `<span class="home-entry__icon" aria-hidden="true">` +
        `<svg viewBox="0 0 24 24"><path d="M5 4h14v3H5zM5 10h14v3H5zM5 16h9v3H5z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>` +
        `</span>` +
        `<span class="home-entry__text"><b>管理赛事</b>` +
        `<span>新建 / 重命名 / 封存 / 删除${isServerAdmin() ? '（含隐藏届）' : ''}</span></span>` +
        `<span class="home-entry__go" aria-hidden="true">›</span>` +
        `</button>`
      : '') +
    `</div>` +
    `<div class="home-tools">${searchBoxHtml('homeSearch')}</div>` +
    `<div id="homeGroups"></div>` +
    `</div>`;
  renderHomeGroups(s);
}

/** 只重绘主页的分组列表（搜索时用，避免重建搜索框丢焦点）。 */
export function renderHomeGroups(s = App.state) {
  renderGroupList('homeGroups', false, s);
}

/* --------------------------- 全部赛事页 --------------------------- */

export async function renderEventsView() {
  const host = qs('#eventsBoard');
  if (!host) return;
  await loadEvents(true);
  const admin = canManageEvents();
  host.innerHTML =
    `<div class="home">` +
    `<section class="home-hero home-hero--slim">` +
    `<p class="home-hero__kicker">管理</p>` +
    `<h1 class="home-hero__title">全部赛事</h1>` +
    `<p class="home-hero__desc">共 ${App.events.length} 届 · 新建 / 重命名 / 封存 / 删除都在这里；` +
    `点卡片进入那一届，或在它自己的页面里编辑。</p>` +
    `<div class="home-tools">` +
    (admin
      ? `<button class="btn btn--sm btn--primary" type="button" data-act="event-new">新建一届</button>`
      : '') +
    `<button class="btn btn--sm" type="button" data-act="event-refresh">刷新</button>` +
    searchBoxHtml('eventsSearch') +
    `</div></section>` +
    `<div id="eventsGroups"></div>` +
    `</div>`;
  renderEventsGroups();
}

/** 只重绘「全部赛事」页的分组列表（搜索时用）。 */
export function renderEventsGroups() {
  renderGroupList('eventsGroups', canManageEvents());
}
