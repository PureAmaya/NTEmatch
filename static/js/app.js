/* 启动引导：装配事件委托、实时通道、视图切换。
 * 各功能视图拆分在 core / ui / views / live / admin / actions 模块中。
 */

import {
  App,
  API,
  PAGES,
  STANDALONE_PAGES,
  TOKEN_KEY,
  VIEW_KEY,
  api,
  canManageEvents,
  copyText,
  hooks,
  isServerAdmin,
  log,
  Modal,
  pageAvailable,
  parseRoute,
  qs,
  qsa,
  refreshPrivate,
  routePath,
  stateKey,
  toast,
} from './core.js';
import { PUSH_TIP_LINE } from './ui.js';
import { loadEvents, renderEventsGroups, renderHomeGroups } from './events.js';
import { installDnD as installTeamDnD } from './teams.js';
import {
  ChannelLive,
  installStageDelegation,
  Live,
  startLiveHealth,
  stopLiveHealth,
} from './live.js';
import {
  channelRooms,
  focusLive,
  liveInfoHtml,
  renderChannels,
  renderOverview,
  renderPublic,
  renderRosterGrid,
  renderView,
  syncTabs,
} from './views.js';
import { renderAdmin } from './admin.js';
import { handleAction, handleForm, login, uploadAvatarFile } from './actions.js';
import {
  refreshMeData,
  refreshServerData,
  renderMemberGrid,
  uploadBackupFile,
} from './members.js';

/* --------------------------- 路由（主页 + 赛事 + 独立页） ---------------------------
 *
 * **主页（``/``）是唯一的总入口**：全部届次按状态分组 + 频道卡片。
 *
 *   主赛事的旧概念已取消——主页列出全部届次，谁都不特殊。
 *
 * 赛事页形如 ``/<届 ID>/<页面>``（``/e001``、``/e001/schedule``），
 * 独立页不带届 ID（``/channels``、``/events``、``/user``、``/admin``）。
 * **比赛与频道之间不能直接互跳**，都得经过主页，所以页签只有两类：
 * 赛事页是「主页 + 总览 / 赛程 / 选手 / 直播（+ 赛事管理）」，其它页面只有「主页」。
 *
 * 后端仍有一个「当前届」（所有写接口的作用对象）：进入某届时**若你有那一届的
 * 管理权限**就静默切过去，于是「打开哪一届就能改哪一届」；没有权限（别人的届 /
 * 未登录）就只读查看，并且忽略服务端推送。
 */
/** 生效中的路由：``eventId`` 非空 = 正在看某一届，为空 = 独立页。 */
let route = { eventId: '', page: 'home', channelId: '' };

/**
 * 直接落到错误页（403 / 404）。
 *
 * **地址栏保持原样**：错的地址留着更诚实（刷新还是这一屏，也方便把链接发给管理员看），
 * 所以这里既不 pushState 也不 replaceState。它自己算独立页，页签里只剩「主页」。
 */
async function gotoDenied(info) {
  App.denied = info;
  App.routeEvent = '';
  App.private = null;
  if (!App.state) {
    try {
      applyState(await api('/state'), { force: true });
    } catch (err) {
      log.warn('状态加载失败', err);
    }
  }
  route = { eventId: '', page: 'denied', channelId: '' };
  if (App.state) syncTabs(App.state, 'denied');
  setView('denied', { silent: true });
  renderPublic();
  log.info('进入错误页', info.code, info.title);
}

