/* 导播台四宫格自检（纯 Node，无依赖）。
 *
 * 为什么需要它：多画面最容易坏在**看不见的地方**——
 * 一旦有人在 renderDirector 里顺手 ``innerHTML`` 重建了网格，四路就会一起重新协商；
 * 一旦把每格的静音写回全局偏好，观众回直播页会发现声音没了。
 * Python 测试跑不到浏览器侧的 RTCPeerConnection 与音量，所以这里用最小桩把
 * ``director.js`` 真跑一遍，钉住几条对外承诺：
 *
 * 1. 首次分配四路 → 建四条连接；
 * 2. **机位没变时重复 assign 不重连**（状态推送很频繁，重连会打断画面）；
 * 3. **只有变化的那一格重连**，其余三路不受影响；
 * 4. 离开页面（stopAll）必须把四条连接全关掉；
 * 5. 默认全静音，点「声音」只让那一格出声，**且不写回全站音量偏好**。
 *
 * 用法：``node tools/check_director.mjs``（提交前与 check_assets.py 一起跑）。
 */

import { installBrowserStub } from './_browser_stub.mjs';

const peers = [];
const fetches = [];
const failures = [];
const { el, setEl, makeEl } = installBrowserStub();

/** 带真实 classList 的格子：is-solo / is-audio 都靠它判状态。 */
function cellNode(index) {
  const classes = new Set();
  const node = makeEl(`dgCell${index}`);
  node.dataset = { cell: String(index) };
  node.classList = {
    add: (name) => classes.add(name),
    remove: (name) => classes.delete(name),
    toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
    contains: (name) => classes.has(name),
  };
  return node;
}

// 每格的元素组：stage 的 querySelector 直接查全局桩表（与 makePlayer 的取法一致）
for (let index = 0; index < 4; index += 1) {
  setEl(`#dgStage${index}`, {
    ...makeEl(`dgStage${index}`),
    querySelector: (sel) => el(sel),
  });
  setEl(`#dgVideo${index}`, makeEl(`dgVideo${index}`));
  setEl(`#dgCover${index}`, makeEl(`dgCover${index}`));
  setEl(`#dgState${index}`, makeEl(`dgState${index}`));
  setEl(`#dgName${index}`, makeEl(`dgName${index}`));
  setEl(`#dgCell${index}`, cellNode(index));
}
setEl('#directorBody', { ...makeEl('directorBody'), dataset: {} });
setEl('#toasts', {
  ...makeEl('toasts'),
  appendChild() {},
});

/** 假 canvas：playMock 要 getContext / captureStream，桩里没有 DOM。 */
function fakeCanvas() {
  const node = makeEl('canvas');
  node.width = 640;
  node.height = 360;
  node.getContext = () => ({
    createLinearGradient: () => ({ addColorStop() {} }),
    fillRect() {},
    fillText() {},
    set fillStyle(_v) {},
    set font(_v) {},
    set textAlign(_v) {},
  });
  node.captureStream = () => ({ getTracks: () => [{ stop() {} }] });
  return node;
}

const createElementOriginal = globalThis.document.createElement;
globalThis.document.createElement = (tag) =>
  String(tag).toLowerCase() === 'canvas' ? fakeCanvas() : createElementOriginal(tag);
// 桩里的 rAF 是 setTimeout：playMock 的循环会一直排下去，测试进程退不出来。
// 这里换成「只登记不执行」——首次 paint 已经同步画过一次了。
let rafSeq = 0;
globalThis.requestAnimationFrame = () => {
  rafSeq += 1;
  return rafSeq;
};
globalThis.cancelAnimationFrame = () => {};

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
  async setLocalDescription(desc) {
    this.localDescription = desc;
  }
  async setRemoteDescription() {}
  close() {
    this.closed = true;
    this.connectionState = 'closed';
  }
  addEventListener() {}
  removeEventListener() {}
  deliver() {
    this.connectionState = 'connected';
    this.ontrack?.({ streams: [{ getTracks: () => [{ stop() {} }] }] });
  }
}

globalThis.RTCPeerConnection = FakePC;
globalThis.fetch = async (url) => {
  fetches.push(url);
  return { ok: true, status: 200, text: async () => 'v=0\r\n' };
};

const { App } = await import('../static/js/core.js');
const Director = await import('../static/js/director.js');

/* ------------------------------ 断言与工具 ------------------------------ */

const openPcs = () => peers.filter((p) => !p.closed).length;
const tick = () => new Promise((resolve) => setTimeout(resolve, 40));
/**
 * 让所有在连的 pc 进入 connected 并送来画面 —— 真实浏览器里这一步由 ICE 完成，
 * 且一定发生在协商（POST /whep）之后。所以这里**先等协商走完**再 deliver，
 * 否则 ontrack 还没注册，deliver 等于没发生。
 */
