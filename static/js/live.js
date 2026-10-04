/* 直播播放控制器：观看线路两条——8889（WebRTC）优先，8888（HLS）兜底。
 * 仅依赖核心层；对外暴露 Live 与事件委托安装函数。
 */

import { App, LIVE_PROTO_KEY, api, copyText, esc, hooks, isAdmin, log, qs, toast } from './core.js';
import { PUSH_TIP_LINE, pushUrlOf, roomFor } from './ui.js';

/**
 * 当前线路对应的**观看地址**：HLS 给 8888 那条，否则给 8889 那条。
 *
 * 两条都是「端口 + 流名」，直接打开就能看，也是播放器用的地址。
 * 地址就是源站地址，不做反代；站点是 HTTPS 时源站也必须是 HTTPS，
 * 否则浏览器会按混合内容拦掉。
 */
function watchUrlOf(room) {
  if (App.liveProto === 'hls' && room?.hls) return room.hls;
  return room?.webrtc || App.liveInfo?.originWebrtc || '';
}

/** hls.js 的 CDN 地址；只在浏览器没有原生 HLS 时才按需加载。 */
const HLS_CDN = 'https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js';
let hlsLoading = null;

/** 按需加载 hls.js（Chrome / Firefox 要它才能播 HLS）；同一页面只加载一次。 */
function loadHlsLib() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (hlsLoading) return hlsLoading;
  hlsLoading = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = HLS_CDN;
    script.async = true;
    script.onload = () =>
      window.Hls ? resolve(window.Hls) : reject(new Error('hls.js 未就绪'));
    script.onerror = () => reject(new Error('hls.js 加载失败（网络不可达）'));
    document.head.appendChild(script);
  }).catch((err) => {
    hlsLoading = null; // 允许下次重试
    throw err;
  });
  return hlsLoading;
}

/**
 * 把信令失败的 HTTP 状态翻成人话。
 *
 * 404 是最常见的那种，但它有**两种完全不同的含义**，实测 MediaMTX 的应答就能分开：
 *
 * * ``{"status":"error","error":"no stream is available on path…"}`` —— 路径对、**这一路没人推**；
 * * ``404 page not found``（Go 路由的默认 404）—— **这个地址压根不存在**，多半是站点里
 *   直播线路的端口 / 地址填错了。
 *
 * 两者混成一句「没有推流」会把人带去查推流端，其实该去改地址——所以按应答体分开说。
 */
function signalError(status, bodies = []) {
  if (status === 404) {
    if (bodies.some((b) => /no stream is available/i.test(b))) {
      return '这个机位现在没有推流（媒体服务器上没有这个流名）';
    }
    if (bodies.some((b) => /page not found/i.test(b))) {
      return '连不上这个观看地址（媒体服务器上没这个路径）：检查站点里直播线路的端口与地址';
    }
    return '这个机位现在没有推流（媒体服务器上没有这个流名）';
  }
  if (status === 401 || status === 403) {
    return `媒体服务器拒绝了信令（HTTP ${status}）：检查推流鉴权是否放行观看`;
  }
  return `信令失败 HTTP ${status}`;
}

function waitIceComplete(pc, timeout = 3000) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => {
      if (pc.iceGatheringState === 'complete') done();
    };
    pc.addEventListener('icegatheringstatechange', check);
    setTimeout(done, timeout);
  });
}

const LIVE_IDS = {
  stage: '#liveStage',
  video: '#liveVideo',
  cover: '#liveCover',
  state: '#liveState',
};

/** 成员频道板块的播放器：同一套实现，只是元素落在另一个容器里。 */
const CHANNEL_IDS = {
  stage: '#channelStage',
  video: '#channelVideo',
  cover: '#channelCover',
  state: '#channelState',
};

/**
 * 直播播放控制器（8889 / WebRTC 优先，8888 / HLS 兜底）。
 *
 * 抽成可复用工厂：比赛直播页与成员频道板块各持有一个实例，互不干扰——
 * 元素 id 不同，实例状态（pc / token / 正在播的机位）也各自独立。
 */
