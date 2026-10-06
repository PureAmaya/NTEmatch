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
  DEFAULT_SITE_NAME,
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
import { isBiliLive, isChannelLive, isMemberLive, ownerHtml } from './ui.js';
import { icon } from './icons.js';
import { renderNoticeBoard, renderServerInfo } from './notices.js';

/* ------------------------------ 届次数据 ------------------------------ */

/** 这一届是否归当前用户管（服务器管理员放行）。 */
const ownedByMe = (e) => isServerAdmin() || Boolean(e?.ownerUid && e.ownerUid === App.me?.uid);

const STATUS_LABEL = { draft: '筹备中', active: '进行中', closed: '已结束' };
const STATUS_CLASS = { draft: 'badge--pending', active: 'badge--live', closed: 'badge--done' };

/** 状态徽标：加一枚小图标，扫一眼就能分出「筹备中 / 进行中 / 已结束」。 */
const STATUS_ICON = { draft: 'clock', active: 'zap', closed: 'checkCircle' };

export const statusBadge = (status) =>
  `<span class="badge ${STATUS_CLASS[status] || 'badge--pending'}">` +
  `${icon(STATUS_ICON[status] || 'clock')}${STATUS_LABEL[status] || esc(status)}</span>`;

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
    `<span class="home-group__label">${esc(group.label)}</span>` +
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

/** 「在新窗口打开这一届」的角标图标（图标集里的 external）。 */
const POP_ICON = icon('external');

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
  // 举办者单独一行带头像：进群被问「谁办的」时一眼能答，也对得上人
  const owner = e.ownerName
    ? `<div class="evt-card__owner">${ownerHtml(e.ownerName, e.ownerAvatar)}</div>`
    : '';
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
    `<article class="evt-card evt-card--act" style="--i:${index}">` +
    `<div class="evt-card__head">` +
    // 真链接 + 拉伸覆盖整张卡：点哪都能进这一届，同时「中键 / ⌘+点击」能开新标签、
    // 右键能复制链接地址（这些都是 `<button data-act>` 做不到的）
    `<a class="evt-card__name" href="${esc(path)}" data-route ` +
    `title="进入这一届：${esc(path)}">${esc(e.name || e.id)}</a>` +
    tags +
    `<a class="evt-card__pop" href="${esc(path)}" target="_blank" rel="noopener" ` +
    `title="在新窗口打开 ${esc(path)}" aria-label="在新窗口打开">${POP_ICON}</a>` +
    `</div>` +
    (e.brief ? `<div class="evt-card__brief">${esc(e.brief)}</div>` : '') +
    owner +
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
  // 成员房间：登记了推流 ID **或** 填了 B站 房间号都算一个房间
  const members = (s.members || []).filter((m) => m.streamId || m.biliRoom);
  const memberIds = new Set(members.map((m) => m.id));
  const extra = (s.channels || []).filter((c) => c.active !== false && !memberIds.has(c.id));
  return {
    total: members.length + extra.length,
    live:
      // 在播两条链路都算：本站推流或 B站 直播
      members.filter((m) => isMemberLive(m.uid) || isBiliLive(m.uid)).length +
      extra.filter((c) => isChannelLive(c.id)).length,
  };
}

