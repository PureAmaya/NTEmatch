/* 启动引导：装配事件委托、实时通道、视图切换。
 * 各功能视图拆分在 core / ui / views / live / admin / actions 模块中。
 */

import {
  App,
  API,
  PAGES,
  VIEW_KEY,
  api,
  copyText,
  hooks,
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
import { installDnD as installTeamDnD } from './teams.js';
import { installStageDelegation, Live, refreshLiveHealth } from './live.js';
import {
  focusLive,
  liveInfoHtml,
  renderOverview,
  renderPublic,
  renderRosterGrid,
  renderView,
} from './views.js';
import { renderAdmin } from './admin.js';
import { handleAction, handleForm, login, uploadAvatarFile } from './actions.js';

/* --------------------------- 路由（届 + 页面） ---------------------------
 *
 * 一届 = 一条路由，形如 ``/<届 ID>/<页面>``，每一页都能刷新 / 收藏 / 分享：
 *
 *   /e001          e001 · 总览            /e001/schedule  e001 · 赛程
 *   /e001/admin    e001 · 管理            /events         往届（不属于任何一届）
 *
 * * 路由指向**主赛事** → 跟着主赛事走（吃实时推送；主赛事被换掉会自动跟着换地址）；
 *   指向别的届 → 锁定它只读回看，所有管理入口收起；
 * * 根路径 ``/`` 不留任何东西：自动跳到主赛事的路由（如 ``/e001``）；
 * * 已完结的届 / 往届回看没有直播页，访问它会回落到总览。
 *
 * 「进哪一届 + 看哪一页」只有这一个入口（goto），地址栏、数据、视图一起换，
 * 浏览器前进/后退也走它。
 */
/** 生效中的路由：eventId 为空 = 跟主赛事走（吃实时推送），非空 = 锁定该届只读回看。 */
let route = { eventId: '', page: 'overview' };

async function goto(eventId = '', page = 'overview', { replace = false } = {}) {
  const want = PAGES.includes(page) ? page : 'overview';
  // 「往届」是独立页：不属于任何一届，地址固定 /events（内容就是全部届的卡片）
  const listPage = want === 'events';
  let id = listPage ? '' : String(eventId || App.eventId || '');
  if (id && !App.events.some((e) => e.id === id)) {
    if (id !== route.eventId) toast(`没有这一届：${id}`, 'warn', 6000);
    id = '';
  }
  // 指向主赛事 = 跟着主赛事走（继续吃实时推送）；指向别的届 = 锁定那一届只读回看。
  // 两种情况地址里都带届 ID，所以刷新 / 收藏 / 分享永远落在同一届上。
  const locked = id && id !== App.eventId ? id : '';
  const switchedEvent = App.routeEvent !== locked;
  App.routeEvent = locked;
  if (switchedEvent) {
    App.livePlayerId = null;
    Live.stop(false);
  }

  if (locked) {
    // 锁定某一届：管理入口由状态里的 readOnly 收起，服务端推送也不再接收
    try {
      App.state = await api(`/events/${locked}/state`);
      App.liveInfo = null;
      App.liveNow = new Set();
      App.private = null;
      log.info('按路由看往届', locked, want);
    } catch (err) {
      log.error('往届状态加载失败', err);
      toast(err.message, 'err');
      return goto('', want, { replace: true });
    }
  } else {
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
    await refreshPrivate();
  }

  // 页面可用性要等状态到手才能定（已完结的届没有直播页）→ 不可用就回落到总览
  const pg = pageAvailable(want, App.state) ? want : 'overview';
  route = { eventId: listPage ? '' : id, page: pg };
  const path = routePath(route.eventId, pg);
  if (location.pathname !== path) history[replace ? 'replaceState' : 'pushState']({ ...route }, '', path);

  setView(pg, { silent: true });
  renderPublic();
  renderAdmin();
  log.debug('路由', path, '届', route.eventId || '(主赛事)');
}

window.addEventListener('popstate', (ev) => {
  const r = parseRoute();
  const page = r.page || ev.state?.page || 'overview';
  goto(r.eventId, page, { replace: true });
});

/* --------------------------- 视图切换 --------------------------- */
function setView(view, { silent = false } = {}) {
  // 页签被收起来的页面不能进（如已完结的届没有直播页）
  if (!qsa('.tab').some((t) => t.dataset.view === view && !t.hidden)) view = 'overview';
  App.view = view;
  document.documentElement.dataset.view = view;
  qsa('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.view === view)));
  qsa('.view').forEach((v) => {
    v.hidden = v.dataset.view !== view;
  });
  localStorage.setItem(VIEW_KEY, view);

  if (view === 'live') {
    if (App.state) focusLive(App.state);
    refreshLiveHealth();
  } else {
    Live.stop(false);
    if (App.state) renderView(view, App.state);
  }

  if (view === 'admin') renderAdmin();
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
      // 正在按路由回看往届：服务端推的是「当前届」，直接忽略，别把页面拽回去
      if (App.routeEvent) return;
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
  // 主赛事被换人了（管理端设定了新的主赛事）：跟着走，并把地址栏拨到新主赛事的路由
  if (!App.routeEvent && data.eventId && data.eventId !== App.eventId) {
    log.info('主赛事已切换', App.eventId, '→', data.eventId);
    App.eventId = data.eventId;
    App.events = []; // 届列表里的「主赛事」标记要重取
    goto('', route.page, { replace: true });
    return;
  }
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
  await goto(r.eventId, r.page || App.view || 'overview', { replace: true });
}

/* --------------------------- 事件装配 --------------------------- */
function bindStatic() {
  // 页签 = 路由：切页只换「页面段」，届次段原样保留（看往届时就在往届里翻页）
  qsa('.tab').forEach((tab) => {
    tab.addEventListener('click', () => goto(route.eventId, tab.dataset.view));
  });

  qs('#btnRefresh').addEventListener('click', async () => {
    if (ws && ws.readyState === WebSocket.OPEN && !App.routeEvent) ws.send('state');
    await hooks.refreshState();
    toast('已同步最新状态', 'ok', 2000);
  });

  qs('#btnAdmin').addEventListener('click', () => goto(route.eventId, 'admin'));

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

  // 管理端表单提交
  qs('#adminPanel').addEventListener('submit', (e) => {
    if (!e.target.dataset.form) return;
    e.preventDefault();
    handleForm(e.target);
  });

  // 头像文件选择（选手编辑弹窗与批量名单共用）
  document.addEventListener('change', (e) => {
    const file = e.target.closest('[data-role="avatar-file"]');
    if (file) uploadAvatarFile(file);
    // 参与名单勾选：同步卡片的「未参与」样式
    const pick = e.target.closest('[data-role="participant"]');
    if (pick) pick.closest('.pick')?.classList.toggle('pick--off', !pick.checked);
  });

  // 门禁：回车登录
  qs('#adminGate').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') login(qs('#adminKey')?.value || '');
  });

  // 桌面端跨断点时重绘排行榜列结构
  window.matchMedia('(min-width: 1024px)').addEventListener('change', () => {
    if (App.state) renderOverview(App.state);
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && ws && ws.readyState === WebSocket.OPEN) ws.send('state');
    // 回到页面时顺手刷新直播状态
    if (!document.hidden) refreshLiveHealth();
  });

  // 「谁真的在推流」是媒体服务器侧的变化，不会走 WebSocket，只能定时问一次
  setInterval(() => {
    if (!document.hidden) refreshLiveHealth();
  }, 15000);
}