function makePlayer(ids) {
  // 只在本播放器的容器内查找元素，两个板块的同名结构不会互相串到
  const q = (sel) => ((ids.stage && qs(ids.stage)) || document).querySelector(sel);
  return {
    ids,
    pc: null,
    token: 0,
    room: null, // 当前机位的地址集合（key_endpoints 结果）
    // 正在播的「机位 + 线路」：只有两者都没变、且画面确实还活着时才跳过重连
    playing: { key: '', mode: '' },
    // 用户**主动**按过「停止」：此后不再自动开播（否则刚停掉、一次信号刷新又给放上了）。
    // 任何「用户自己想看」的动作（选台 / 点播放 / 换线路）都会把它清掉。
    stoppedByUser: false,
    hlsInst: null, // hls.js 实例（非原生 HLS 的浏览器才有）

  el: () => q(ids.video),

  setState(text) {
    const el = q(ids.state);
    if (el) el.textContent = text;
  },

  setCover(title, msg) {
    const cover = q(ids.cover);
    const video = this.el();
    if (!cover) return;
    if (video) video.hidden = true;
    cover.hidden = false;
    // 两个入参都要转义：`msg` 里会带**媒体服务器返回的错误原因**（含 URL / 报文片段），
    // 那是本站之外的文本，直接当 HTML 插进来就是一个跨站脚本口子。
    cover.innerHTML =
      `<div class="stage-cover__noise"></div>` +
      `<div class="stage-cover__title">${esc(title)}</div>` +
      `<div class="stage-cover__msg">${esc(msg)}</div>`;
  },

  hideCover() {
    const cover = q(ids.cover);
    const video = this.el();
    if (cover) cover.hidden = true;
    if (video) video.hidden = false;
  },

  /** HLS：Safari 原生直放；其它浏览器临时取 hls.js（失败只能改用 WebRTC 线路）。 */
  async hls(url) {
    const video = this.el();
    if (!video || !url) {
      this.setCover('缺少 HLS 地址', '请在管理端填写 HLS 根地址，或改用 WebRTC 线路');
      return;
    }
    this.destroyHls();
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      this.hideCover();
      video.src = url;
      this.setState('HLS 播放中（原生）');
      video.play().catch((err) => log.warn('HLS 自动播放被拦截', err));
      log.info('直播使用 HLS（原生）', url);
      return;
    }
    this.setState('HLS 加载中…');
    try {
      const Hls = await loadHlsLib();
      if (!Hls.isSupported()) throw new Error('浏览器不支持 MSE');
      // 直播以「能看」为先：lowLatencyMode 会死追直播边缘，网络稍抖就反复追赶 + 重
      // 缓冲（看起来就是一直卡）；这里改成给足缓冲、只跟到直播边缘往后几秒。
      const inst = new Hls({
        lowLatencyMode: false,
        liveSyncDurationCount: 3,
        maxBufferLength: 12,
        maxMaxBufferLength: 30,
        backBufferLength: 30,
        liveDurationInfinity: true,
        enableWorker: true,
      });
      this.hlsInst = inst;
      inst.on(Hls.Events.ERROR, (_evt, data) => {
        if (!data?.fatal) return;
        log.warn('HLS 播放出错', data.type, data.details);
        this.destroyHls();
        this.setCover(
          'HLS 播放失败',
          `${data.details || data.type}；可在播放器上方把线路切到 WebRTC 再试`
        );
        this.setState('HLS 失败');
      });
      inst.loadSource(url);
      inst.attachMedia(video);
      this.hideCover();
      this.setState('HLS 播放中');
      video.play().catch((err) => log.warn('HLS 自动播放被拦截', err));
      log.info('直播使用 HLS（hls.js）', url);
    } catch (err) {
      log.warn('hls.js 不可用', err);
      this.setCover(
        '当前浏览器不支持 HLS',
        `${err.message || err}；可在播放器上方把线路切到 WebRTC 再试`
      );
      this.setState('HLS 不可用');
    }
  },

  destroyHls() {
    if (!this.hlsInst) return;
    try {
      this.hlsInst.destroy();
    } catch (err) {
      log.debug('销毁 hls.js 实例异常', err);
    }
    this.hlsInst = null;
  },

  /** WebRTC 观看：向「8889 观看地址」POST 一次 SDP 换回 answer。 */
  async webrtc(url) {
    if (!url) throw new Error('缺少 WebRTC 观看地址');
    const video = this.el();
    if (!video) throw new Error('播放器尚未就绪');
    this.hideCover();
    this.setState('WebRTC 协商中…');

    const pc = new RTCPeerConnection({ iceServers: [] });
    this.pc = pc;
    const myToken = ++this.token;

    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });
    // 流畅优先：默认的抖动缓冲太小，网络一抖就丢帧（画面顿）；给到 ~0.3s 缓冲，
    // 代价只是多一点点延迟。不支持这个属性的浏览器会抛错，忽略即可。
    pc.getReceivers().forEach((recv) => {
      try {
        recv.jitterBufferTarget = 300;
      } catch (err) {
        log.debug('设置抖动缓冲失败（忽略）', err);
      }
    });
    pc.ontrack = (ev) => {
      if (myToken !== this.token) return;
      if (ev.streams && ev.streams[0]) {
        video.srcObject = ev.streams[0];
        video.play().catch((err) => log.warn('WebRTC 自动播放被拦截', err));
      }
    };
    pc.onconnectionstatechange = () => {
      if (myToken !== this.token) return;
      const st = pc.connectionState;
      log.debug('WebRTC 连接状态', st);
      if (st === 'connected') this.setState('WebRTC 已连接');
      else if (st === 'failed') this.setState('WebRTC 连接失败');
      else if (st === 'disconnected') this.setState('WebRTC 已断开');
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIceComplete(pc);
    if (myToken !== this.token) throw new Error('已取消');

    // 信令端点：MediaMTX 的**读流端点是 `<路径>/whep`**（它自带的播放页发的也是这个），
    // 而裸路径 `<路径>` 是那张**播放页**（只认 GET）——对它 POST 只会拿到 Go 路由的
    // 「404 page not found」，跟随便编个路径完全一样（实测过，见 README）。
    // 所以先发 /whep；只有 404/405（这个端点不存在）才退回裸路径，兼容只认裸路径的老版本。
    const endpoints = url.endsWith('/whep') ? [url] : [`${url}/whep`, url];
    let res = null;
    const bodies = []; // 各端点的应答体：两种 404 意思不同，报错时要认出来（见 signalError）
    for (const endpoint of endpoints) {
      res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: pc.localDescription.sdp,
      });
      if (res.ok) {
        url = endpoint;
        break;
      }
      // 只有「这个端点不存在」才换下一个；其它状态码按它本身报错
      if (res.status !== 404 && res.status !== 405) break;
      bodies.push(await res.text().catch(() => ''));
      log.warn('信令端点在媒体服务器上不存在，换下一个', endpoint, res.status);
    }
    if (!res.ok) throw new Error(signalError(res.status, bodies));
    await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() });
    this.setState('WebRTC 播放中');
    log.info('直播使用 WebRTC', url);
  },

  /** 播放一个机位；room 为 key_endpoints 结果。 */
  async playRoom(room) {
    const target = room && room.key ? room : null;
    const st = App.state?.stream || {};
    // 观众手动选的线路优先（WebRTC 延迟低但 UDP 怕抖动；HLS 走 TCP 更稳）
    const mode = App.liveProto || st.mode || 'auto';
    // 同一机位 + 同一条线路、而且确实还在播：只把暂停的画面恢复，不做重协商
    // （重连会把画面打断一下）。**换线路（WebRTC ↔ HLS）必须真的重连**，
    // 所以这里一定要比 mode——只比机位的话，点「HLS / WebRTC」会像没反应。
    const video = this.el();
    const alive =
      this.pc?.connectionState === 'connected' ||
      Boolean(video && video.src && !video.paused);
    if (target && alive && this.playing.key === target.key && this.playing.mode === mode) {
      if (video && video.paused) video.play().catch((err) => log.warn('恢复播放失败', err));
      return;
    }
    await this.stop(false);
    this.room = target;
    if (!this.room) {
      // 检测还没出结果 / 拿不到结果时，别直接说「没有任何人在直播」——那是误导
      const state = App.liveHealthState;
      if (state === 'idle' || state === 'loading') {
        this.setCover('正在检测推流状态…', '检测完成后会自动列出正在推流的机位，不用手动刷新。');
        this.setState('检测中');
        return;
      }
      if (state === 'error') {
        this.setCover(
          '获取推流状态失败',
          `${App.liveHealth?.reason || '已连续 3 次未取到推流状态'}；请等服务器恢复后在下方点「重试」。`
        );
        this.setState('检测失败');
        return;
      }
      this.setCover('当前没有直播', '有人开播后，机位会自动出现在这里，点击即可观看。');
      this.setState('无人直播');
      return;
    }
    if (st.enabled === false) {
      this.setCover('直播已关闭', '管理员可在后台开启直播功能');
      this.setState('已关闭');
      return;
    }
    this.setState(mode === 'hls' ? '切换到 HLS…' : '连接中…');
    try {
      // 源站地址直连：webrtc = 8889 观看地址，hls = 8888 观看地址
      if (mode === 'hls') await this.hls(this.room.hls);
      else {
        try {
          await this.webrtc(this.room.webrtc);
        } catch (err) {
          // 自动模式：WebRTC 不通就退到 HLS（TCP，抗抖动）；手动选了 WebRTC 就直接报错
          if (mode !== 'auto') throw err;
          log.warn('WebRTC 不可用，改用 HLS', err);
          await this.hls(this.room.hls);
        }
      }
      // 记下「现在播的是哪个机位、哪条线路」，供下一次复用判断
      this.playing = { key: this.room.key, mode };
    } catch (err) {
      log.warn('直播播放失败', mode, err);
      if (mode === 'auto') {
        this.setCover(
          '直播连接失败',
          `${err.message || err}；可在播放器上方手动切换 WebRTC / HLS 线路再试`
        );
      } else {
        this.setCover('直播连接失败', err.message || String(err));
      }
      this.setState('未连接');
    }
  },

  /** 播放当前选中的机位（未选择时展示引导提示）。 */
  playSelected(s) {
    void s;
    return this.playRoom(roomFor(App.livePlayerId));
  },

  async stop(manual) {
    this.token += 1;
    this.playing = { key: '', mode: '' };
    this.destroyHls();
    if (this.pc) {
      try {
        this.pc.getReceivers().forEach((r) => r.track && r.track.stop());
        this.pc.close();
      } catch (err) {
        log.debug('关闭 PeerConnection 异常', err);
      }
      this.pc = null;
    }
    const video = this.el();
    if (video) {
      if (video.srcObject) {
        video.srcObject.getTracks().forEach((t) => t.stop());
        video.srcObject = null;
      }
      video.removeAttribute('src');
      try {
        video.load();
      } catch (err) {
        log.debug('重置 video 异常', err);
      }
    }
    if (manual) {
      this.stoppedByUser = true; // 别再自动开播把他烦回来（见 stoppedByUser 注释）
      this.setState('已停止');
      this.setCover('已停止播放', '点击「播放」重新连接直播信号');
      log.info('直播已手动停止');
    }
  },
  };
}