function searchBoxHtml(id) {
  return (
    `<div class="home-search">` +
    icon('search') +
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
/* --------------------------- 新装引导（三步开赛） --------------------------- */

/** 引导卡「不再提示」的本机记忆键（换设备的人本来就该再看一眼现状）。 */
const GUIDE_KEY = 'nte.guide.hidden';

/** 关掉引导卡（由 actions.js 的 data-act="guide-hide" 调用）。 */
export function hideSetupGuide() {
  try {
    localStorage.setItem(GUIDE_KEY, '1');
  } catch {
    /* 隐私模式下写不进 localStorage：本次会话照常隐藏即可 */
  }
}

/**
 * 「三步开赛」引导卡：只在**能管赛事的人**眼里出现，而且**缺哪步才说哪步**。
 *
 * 刻意不做成导瞄准星 / 弹窗——那会挡住首页。这里就是一张普通卡片：全部满足后
 * 自然消失；不想看的人点「不再提示」即可。每一步都直接给一条去对应页面的路。
 */
function setupGuideHtml(s) {
  if (!canManageEvents()) return '';
  try {
    if (localStorage.getItem(GUIDE_KEY)) return '';
  } catch {
    /* 读不到 localStorage 就当没关过 */
  }
  const steps = [];
  if (siteName(s) === DEFAULT_SITE_NAME) {
    steps.push({
      title: '给站点起个名',
      desc: `现在还叫「${DEFAULT_SITE_NAME}」——它会出现在浏览器标签与分享卡片上。`,
      href: '/admin',
      cta: '去设置',
    });
  }
  // 直播没有总开关（有赛事就能播），只要没填源站地址就提醒一次
  if (!String(s?.stream?.baseUrl || '').trim()) {
    steps.push({
      title: '填直播服务器地址',
      desc: '没填的话「直播」页只能给出推流地址，观众那边点不开播放器。',
      href: '/admin',
      cta: '去填地址',
    });
  }
  if (!(App.events || []).length) {
    steps.push({
      title: '新建一届比赛',
      desc: '有了届次才会有赛程、名单与排行。',
      href: '/events',
      cta: '新建一届',
    });
  } else if (Array.isArray(s?.players) && s.players.length === 0) {
    steps.push({
      title: '添加参赛选手',
      desc: '本届还没有选手：先在「选手」页录入名单，再生成赛程。',
      href: routePath(s?.eventId || '', 'roster'),
      cta: '去加选手',
    });
  }
  if (!steps.length) return '';
  return (
    `<section class="setup panel">` +
    `<header class="setup__head"><b>把这里跑起来 · 还剩 ${steps.length} 步</b>` +
    `<button class="setup__skip" type="button" data-act="guide-hide">不再提示</button></header>` +
    `<ol class="setup__list">` +
    steps
      .map(
        (st, i) =>
          `<li class="setup__item"><span class="setup__no">${i + 1}</span>` +
          `<span class="setup__text"><b>${esc(st.title)}</b><span>${esc(st.desc)}</span></span>` +
          `<a class="btn btn--sm" href="${esc(st.href)}" data-route>${esc(st.cta)}</a></li>`
      )
      .join('') +
    `</ol></section>`
  );
}

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
    setupGuideHtml(s) +
    `<div class="home-entries">` +
    `<a class="home-entry home-entry--channel" href="/channels" data-route>` +
    `<span class="home-entry__icon" aria-hidden="true">` +
    icon('radio') +
    `</span>` +
    `<span class="home-entry__text"><b>频道</b>` +
    `<span>${ch.total} 个直播间${ch.live ? ` · ${ch.live} 个在播` : ' · 暂时没人在播'}</span></span>` +
    `<span class="home-entry__go" aria-hidden="true">›</span>` +
    `</a>` +
    (canManageEvents()
      ? `<a class="home-entry home-entry--admin" href="/events" data-route>` +
        `<span class="home-entry__icon" aria-hidden="true">` +
        icon('tool') +
        `</span>` +
        `<span class="home-entry__text"><b>管理赛事</b>` +
        `<span>新建 / 重命名 / 封存 / 删除${isServerAdmin() ? '（含隐藏届）' : ''}</span></span>` +
        `<span class="home-entry__go" aria-hidden="true">›</span>` +
        `</a>`
      : '') +
    `</div>` +
    `<div class="home-tools">${searchBoxHtml('homeSearch')}</div>` +
    `<div id="homeGroups"></div>` +
    // 站点级内容放主页最下面：一条条的通知（历史留档）+ 「关于本站」
    `<div class="panel" id="homeNotices"></div>` +
    `<div class="panel" id="homeServerInfo" hidden></div>` +
    `</div>`;
  renderHomeGroups(s);
  renderNoticeBoard('server', qs('#homeNotices'), { hint: '全站弹窗 · 历史留档' });
  renderServerInfo(qs('#homeServerInfo'));
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
