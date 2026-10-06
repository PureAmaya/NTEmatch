/* 通知（赛事通知 / 服务器通知）：自动弹出、历史卡片列表（分页）、发布与编辑。
 *
 * 三条约定：
 *
 * 1. **要不要弹由「本机已读」决定**（localStorage），不去服务端记已读：
 *    谁看过哪条通知是个人隐私级的细枝末节，不值得为它建一张表；
 *    key 里带上 ``updatedAt``，所以**通知被改了会再弹一次**（那正是它该被重看的场景）。
 * 2. **正文按需再取**：状态广播里只有「最新一条的 id / 标题 / 时间」，
 *    真要看内容才去拉全文并渲染 HTML —— 免得每次改比分都重发一遍公告。
 * 3. 列表卡片只给摘要（后端已截成一段话），超过 4 条就分页。
 */

import { App, Modal, api, esc, fmtFull, hooks, log, toast } from './core.js';
import { iconBtn } from './icons.js';
import { openMarkdownEditor } from './mdeditor.js';

/** 已读记录的本机键（每台设备各自记，换设备就再看一遍——这没有坏处）。 */
const SEEN_KEY = 'nte.notice.seen';
/** 每页几条：需求里是「超过 4 行开启分页」。 */
export const PAGE_SIZE = 4;
/** 已读记录最多留这么多条（通知是长期增长的东西，不能无限占 localStorage）。 */
const SEEN_MAX = 300;

const SCOPE_LABEL = { event: '赛事通知', server: '服务器通知' };

/* --------------------------- 已读记忆（本机） --------------------------- */
function seenList() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]');
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function markSeen(key) {
  try {
    const list = seenList().filter((item) => item !== key);
    list.push(key);
    localStorage.setItem(SEEN_KEY, JSON.stringify(list.slice(-SEEN_MAX)));
  } catch {
    /* 隐私模式写不进去就算了：最多下次再弹一遍 */
  }
}

const seenKeyOf = (head) => `${head.id}:${head.updatedAt || head.createdAt || ''}`;

/** 让调用方（如发布通知的人自己）把某条直接标成已读。 */
export function markNoticeSeen(notice) {
  if (notice && notice.id) markSeen(seenKeyOf(notice));
}

/** 当前有没有弹窗开着（有的话就先不弹通知，免得叠在一起）。 */
function modalBusy() {
  const el = document.getElementById('modal');
  return Boolean(el && !el.hidden);
}

/* ------------------------------ 自动弹出 ------------------------------ */
/**
 * 检查有没有「没看过的新通知」并弹出来。**服务器级优先**（它面向全站），
 * 一次只弹一个，关上之后下一次状态更新会接着弹赛事通知。
 */
export function checkNotices(state = App.state) {
  if (modalBusy()) return; // 正在看别的弹窗，别打断
  const heads = state?.notices || {};
  const seen = new Set(seenList());
  // 服务器通知：任何路由都要弹；赛事通知：看的是当前这一届
  const queue = [['server', heads.server], ['event', heads.event]];
  for (const [scope, head] of queue) {
    if (!head || !head.id) continue;
    if (seen.has(seenKeyOf(head))) continue;
    openNoticeReader(head.id, scope);
    return;
  }
}

/** 打开单条通知（含渲染后的正文）。 */
export async function openNoticeReader(noticeId, scope = 'event') {
  if (!noticeId) {
    toast('这条通知的编号丢了，刷新页面后重试', 'err', 6000);
    return;
  }
  let notice;
  try {
    const res = await api(`/notices/${encodeURIComponent(noticeId)}`);
    notice = res && res.notice;
  } catch (err) {
    // 这里以前只写日志：点了「查看全文」什么也不发生，看起来就是「按钮无效」。
    // 现在一律说出来，并且 404（多半已经被删）时顺手把列表换成最新的。
    log.warn('通知读取失败', err);
    toast(err.message || '通知读取失败，请稍后再试', 'err', 7000);
    if (err.status === 404) await refreshNotices(scope);
    return;
  }
  if (!notice) {
    toast('通知内容为空（可能刚被修改），请刷新后重试', 'err', 6000);
    return;
  }
  // 打开即视为已读：关掉窗口不该又弹一次（改过之后 updatedAt 变了，会再弹）
  markNoticeSeen(notice);
  Modal.open({
    title: notice.title || '通知',
    className: 'modal__box--notice',
    body:
      `<div class="notice-view">` +
      `<div class="notice-view__meta">` +
      `<span class="notice-view__scope">${esc(SCOPE_LABEL[notice.scope] || '通知')}</span>` +
      `<time>${esc(fmtFull(notice.updatedAt || notice.createdAt))}</time>` +
      (notice.author ? `<span>· ${esc(notice.author)}</span>` : '') +
      `</div>` +
      `<div class="md">${notice.html || ''}</div></div>`,
    footer: `<button class="btn btn--primary" type="button" data-close>知道了</button>`,
  });
}