/** 比赛直播页的播放器（元素在 ``#liveStage`` 里）。 */
export const Live = makePlayer(LIVE_IDS);

/** 成员频道板块的播放器（元素在 ``#channelStage`` 里）。 */
export const ChannelLive = makePlayer(CHANNEL_IDS);

/* --------------------------- 直播信号检测 ---------------------------
 *
 * **只在直播 / 频道页运行**：进入这两个页面才开始检测，离开就停。
 * 没人看直播时既不轮询、后端也不做任何探测。
 *
 * 检测结果分四种状态（``App.liveHealthState``）：
 *   idle    → 还没检测过
 *   loading → 正在检测（界面显示加载动画）
 *   ok      → 拿到了推流状态
 *   error   → 连续失败达到上限，界面显示「获取失败」，不再自动重试
 *
 * 关键：每次请求都是「服务端只读缓存、毫秒级返回」，探测本身在服务端后台做，
 * 所以这里永远不会把界面卡住。
 */
export const LIVE_HEALTH_MAX_FAILS = 3;
const LIVE_HEALTH_INTERVAL = 15000;   // 有人在播：勤一点，观众不必等太久才看到新机位
const LIVE_HEALTH_IDLE = 60000;       // 没人在播：慢一点，别一直敲媒体服务器
const LIVE_HEALTH_RETRY = 3000;       // 失败后 / 等待后台探测结果时的重试间隔

