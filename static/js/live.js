/* 直播播放控制器：观看线路两条——8889（WebRTC）优先，8888（HLS）兜底。
 * 仅依赖核心层；对外暴露 Live 与事件委托安装函数。
 */

import {
  App,
  LIVE_MUTE_KEY,
  LIVE_PROTO_KEY,
  api,
  copyText,
  esc,
  hooks,
  isAdmin,
  log,
  qs,
  toast,
} from './core.js';
import { PUSH_TIP_LINE, pushUrlOf, roomFor } from './ui.js';

/**
 * 信令超时（毫秒）：媒体服务器半死不活时**不能无限等**。
 *
 * 没有这个超时，一次卡住的 WHEP 请求会让「连接中…」永远停在那里，
 * 观众看到的是「点哪个台都没反应」——这正是换台看起来坏掉的样子。
 */
const SIGNAL_TIMEOUT = 8000;
/** hls.js 的 CDN 加载超时（毫秒）：CDN 被墙 / 抽风时不能让播放卡在这儿。 */
const HLS_LOAD_TIMEOUT = 8000;

/**
 * 「正在连」超过这么久就当作那次尝试已经僵住，允许重新发起（见 ``playRoom``）。
 *
 * 比信令超时（8 秒）宽出不少：正常的慢连接不该被误判成僵死，但真僵住时也不能把某一路
 * 永久锁在「正在连」上——那表现出来就是「点它没反应」。
 */
