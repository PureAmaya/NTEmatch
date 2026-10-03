/* 核心层：日志 / 通用工具 / 客户端状态 / REST / 弹窗 / 提示
 * 该模块不依赖任何视图，供上层（views / admin / actions / app）复用。
 * 调试：?debug=1 或 localStorage['nte:debug']='1'
 */

export const DEBUG =
  new URLSearchParams(location.search).get('debug') === '1' ||
  localStorage.getItem('nte:debug') === '1';

export const log = {
  debug: (...a) => DEBUG && console.debug('[nte]', ...a),
  info: (...a) => console.info('[nte]', ...a),
  warn: (...a) => console.warn('[nte]', ...a),
  error: (...a) => console.error('[nte]', ...a),
};

export const TOKEN_KEY = 'nte:token';
export const VIEW_KEY = 'nte:view';
/** 观众自选播放线路的本地记忆键（WebRTC / HLS）。 */
export const LIVE_PROTO_KEY = 'nte:liveProto';

/**
 * 状态指纹：届名 + 版本号。
 *
 * 用来判断「这份状态是不是已经渲染过」——重连时服务端会回放一次最近状态，
 * 指纹相同就不必把整页 DOM 重建一遍；换届时届名不同，不会误判。
 */
export const stateKey = (s) => (s ? `${s.revision ?? 0}|${s.event?.name || ''}` : '');
export const API = '/api';
/** 站点默认名称与默认副标题（服务器管理员可在「服务器 → 站点」里改名称）。 */
export const DEFAULT_SITE_NAME = 'NTE 比赛';
export const DEFAULT_TAGLINE = 'NEVERNESS TO EVERNESS · MATCH';

export const qs = (sel, root = document) => root.querySelector(sel);
export const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);
export const num = (v, d = 0) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
};
export const sign = (n) => (n > 0 ? `+${n}` : String(n));