let healthRunning = false;
let healthTimer = null;
let healthInFlight = false;

/** 进入直播 / 频道页：开始检测（幂等；已排期或正在请求时不重复发）。 */
export function startLiveHealth() {
  healthRunning = true;
  if (healthTimer !== null || healthInFlight) return;
  log.debug('开始检测直播信号');
  void healthTick();
}

/** 离开直播 / 频道页：停止检测，也不再刷新「直播中」标记。 */
export function stopLiveHealth() {
  if (!healthRunning && healthTimer === null) return;
  healthRunning = false;
  clearTimeout(healthTimer);
  healthTimer = null;
  log.debug('停止检测直播信号');
}

/**
 * 用户显式要求重新检测（「刷新信号」按钮 / 失败后的「重试」）：
 * 清掉失败计数，让服务端现场探测一次，并恢复轮询。
 */
export async function probeLiveHealth() {
  App.liveHealthFails = 0;
  if (App.liveHealthState === 'error') App.liveHealthState = 'loading';
  healthRunning = true;
  clearTimeout(healthTimer);
  healthTimer = null;
  if (healthInFlight) return false; // 已有一次检测在跑，让它自己收尾即可
  return healthTick(true);
}

/** 当前有没有人在推流（选手机位 / 成员频道 / 主直播间三者任一）。 */
const anyoneStreaming = () =>
  App.liveMain === true ||
  (App.liveNow instanceof Set && App.liveNow.size > 0) ||
  (App.liveChannelsNow instanceof Set && App.liveChannelsNow.size > 0) ||
  (App.liveMembersNow instanceof Set && App.liveMembersNow.size > 0);