async function goto(eventId = '', page = 'home', { replace = false, channelId = '' } = {}) {
  const want = PAGES.includes(page) ? page : 'home';
  const standalone = STANDALONE_PAGES.includes(want);
  const id = standalone ? '' : String(eventId || '');

  // 需要权限的入口：没权限直接给错误页，而不是悄悄回落到别的页面
  if (want === 'events' && !canManageEvents()) {
    return gotoDenied({
      code: '403',
      title: '「全部赛事」只对赛事管理员开放',
      desc:
        '这一页里有新建 / 重命名 / 封存 / 删除等操作，需要赛事管理员或服务器管理员权限。' +
        '看比赛本身不需要权限——直接在主页点某一届就行。',
    });
  }
  if (want === 'server' && !isServerAdmin()) {
    return gotoDenied({
      code: '403',
      title: '服务器管理仅限服务器管理员',
      desc:
        '成员、届次、备份、QQ 机器人、直播封禁这些都是服务器级设置。' +
        '如果你只是想改自己的资料，请从右上角「头像 + 用户名」进「我的」。',
    });
  }
  if (want === 'manage' && !canManageEvents()) {
    return gotoDenied({
      code: '403',
      title: '没有赛事管理权限',
      desc: '这一届的管理页需要赛事管理员或服务器管理员权限。赛程与战况仍然可以在总览 / 赛程页看。',
    });
  }

  // 赛事页必须带届 ID（``/overview`` 这种是手敲的）→ 回主页
  if (!standalone && !id) return goto('', 'home', { replace: true });
  // 深链 / 刷新进来时届次列表可能还没到手，先补一次再判断这一届存不存在
  if (id && !App.events.length) await loadEvents();
  if (id && !App.events.some((e) => e.id === id)) {
    return gotoDenied({
      code: '404',
      title: `没有这一届：${id}`,
      desc: '它可能已经被删除；如果它被设成了「隐藏」，则只有服务器管理员能看到。',
    });
  }

  const switchedEvent = App.routeEvent !== id;
  App.routeEvent = id;
  if (switchedEvent) {
    App.livePlayerId = null;
    Live.stop(false);
    ChannelLive.stop(false);
  }

  if (standalone) {
    // 独立页也要一份状态：站点名称 / 主题 / 频道与成员直播间都在里面
    try {
      applyState(await api('/state'), { force: true });
    } catch (err) {
      log.error('状态加载失败', err);
      toast(err.message, 'err');
    }
    if (want === 'server') {
      // 每次进入本页重新拉一次，并复位「只试一次」标记
      App.serverTried = false;
      App.eventsTried = false;
      await refreshServerData({ silent: true });
    } else if (want === 'user') {
      await refreshMeData();
    } else if (want === 'home') {
      // 主页全靠届次列表：每次进入都重取，别拿旧缓存（否则会先闪一下「还没有赛事」）
      await loadEvents(true);
    } else if (want === 'channels' && channelId) {
      // 深链 /channels/<推流 ID>：按流名选中那一路（找不到就照常落到第一个在播的）
      const room = channelRooms(App.state).find((c) => (c.play || {}).key === channelId);
      if (room) App.channelId = room.id;
      else toast(`没有推流 ID 为「${channelId}」的直播间`, 'warn', 6000);
    }
  } else {
    const entry = App.events.find((e) => e.id === id);
    const owned = isServerAdmin() || Boolean(entry?.ownerUid && entry.ownerUid === App.me?.uid);
    const canEditThis = Boolean(App.me) && canManageEvents() && owned;
    // 有权限就直接切到这一届：后端「当前届」只是写接口的作用对象，不是「主赛事」
    if (canEditThis && id !== App.eventId) {
      try {
        await api(`/events/${id}/switch`, { method: 'POST', auth: true });
        App.eventId = id;
      } catch (err) {
        log.warn('切换届次失败，按只读处理', err.message);
      }
    }
    if (id === App.eventId) {
      try {
        // force：切届时版本号可能刚好相同，必须重绘
        applyState(await api('/state'), { force: true });
      } catch (err) {
        log.error('状态加载失败', err);
        toast(err.message, 'err');
      }
      try {
        App.liveInfo = (await api('/live/info')).endpoints || App.liveInfo;
      } catch (err) {
        log.warn('直播信息加载失败', err);
      }
      // 隐私字段 / 推流地址只对有赛事管理权限的人拉（普通成员会被 403）
      if (canManageEvents()) await refreshPrivate();
    } else {
      // 别人的届 / 未登录 / 已封存：只读查看这一届，且不吃服务端推送
      try {
        App.state = await api(`/events/${id}/state`);
        App.liveInfo = null;
        // 只读回看：一只机位都不摆（连主直播间的标记也清掉，免得串到这一届）
        App.liveNow = new Set();
        App.liveMain = false;
        App.private = null;
        log.info('只读查看这一届', id, want);
      } catch (err) {
        log.error('届次状态加载失败', err);
        toast(err.message, 'err');
        return goto('', 'home', { replace: true });
      }
    }
  }

  // 只读查看别人的届时，管理页直接给错误页（点卡片进来的是总览，不受影响）
  if (want === 'manage' && App.state?.readOnly) {
    return gotoDenied({
      code: '403',
      title: '这一届不归你管',
      desc:
        '赛事管理员只能管理自己创建的届，服务器管理员可以管理全部届次。' +
        '赛程、战况与选手名单都还能正常查看。',
    });
  }

  // 页面可用性要等状态到手才能定（已完结的届没有直播页）→ 不可用就回落
  const pg = pageAvailable(want, App.state) ? want : standalone ? 'home' : 'overview';
  // 只有「地址里本来就给了推流 ID」时才把它留在 URL 里（/channels/<流名>）；
  // 从主页点「频道」进来时不改地址，免得后退键变得更绕。在页面里换台由
  // hooks.syncChannelUrl 用 replaceState 更新，不新增历史。
  const chKey = pg === 'channels' && channelId ? channelId : '';
  route = { eventId: standalone ? '' : id, page: pg, channelId: chKey };
  const path = routePath(route.eventId, pg, chKey);
  if (location.pathname !== path) history[replace ? 'replaceState' : 'pushState']({ ...route }, '', path);

  // 先把页签可见性同步好，否则 setView 会因为「页签被收起来」而回落到总览。
  // 这里必须把目标页传进去：此刻 App.view 还是上一页的值。
  if (App.state) syncTabs(App.state, pg);
  setView(pg, { silent: true });
  renderPublic();
  renderAdmin();
  log.debug('路由', path, '届', route.eventId || '(独立页)');
}