export function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 完整日期时间：2026/10/03 20:00（用于赛事起止等需要年份的场合）。 */
export function fmtFull(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 只取时刻：20:00（同一天的区间显示用）。 */
export function fmtClock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 与 fmtTime 同天时只显示时刻，避免「10/03 20:00 → 10/03 21:12」的重复。 */
export function fmtRange(startIso, endIso) {
  if (!startIso || !endIso) return '';
  const a = new Date(startIso);
  const b = new Date(endIso);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return '';
  const sameDay =
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  return `${fmtTime(startIso)} → ${sameDay ? fmtClock(endIso) : fmtTime(endIso)}`;
}

/**
 * 起止时间区间文案（开始 / 结束都可为空）。
 * 例：「10/03 20:00 → 22:12」「10/03 20:00 开始 · 结束待定」「起止时间未登记」。
 */
export function fmtSpan(startIso, endIso, { empty = '起止时间未登记', open = '结束待定' } = {}) {
  if (startIso && endIso) return fmtRange(startIso, endIso);
  if (startIso) return `${fmtTime(startIso)} 开始 · ${open}`;
  if (endIso) return `结束于 ${fmtTime(endIso)}`;
  return empty;
}

/** 时长：42 分钟 / 1 小时 12 分 / 2 小时。 */
export function fmtDuration(minutes) {
  const n = Number(minutes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 60) return `${Math.round(n)} 分钟`;
  const h = Math.floor(n / 60);
  const m = Math.round(n % 60);
  return m ? `${h} 小时 ${m} 分` : `${h} 小时`;
}

/** 当前本地时间，格式化为 <input type="datetime-local"> 需要的值。 */
export function nowLocalInput(offsetMinutes = 0) {
  const d = new Date(Date.now() + offsetMinutes * 60_000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 把 ISO / 任意时间串转成 datetime-local 输入框的取值（YYYY-MM-DDTHH:MM）。 */
export function toLocalInput(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (err) {
    log.warn('剪贴板 API 不可用，尝试回退', err);
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (err) {
    log.error('复制失败', err);
    return false;
  }
}

/* ------------------------------- 客户端状态 ------------------------------- */
export const App = {
  state: null,
  liveInfo: null,
  liveHealth: null,
  // 登录身份（服务器返回）：{ uid, name, permission, isServer, canManageEvents }
  // null = 未登录或尚未校验；permission: member / event_admin / server_admin
  me: null,
  // 服务器管理页数据：成员列表 + 服务器配置（仅服务器管理员可写）
  server: null,
  // 直播信号检测的状态机：idle（还没检测）/ loading（检测中）/ ok / error（连续失败到上限）
  // 只在直播 / 频道页推进，见 live.js 的 startLiveHealth / stopLiveHealth
  liveHealthState: 'idle',
  liveHealthFails: 0, // 连续失败次数
  livePlayerId: null, // 当前选中的直播机位（选手 ID）
  liveRound: '', // 观众选中的比赛（对局编号）；'' = 全部机位
  // 「真的在推流」的选手集合（由媒体服务器上报，见 refreshLiveHealth）；
  // null = 还没探测过，此时回退到状态里的兜底值
  liveNow: null,
  // 主直播间（直播配置里的「默认流名」）有没有人在推流：true / false；
  // null = 还没探测过（此时以状态里的 /api/state → live.streaming 为准）
  liveMain: null,
  livePicked: null, // 最近一次解析出的机位对象（供局部刷新复用）
  // 成员频道（日常直播）：正在推流的频道 ID 集合；null = 还没探测过
  liveChannelsNow: null,
  // 成员直播间：正在推流的成员 uid 集合；null = 还没探测过
  liveMembersNow: null,
  // 当前选中的成员频道 ID，以及最近一次解析出的频道对象（供局部刷新复用）
  channelId: null,
  channelPicked: null,
  events: [], // 届次列表缓存
  // 后端「当前届」：写接口的作用对象（进某一届时若你有权限会自动切过去）。
  // 它不再是「主赛事」这种用户概念——主页列出全部届次，谁都不特殊。
  eventId: '',
  // 路由锁定的届：赛事页恒等于地址栏里的届 ID；独立页（主页 / 频道 / …）为空。
  // 非空且与 eventId 不同 = 正在只读查看另一届，此时忽略服务端推送。
  routeEvent: '',
  // 主页 / 全部赛事页的搜索词与分组展开状态（'active' / 'draft' / 'closed'）
  homeSearch: '',
  homeOpen: { active: true, draft: true, closed: false },
  // 错误页内容：{ code, title, desc }——找不到 / 没权限时由路由填好，denied 页只管渲染
  denied: null,
  // 管理端专用私有数据（选手 UUID / QQ / 推流流名 / 推流地址）。
  // 公开状态里不含这些字段，登录后单独取，未登录时为 null。
  private: null,
  // 观众自选的播放线路：'' = 跟随服务端配置；'webrtc' | 'hls' = 手动指定
  liveProto: localStorage.getItem(LIVE_PROTO_KEY) || '',
  // 选手 ID → 时间戳：点过「刷新头像」后给图片地址加参数，绕过浏览器缓存
  avatarBust: {},
  token: localStorage.getItem(TOKEN_KEY) || '',
  view: localStorage.getItem(VIEW_KEY) || 'home',
  online: false,
  filter: 'all',
  search: '',
  // 选手页的筛选（全部 / 已参与 / 未参与 / 替补 / 直播中 / 封禁中）
  rosterFilter: 'all',
  // 成员管理页的搜索 / 筛选
  memberSearch: '',
  memberFilter: 'all',
};

/** 上层注入的回调，避免核心层反向依赖视图层。 */
export const hooks = {};

export const isAdmin = () => Boolean(App.token);
export const reveal = () => !App.state || App.state.ui?.revealResults !== false || isAdmin();

/**
 * 当前登录者的权限：member / event_admin / server_admin；**没拿到身份就是空**。
 *
 * 刻意不做「有 token 就先当管理员」的保守兜底：令牌过期 / 会话失效（会话在内存里，
 * 服务重启即失效）时会短暂冒出「没登录却顶着管理入口」的假象。启动流程本来就是
 * 先 ``await`` 身份、再首次渲染，所以收紧不会让入口闪烁。
 */
export const myPermission = () => App.me?.permission || '';
export const isServerAdmin = () => myPermission() === 'server_admin';
export const canManageEvents = () =>
  myPermission() === 'event_admin' || myPermission() === 'server_admin';

/**
 * 这一届是否**已经完结**（只读 + 没有直播页）。
 *
 * 两种情形都算完结：届状态标了 ``closed``，或者登记了结束时间
 * （管理端写明「填了即视为已结束」，服务端 eventTime.state 也会变成 finished）。
 */
export const isFinished = (s) => {
  const st = s || App.state;
  return Boolean(st) && (st.eventStatus === 'closed' || st.eventTime?.state === 'finished');
};

/**
 * 站点名称（服务器管理员设定，全局）。
 *
 * 与比赛无关的页面（主页 / 频道 / 赛事列表 / 我的 / 服务器）顶栏显示它，
 * 浏览器标签也用它；进了某一届才换成那一届的名字。
 */
export const siteName = (s = App.state) => s?.siteName || DEFAULT_SITE_NAME;

/**
 * 直播页可用吗：**这一届还没完结**就有（筹备中 / 进行中都保留）。
 *
 * 已完结（标了 closed 或登记了结束时间）就没有直播页——比赛都结束了，
 * 不可能还在推流。
 */
export const liveAvailable = (s) => !isFinished(s);

/**
 * 页面在当前状态下可用吗。
 *
 * 赛事页（总览 / 赛程 / 选手 / 直播 / 赛事管理）要挂在某一届上；其中
 * 「直播」随届次状态消失、「赛事管理」要有赛事管理权限。独立页永远可用。
 */
export const pageAvailable = (page, s) => {
  if (STANDALONE_PAGES.includes(page)) return true;
  // 只读查看别人的届时，直播页签与赛事管理一样收起来（判定与 views.syncTabs 对齐）
  if (page === 'live') return liveAvailable(s) && !s?.readOnly;
  if (page === 'manage') return canManageEvents();
  return ['overview', 'schedule', 'roster'].includes(page);
};

/**
 * 能改动数据吗：登录了、有赛事管理权限，而且**不是只读查看**（别人的届 / 未登录）、
 * 也**不是已完结的届**。
 *
 * 写接口都作用在「后端当前届」上（没有届次参数），所以进入某届时若有权限会先把它
 * 切过去；没有权限就只能只读，所有管理入口一并收起。已完结的届只留只读信息，
 * 要改先把它恢复成「进行中」。
 */
export const canEdit = () =>
  Boolean(App.token) && canManageEvents() && !App.state?.readOnly && !isFinished();

/* --------------------------------- 路由 --------------------------------- */
/** 页面段：地址栏里的页名，与 index.html 的 .tab[data-view] 一一对应。 */
export const PAGES = [
  'home',
  'overview',
  'schedule',
  'roster',
  'live',
  'manage',
  'channels',
  'events',
  'server',
  'user',
];
/**
 * 不属于任何一届的独立页（地址不带届 ID）。
 *
 * 这里的分界线就是「页签长什么样」：**只有赛事页（overview…manage）能看到
 * 总览 / 赛程 / 选手 / 直播**；独立页（主页 / 频道 / 赛事列表 / 服务器 / 我的）
 * 页签里只剩一个「主页」——比赛与频道之间不能直接互跳，都得经过主页。
 */
export const STANDALONE_PAGES = ['home', 'channels', 'events', 'server', 'user', 'denied'];
export const PAGE_LABEL = {
  home: '主页',
  overview: '总览',
  schedule: '赛程',
  roster: '选手',
  live: '直播',
  channels: '频道',
  events: '全部赛事',
  manage: '赛事管理',
  server: '服务器',
  user: '我的',
};

/**
 * 解析地址栏。
 *
 * * ``/``                    → 主页（不属于任何一届）
 * * ``/e001``、``/e001/schedule`` → 某一届的页面
 * * ``/events``、``/admin``、``/user`` → 独立页
 * * ``/channels``            → 频道模块
 * * ``/channels/<推流 ID>``  → 直接打开那个直播间（第二段是**推流 ID / 流名**，不是届次）
 *
 * ``page`` 为空表示「只给了届次，没给页面段」（由调用方补默认页）。
 */
export function parseRoute(path = location.pathname) {
  const segs = String(path).split('/').filter(Boolean);
  if (!segs.length) return { eventId: '', page: '', channelId: '' };
  const decode = (raw) => {
    try {
      return decodeURIComponent(raw);
    } catch {
      return raw; // 非法转义就按原样处理
    }
  };
  // 频道是独立模块：/channels 与 /channels/<推流 ID>
  if (segs[0].toLowerCase() === 'channels') {
    return { eventId: '', page: 'channels', channelId: segs[1] ? decode(segs[1]) : '' };
  }
  let eventId = '';
  let page = '';
  segs.forEach((seg) => {
    let s = decode(seg);
    // /admin 固定 = 服务器管理页（与届次无关）；赛事管理是 /<届>/manage
    if (s === 'admin' && segs.length === 1) s = 'server';
    if (!page && PAGES.includes(s)) page = s;
    else if (!eventId && /^[A-Za-z0-9_-]{2,40}$/.test(s)) eventId = s;
  });
  return { eventId, page, channelId: '' };
}

/**
 * 拼地址。届次段缺省时就是独立页（``/``、``/channels``、``/events``…）。
 *
 * ``channelId`` 是**推流 ID / 流名**（不是内部频道 ID）——它就是观众能在别处
 * 看到的那串名字，所以分享出去的链接别人也能对上。
 */
export function routePath(eventId = '', page = 'overview', channelId = '') {
  const p = PAGES.includes(page) ? page : 'overview';
  if (p === 'home') return '/';
  if (p === 'channels') {
    return channelId ? `/channels/${encodeURIComponent(channelId)}` : '/channels';
  }
  if (p === 'events') return '/events';
  if (p === 'server') return '/admin';
  if (p === 'user') return '/user';
  if (!eventId) return p === 'overview' ? '/' : `/${p}`;
  return p === 'overview' ? `/${eventId}` : `/${eventId}/${p}`;
}

/* --------------------------------- REST --------------------------------- */
export async function api(path, { method = 'GET', body, auth = false } = {}) {
  const headers = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth && App.token) headers['X-NTE-Token'] = App.token;

  let res;
  try {
    res = await fetch(API + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
  } catch (err) {
    log.error('网络请求失败', method, path, err);
    throw new Error('网络不可达，请检查服务是否在线');
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }

  if (!res.ok) {
    const detail = data && (data.error || data.detail);
    const message = typeof detail === 'string' ? detail : `请求失败 (HTTP ${res.status})`;
    if (res.status === 401 && auth) {
      log.warn('管理会话失效，清除本地令牌');
      App.token = '';
      localStorage.removeItem(TOKEN_KEY);
      if (hooks.onAuthLost) hooks.onAuthLost();
    }
    const error = new Error(message);
    error.status = res.status;
    log.warn('API 错误', method, path, res.status, message);
    throw error;
  }
  log.debug('API 成功', method, path, res.status);
  return data;
}

/**
 * 拉取管理端私有数据（选手隐私字段 + 推流地址）。
 *
 * 公开状态（含 WebSocket 广播）里**没有** UUID / QQ / 推流凭据，
 * 因此管理端界面需要这些内容时必须先登录，再从这里取。
 */
export async function refreshPrivate() {
  if (!App.token) {
    App.private = null;
    return null;
  }
  try {
    App.private = await api('/private', { auth: true });
    log.debug('私有数据已加载', Object.keys(App.private?.players || {}).length, '位选手');
  } catch (err) {
    log.warn('私有数据加载失败', err);
    App.private = null;
  }
  return App.private;
}

/**
 * 拉取当前登录身份（权限 / uid / 昵称），用于恢复「我是谁、能做什么」。
 *
 * 未登录时清空，登录失效（401）也会清空并触发 ``hooks.onAuthLost``。
 */
export async function refreshMe() {
  if (!App.token) {
    App.me = null;
    return null;
  }
  try {
    App.me = await api('/auth/check', { auth: true });
    log.debug('身份已加载', App.me?.permission);
  } catch (err) {
    log.warn('身份校验失败', err.message);
    App.me = null;
  }
  return App.me;
}

/* -------------------------------- Toast -------------------------------- */
export function toast(message, kind = 'info', ms = 3600) {
  const box = qs('#toasts');
  if (!box) return;
  const el = document.createElement('div');
  el.className = `toast toast--${kind}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 220);
  }, ms);
  log.debug('提示', kind, message);
}

/* -------------------------------- Modal -------------------------------- */
export const Modal = {
  el: null,
  init() {
    this.el = qs('#modal');
    if (!this.el) return;
    this.el.addEventListener('click', (e) => {
      if (e.target.closest('[data-close]')) Modal.close();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !Modal.el.hidden) Modal.close();
    });
  },
  open({ title = '', body = '', footer = '', onMount } = {}) {
    qs('#modalTitle').textContent = title;
    qs('#modalBody').innerHTML = body;
    const footEl = qs('#modalFoot');
    footEl.innerHTML = footer;
    footEl.hidden = !footer;
    this.el.hidden = false;
    if (typeof onMount === 'function') onMount(qs('#modalBody'), footEl);
    log.debug('打开弹窗', title);
  },
  close() {
    if (this.el) this.el.hidden = true;
    qs('#modalBody').innerHTML = '';
    qs('#modalFoot').innerHTML = '';
    this._onPick = null;
  },
};
