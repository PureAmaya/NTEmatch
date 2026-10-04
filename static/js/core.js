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

/* ---------------------------- 比法（metric） ----------------------------
 * 与后端 app/metrics.py 一一对应：score = 计分制（分高者胜）、
 * time = 用时制（用时短者胜）。方向、0 的含义、显示格式三件事必须与后端一致，
 * 否则「预览说 A 胜、结算说 B 胜」——观众只会看到自相矛盾。
 *
 * 用时制的数值一律是**毫秒**（整数）：要和后端一样能可靠地求和、比并列。
 */
const DNF_WORDS = /^(dnf|dns|dnq|退赛|未完赛|未完成|-|—|\/|无)$/i;

/** 当前比法；认不出来按计分制（老数据没有这个字段）。 */
export const metricOf = (s) => (s?.rules?.metric === 'time' ? 'time' : 'score');
export const isTimeMetric = (s) => metricOf(s) === 'time';

/** 毫秒 → 1:23.456 / 83.45（规则与后端 metrics.format_time 完全一致）。 */
export function fmtMilli(ms) {
  const n = Math.round(Number(ms) || 0);
  if (n <= 0) return '—';
  const dec = n % 10 === 0 ? 2 : 3;
  const unit = 1000;
  const p2 = (v) => String(v).padStart(2, '0');
  const hours = Math.floor(n / (3600 * unit));
  const minutes = Math.floor(n / (60 * unit)) % 60;
  const secs = Math.floor(n / unit) % 60;
  const millis = String(n % unit).padStart(3, '0').slice(0, dec);
  if (hours) return `${hours}:${p2(minutes)}:${p2(secs)}.${millis}`;
  if (minutes) return `${minutes}:${p2(secs)}.${millis}`;
  return `${secs}.${millis}`;
}

/** 按比法显示一个成绩：time → 时间，score → 数字。 */
export function fmtVal(value, metric = metricOf()) {
  return metric === 'time' ? fmtMilli(value) : String(num(value));
}