async function healthTick(probe = false) {
  const ok = await refreshLiveHealth({ probe });
  if (!healthRunning) return ok;
  // 连续失败到上限就停手：界面显示「获取失败，等待服务器修复」，等用户手动重试
  if (App.liveHealthState === 'error') {
    healthRunning = false;
    return ok;
  }
  // 没人在播时把间隔拉长（服务端探测很轻，但没必要一直敲媒体服务器）；
  // 服务端后台还在探端口（pending）时短间隔催一下，避免面板一直显示「未探测」。
  const pending = ok && App.liveHealth?.pending === true;
  const delay = !ok || pending ? LIVE_HEALTH_RETRY : anyoneStreaming() ? LIVE_HEALTH_INTERVAL : LIVE_HEALTH_IDLE;
  clearTimeout(healthTimer);
  healthTimer = setTimeout(() => {
    healthTimer = null;
    void healthTick();
  }, delay);
  return ok;
}

/**
 * 拉一次直播链路健康视图；返回本次是否成功。
 *
 * 服务端默认**只读缓存**（毫秒级返回，探测在服务端后台跑，不会挂住界面）；
 * 只有用户显式点「刷新信号」时才传 ``probe`` 让服务端现场重新探测一次。
 */
export async function refreshLiveHealth({ probe = false } = {}) {
  // 只读查看**另一届**（不是后端当前届）时不探测，也不显示任何「直播中」标记；
  // 在看当前届时正常探测（event 页是看比赛直播的主场，不能因为 routeEvent 有值就停）
  if (App.routeEvent && App.routeEvent !== App.eventId) return true;
  if (healthInFlight) return true;
  healthInFlight = true;
  const prevState = App.liveHealthState;
  // 首次检测显示加载动画（已有结果时不要闪一下，直接用旧数据渲染）
  if (App.liveHealthState === 'idle') {
    App.liveHealthState = 'loading';
    if (hooks.onLiveHealth) hooks.onLiveHealth(true);
  }
  let changed = false;
  let ok = false;
  // 集合是否与上一轮不同（与顺序无关）
  const setDiff = (prev, next) =>
    !(prev instanceof Set) || prev.size !== next.size || [...next].some((x) => !prev.has(x));
  try {
    App.liveHealth = await api(`/live/health${probe ? '?probe=1' : ''}`);
    // 「谁真的在推流」由媒体服务器上报；只有这里报出来的才显示「直播中」
    const next = new Set(Array.isArray(App.liveHealth.streaming) ? App.liveHealth.streaming : []);
    // 成员频道（日常直播）：同一次探测里也回报哪些频道在推流
    const nextChannels = new Set(
      Array.isArray(App.liveHealth.streamingChannels) ? App.liveHealth.streamingChannels : []
    );
    // 成员直播间：回报哪些成员（按 uid）在推流
    const nextMembers = new Set(
      Array.isArray(App.liveHealth.streamingMembers) ? App.liveHealth.streamingMembers : []
    );
    // 主直播间（默认流名）：探测不到就是 null（未知），此时一律不给这一路信号
    const main = App.liveHealth.streamingKnown ? Boolean(App.liveHealth.mainStreaming) : null;
    changed =
      main !== App.liveMain ||
      setDiff(App.liveNow, next) ||
      setDiff(App.liveChannelsNow, nextChannels) ||
      setDiff(App.liveMembersNow, nextMembers);
    App.liveNow = next;
    App.liveChannelsNow = nextChannels;
    App.liveMembersNow = nextMembers;
    App.liveMain = main;
    App.liveHealthFails = 0;
    App.liveHealthState = 'ok';
    ok = true;
    log.info('直播信号状态', App.liveHealth);
  } catch (err) {
    log.warn('直播信号探测失败', err);
    App.liveHealthFails += 1;
    // 连续失败到上限就停在 error：界面显示「获取失败，等待服务器修复」
    App.liveHealthState = App.liveHealthFails >= LIVE_HEALTH_MAX_FAILS ? 'error' : 'loading';
    // 拿不到数据就不要挂「直播中」标记（宁可少显示，也不给假的）
    App.liveHealth = {
      ok: false,
      streamingKnown: false,
      reason: err.message,
      streaming: [],
      streamingChannels: [],
    };
    changed =
      App.liveMain !== null ||
      (App.liveNow instanceof Set && App.liveNow.size > 0) ||
      (App.liveChannelsNow instanceof Set && App.liveChannelsNow.size > 0);
    App.liveNow = new Set();
    App.liveChannelsNow = new Set();
    App.liveMain = null;
  } finally {
    healthInFlight = false;
  }
  // 检测状态本身变了（检测中 → 已获取 / 检测中 → 获取失败）也要重绘，提示才会跟着换；
  // 其余情况只有「谁在推流」真的变了才重绘，避免每次轮询都重建 DOM。
  if (prevState !== App.liveHealthState) changed = true;
  if (hooks.onLiveHealth) hooks.onLiveHealth(changed);
  return ok;
}