/* ------------------------------ 卡片列表 ------------------------------ */
function cardsHtml(items, { scope, manage }) {
  if (!items.length) {
    return (
      `<div class="empty"><b>还没有通知</b>` +
      `${manage ? '发一条通知，打开站点的所有人都能看到' : '有新动态时会出现在这里'}</div>`
    );
  }
  return items
    .map(
      (item) =>
        `<article class="ncard">` +
        `<header class="ncard__head">` +
        `<b class="ncard__title">${esc(item.title)}</b>` +
        `<span class="ncard__scope">${esc(SCOPE_LABEL[scope] || '')}</span>` +
        `<time class="ncard__date">${esc(fmtFull(item.updatedAt || item.createdAt))}</time>` +
        `</header>` +
        `<p class="ncard__summary">${esc(item.summary || '')}</p>` +
        `<div class="ncard__ops">` +
        iconBtn('eye', '查看全文', {
          act: 'notice-read',
          data: { id: item.id, scope },
          cls: 'btn--sm',
        }) +
        (manage
          ? iconBtn('edit', '编辑', {
              act: 'notice-edit',
              data: { id: item.id, scope },
              cls: 'btn--sm',
            }) +
            iconBtn('trash', '删除', {
              act: 'notice-del',
              data: { id: item.id, scope },
              cls: 'btn--sm btn--danger',
            })
          : '') +
        `</div></article>`
    )
    .join('');
}

function pagerHtml(data, scope) {
  if ((data?.pages || 1) <= 1) return '';
  const page = data.page || 1;
  return (
    `<div class="npager">` +
    `<button class="btn btn--sm" type="button" data-act="notice-page" data-scope="${esc(scope)}" ` +
    `data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>` +
    `<span class="npager__info">第 ${page} / ${data.pages} 页 · 共 ${data.total} 条</span>` +
    `<button class="btn btn--sm" type="button" data-act="notice-page" data-scope="${esc(scope)}" ` +
    `data-page="${page + 1}" ${page >= data.pages ? 'disabled' : ''}>下一页</button></div>`
  );
}

/** 拉一页通知（带缓存；``force`` 时强制重取）。 */
export async function loadNotices(scope, page = 1, { force = false } = {}) {
  App.notices = App.notices || {};
  const cached = App.notices[scope];
  if (!force && cached && cached.page === page) return cached;
  const query = `?scope=${encodeURIComponent(scope)}&page=${page}&size=${PAGE_SIZE}`;
  const data = await api(`/notices${query}`);
  App.notices[scope] = data;
  return data;
}

/**
 * 把通知列表渲染到某个容器里（自带分页状态）。
 *
 * ``manage`` 为真时多出「发布 / 编辑 / 删除」——权限由调用方决定传不传
 * （前端只做展示，真正的闸门在服务端）。
 *
 * 宿主是不是可见也由这里定：总览页的 ``#noticeBoard`` 在 HTML 里是预置的 ``hidden``
 * 占位，以前只填内容、从不显示，于是「赛事通知」面板在用户端永远看不见。
 * 规则：有内容就显示；管理面板即使一条都没有也要显示（那里要能点「发布通知」）。
 */
export async function renderNoticeBoard(scope, host, { manage = false, page = 0, hint = '' } = {}) {
  if (!host) return;
  App.noticeHosts = App.noticeHosts || {};
  App.noticeHosts[scope] = { host, hostId: host.id || '', manage, hint };
  const want = page || (App.notices && App.notices[scope]?.page) || 1;
  // 状态里那一条「最新通知」变了就强制重取：这样**别人刚发的通知**会随
  // WebSocket 推送自动出现在列表里，不用手动刷新页面
  App.noticeFingerprint = App.noticeFingerprint || {};
  const head = (App.state?.notices || {})[scope];
  const fingerprint = head ? `${head.id}:${head.updatedAt || ''}` : '';
  const force = page === 0 && App.noticeFingerprint[scope] !== fingerprint;
  App.noticeFingerprint[scope] = fingerprint;
  let data;
  try {
    data = await loadNotices(scope, want, { force });
  } catch (err) {
    log.warn('通知列表读取失败', err);
    host.innerHTML = `<div class="empty"><b>通知读取失败</b>稍后再试或刷新页面</div>`;
    return;
  }
  const tips = [hint, `共 ${data.total} 条 · 每页 ${PAGE_SIZE} 条`].filter(Boolean).join(' · ');
  host.hidden = !(data.total > 0 || manage);
  host.innerHTML =
    `<div class="panel__head"><h2>${esc(SCOPE_LABEL[scope])}</h2>` +
    `<span class="panel__hint">${esc(tips)}</span>` +
    (manage
      ? iconBtn('plus', '发布通知', { act: 'notice-new', data: { scope }, cls: 'btn--sm' })
      : '') +
    `</div>` +
    `<div class="nlist">${cardsHtml(data.items || [], { scope, manage })}</div>` +
    pagerHtml(data, scope);
}

