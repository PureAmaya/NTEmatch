/* 多届赛事：每届一张卡片的「往届」列表（独立路由 /events）。
 *
 * 这一页不属于任何一届：列出全部届次，点卡片进入那一届自己的路由
 * （/e001、/e001/schedule…），名单 / 赛程 / 直播 / 管理都在那一届的路由里做。
 * 届列表是公开数据（访客也能看历史）；建届 / 改名 / 删除 / 设为主赛事需登录。
 */

import { App, api, esc, fmtFull, fmtSpan, log, qs, routePath, toast } from './core.js';

const STATUS_LABEL = { draft: '筹备中', active: '进行中', closed: '已结束' };
const STATUS_CLASS = { draft: 'badge--pending', active: 'badge--live', closed: 'badge--done' };

export const statusBadge = (status) =>
  `<span class="badge ${STATUS_CLASS[status] || 'badge--pending'}">${STATUS_LABEL[status] || esc(status)}</span>`;

/** 拉取届列表（默认走缓存）。 */
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

/* 卡片右上角的「新窗口打开」图标 */
const POP_ICON =
  `<svg viewBox="0 0 24 24" aria-hidden="true">` +
  `<path d="M13.5 5H19v5.5M19 5l-7.2 7.2M17 14.5V18a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 4 18V7.5A1.5 1.5 0 0 1 5.5 6H9" ` +
  `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

/**
 * 一届 = 一张卡片。
 *
 * * 整张卡可点：跳到这一届的路由（/e001）；
 * * **只有主赛事高亮**（同期只有一个主赛事，访问根路径就落在它身上）；
 * * 角落 ↗ 在新窗口打开同一届，便于并排对比；
 * * 卡片不摆届次编号（`e001` 只在地址栏与 tooltip 里出现）。
 */
function eventCardHtml(e) {
  const path = routePath(e.id, 'overview');
  const ops = App.token
    ? `<div class="evt-card__ops">` +
      (e.current
        ? `<span class="panel__hint">当前主赛事</span>`
        : `<button class="btn btn--sm btn--primary" type="button" data-act="event-switch" data-id="${esc(e.id)}">` +
          `设为主赛事</button>`) +
      `<button class="btn btn--sm" type="button" data-act="event-rename" data-id="${esc(e.id)}">重命名</button>` +
      (e.status === 'closed'
        ? `<button class="btn btn--sm" type="button" data-act="event-reopen" data-id="${esc(e.id)}">恢复进行</button>`
        : `<button class="btn btn--sm" type="button" data-act="event-close" data-id="${esc(e.id)}">标记结束</button>`) +
      `<button class="btn btn--sm btn--danger" type="button" data-act="event-delete" data-id="${esc(e.id)}">删除</button>` +
      `</div>`
    : '';
  return (
    `<article class="evt-card evt-card--act${e.current ? ' evt-card--on' : ''}" ` +
    `data-act="event-view" data-id="${esc(e.id)}" data-page="overview" ` +
    `title="进入这一届：${esc(path)}${e.current ? '（主赛事）' : '（只读回看）'}">` +
    `<div class="evt-card__head">` +
    `<button class="evt-card__name" type="button" data-act="event-view" data-id="${esc(e.id)}" ` +
    `data-page="overview">${esc(e.name || e.id)}</button>` +
    statusBadge(e.status) +
    (e.current ? `<span class="badge badge--live">主赛事</span>` : '') +
    `<button class="evt-card__pop" type="button" data-act="event-open" data-id="${esc(e.id)}" ` +
    `data-page="overview" title="在新窗口打开 ${esc(path)}" aria-label="在新窗口打开">${POP_ICON}</button>` +
    `</div>` +
    `<div class="evt-card__meta">${e.players || 0} 人 · 已赛 ${e.played || 0} / ${e.rounds || 0} 局` +
    (e.champion ? ` · 榜首 ${esc(e.champion)}` : '') +
    `</div>` +
    `<div class="evt-card__meta" title="创建 ${esc(fmtFull(e.createdAt))} · 更新 ${esc(fmtFull(e.updatedAt))}">` +
    `起止 ${esc(fmtSpan(e.startTime, e.endTime))}` +
    (e.status === 'closed' ? ' · 已封存' : '') +
    `</div>` +
    ops +
    `</article>`
  );
}

/** 「往届」页：全部届次（一届一张卡），供 VIEW_RENDERERS 调用。 */
export async function renderEventsView() {
  const host = qs('#eventsList');
  if (!host) return;
  const events = await loadEvents(false);
  const bar = App.token
    ? `<div class="tool-group" style="margin-bottom:10px">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="event-new">新建一届</button>` +
      `<button class="btn btn--sm" type="button" data-act="event-refresh">刷新</button>` +
      `<span class="panel__hint">新建后自动设为主赛事；同期只会有一个主赛事</span>` +
      `</div>`
    : '';
  host.innerHTML =
    bar +
    (events.length
      ? `<div class="evt-grid">${events.map(eventCardHtml).join('')}</div>`
      : `<div class="empty"><b>暂无赛事记录</b>${
          App.token ? '点上面的「新建一届」开始' : '请等待管理员新建一届赛事'
        }</div>`);
}
