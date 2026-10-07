/* 直播播放器状态机自检（纯 Node，无依赖）。
 *
 * 为什么需要它：``static/js/live.js`` 里那套「连上 → 换台 → 舞台重建 → 重连」的状态
 * 机是全站最容易被无声改坏的一处，而 Python 测试跑不到浏览器侧的 RTCPeerConnection 与
 * 自动播放策略。这里用最小桩把播放器真跑一遍，把几条**对外承诺**钉住：
 *
 * 1. **换了台 / 舞台重建之后必须重连**：``views.js`` 每次重建舞台都会换掉 ``<video>``，
 *    老 pc 还连着那个已经从文档里摘掉的旧元素——只看 ``pc.connectionState`` 会以为
 *    「还在播」，于是换台、点播放全都没反应，封面一直挂着（用户报的「播一会儿之后
 *    不能换台」就是这个）；
 * 2. 画面正常时重复 ``playRoom`` **不许重连**（重连会打断画面）；
 * 3. 失败必须收拾干净：不能留下没关的 pc（否则换几次台就在媒体服务器上堆一堆会话）；
 * 4. **默认不静音**，而且舞台重建之后还得是不静音（新 ``<video>`` 是干净的）；
 * 5. 浏览器拦住带声音的自动播放时，退回静音起播并提示怎么开声音（而不是黑屏）；
 * 6. HLS 线路同样要认出「画面落在哪个元素上」并按音量偏好起播；
 * 7. **换台 / 换人之后老 ``<video>`` 必须被释放**：舞台重建会把元素换掉，而媒体元素离开
 *    DOM 不会自己停——没人收，它就带着上一路的音频继续响（听起来是两路声音叠在一起）；
 * 8. **探测失败不许把「谁在播」清空**：取不到状态 ≠ 没人播。清空会让整站瞬间变成「谁都没
 *    开播」——换台、点别的直播间全都点不动（判定「在不在播」用的就是那几个集合），而且
 *    这不是「少显示」，是**错显示**；探测恢复后还要能自动回到 ok。
 *
 * 用法：``node tools/check_live_player.mjs``（提交前与 check_assets.py 一起跑）。
 */

import { installBrowserStub } from './_browser_stub.mjs';

/* ------------------------------ 浏览器桩 ------------------------------ */

const peers = [];
const fetches = [];
const toasts = [];
const failures = [];

const { el, setEl, makeEl } = installBrowserStub();

function rebuildStage() {
  // 等价于 views.js 的 stage.innerHTML = stageHtml(...)：元素被换掉，pc 还连着老元素
  setEl('#liveVideo', makeEl('liveVideo'));
  setEl('#liveCover', makeEl('liveCover'));
  setEl('#liveState', makeEl('liveState'));
}

rebuildStage();
// 提示盒：toast() 会往 #toasts 里塞一个 div，这里把文本记下来好断言
setEl('#toasts', {
  ...makeEl('toasts'),
  appendChild(child) {
    toasts.push(child.textContent || '');
  },
});
setEl('#liveStage', {
  id: 'liveStage',
  innerHTML: '',
  dataset: {},
  addEventListener() {},
  querySelector: (sel) => el(sel),
  querySelectorAll: () => [],
});

class FakePC {
  constructor() {
    this.connectionState = 'new';
    this.iceGatheringState = 'complete';
    this.closed = false;
    peers.push(this);
  }
  addTransceiver() {}
  getReceivers() {
    return [];
  }
  async createOffer() {
    return { type: 'offer', sdp: 'v=0' };
  }
  async setLocalDescription(d) {
    this.localDescription = d;
  }
  async setRemoteDescription() {}
  close() {
    this.closed = true;
    this.connectionState = 'closed';
  }
  addEventListener() {}
  removeEventListener() {}
  /** 桩：让测试手动表示「画面到了」 */
  deliver() {
    this.connectionState = 'connected';
    this.ontrack &&
      this.ontrack({ streams: [{ getTracks: () => [{ stop() {} }] }] });
  }
}

globalThis.RTCPeerConnection = FakePC;
const okSignaling = () => {
  globalThis.fetch = async (url) => {
    fetches.push(url);
    return { ok: true, status: 200, text: async () => 'v=0\r\n' };
  };
};
okSignaling();

