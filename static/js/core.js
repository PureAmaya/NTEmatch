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
  // 当前选中的成员频道 ID，以及最近一次解析出的频道对象（供局部刷新复用）
  channelId: null,
  channelPicked: null,
  events: [], // 届次列表缓存
  eventId: '', // 主赛事 ID（管理端设定的那一届；根路径就落在它身上）
  // 路由锁定的届（如 /e002 且它不是主赛事）：非空时整站看这一届，且不接受推送
  routeEvent: '',
  // 管理端专用私有数据（选手 UUID / QQ / 推流流名 / 推流地址）。
  // 公开状态里不含这些字段，登录后单独取，未登录时为 null。
  private: null,
  // 观众自选的播放线路：'' = 跟随服务端配置；'webrtc' | 'hls' = 手动指定
  liveProto: localStorage.getItem(LIVE_PROTO_KEY) || '',
  // 选手 ID → 时间戳：点过「刷新头像」后给图片地址加参数，绕过浏览器缓存
  avatarBust: {},
  token: localStorage.getItem(TOKEN_KEY) || '',
  view: localStorage.getItem(VIEW_KEY) || 'overview',
  online: false,
  filter: 'all',
  search: '',
};

/** 上层注入的回调，避免核心层反向依赖视图层。 */
export const hooks = {};

export const isAdmin = () => Boolean(App.token);
export const reveal = () => !App.state || App.state.ui?.revealResults !== false || isAdmin();

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
 * 直播页可用吗。
 *
 * 只有「主赛事 + 还没完结」才有直播：往届回看一律没有（比赛都结束了，
 * 不可能还在推流），已完结的届也没有；筹备中 / 进行中都保留。
 */
export const liveAvailable = (s) => !App.routeEvent && !isFinished(s);

/** 某个页面在当前状态下可用吗（目前只有直播页会随届次状态消失）。 */
export const pageAvailable = (page, s) => page !== 'live' || liveAvailable(s);

/**
 * 能改动数据吗：登录了，而且**不是在回看往届**、也**不是已完结的届**。
 *
 * 接口都是按主赛事设计的（没有届次参数），所以回看往届时必须把所有管理入口
 * 收起来——否则一次手滑就会写到主赛事上；已完结的届只留只读信息，要改先把
 * 它恢复成「进行中」。
 */
export const canEdit = () => Boolean(App.token) && !App.state?.readOnly && !isFinished();

/* --------------------------------- 路由 --------------------------------- */
/** 页面段：地址栏里的页名，与 index.html 的 .tab[data-view] 一一对应。 */
export const PAGES = ['overview', 'schedule', 'roster', 'live', 'channels', 'events', 'admin'];
export const PAGE_LABEL = {
  overview: '总览',
  schedule: '赛程',
  roster: '选手',
  live: '直播',
  channels: '频道',
  events: '往届',
  admin: '管理',
};

/**
 * 解析地址栏：``/<届 ID>/<页面>``，两段都可以缺省。
 *
 * * 带届 ID     → 就是那一届：``/e001``、``/e001/schedule``
 * * 只有页面段  → 独立页（``/events`` 往届），或主赛事的短链（``/admin``）
 * * 什么都没有  → 根路径，由 ``goto`` 跳到主赛事的路由
 */
export function parseRoute(path = location.pathname) {
  const segs = String(path).split('/').filter(Boolean);
  let eventId = '';
  let page = '';
  segs.forEach((seg) => {
    let s = seg;
    try {
      s = decodeURIComponent(seg);
    } catch {
      /* 非法转义就按原样处理 */
    }
    if (!page && PAGES.includes(s)) page = s;
    else if (!eventId && /^[A-Za-z0-9_-]{2,40}$/.test(s)) eventId = s;
  });
  return { eventId, page };
}

/** 拼地址：届 ID 缺省时回落到「主赛事」的短链（根路径 / 或 /events 这类独立页）。 */
export function routePath(eventId = '', page = 'overview') {
  const p = PAGES.includes(page) ? page : 'overview';
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