window.addEventListener('popstate', (ev) => {
  const r = parseRoute();
  const page = r.page || ev.state?.page || (r.eventId ? 'overview' : 'home');
  goto(r.eventId, page, { replace: true, channelId: r.channelId });
});

/* --------------------------- 视图切换 --------------------------- */
function setView(view, { silent = false } = {}) {
  // 页签被收起来的页面不能进（如已完结的届没有直播页）。
  // 独立页（主页 / 频道 / 全部赛事 / 我的 / 服务器）本来就没有对应的赛事页签，
  // 它们**不存在同名页签**，因此这里的兜底不会误伤。
  const tab = qsa('.tab').find((t) => t.dataset.view === view);
  if (tab && tab.hidden) view = App.routeEvent ? 'overview' : 'home';
  App.view = view;
  document.documentElement.dataset.view = view;
  qsa('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.view === view)));
  qsa('.view').forEach((v) => {
    v.hidden = v.dataset.view !== view;
  });
  localStorage.setItem(VIEW_KEY, view);

  if (view === 'live') {
    ChannelLive.stop(false);
    if (App.state) focusLive(App.state);
  } else if (view === 'channels') {
    Live.stop(false);
    if (App.state) renderChannels(App.state);
  } else {
    Live.stop(false);
    ChannelLive.stop(false);
    if (App.state) renderView(view, App.state);
  }
  // 推流检测只在直播 / 频道页运行：进页面开始，离开就停（后端随之不再探测）。
  // 首次初始化时 App.state 还没到手，这里不启动——等路由把状态拉回来后的那次
  // setView 再启动，避免「启动得太早、进直播页反而要等一个轮询周期」。
  if ((view === 'live' || view === 'channels') && App.state) startLiveHealth();
  else if (view !== 'live' && view !== 'channels') stopLiveHealth();

  if (view === 'manage') renderAdmin();
  // 记下已渲染的视图与指纹（切页本身就重建过 DOM，避免随后一次推送再重建一遍）
  App.renderedView = view;
  App.renderedKey = stateKey(App.state);
  if (!silent) log.debug('切换视图', view);
}

/* --------------------------- 实时通道 --------------------------- */
let ws = null;
let wsRetry = 0;
let wsTimer = null;
let pingTimer = null;

/** 记录实时通道状态（顶栏不再显示芯片，断开时仍按下面逻辑自动重连）。 */
export function setOnline(online) {
  App.online = online;
}

function scheduleReconnect() {
  if (wsTimer) return;
  const delay = Math.min(15000, 1000 * 2 ** Math.min(wsRetry++, 4));
  log.debug('计划重连', delay, 'ms');
  wsTimer = setTimeout(() => {
    wsTimer = null;
    connectWS();
  }, delay);
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws`);
  } catch (err) {
    log.error('WebSocket 创建失败', err);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    log.info('WebSocket 已连接');
    wsRetry = 0;
    setOnline(true);
    // 连接握手时服务端会回放最近状态，无需再主动请求，省一次全量下发
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 25000);
  };

  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      log.warn('无法解析服务端消息', ev.data);
      return;
    }
    if (msg.type === 'state' && msg.data) {
      // 正在只读查看另一届：服务端推的是「当前届」，忽略它，别把页面拽回去
      if (App.routeEvent && App.routeEvent !== msg.data.eventId) return;
      applyState(msg.data);
    } else if (msg.type === 'pong') {
      log.debug('心跳回包');
    }
  };

  ws.onclose = (ev) => {
    if (App.online) log.warn('WebSocket 断开', ev.code, ev.reason);
    setOnline(false);
    clearInterval(pingTimer);
    scheduleReconnect();
  };

  ws.onerror = () => {
    /* 统一由 onclose 处理重连 */
  };
}

let renderQueued = false;

/** 把连续的状态推送合并到一帧内渲染，避免突发变更引起多次重排。 */
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderPublic();
  });
}

/**
 * 收到一份状态。
 *
 * ``force`` 为真时一定重绘（切届 / 新建届后主动拉取的那次，见 hooks.refreshState）；
 * 否则当指纹与已渲染的一致（典型场景：重连时服务端回放最近状态）就跳过重建 DOM。
 */
function applyState(data, { force = false } = {}) {
  if (data.live) App.liveInfo = data.live;
  // 后端「当前届」（写接口的作用对象）变了：只记下来，**不跳页**——
  // 主页列出全部届次，没有「主赛事」这回事，不该因为别人切换而打断当前阅读。
  if (data.eventId) App.eventId = data.eventId;
  const changed = App.state?.revision !== data.revision;
  App.state = data;
  if (changed) log.info('状态已更新', 'revision', data.revision);
  if (!force && App.renderedKey === stateKey(data) && App.renderedView === App.view) {
    log.debug('状态未变化，跳过重绘', 'revision', data.revision);
    return;
  }
  scheduleRender();
}

/**
 * 首次加载：先读地址栏（届列表拿不到时按当前届继续，别整页卡死）。
 *
 * 页面段缺省时沿用上次看的那一页，并把地址栏规范成 ``/<届>/<页>``，
 * 这样刷新 / 收藏 / 分享回到的永远是同一个页面。
 */
async function loadInitial() {
  try {
    const events = await api('/events');
    App.events = events.events || [];
    App.eventId = events.current || App.eventId;
  } catch (err) {
    log.warn('届次列表加载失败', err);
  }
  const r = parseRoute();
  // 只给了届次（/e001）就补总览；什么都没有（/）就是主页
  await goto(r.eventId, r.page || (r.eventId ? 'overview' : 'home'), {
    replace: true,
    channelId: r.channelId,
  });
}

/* --------------------------- 事件装配 --------------------------- */
function bindStatic() {
  // 页签 = 路由：切页只换「页面段」，届次段原样保留（在一届里翻页不会换届）
  qsa('.tab').forEach((tab) => {
    tab.addEventListener('click', () => goto(route.eventId, tab.dataset.view));
  });

  qs('#btnRefresh').addEventListener('click', async () => {
    // 在看别人的届（只读）时别去要「当前届」的推送，那会是一份用不上的数据
    const wantsPush = !App.routeEvent || App.routeEvent === App.eventId;
    if (ws && ws.readyState === WebSocket.OPEN && wantsPush) ws.send('state');
    await hooks.refreshState();
    toast('已同步最新状态', 'ok', 2000);
  });

  // 顶栏「管理」= 服务器管理（只有服务器管理员看得见，见 views.syncHeader）
  qs('#btnAdmin')?.addEventListener('click', () => {
    if (!isServerAdmin()) return;
    goto('', 'server');
  });

  // 顶栏「登录」= 去 /user 的登录门（未登录时才出现）
  qs('#btnLogin')?.addEventListener('click', () => goto('', 'user'));

  // 顶栏右上角的「头像 + 用户名」：进自己的 /user 页
  qs('#btnMe')?.addEventListener('click', () => goto('', 'user'));

  // 头像加载失败（没配 QQ / 接口 4xx / 断网）：移除坏图，露出底下的首字兜底。
  // error 事件不冒泡，所以必须用捕获阶段监听。
  document.addEventListener(
    'error',
    (e) => {
      const img = e.target;
      if (img instanceof HTMLImageElement && img.parentElement?.classList.contains('ava')) {
        img.remove();
      }
    },
    true
  );

  // 全局动作委托：舞台内元素交给 live.js 自己的委托，避免重复处理
  document.addEventListener('click', (e) => {
    if (e.target.closest('#liveStage')) return;
    // 复制按钮在 data-act 之前拦下：带 data-copy 的都走这里（含 data-act="copy" 的按钮）
    const copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      // data-tip="push" 说明复制的是推流地址：顺手提醒「优先 WHIP / 关掉 B 帧」
      const isPush = copyBtn.dataset.tip === 'push';
      copyText(copyBtn.dataset.copy || '').then((ok) => {
        if (!ok) return toast('复制失败', 'err');
        return toast(
          isPush ? `推流地址已复制 · ${PUSH_TIP_LINE}` : '已复制',
          'ok',
          isPush ? 9000 : 2500
        );
      });
      return;
    }
    const el = e.target.closest('[data-act]');
    if (el && !el.disabled) handleAction(el.dataset.act, el);
  });

  // 名单搜索：只重绘网格并做防抖，避免逐字触发重排
  let searchTimer = null;
  qs('#rosterTools').addEventListener('input', (e) => {
    if (e.target.id !== 'rosterSearch') return;
    App.search = e.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      if (App.state) renderRosterGrid(App.state);
    }, 120);
  });
  // 选手筛选：切换下拉立即重绘网格
  qs('#rosterTools').addEventListener('change', (e) => {
    if (e.target.id !== 'rosterFilter') return;
    App.rosterFilter = e.target.value;
    if (App.state) renderRosterGrid(App.state);
  });

  // 主页 / 全部赛事页的搜索：防抖，且只重绘分组列表（不重建搜索框，免得丢焦点）
  let homeTimer = null;
  const searchTargets = {
    homeSearch: () => renderHomeGroups(App.state),
    eventsSearch: () => renderEventsGroups(),
  };
  const onEventsSearch = (e) => {
    const run = searchTargets[e.target.id];
    if (!run) return;
    App.homeSearch = e.target.value;
    clearTimeout(homeTimer);
    homeTimer = setTimeout(run, 120);
  };
  qs('#homeBody')?.addEventListener('input', onEventsSearch);
  qs('#eventsBoard')?.addEventListener('input', onEventsSearch);

  // 管理端表单提交
  qs('#adminPanel').addEventListener('submit', (e) => {
    if (!e.target.dataset.form) return;
    e.preventDefault();
    handleForm(e.target);
  });

  // 服务器 / 个人页的表单提交（成员、自定义 HTML、我的资料…）
  qs('#serverBody').addEventListener('submit', (e) => {
    if (!e.target.dataset.form) return;
    e.preventDefault();
    handleForm(e.target);
  });
  qs('#userBody').addEventListener('submit', (e) => {
    if (!e.target.dataset.form) return;
    e.preventDefault();
    handleForm(e.target);
  });

  // 成员管理：搜索（防抖、只重绘网格）/ 筛选
  let memberTimer = null;
  qs('#serverBody').addEventListener('input', (e) => {
    if (e.target.id !== 'memberSearch') return;
    App.memberSearch = e.target.value;
    clearTimeout(memberTimer);
    memberTimer = setTimeout(renderMemberGrid, 120);
  });
  qs('#serverBody').addEventListener('change', (e) => {
    if (e.target.id !== 'memberFilter') return;
    App.memberFilter = e.target.value;
    renderMemberGrid();
  });

  // 头像文件选择（选手编辑弹窗与批量名单共用）+ 备份上传还原
  document.addEventListener('change', (e) => {
    const file = e.target.closest('[data-role="avatar-file"]');
    if (file) uploadAvatarFile(file);
    const backupFile = e.target.closest('[data-role="backup-file"]');
    if (backupFile) uploadBackupFile(backupFile);
    // 参与名单勾选：同步卡片的「未参与」样式
    const pick = e.target.closest('[data-role="participant"]');
    if (pick) pick.closest('.pick')?.classList.toggle('pick--off', !pick.checked);
  });

  // 门禁：回车登录。三个门禁（赛事管理 / 服务器 / 我的）各有一个密钥框，
  // 只认「焦点所在门禁」里的密码输入，别去全局找 id（会命中隐藏页面里的空框）。
  ['#adminGate', '#serverBody', '#userBody'].forEach((sel) => {
    qs(sel).addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' || e.target.type !== 'password' || !e.target.closest('.gate')) return;
      login(e.target.value || '');
    });
  });

  // 桌面端跨断点时重绘排行榜列结构
  window.matchMedia('(min-width: 1024px)').addEventListener('change', () => {
    if (App.state) renderOverview(App.state);
  });

  // 「谁真的在推流」是媒体服务器侧的变化，不会走 WebSocket，只能在直播 / 频道页定时问。
  // 标签页切走就停掉轮询，切回来（且仍在这两个页面）立刻补一次。
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && ws && ws.readyState === WebSocket.OPEN) ws.send('state');
    if (document.hidden) stopLiveHealth();
    else if (App.view === 'live' || App.view === 'channels') startLiveHealth();
  });
}

/* --------------------------- 初始化 --------------------------- */
async function init() {
  Modal.init();
  installStageDelegation();
  installTeamDnD();
  hooks.onAuthLost = () => {
    App.me = null;
    App.server = null;
    App.serverTried = false;
    renderAdmin({ force: true });
    renderPublic();
  };
  hooks.onLiveHealth = (changed = true) => {
    if (!App.state) return;
    // 「直播中」标记跟着媒体服务器上报走：只有集合变了才重绘视图（含头像光环）
    if (changed) renderView(App.view, App.state);
    if (App.view === 'live') {
      qs('#liveInfoPanel').innerHTML = liveInfoHtml(App.state, App.livePicked || null);
    }
    // 管理页**不在这里重绘**：它整页是表单，15 秒一次的信号探测（以及切回
    // 标签页时的那次）会把正在填的内容冲掉。管理页跟「谁在推流」无关，
    // 它需要的数据在保存后由 hooksRenderAdmin 刷新。
  };
  hooks.gotoLive = (pid) => {
    App.livePlayerId = pid || null;
    goto(route.eventId, 'live');
  };
  // 舞台内切换机位 / 比赛：重绘机位条与地址面板，并按需换流播放
  hooks.onLivePick = () => {
    if (!App.state) return;
    const rebuilt = focusLive(App.state);
    if (!rebuilt) Live.playSelected(App.state);
  };
  // 在页面里换台时把地址更新成 /channels/<推流 ID>：用 replaceState，不新增历史，
  // 于是「刷新 / 收藏 / 分享」掉的都是当前这一路直播间。
  hooks.syncChannelUrl = (key = '') => {
    if (App.view !== 'channels') return;
    const path = routePath('', 'channels', key || '');
    if (location.pathname === path) return;
    route = { eventId: '', page: 'channels', channelId: key || '' };
    history.replaceState({ ...route }, '', path);
  };
  // 供 actions / events 等模块切路由（届 + 页面），避免它们反向依赖本模块
  hooks.goto = goto;
  // 数据变更后重新拉一遍当前路由（补齐 WS 推送里没有的 live/server 字段）
  hooks.refreshState = () => goto(route.eventId, route.page, { replace: true });

  bindStatic();
  setView(App.view, { silent: true });

  if (App.token) {
    const me = await refreshMeData();
    if (me) {
      log.info('会话校验通过', me.permission);
      await refreshPrivate();
    } else {
      log.warn('会话校验失败，已清除登录态');
      App.private = null;
    }
  } else {
    // 本机（localhost）直连免登录：能签发就直接以服务器管理员身份进入
    await tryLocalLogin();
  }

  await loadInitial();
  connectWS();
  renderAdmin();

  log.info('前端已就绪', 'api', API, 'view', App.view);
}

/**
 * 本机（localhost）直连免登录：向后端要一个服务器管理员会话。
 *
 * 服务端只在「回环地址 + 无转发头 + Host 为本机名」时才签发（见 `login_guard.is_local`），
 * 且可在「服务器 → 登录限制」里用「本机不设防」开关关闭；失败就静默忽略、照常显示登录页。
 */
async function tryLocalLogin() {
  try {
    const res = await api('/auth/local', { method: 'POST' });
    if (!res || !res.token) return false;
    App.token = res.token;
    localStorage.setItem(TOKEN_KEY, res.token);
    // 身份与成员视图都走 /api/me：会话就绑在那位唯一的服务器管理员成员上，
    // 所以这里能直接拿到他自己的资料（/user 页因此可用）
    await Promise.all([refreshMeData(), refreshPrivate()]);
    log.info('本机直连免登录：已获得服务器管理员会话');
    return true;
  } catch (err) {
    log.debug('本机免登录不可用（忽略）', err.message);
    return false;
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