/* --------------------------- 初始化 --------------------------- */
async function init() {
  Modal.init();
  installStageDelegation();
  installTeamDnD();
  hooks.onAuthLost = () => {
    renderAdmin();
    renderPublic();
  };
  hooks.onLiveHealth = (changed = true) => {
    if (!App.state) return;
    // 「直播中」标记跟着媒体服务器上报走：只有集合变了才重绘视图（含头像光环）
    if (changed) renderView(App.view, App.state);
    if (App.view === 'live') {
      qs('#liveInfoPanel').innerHTML = liveInfoHtml(App.state, App.livePicked || null);
    }
    if (App.view === 'admin') renderAdmin();
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
  // 供 actions / events 等模块切路由（届 + 页面），避免它们反向依赖本模块
  hooks.goto = goto;
  // 数据变更后重新拉一遍当前路由（补齐 WS 推送里没有的 live/server 字段）
  hooks.refreshState = () => goto(route.eventId, route.page, { replace: true });

  bindStatic();
  setView(App.view, { silent: true });

  if (App.token) {
    try {
      await api('/auth/check', { auth: true });
      log.info('管理会话校验通过');
      await refreshPrivate();
    } catch (err) {
      log.warn('管理会话校验失败', err.message);
      App.private = null;
    }
  }

  await loadInitial();
  connectWS();
  renderAdmin();

  log.info('前端已就绪', 'api', API, 'view', App.view);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