/**
 * 现在真正挂在页面上的宿主。
 *
 * 管理页整块重绘会换掉 `#adminNotices` 这个元素，早先记下的引用就成了「幽灵」——
 * 往里写内容没人看得见（表现就是「保存 / 删除后界面没变」）。所以按 **id 现查**，
 * 查不到再退回引用（宿主没有 id 的情况）。
 */
function liveHostOf(target) {
  if (!target) return null;
  if (target.hostId) {
    const el = document.getElementById(target.hostId);
    if (el) return el;
  }
  return target.host && target.host.isConnected ? target.host : null;
}

/** 翻页（由 actions.js 的分发调用）。 */
export async function goNoticePage(scope, page) {
  const target = (App.noticeHosts || {})[scope];
  const host = liveHostOf(target);
  if (!target || !host) return;
  const data = await loadNotices(scope, Math.max(1, Number(page) || 1), { force: true });
  await renderNoticeBoard(scope, host, {
    manage: target.manage,
    hint: target.hint,
    page: data.page,
  });
}

/** 变更之后刷新（管理面板 + 总览 / 主页里的只读板都换新）。 */
export async function refreshNotices(scope) {
  const target = (App.noticeHosts || {})[scope];
  const host = liveHostOf(target);
  if (!target || !host) return;
  await renderNoticeBoard(scope, host, {
    manage: target.manage,
    hint: target.hint,
    page: 1,
  });
}

/**
 * 保存 / 删除之后把该刷的都刷一遍。
 *
 * 先让 app.js 整体重绘一次（管理面板、服务器页、总览都算），再按 id 找到新的
 * 通知面板填内容——这样「改完立刻能看到结果」在任何页面上都成立。
 */
async function refreshAfterChange(scope) {
  if (hooks.onSaved) hooks.onSaved();
  await refreshNotices(scope);
}

/* ------------------------------ 发布与编辑 ------------------------------ */
/**
 * 新建 / 编辑通知：都用同一套 Markdown 编辑器（标题也在编辑器里填，少一次弹窗来回）。
 * ``noticeId`` 为空即新建；否则先取详情——编辑器里要放**原文**，不能只有渲染结果。
 */
export async function composeNotice(scope = 'event', noticeId = '') {
  let existing = null;
  if (noticeId) {
    try {
      existing = (await api(`/notices/${encodeURIComponent(noticeId)}`)).notice;
    } catch (err) {
      toast(err.message || '通知读取失败', 'err');
      return;
    }
  }
  // 「同时发到 QQ 群」：只有推送确实就绪（已启用 + 有 Key + 有目标会话）才给出这个勾选，
  // 否则勾了也只能得到一句「未启用」。查一次状态（轻量只读接口，赛事管理员也能调）。
  let canPush = false;
  try {
    canPush = (await api('/qqbot/status', { auth: true })).ready === true;
  } catch (err) {
    log.debug('推送状态未知，不显示「同时发到群」', err);
  }
  openMarkdownEditor({
    title: existing ? '编辑通知' : `发布${SCOPE_LABEL[scope] || '通知'}`,
    titleLabel: '通知标题',
    titleValue: existing?.title || '',
    titlePlaceholder: '如：第 2 轮改期到 19:30（卡片与弹窗都用它）',
    value: existing?.body || '',
    hint:
      `${SCOPE_LABEL[scope] || ''}：发布后打开站点的人会自动弹窗看到` +
      `${scope === 'event' ? '（只在本届内弹）' : '（任何页面都弹）'}。`,
    extras: canPush
      ? [
          {
            name: 'push',
            label: '同时发到 QQ 群',
            hint: '群里只发摘要 + 站点链接（完整内容在站点看）；发不出去不影响通知本身',
          },
        ]
      : [],
    onSave: async ({ title, text, extras }) => {
      const payload = { scope, title, body: text, push: Boolean(extras?.push) };
      const res = existing
        ? await api(`/notices/${encodeURIComponent(existing.id)}`, {
            method: 'PUT',
            auth: true,
            body: payload,
          })
        : await api('/notices', { method: 'POST', auth: true, body: payload });
      markNoticeSeen(res.notice); // 自己发的不用再弹给自己看
      toast(existing ? '通知已更新' : '通知已发布', 'ok');
      // 顺带推群的结果单独说一句：推不出去时通知**已经发好了**，别说成失败
      const pushed = res.push;
      if (pushed) {
        toast(
          pushed.ok
            ? `已同时推到群里（${pushed.sent}/${pushed.total} 段）`
            : `通知已发布，但没能推到群里：${pushed.detail || '未知原因'}`,
          pushed.ok ? 'ok' : 'warn',
          9000
        );
      }
      await refreshAfterChange(scope);
    },
  });
}