const PENDING_STALE_MS = 15000;

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
    // CDN 拉不动（被墙 / 超时）时必须**失败**而不是永远挂着：否则播放器一直停在
    // 「HLS 加载中…」，换台点谁都没反应（而且每次都会再挂一个未完成的加载）。
    const timer = setTimeout(() => {
      script.remove?.();
      reject(new Error('hls.js 加载超时（CDN 不可达）'));
    }, HLS_LOAD_TIMEOUT);
    const done = (fn, arg) => {
      clearTimeout(timer);
      fn(arg);
    };
    script.onload = () =>
      done(window.Hls ? resolve : reject, window.Hls || new Error('hls.js 未就绪'));
    script.onerror = () => done(reject, new Error('hls.js 加载失败（网络不可达）'));
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
    // 画面**到底连在哪个 <video> 上**：舞台一重建（换台、切线路、来一路新直播）元素就
    // 被换掉了，而老 pc 还连着那个已经从文档里摘掉的旧元素。只看 pc.connectionState
    // 会以为「还在播」，于是换台 / 再点播放都不再重连，观众看到封面一直挂着
    // ——「播一会儿之后换台没反应」的根子就在这里。
    attached: null,
    // 正在连的目标（``机位key|线路``）：同一个目标正在连时别重复发信令（重绘很频繁）
    pending: '',
    //: 上面那次「正在连」是什么时候开始的（**毫秒时间戳**）：太久没结果就重来，见 PENDING_STALE_MS
    pendingAt: 0,
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

  /**
   * 把用户上一次的音量选择套到这个 ``<video>`` 上。
   *
   * **默认不静音**（要听得到声音）：舞台重建会换一个新的 ``<video>``，不重新套一遍
   * 就等于把观众刚调好的音量（以及「别静音」这个默认）丢掉。
   */
  applyAudio(video) {
    if (!video) return;
    video.muted = Boolean(App.liveMuted);
    if (Number.isFinite(App.liveVolume) && App.liveVolume > 0) video.volume = App.liveVolume;
  },

  /** 记住观众在播放器上改的音量，并在流断掉时把「正在播」的标记清掉（好自动重连）。 */
  bindVideo(video) {
    if (!video || video.dataset.nteAudio === '1') return;
    video.dataset.nteAudio = '1';
    video.addEventListener('volumechange', () => {
      App.liveMuted = video.muted;
      App.liveVolume = video.volume;
      try {
        localStorage.setItem(LIVE_MUTE_KEY, video.muted ? '1' : '0');
      } catch (err) {
        log.debug('音量偏好写入失败（忽略）', err);
      }
    });
    // 流结束 / 元素被清空：当作「没在播」，下一次重绘（或观众再点一次）就会重连。
    // 不这么做的话，`playing` 会一直停在「正在播」，而画面其实是黑的。
    video.addEventListener('ended', () => this.markDead(video));
    video.addEventListener('emptied', () => this.markDead(video));
  },

  /**
   * 起播；被自动播放策略拦下时**退回静音再试一次**，并告诉观众怎么开声音。
   *
   * 不静音是默认值，但 Chrome 会拦「带声音的自动播放」（尤其是刚打开页面、还没点过
   * 这个站的时候）。这里不能就这么算了——否则观众看到的是「黑屏 + 什么都没发生」。
   */
  playSafely(video) {
    if (!video) return;
    video.play().catch((err) => {
      if (err && err.name === 'NotAllowedError' && !video.muted) {
        log.warn('带声音的自动播放被拦住，先静音起播', err);
        video.muted = true;
        App.liveMuted = true;
        try {
          localStorage.setItem(LIVE_MUTE_KEY, '1');
        } catch (e) {
          log.debug('音量偏好写入失败（忽略）', e);
        }
        video.play().catch((again) => log.warn('静音起播仍失败', again));
        toast('浏览器拦了带声音的自动播放：点播放器上的音量图标即可开声音', 'warn', 9000);
        return;
      }
      log.warn('自动播放被拦截', err);
    });
  },

  /** 这一路已经没画面了：清掉「正在播」的标记，让下一次重绘（或观众再点一次）能重连。 */
  markDead(video) {
    if (video && this.attached && this.attached !== video) return;
    this.attached = null;
    this.playing = { key: '', mode: '' };
  },

  /**
   * 把某个 ``<video>`` 从「正在播」里彻底摘出来：暂停、停轨道、清 ``src`` / ``srcObject``，
   * 再 ``load()`` 一次，把已经缓冲的内容也丢掉。
   *
   * **必须也能作用在「已经被从文档里摘掉」的旧元素上**：换台 / 换人 / 换线路时舞台会重建
   * （见 ``views.js`` 的 ``stage.innerHTML = channelStageHtml(…)``），老 ``<video>`` 当场
   * 被换成新的。而媒体元素**离开 DOM 不会自己停下来**——它身上的 ``srcObject`` / HLS 源
   * 照旧在响。所以清理必须点名做，认的就是「当初真正连上的那个元素」（``this.attached``）。
   */
  release(video) {
    if (!video) return;
    try {
      video.pause();
    } catch (err) {
      log.debug('暂停旧播放器异常（忽略）', err);
    }
    const stream = video.srcObject;
    if (stream && typeof stream.getTracks === 'function') {
      stream.getTracks().forEach((t) => t.stop());
      video.srcObject = null;
    }
    video.removeAttribute('src');
    try {
      video.load();
    } catch (err) {
      log.debug('重置旧播放器异常（忽略）', err);
    }
  },

  /**
   * 把某个 ``<video>`` 认作「当前画面的落点」：记下来、套音量、揭开封面、起播。
   *
   * 三处（原生 HLS / hls.js / WebRTC 的 ontrack）都走它，免得漏掉哪一步——漏一步的
   * 后果就是「有画面但没声音」或者「连上了还盖着封面」。
   */
  attach(video) {
    if (!video) return;
    // 换落点：上一个元素身上挂的流先收干净（正常路径上 stop() 已经清过，这里是道保险
    // ——舞台重建之后没人调 stop 时，也不会留下一路还在响的音频）。
    if (this.attached && this.attached !== video) this.release(this.attached);
    this.attached = video;
    this.bindVideo(video);
    this.applyAudio(video);
    this.hideCover();
    this.playSafely(video);
  },

  /** 这个元素上现在真有我们要的画面吗（没有就当「没在播」，交给下面重连）。 */
  isAlive(video) {
    if (!video) return false;
    // 元素必须还是当初连上的那一个：换了元素 = 舞台重建过，老连接对观众没有意义
    if (this.attached !== video) return false;
    if (this.pc) return this.pc.connectionState === 'connected' && Boolean(video.srcObject);
    if (this.hlsInst || video.src) return Boolean(video.src);
    return Boolean(video.srcObject);
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
      video.src = url;
      this.attach(video);
      this.setState('HLS 播放中（原生）');
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
        // 这一路真没了：清掉「正在播」标记，否则下一次重绘会以为还在播、不再重连
        this.markDead(video);
        this.setCover(
          'HLS 播放失败',
          `${data.details || data.type}；可在播放器上方把线路切到 WebRTC 再试`
        );
        this.setState('HLS 失败');
      });
      inst.loadSource(url);
      inst.attachMedia(video);
      this.attach(video);
      this.setState('HLS 播放中');
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
    if (!this.el()) throw new Error('播放器尚未就绪');
    this.hideCover();
    this.setState('WebRTC 协商中…');

    const pc = new RTCPeerConnection({ iceServers: [] });
    this.pc = pc;
    const myToken = ++this.token;
    // 失败一律把这条 pc 收干净再抛：留着半截连接会让下一次连接/换台越来越难，
    // 也会在媒体服务器上堆出一堆没人看的会话。
    const fail = (err) => {
      if (this.pc === pc) this.pc = null;
      try {
        pc.close();
      } catch (e) {
        log.debug('关闭 PeerConnection 异常', e);
      }
      throw err;
    };

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
      // 用**当前**元素而不是协商开始时抓的那个：协商期间舞台可能被重建过
      // （换台 / 切线路 / 来了一路新直播），画面必须落到观众正看着的那个 <video> 上。
      const live = this.el();
      if (!live || !ev.streams || !ev.streams[0]) return;
      live.srcObject = ev.streams[0];
      this.attach(live);
      this.setState('WebRTC 播放中');
    };
    pc.onconnectionstatechange = () => {
      if (myToken !== this.token) return;
      const st = pc.connectionState;
      log.debug('WebRTC 连接状态', st);
      if (st === 'connected') this.setState('WebRTC 已连接');
      else if (st === 'failed' || st === 'closed') {
        this.setState('WebRTC 连接失败');
        this.markDead();
      } else if (st === 'disconnected') this.setState('WebRTC 已断开');
    };

    let offer = null;
    try {
      offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitIceComplete(pc);
    } catch (err) {
      return fail(err);
    }
    if (myToken !== this.token) return fail(new Error('已取消'));

    // 信令端点：MediaMTX 的**读流端点是 `<路径>/whep`**（它自带的播放页发的也是这个），
    // 而裸路径 `<路径>` 是那张**播放页**（只认 GET）——对它 POST 只会拿到 Go 路由的
    // 「404 page not found」，跟随便编个路径完全一样（实测过，见 README）。
    // 所以先发 /whep；只有 404/405（这个端点不存在）才退回裸路径，兼容只认裸路径的老版本。
    const endpoints = url.endsWith('/whep') ? [url] : [`${url}/whep`, url];
    let res = null;
    const bodies = []; // 各端点的应答体：两种 404 意思不同，报错时要认出来（见 signalError）
    for (const endpoint of endpoints) {
      // 每个端点各自计时：媒体服务器半死不活时**必须超时失败**，
      // 不能永远停在「协商中…」——那就是观众眼里的「换台点了没反应」。
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), SIGNAL_TIMEOUT);
      try {
        res = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: pc.localDescription.sdp,
          signal: ctrl.signal,
        });
      } catch (err) {
        if (myToken !== this.token) return fail(new Error('已取消'));
        if (err && err.name === 'AbortError') {
          return fail(
            new Error(`信令超时（媒体服务器 ${SIGNAL_TIMEOUT / 1000} 秒没应答）：可改用 HLS 线路再试`)
          );
        }
        return fail(new Error(`连不上媒体服务器：${err.message || err}`));
      } finally {
        clearTimeout(timer);
      }
      if (res.ok) {
        url = endpoint;
        break;
      }
      // 只有「这个端点不存在」才换下一个；其它状态码按它本身报错
      if (res.status !== 404 && res.status !== 405) break;
      bodies.push(await res.text().catch(() => ''));
      log.warn('信令端点在媒体服务器上不存在，换下一个', endpoint, res.status);
    }
    if (!res.ok) return fail(new Error(signalError(res.status, bodies)));
    try {
      await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() });
    } catch (err) {
      return fail(err);
    }
    // 到这里只是「协商成功」，画面要等 ontrack 才算真的到了（那时才写「播放中」）
    this.setState('WebRTC 已连接，等画面…');
    log.info('直播使用 WebRTC', url);
  },

  /** 播放一个机位；room 为 key_endpoints 结果（B站 机位是 ``{bili: true, embed, jump}``）。 */
  async playRoom(room) {
    const target = room && room.key ? room : null;
    if (target?.bili) {
      // B站 直播：画面由舞台里的 <iframe> 直嵌 B站 官方播放器（见 views.stageHtml），
      // 这里**不连任何地址**，只维护状态并把 MediaMTX 那套停干净（免得切回普通机位时
      // 两路画面打架）。每次重绘都会调进来，所以同一路已经在播就直接返回。
      this.room = target;
      if (this.playing.key === target.key && this.playing.mode === 'bili') {
        this.setState('B站直播中');
        return;
      }
      await this.stop(false);
      this.room = target;
      this.playing = { key: target.key, mode: 'bili' };
      this.setState('B站直播中');
      return;
    }
    const st = App.state?.stream || {};
    // 观众手动选的线路优先（WebRTC 延迟低但 UDP 怕抖动；HLS 走 TCP 更稳）
    const mode = App.liveProto || st.mode || 'auto';
    const video = this.el();
    // 同一机位 + 同一条线路、而且**画面确实还在眼前**：只把暂停恢复，不做重协商
    // （重连会把画面打断一下）。**换线路（WebRTC ↔ HLS）必须真的重连**，
    // 所以这里一定要比 mode——只比机位的话，点「HLS / WebRTC」会像没反应。
    //
    // 「还在眼前」由 isAlive 判：它要求画面连的**就是当前这个 <video>**。
    // 舞台重建（换台 / 切线路 / 刚来一路新直播）之后元素换了新的、老 pc 还连着
    // 早就被摘掉的旧元素——那种情况必须重连，否则封面一直挂着、点什么都没反应。
    if (target && this.isAlive(video) && this.playing.key === target.key && this.playing.mode === mode) {
      this.hideCover();
      this.applyAudio(video);
      if (video.paused) this.playSafely(video);
      return;
    }
    // 同一个目标正在连：别重复发信令（重绘很频繁，重复连会把慢连接反复掐掉）。
    // 换目标 / 换线路不算——那条路要立刻改道。
    const ticket = `${target?.key || ''}|${mode}`;
    // 同一路正在连就别重发信令（重绘很频繁，重复连会把慢连接反复掐掉）；
    // 但**连太久还没结果**说明那一次尝试已经僵住（信令超时也才 8 秒），这时候再点
    // 就该真的重连一次——否则这一路会被「正在连」永久锁住，怎么点都没反应。
    if (target && this.pending === ticket && Date.now() - this.pendingAt < PENDING_STALE_MS) return;
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
    this.setState(mode === 'hls' ? '切换到 HLS…' : '连接中…');
    this.pending = ticket;
    this.pendingAt = Date.now();
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
      // 记下「现在播的是哪个机位、哪条线路」，供下一次复用判断。
      // 这里只是记个意向：到底有没有真的接上，由 isAlive（画面落在哪个元素上）判——
      // 协商成功但画面还没到（或 hls 那边只给了个失败封面）时，下一次重绘会重连。
      this.playing = { key: this.room.key, mode };
    } catch (err) {
      // 失败必须收拾干净（pc / hls 实例 / 元素），否则残留会拖累后续的连接与换台
      await this.stop(false);
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
    } finally {
      if (this.pending === ticket) this.pending = '';
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
    this.pending = '';
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
    // **两个都要清**：当初真正连上的那个元素（舞台重建后就落到它头上了，它多半已经不在
    // 文档里）与当前 DOM 里那个。原来只清「当前 DOM 里那个」——换台换掉元素之后，老元素
    // 身上的流没人管，它就带着上一路的音频继续响（听起来就是两路声音叠在一起）。
    const current = this.el();
    this.release(this.attached);
    if (current && current !== this.attached) this.release(current);
    this.attached = null; // 画面落点也清掉：下一次 playRoom 必须真的重连
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
 * **服务端常驻探测，前端只管显示**（见后端 ``live.watch_loop``）。数据有两个来源，
 * 但都走**同一段逻辑**（:func:`applyLiveHealth`）：
 *
 * * **WebSocket 推送**（``{"type": "live"}``）：探测结果一变，服务端立刻推过来。
 *   这是「及时更新」的主要通道——开播 / 下播几乎当场可见，也不占额外请求；
 * * **HTTP 轮询**（``/api/live/health``）：兜底（推送断了、或刚打开页面）。
 *   服务端只读缓存、毫秒级返回，永远不会把界面卡住。
 *
 * 两边共用一段解析，就不会出现「推送说在播、轮询说没播」这种抖动。
 *
 * 检测结果分四种状态（``App.liveHealthState``）：
 *   idle    → 还没检测过
 *   loading → 正在检测（界面显示加载动画）
 *   ok      → 拿到了推流状态
 *   error   → 连续失败达到上限，界面显示「获取失败」，不再自动重试
 */
export const LIVE_HEALTH_MAX_FAILS = 3;
// 推送已经把「即时」这件事做到了，轮询只做兜底，所以间隔比从前放宽，
// 少一份无谓的请求（尤其是没人在播的时候）。
const LIVE_HEALTH_INTERVAL = 20000;   // 有人在播：轮询兜底，别和推送抢活
const LIVE_HEALTH_IDLE = 60000;       // 没人在播：更慢（服务端仍在常驻探测）
const LIVE_HEALTH_RETRY = 3000;       // 失败后 / 等待后台探测结果时的重试间隔
const LIVE_HEALTH_ERROR_RETRY = 30000; // 连续失败到上限后的**慢速重试**（不是停手，见 healthTick）

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
  // 连续失败到上限：不再短间隔猛敲，但**不能就此停手**。停手之后就只剩「状态变了才推」的
  // WebSocket 一条路——一旦它也没消息（手机锁屏 / 切网之后 socket 假死，`onclose` 不一定来），
  // 页面会永远停在旧数据上：「新开播的人一直不出现」「换台点了没反应」都是这么来的。
  // 慢速重试的成本可以忽略（本地几十字节的 HTTP），换来的是它会自己好。
  const errored = App.liveHealthState === 'error';
  // 没人在播时把间隔拉长（服务端探测很轻，但没必要一直敲媒体服务器）；
  // 服务端后台还在探端口（pending）时短间隔催一下，避免面板一直显示「未探测」。
  const pending = ok && App.liveHealth?.pending === true;
  const delay = errored
    ? LIVE_HEALTH_ERROR_RETRY
    : !ok || pending
      ? LIVE_HEALTH_RETRY
      : anyoneStreaming()
        ? LIVE_HEALTH_INTERVAL
        : LIVE_HEALTH_IDLE;
  clearTimeout(healthTimer);
  healthTimer = setTimeout(() => {
    healthTimer = null;
    void healthTick();
  }, delay);
  return ok;
}