/** 按比法解析输入；解析不了抛错（静默当 0 会把人记成「未完赛」）。 */
export function parseVal(text, metric = metricOf()) {
  const raw = String(text ?? '').trim();
  if (!raw) return 0;
  if (metric !== 'time') {
    const n = Number(raw);
    if (!Number.isFinite(n)) throw new Error(`「${raw}」不是合法的分数`);
    return Math.max(0, Math.trunc(n)); // 与后端 int() 一致
  }
  if (DNF_WORDS.test(raw)) return 0;
  const parts = raw
    .replace(/["”″]/g, '.')
    .replace(/[:：'’′]/g, ':')
    .split(':')
    .filter((part) => part !== '');
  if (!parts.length) return 0;
  const step = (part) => {
    const n = Number(part);
    if (!Number.isFinite(n)) {
      throw new Error(`「${raw}」不是合法的用时（可写 1:23.456 或 83.45）`);
    }
    return n;
  };
  let total = step(parts[parts.length - 1]) * 1000;
  parts
    .slice(0, -1)
    .reverse()
    .forEach((part, i) => {
      total += step(part) * 60 ** (i + 1) * 1000;
    });
  return Math.max(0, Math.round(total));
}

/**
 * 成绩比较器（Array#sort 用）：负数 = a 在前。
 *
 * **0 / 空 = 没有成绩，永远排在有成绩的后面**——否则用时制里
 * 「0 秒」会被当成最快的人（与后端 metrics.value_key 同一条约定）。
 */
export function cmpVal(a, b, metric = metricOf()) {
  const noA = !(Number(a) > 0);
  const noB = !(Number(b) > 0);
  if (noA !== noB) return noA ? 1 : -1;
  if (noA) return 0;
  return metric === 'time' ? Number(a) - Number(b) : Number(b) - Number(a);
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
  // 成员分页：当前页 + 上次量到的列数（一页 = 4 行 × 列数，见 renderMemberGrid）
  memberPage: 1,
  memberCols: 0,
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
  'developer',
];
/**
 * 不属于任何一届的独立页（地址不带届 ID）。
 *
 * 这里的分界线就是「页签长什么样」：**只有赛事页（overview…manage）能看到
 * 总览 / 赛程 / 选手 / 直播**；独立页（主页 / 频道 / 赛事列表 / 服务器 / 我的 /
 * 开发者）页签里只剩一个「主页」——比赛与频道之间不能直接互跳，都得经过主页。
 */
export const STANDALONE_PAGES = [
  'home',
  'channels',
  'events',
  'server',
  'user',
  'developer',
  'denied',
];
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
  developer: '开发者',
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
  if (p === 'developer') return '/developer';
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
/**
 * 全站唯一的弹窗宿主（`#modal`）：所有弹窗——Markdown 编辑器、版权信息、通知、
 * 成员编辑、确认框——都从这里开关，所以**动画只在这一处实现**，新弹窗自动继承。
 *
 * 动效刻意做得很轻：背板淡入淡出 + 面板一点点上浮与微缩放（见 nte.css 的 `.modal`）。
 * 关闭比打开更快一点：关窗是「这件事做完了」，不该让人等动画。
 */
const MODAL_OUT_MS = 110 + 50; // 与 .modal.is-closing 的 transition-duration 对齐，多 50ms 兜底

export const Modal = {
  el: null,
  _timer: null,
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
  open({ title = '', body = '', footer = '', onMount, className = '' } = {}) {
    const alreadyOpen = this.el && !this.el.hidden;
    // 上一次的淡出还没结束就又要开（比如「保存 → 关掉 → 立刻开下一个」）：取消它，
    // 否则那个定时器会在新弹窗上补一刀，把刚打开的面板又藏起来。
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    qs('#modalTitle').textContent = title;
    qs('#modalBody').innerHTML = body;
    // 弹窗尺寸由调用方按需指定（编辑器要一个大的悬浮窗；普通弹窗保持原样）。
    // 每次打开都重置，避免上一回的 className 粘在下一个弹窗上。
    const box = qs('.modal__box', this.el);
    if (box) box.className = `modal__box${className ? ` ${className}` : ''}`;
    const footEl = qs('#modalFoot');
    footEl.innerHTML = footer;
    footEl.hidden = !footer;
    this.el.hidden = false;
    this.el.classList.remove('is-closing');
    if (alreadyOpen) {
      // 本来就开着（只是换了内容）：直接置成终态，不再播一次入场动画
      this.el.classList.add('is-open');
    } else {
      // 先**强制一次布局**，让浏览器把「初始状态」（透明度 0、略微下沉）真正算出来，
      // 再切到终态；否则同一个任务里「display:none → 可见」和「opacity 0 → 1」会被
      // 合并成一次样式计算，浏览器没有可插值的起点，动画根本不播（只闪一下）。
      //
      // 读一次 offsetWidth 是最省的办法（等于要求布局）。这也是为什么这里**不用**
      // requestAnimationFrame：它的回调在本帧绘制**之前**跑，照样会被合并掉。
      void this.el.offsetWidth;
      this.el.classList.add('is-open');
    }
    if (typeof onMount === 'function') onMount(qs('#modalBody'), footEl);
    log.debug('打开弹窗', title);
  },
  close() {
    if (!this.el || this.el.hidden) {
      this._reset();
      return;
    }
    // 移除终态 → 面板与背板一起淡出；淡出播完再 hidden，否则会「啪」地消失
    this.el.classList.remove('is-open');
    this.el.classList.add('is-closing');
    this._timer = setTimeout(() => {
      this._timer = null;
      if (this.el && !this.el.classList.contains('is-open')) {
        this.el.hidden = true;
        this.el.classList.remove('is-closing');
      }
      this._reset();
    }, MODAL_OUT_MS);
    log.debug('关闭弹窗');
  },
  /** 清空内容（弹窗已经不可见时也要清，避免上一份内容留到下一次打开）。 */
  _reset() {
    const body = qs('#modalBody');
    const foot = qs('#modalFoot');
    if (body) body.innerHTML = '';
    if (foot) foot.innerHTML = '';
    this._onPick = null;
  },
};