/** 在 #liveStage 上安装一次点击委托（舞台会被重建，故委托挂在稳定祖先上）。 */
export function installStageDelegation() {
  const stage = qs('#liveStage');
  if (!stage) return;
  stage.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    // 注意：舞台内的点击不会走全局委托（app.js 里对 #liveStage 直接 return），
    // 所以机位 / 比赛切换必须在这里处理，否则点了没反应。
    if (act === 'live-select') {
      const pid = btn.dataset.pid || null;
      Live.stoppedByUser = false; // 主动点某一路 = 想看，解除「别再自动播」的标记
      if (App.livePlayerId !== pid) {
        App.livePlayerId = pid;
        log.info('切换直播机位', pid);
        if (hooks.onLivePick) hooks.onLivePick();
      }
      return;
    }
    if (act === 'live-round') {
      const code = btn.dataset.code || '';
      if (App.liveRound !== code) {
        App.liveRound = code;
        log.info('切换直播比赛', code || '全部机位');
        if (hooks.onLivePick) hooks.onLivePick();
      }
      return;
    }
    if (act === 'live-play') {
      Live.stoppedByUser = false; // 用户点了「播放」：恢复自动开播的资格
      Live.playSelected(App.state);
    } else if (act === 'live-stop') Live.stop(true);
    else if (act === 'live-open') {
      const url = watchUrlOf(Live.room);
      if (url) window.open(url, '_blank', 'noopener');
    } else if (act === 'live-copy') {
      // 复制观看地址：跟随当前线路给 8888 或 8889 那一条
      const url =
        (App.liveProto === 'hls' ? Live.room?.hls : Live.room?.webrtc) ||
        Live.room?.webrtc ||
        App.liveInfo?.originWebrtc ||
        '';
      copyText(url).then((ok) =>
        toast(ok ? `已复制播放地址：${url}` : '复制失败', ok ? 'ok' : 'err', ok ? 6000 : 3600)
      );
    } else if (act === 'live-copy-push') {
      // 推流地址只在管理端私有数据里，用户端拿不到；推流只有 WHIP 一种
      if (!isAdmin()) {
        toast('推流地址仅管理员可见', 'warn');
        return;
      }
      const url = pushUrlOf(App.livePlayerId);
      if (!url) {
        toast('该机位还没有 WHIP 推流地址（在「选手」页编辑该选手填推流流名）', 'warn', 6000);
        return;
      }
      copyText(url).then((ok) => {
        if (!ok) return toast('复制失败', 'err');
        return toast(`WHIP 推流地址已复制 · ${PUSH_TIP_LINE}`, 'ok', 9000);
      });
    } else if (act === 'live-proto') {
      // 观众自行切换播放线路：再点一次同一线路 = 恢复默认（跟随服务端配置）
      const proto = btn.dataset.proto || '';
      App.liveProto = App.liveProto === proto ? '' : proto;
      try {
        localStorage.setItem(LIVE_PROTO_KEY, App.liveProto);
      } catch (err) {
        log.debug('线路偏好写入失败（忽略）', err);
      }
      document.querySelectorAll('[data-act="live-proto"]').forEach((item) => {
        item.classList.toggle('btn--primary', (item.dataset.proto || '') === App.liveProto);
      });
      toast(
        App.liveProto
          ? `已切换到 ${proto === 'hls' ? 'HLS（TCP，抗抖动）' : 'WebRTC（UDP，低延迟）'} 线路`
          : '已恢复默认线路',
        'info',
        4000
      );
      Live.playSelected(App.state);
    } else if (act === 'live-refresh') {
      // 用户显式要求刷新：让服务端现场重新探测（可能要等媒体服务器超时，但这是主动操作）
      probeLiveHealth().then(() => Live.playSelected(App.state));
    } else if (act === 'live-health-retry') {
      // 「获取失败」提示里的重试：清零失败计数并现场探一次
      probeLiveHealth().then(() => Live.playSelected(App.state));
    }
  });
}