/** 集合是否与上一轮不同（与顺序无关）。 */
function setDiff(prev, next) {
  return !(prev instanceof Set) || prev.size !== next.size || [...next].some((x) => !prev.has(x));
}

/**
 * 把一份直播健康视图（``/api/live/health`` 的响应，或 WebSocket 推来的同一份）
 * 应用到 ``App``；返回**有没有真的变化**。
 *
 * 判定的那几项与服务端的 ``live.live_fingerprint``（决定「要不要推」用的就是它）
 * 刻意对齐：主直播间、选手机位、成员频道、成员直播间、B站 机位。
 *
 * 「只有变了才重绘」是**保护正在播的画面**的关键：重绘由 ``hooks.onLiveHealth``
 * 落地，而它按视图签名决定要不要重建播放器元素（签名没变就只更新机位条与面板）。
 */
export function applyLiveHealth(data) {
  const before = App.liveHealthState;
  App.liveHealth = data || {};
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
  // B站 直播（另一条链路）：谁在播由服务端的 bili 视图给出（只读缓存）
  const nextBili = new Set(
    ((App.liveHealth.bili && App.liveHealth.bili.items) || []).map((item) => item.uid)
  );
  let changed =
    main !== App.liveMain ||
    setDiff(App.liveNow, next) ||
    setDiff(App.liveChannelsNow, nextChannels) ||
    setDiff(App.liveMembersNow, nextMembers) ||
    setDiff(App.liveBiliNow, nextBili);
  App.liveNow = next;
  App.liveChannelsNow = nextChannels;
  App.liveMembersNow = nextMembers;
  App.liveBiliNow = nextBili;
  App.liveMain = main;
  App.liveHealthFails = 0;
  App.liveHealthState = 'ok';
  // 「检测中 → 已获取」本身也要重绘，提示文案才会跟着换
  if (before !== 'ok') changed = true;
  log.info('直播信号状态', App.liveHealth);
  return changed;
}