const deliverAll = async () => {
  await tick();
  peers.filter((p) => !p.closed).forEach((p) => p.deliver());
  await tick();
};
const cam = (key, name) => ({
  id: key,
  name,
  room: { key, webrtc: `https://mtx/${key}`, hls: `https://mtx/${key}/index.m3u8` },
});

function check(name, cond, detail = '') {
  console.log(`  ${cond ? 'OK ' : '✗  '} ${name}${detail ? `（${detail}）` : ''}`);
  if (!cond) failures.push(name);
}

/* ------------------------------ 场景 ------------------------------ */

console.log('=== 首次分配四路 ===');
App.liveMuted = true; // 模拟「观众在直播页把声音关了」：导播台不该被这个偏好影响
Director.assign([cam('a', '甲'), cam('b', '乙'), cam('c', '丙'), cam('d', '丁')]);
await deliverAll();
check('建了四条连接', openPcs() === 4, `${openPcs()} 条`);
check('四格的机位名都写上了', el('#dgName0').textContent === '甲' && el('#dgName3').textContent === '丁');
check('四格默认全部静音', [0, 1, 2, 3].every((i) => el(`#dgVideo${i}`).muted === true));

console.log('');
console.log('=== 机位没变时重复 assign 不许重连 ===');
const sigBefore = fetches.length;
Director.assign([cam('a', '甲'), cam('b', '乙'), cam('c', '丙'), cam('d', '丁')]);
await tick();
check('没有多发信令', fetches.length === sigBefore, `多发了 ${fetches.length - sigBefore} 个`);
check('连接数没变', openPcs() === 4, `${openPcs()} 条`);

console.log('');
console.log('=== 只换一路：只有那一格重连 ===');
const beforeSwap = fetches.length;
Director.assign([cam('a', '甲'), cam('b', '乙'), cam('x', '戊'), cam('d', '丁')]);
await deliverAll();
const swapped = fetches.length - beforeSwap;
check('只发了一次信令', swapped === 1, `发了 ${swapped} 个`);
check('被换掉那一路的新地址是对的', String(fetches[fetches.length - 1]).includes('/x/whep'));
check('第 3 格的机位名跟着换了', el('#dgName2').textContent === '戊');
check('连接总数仍是四', openPcs() === 4, `${openPcs()} 条`);

console.log('');
console.log('=== 空出来的格子要收摊 ===');
Director.assign([cam('a', '甲'), cam('b', '乙')]);
await deliverAll();
check('后两格的连接被关掉', openPcs() === 2, `${openPcs()} 条`);
check('空格子显示「空闲」封面', el('#dgCover2').innerHTML.includes('空闲'));

console.log('');
console.log('=== 主音：只开一路，且不写回全站偏好 ===');
Director.setAudio(1);
await tick();
check('只有第 2 格出声', el('#dgVideo1').muted === false);
check('其余仍静音', el('#dgVideo0').muted === true && el('#dgVideo3').muted === true);
check('全站音量偏好没被改写', App.liveMuted === true);
check('主音格被点亮', el('#dgCell1').classList.contains('is-audio'));
Director.setAudio(1);
check('再点一次恢复全静音', [0, 1, 2, 3].every((i) => el(`#dgVideo${i}`).muted === true));

console.log('');
console.log('=== 离开页面：四条连接全部关掉 ===');
Director.stopAll();
await tick();
check('没有留下没关的连接', openPcs() === 0, `${openPcs()} 条`);

console.log('');
console.log('=== 调试模拟机位：不发信令，画面本地生成 ===');
const sigBeforeMock = fetches.length;
Director.assign([
  { id: 'm1', name: '模拟一', room: { key: 'mock-1', mock: true } },
  { id: 'm2', name: '模拟二', room: { key: 'mock-2', mock: true } },
]);
await tick();
check('没有为模拟机位发信令', fetches.length === sigBeforeMock, `多发了 ${fetches.length - sigBeforeMock} 个`);
check('两格都拿到了本地画面', Boolean(el('#dgVideo0').srcObject) && Boolean(el('#dgVideo1').srcObject));
check('第三格仍是空闲', !el('#dgVideo2').srcObject);
Director.stopAll();
await tick();
check('模拟画面随 stopAll 一起收掉', !el('#dgVideo0').srcObject);

console.log('');
if (failures.length) {
  console.error(`导播台自检失败 ${failures.length} 项：`);
  failures.forEach((name) => console.error(`  - ${name}`));
  process.exit(1);
}
console.log('导播台自检全部通过');