const { App } = await import('../static/js/core.js');
const { Live, applyLiveHealth, refreshLiveHealth } = await import('../static/js/live.js');
const { isChannelLive, isMemberLive } = await import('../static/js/ui.js');

/* ------------------------------ 断言与工具 ------------------------------ */

const video = () => document.querySelector('#liveVideo');
const cover = () => document.querySelector('#liveCover');
const openPcs = () => peers.filter((p) => !p.closed).length;
const tick = () => new Promise((r) => setTimeout(r, 20)); // 等 play()/ontrack 的后续微任务

function check(name, cond, detail = '') {
  console.log(`  ${cond ? 'OK ' : '✗  '} ${name}${detail ? `（${detail}）` : ''}`);
  if (!cond) failures.push(name);
}

async function fresh() {
  rebuildStage();
  peers.length = 0;
  fetches.length = 0;
  Live.playing = { key: '', mode: '' };
  Live.attached = null;
  Live.pc = null;
  Live.pending = '';
  Live.stoppedByUser = false;
  okSignaling();
}

async function play(room) {
  await Live.playRoom(room);
  await tick();
}

const roomA = { key: 'alice', webrtc: 'https://mtx/alice', hls: 'https://mtx/alice/index.m3u8' };
const roomB = { key: 'bob', webrtc: 'https://mtx/bob', hls: 'https://mtx/bob/index.m3u8' };

/* ------------------------------ 场景 ------------------------------ */

console.log('=== 舞台重建之后必须重连（换台 / 重绘都不该让画面卡死）===');
await fresh();
await play(roomA);
const first = peers.filter((p) => !p.closed);
first.forEach((p) => p.deliver());
await tick();
check('播 A 成功（画面落在当前 <video> 上）', Boolean(video().srcObject));
const pcsBefore = openPcs();

rebuildStage(); // 换台 / 重绘都会重建舞台
await play(roomA);
const rebuiltPcs = peers.filter((p) => !p.closed && !first.includes(p));
check('重建之后重新协商了（不是拿老连接糊弄）', rebuiltPcs.length > 0);
check('重建之后封面被揭开', cover().hidden === true);
rebuiltPcs.forEach((p) => p.deliver());
await tick();
check('重建之后画面真的回来了', Boolean(video().srcObject));
check('没有留下多余的老连接', openPcs() === pcsBefore);

console.log('');
console.log('=== 画面正常时重复调用不许重连（重连会打断画面）===');
const sigBefore = fetches.length;
await play(roomA);
await play(roomA);
check('没有多发信令请求', fetches.length === sigBefore, `多发了 ${fetches.length - sigBefore} 个`);
check('连接数没变', openPcs() === pcsBefore);

console.log('');
console.log('=== 换到另一路 ===');
await play(roomB);
peers.filter((p) => !p.closed).forEach((p) => p.deliver());
await tick();
check('换台后画面在（新的一路）', Boolean(video().srcObject));
check('换台后连接只有一个', openPcs() === 1, `${openPcs()} 个`);

console.log('');
console.log('=== 换台 / 换人：老 <video> 必须被彻底释放（否则上一路的音频还在响）===');
await fresh();
await play(roomA);
peers.filter((p) => !p.closed).forEach((p) => p.deliver());
await tick();
const oldVideo = video();
const stoppedTracks = [];
// 盯住上一路音频的轨道：老元素被释放时必须把它们停掉。
// 顺便挂个 src（HLS / MSE 那条路就是这么挂上去的），验证它也被清掉。
oldVideo.srcObject = { getTracks: () => [{ stop: () => stoppedTracks.push(1) }] };
oldVideo.src = 'blob:old-route';
oldVideo.paused = false; // 假装它正在播

rebuildStage(); // 舞台重建：元素被换掉，但老元素身上还挂着上一路的流
await play(roomB);
peers.filter((p) => !p.closed).forEach((p) => p.deliver());
await tick();
check('老元素被暂停（离开 DOM 不会自己停）', oldVideo.paused === true);
check('老元素上那一路的轨道被停掉', stoppedTracks.length > 0);
check('老元素的 srcObject 被清空', !oldVideo.srcObject);
check('老元素的 src 被清掉（HLS 缓冲不会再播完）', oldVideo.src === '');
check('画面落在新元素上', video() !== oldVideo && Boolean(video().srcObject));

