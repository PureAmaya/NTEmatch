/* 直播播放控制器：WebRTC(WHEP) 优先，HLS / 网页内嵌兜底。
 * 仅依赖核心层；对外暴露 Live 与事件委托安装函数。
 */

import { App, LIVE_PROTO_KEY, api, copyText, hooks, isAdmin, log, qs, toast } from './core.js';
import { PUSH_TIP_LINE, pushUrlOf, roomFor } from './ui.js';

/**
 * 内嵌观看地址：用媒体服务器自带的播放页 ``<base>/<标识>/``
 * （如 ``https://live.shiyora.net:8889/G-A-1-1-p3/``）。
 *
 * 地址就是源站地址，不做反代；站点是 HTTPS 时源站也必须是 HTTPS，
 * 否则浏览器会按混合内容拦掉（此时提示改用 WebRTC/HLS 线路）。
 */
function embedUrl(room) {
  // 观众选了 HLS 线路就给 HLS 播放页（地址到 /<流名>/ 为止），否则给 WebRTC 播放页
  if (App.liveProto === 'hls' && room?.hlsPage) return room.hlsPage;
  return room?.page || App.liveInfo?.originPlayPage || '';
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

export const Live = {
  pc: null,
  token: 0,
  room: null, // 当前机位的地址集合（key_endpoints 结果）
  // 正在播的「机位 + 线路」：只有两者都没变、且画面确实还活着时才跳过重连
  playing: { key: '', mode: '' },
  hlsInst: null, // hls.js 实例（非原生 HLS 的浏览器才有）

  el: () => qs('#liveVideo'),

  setState(text) {
    const el = qs('#liveState');
    if (el) el.textContent = text;
  },

  setCover(title, msg, action) {
    const cover = qs('#liveCover');
    const video = this.el();
    if (!cover) return;
    if (video) video.hidden = true;
    cover.hidden = false;
    cover.innerHTML =
      `<div class="stage-cover__noise"></div>` +
      `<div class="stage-cover__title">${title}</div>` +
      `<div class="stage-cover__msg">${msg}</div>` +
      (action
        ? `<button class="btn btn--sm btn--primary" type="button" data-cover-act="${action.act}">${action.label}</button>`
        : '');
  },

  hideCover() {
    const cover = qs('#liveCover');
    const video = this.el();
    if (cover) cover.hidden = true;
    // 有内嵌 iframe 时不要把空的 video 露出来（它会盖住 iframe 的下半张脸）
    if (video) video.hidden = Boolean(qs('#liveEmbed'));
  },

  embed(url) {
    const frame = qs('#stageFrame');
    if (!frame || !url) {
      this.setCover('无法内嵌', '缺少直播源地址，请检查管理端配置');
      return;
    }
    const old = qs('#liveEmbed');
    if (old) old.remove();
    const video = this.el();
    if (video) video.hidden = true;
    const el = document.createElement('iframe');
    el.id = 'liveEmbed';
    el.src = url;
    el.allow = 'autoplay; fullscreen; picture-in-picture';
    el.setAttribute('allowfullscreen', '');
    frame.appendChild(el);
    // 关键：必须把遮罩收掉，否则那层噪点/渐变会盖在 iframe 上——画面全被挡住
    this.hideCover();
    this.setState('网页内嵌');
    log.info('直播使用网页内嵌', url);
  },

  /** HLS：Safari 原生直放；其它浏览器临时取 hls.js（失败再退到内嵌播放页）。 */
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
        this.setCover('HLS 播放失败', `${data.details || data.type}；可改用网页内嵌`, {
          act: 'embed',
          label: '改用网页播放',
        });
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
      this.setCover('当前浏览器不支持 HLS', `${err.message || err}；可改用网页内嵌`, {
        act: 'embed',
        label: '改用网页播放',
      });
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

  async whep(url) {
    if (!url) throw new Error('缺少 WHEP 信令地址');
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

    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp' },
      body: pc.localDescription.sdp,
    });
    if (!res.ok) throw new Error(`信令失败 HTTP ${res.status}`);
    await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() });
    this.setState('WebRTC 播放中');
    log.info('直播使用 WebRTC(WHEP)', url);
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
      Boolean(qs('#liveEmbed')) ||
      this.pc?.connectionState === 'connected' ||
      Boolean(video && video.src && !video.paused);
    if (target && alive && this.playing.key === target.key && this.playing.mode === mode) {
      if (video && video.paused) video.play().catch((err) => log.warn('恢复播放失败', err));
      return;
    }
    await this.stop(false);
    this.room = target;
    if (!this.room) {
      this.setCover('请选择正在直播的选手', '在上方机位列表中选择一位选手，即可自动切换到他的直播间');
      this.setState('未选择');
      return;
    }
    if (st.enabled === false) {
      this.setCover('直播已关闭', '管理员可在后台开启直播功能');
      this.setState('已关闭');
      return;
    }
    this.setState(mode === 'hls' ? '切换到 HLS…' : '连接中…');
    try {
      // 源站地址直连：whep = WebRTC 拉流，hls = HLS 兜底（用播放列表 index.m3u8）
      if (mode === 'embed' || mode === 'flv') this.embed(embedUrl(this.room));
      else if (mode === 'hls') await this.hls(this.room.hls);
      else {
        try {
          await this.whep(this.room.whep);
        } catch (err) {
          // 自动模式：WebRTC 不通就先退到 HLS（TCP，抗抖动），再不行才用内嵌页
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
        this.setCover('直播连接失败', `${err.message || err}；可改用网页播放`, {
          act: 'embed',
          label: '网页播放',
        });
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
    const embedEl = qs('#liveEmbed');
    if (embedEl) embedEl.remove();

    if (manual) {
      this.setState('已停止');
      this.setCover('已停止播放', '点击「播放」重新连接直播信号');
      log.info('直播已手动停止');
    }
  },
};

export async function refreshLiveHealth() {
  // 按路由回看往届：不探测、也不显示任何「直播中」标记
  if (App.routeEvent) return;
  let changed = false;
  try {
    App.liveHealth = await api('/live/health');
    // 「谁真的在推流」由媒体服务器上报；只有这里报出来的才显示「直播中」
    const next = new Set(Array.isArray(App.liveHealth.streaming) ? App.liveHealth.streaming : []);
    changed =
      !(App.liveNow instanceof Set) ||
      next.size !== App.liveNow.size ||
      [...next].some((pid) => !App.liveNow.has(pid));
    App.liveNow = next;
    log.info('直播信号状态', App.liveHealth);
  } catch (err) {
    log.warn('直播信号探测失败', err);
    App.liveHealth = { ok: false, reason: err.message, streaming: [] };
    changed = App.liveNow instanceof Set && App.liveNow.size > 0;
    App.liveNow = new Set();
  }
  // 只有「谁在推流」真的变了才重绘视图，避免每次轮询都重建 DOM
  if (hooks.onLiveHealth) hooks.onLiveHealth(changed);
}

/** 在 #liveStage 上安装一次点击委托（舞台会被重建，故委托挂在稳定祖先上）。 */
export function installStageDelegation() {
  const stage = qs('#liveStage');
  if (!stage) return;
  stage.addEventListener('click', (e) => {
    const cover = e.target.closest('[data-cover-act]');
    if (cover) {
      if (cover.dataset.coverAct === 'embed') {
        Live.embed(embedUrl(Live.room));
      }
      return;
    }
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    // 注意：舞台内的点击不会走全局委托（app.js 里对 #liveStage 直接 return），
    // 所以机位 / 比赛切换必须在这里处理，否则点了没反应。
    if (act === 'live-select') {
      const pid = btn.dataset.pid || null;
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
    if (act === 'live-play') Live.playSelected(App.state);
    else if (act === 'live-stop') Live.stop(true);
    else if (act === 'live-open') {
      const url = embedUrl(Live.room);
      if (url) window.open(url, '_blank', 'noopener');
    } else if (act === 'live-copy') {
      // 复制哪条播放地址跟随当前线路：HLS 给播放页（…/<流名>/），WebRTC 给 WHEP
      const url =
        (App.liveProto === 'hls' ? Live.room?.hlsPage : Live.room?.whep) ||
        Live.room?.whep ||
        App.liveInfo?.originWhep ||
        '';
      copyText(url).then((ok) =>
        toast(ok ? `已复制播放地址：${url}` : '复制失败', ok ? 'ok' : 'err', ok ? 6000 : 3600)
      );
    } else if (act === 'live-copy-push') {
      // 推流地址只在管理端私有数据里，用户端拿不到；
      // 复制哪一套跟随当前播放线路：WebRTC 线路配 WHIP，HLS（TCP）线路配 RTMP。
      if (!isAdmin()) {
        toast('推流地址仅管理员可见', 'warn');
        return;
      }
      // 推流优先 WHIP：默认复制 WHIP，只有观众切到 HLS 线路时才给 TCP 套的 RTMP
      const proto = App.liveProto === 'hls' ? 'rtmp' : 'whip';
      const url = pushUrlOf(App.livePlayerId, proto);
      if (!url) {
        toast(`该选手还没有 ${proto.toUpperCase()} 推流地址（在「选手」页编辑该选手填推流流名）`, 'warn', 6000);
        return;
      }
      copyText(url).then((ok) => {
        if (!ok) return toast('复制失败', 'err');
        return toast(`${proto.toUpperCase()} 推流地址已复制 · ${PUSH_TIP_LINE}`, 'ok', 9000);
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
      refreshLiveHealth().then(() => Live.playSelected(App.state));
    }
  });
}