export async function removeNotice(scope, noticeId) {
  // 删除是不可逆的（通知没有回收站），一律先问一句
  if (!window.confirm('删除这条通知？删掉之后打开站点的所有人都看不到它了。')) return;
  try {
    await api(`/notices/${encodeURIComponent(noticeId)}`, { method: 'DELETE', auth: true });
    toast('通知已删除', 'ok');
    await refreshAfterChange(scope);
  } catch (err) {
    toast(err.message || '删除失败', 'err');
  }
}

/* --------------------------- 赛事信息 / 服务器信息 --------------------------- */
/** 两者都是「一篇 Markdown」，所以共用同一套编辑器，只是接口与提示不同。 */
export async function editEventInfo() {
  let info;
  try {
    info = await api('/event/info');
  } catch (err) {
    toast(err.message || '赛事信息读取失败', 'err');
    return;
  }
  if (!info.editable) {
    toast('本届已结束：赛事信息只能查看，要发内容请用「赛事通知」', 'err');
    return;
  }
  openMarkdownEditor({
    title: '编辑赛事信息',
    value: info.text || '',
    hint: '会显示在用户端「比赛规则」面板末尾（规则主体仍由赛制参数自动生成）。',
    placeholder: '例：参赛须知、场地位置、注意事项……',
    onSave: async ({ text }) => {
      const res = await api('/config', {
        method: 'PUT',
        auth: true,
        body: { event: { rulesText: text } },
      });
      // 服务端返回了新的整份状态就直接用；没有就现拉一次，省得界面停在旧内容上
      if (res && res.state) App.state = res.state;
      else if (hooks.refreshState) await hooks.refreshState();
      toast('赛事信息已保存', 'ok');
      if (hooks.onSaved) hooks.onSaved();
    },
  });
}

export async function editServerInfo() {
  let info;
  try {
    info = await api('/server/info');
  } catch (err) {
    toast(err.message || '服务器信息读取失败', 'err');
    return;
  }
  openMarkdownEditor({
    title: '编辑服务器信息',
    value: info.text || '',
    hint: '会显示在主页底部「关于本站」，任何访客都能看到。',
    onSave: async ({ text }) => {
      await api('/server/info', { method: 'PUT', auth: true, body: { text } });
      toast('服务器信息已保存', 'ok');
      // 服务器信息在内存里缓存了一份：清掉缓存再整页重绘，否则界面还是旧内容
      App.serverInfo = null;
      if (hooks.onSaved) hooks.onSaved();
    },
  });
}

/**
 * 把「服务器信息」渲染到容器里（带自己的面板头）。
 *
 * ``manage`` 为真时多一个编辑按钮（服务器管理页用）；主页用默认参数即可 ——
 * 没有内容时整块隐藏，不占版面。
 */
export async function renderServerInfo(host, { title = '关于本站', manage = false } = {}) {
  if (!host) return;
  try {
    if (!App.serverInfo) App.serverInfo = await api('/server/info');
  } catch (err) {
    log.warn('服务器信息读取失败', err);
    host.hidden = true;
    return;
  }
  const data = App.serverInfo;
  if (!data || (!data.html && !manage)) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.innerHTML =
    `<div class="panel__head"><h2>${esc(title)}</h2>` +
    `<span class="panel__hint">Markdown · ${manage ? '显示在主页底部' : '由服务器管理员维护'}</span>` +
    (manage ? iconBtn('edit', '编辑', { act: 'server-info-edit', cls: 'btn--sm' }) : '') +
    `</div>` +
    `<div class="panel__body md">${data.html || '<span class="panel__hint">还没有内容</span>'}</div>`;
}