console.log('');
console.log('=== 默认不静音，且重建之后仍然不静音 ===');
check('正常播时没被静音', video().muted === false);
rebuildStage();
await play(roomB);
peers.filter((p) => !p.closed).forEach((p) => p.deliver());
await tick();
check('舞台重建之后仍然没被静音', video().muted === false);

console.log('');
console.log('=== 浏览器拦住带声音的自动播放：退回静音起播 + 提示开声音 ===');
await fresh();
const blocked = video();
let attempts = 0;
blocked.play = () => {
  attempts += 1;
  if (attempts === 1) {
    const err = new Error('blocked');
    err.name = 'NotAllowedError';
    return Promise.reject(err);
  }
  blocked.paused = false;
  return Promise.resolve();
};
await play(roomA);
peers.filter((p) => !p.closed).forEach((p) => p.deliver());
await tick();
check('被拦后重试了一次（不是黑屏）', attempts === 2, `尝试 ${attempts} 次`);
check('画面照常有（先静音起播）', Boolean(video().srcObject));
check('提示了怎么开声音', toasts.some((t) => t.includes('音量图标')));

console.log('');
console.log('=== HLS 线路（hls.js）也要认出画面落点并按音量偏好起播 ===');
await fresh();
App.liveMuted = false;
App.liveProto = 'hls';
video().canPlayType = () => '';
globalThis.window.Hls = class {
  static isSupported() {
    return true;
  }
  static Events = { ERROR: 'hlsError' };
  on() {}
  loadSource(url) {
    this.url = url;
  }
  attachMedia(el) {
    el.src = this.url;
  }
  destroy() {}
};
await play(roomA);
check('HLS 地址已交给播放器', Boolean(video().src));
check('HLS 也认出「画面在这个元素上」', Live.attached === video());
check('HLS 也没被静音', video().muted === false);
App.liveProto = '';
globalThis.window.Hls = undefined;

console.log('');
console.log('=== 失败必须收拾干净（不留下没关的连接）===');
await fresh();
App.liveProto = 'webrtc'; // 手动指定线路：失败就失败，不再自动退 HLS（那条路要等 CDN）
globalThis.fetch = async () => ({
  ok: false,
  status: 404,
  text: async () => 'no stream is available',
});
await play(roomA);
check('没有残留未关闭的连接', openPcs() === 0, `${openPcs()} 个`);
check('给了明确的失败封面', cover().hidden === false && cover().innerHTML.includes('stage-cover'));
check('失败后可以重来（playing 已清空）', Live.playing.key === '');
App.liveProto = '';

console.log('');
console.log('=== 探测失败不许把「谁在播」清空（取不到 ≠ 没人播）===');
await fresh();
// 先来一份「有人在播」的服务端视图（有一个频道 c01、一个成员 u1）
applyLiveHealth({
  streamingKnown: true,
  streaming: ['p1'],
  streamingChannels: ['c01'],
  streamingMembers: ['u1'],
  mainStreaming: true,
  bili: { known: true, items: [] },
});
check('先有一份已知的「谁在播」', isChannelLive('c01') && isMemberLive('u1'));

// 探测连着失败：媒体服务器不可达 / 服务正在重启
globalThis.fetch = async () => {
  throw new Error('网络不可达');
};
for (let i = 0; i < 3; i += 1) await refreshLiveHealth();
check('失败之后仍然知道谁在播（别整站变「谁都没开播」）', isChannelLive('c01') && isMemberLive('u1'));
check(
  '状态标成「取不到」，界面文案才不会骗人',
  App.liveHealthState === 'error' && App.liveHealth.streamingKnown === false
);

// 探测恢复：不必刷新页面，下一次成功就自己好
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  text: async () =>
    JSON.stringify({
      streamingKnown: true,
      streamingChannels: ['c01'],
      streamingMembers: ['u1'],
    }),
});
await refreshLiveHealth();
check('探测一恢复就自动回到 ok', App.liveHealthState === 'ok' && isChannelLive('c01'));

console.log('');
if (failures.length) {
  console.log(`✗ 播放器自检未通过：${failures.join('；')}`);
  process.exit(1);
}
console.log('播放器自检全部通过');