/**
 * 拉一次直播链路健康视图；返回本次是否成功。
 *
 * 服务端**只读缓存**（毫秒级返回，常驻探测在服务端跑，不会挂住界面）；
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
  try {
    changed = applyLiveHealth(await api(`/live/health${probe ? '?probe=1' : ''}`));
    ok = true;
  } catch (err) {
    log.warn('直播信号探测失败', err);
    App.liveHealthFails += 1;
    App.liveHealthState = App.liveHealthFails >= LIVE_HEALTH_MAX_FAILS ? 'error' : 'loading';
    // **保留上一份「谁在播」，只把这份数据标成取不到。**
    // 「取不到」不等于「没人播」：原来把四个集合一起清空，会让整站瞬间变成「谁都没开播」
    // ——正看着的那一路画面还在（它不由这份数据驱动），但**换台、点别的直播间全都点不动**
    // （「在不在播」的判定用的就是这几个集合），而且这不是「少显示」，是**错显示**。
    // 探测一恢复、或收到一次推送就会自动纠正（见 healthTick 的慢速重试）。
    App.liveHealth = {
      ...(App.liveHealth || {}),
      ok: false,
      streamingKnown: false,
      reason: err.message,
    };
    // 只有「状态本身」变了才需要重绘（提示文案跟着换）；集合没动就不打扰正在播的画面
    changed = prevState !== App.liveHealthState;
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
      // B站 机位没有本站的观看地址：它的「源页」就是 B站 直播间
      const url = Live.room?.jump || watchUrlOf(Live.room);
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
