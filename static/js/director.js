/* 导播台：一个页面里同时看四路直播（2×2 网格）。
 *
 * 为什么不是「复用直播页的播放器再放三个 video」：
 *
 * 1. 现有舞台是**整体重建**的（views.js 的 stage.dataset.sig + innerHTML），
 *    四个格子照抄的话，任何一路开播 / 下播都会把所有 <video> 换掉，
 *    等于四路同时重新协商 WHEP —— 表现就是反复黑屏。所以**骨架只建一次**，
 *    之后只改每格挂的机位，元素永不重建。
 * 2. 播放器本身是「一格一路」的单槽位状态机，正好一对一：每格一个独立实例
 *    （元素 id 不同即可），不需要把它改造成连接池。
 * 3. 音量必须由这里管：四路同时出声是噪音，而且不能让「我静音了这一格」
 *    写回全站音量偏好（否则回直播页会发现声音没了），所以实例用
 *    manageAudio: false 创建，静音策略完全在下面。
 *
 * B站 机位不进网格：它是外站 <iframe>，缩在四分之一格里看不清，
 * 也没法和本站的音量 / 重连逻辑统一，留给直播页单看。
 */

import { App, LIVE_PROTO_KEY, log, qs, toast } from './core.js';
import { makePlayer, probeLiveHealth } from './live.js';

/** 格子数：2×2。再大就要考虑观众的解码能力（见 README 的导播台说明）。 */
const CELLS = 4;

/** 每格一组唯一元素 id —— makePlayer 就是按 id 找元素的，这正是它可复用的地方。 */
const cellIds = (index) => ({
  stage: `#dgStage${index}`,
  video: `#dgVideo${index}`,
  cover: `#dgCover${index}`,
  state: `#dgState${index}`,
});

const players = [];
/** 每格当前挂的机位（空 = 空闲），只用来判断「从有到无」时要不要收摊。 */
let assigned = [];
/** 上一次分配到的机位列表：切线路 / 重连时要按原样重放。 */
let lastCams = [];
/** 哪一格出声（-1 = 全静音）。默认全静音：四路一起响没有意义，也过不了自动播放策略。 */
let audioIndex = -1;

/** 骨架是否已经建好（建过就不再动 DOM 结构）。 */
const shellReady = () => Boolean(qs('#dgStage0'));

/** 建实例：必须在骨架进 DOM 之后，makePlayer 是按 id 找元素的。 */
function ensurePlayers() {
  if (players.length) return true;
  if (!shellReady()) return false;
  for (let index = 0; index < CELLS; index += 1) {
    const player = makePlayer(cellIds(index), { manageAudio: false });
    // 每次起播都校正一次静音：新连上的流不该因为「上一格是主音」而跟着出声。
    // 只挂 play 不够（改 muted 不会再触发 play），所以 setAudio 里还会主动改一遍。
    const video = qs(`#dgVideo${index}`);
    video?.addEventListener('play', () => {
      video.muted = index !== audioIndex;
    });
    players.push(player);
  }
  return true;
}

/** 每格顶部的机位名（用 textContent，绝不重建元素）。 */
function setName(index, text) {
  const el = qs(`#dgName${index}`);
  if (el) el.textContent = text || '';
}

/** 把静音策略落到当前所有 <video> 上，并同步按钮高亮。 */
function applyAudio() {
  for (let index = 0; index < CELLS; index += 1) {
    const video = players[index]?.el();
    if (video) {
      video.muted = index !== audioIndex;
      if (index === audioIndex) {
        // 点「声音」是一次用户手势，此时带声音起播不会被拦
        video.play().catch((err) => log.debug('主音起播被拦（忽略）', err));
      }
    }
    const cell = qs(`#dgCell${index}`);
    if (cell) cell.classList.toggle('is-audio', index === audioIndex);
  }
}

/** 单格重连：停干净再按原机位重连（stop 会清掉「正在播」标记）。 */
function reconnect(index) {
  const cam = lastCams[index];
  const player = players[index];
  if (!cam || !player) return;
  void player.stop(false).then(() => player.playRoom(cam.room));
  log.info('导播台重连', index, cam.room?.key || '');
}

/**
 * 按给定机位列表更新四格。
 *
 * **每次都调 playRoom**，而不是「key 变了才调」：它内部会判断画面是不是还活着
 * （isAlive），活着就只恢复播放、不重连；失败了才会重来。这正是我们要的
 * 「自动重试」，我们只需要在「这格从有机位变成没机位」时主动收摊。
 */
export function assign(cams = []) {
  if (!ensurePlayers()) return;
  lastCams = cams.slice(0, CELLS);
  for (let index = 0; index < CELLS; index += 1) {
    const cam = lastCams[index] || null;
    const player = players[index];
    if (cam) {
      setName(index, cam.name || cam.room?.key || '机位');
      void player.playRoom(cam.room);
      assigned[index] = cam.id || cam.room?.key || '';
      continue;
    }
    setName(index, '');
    if (!assigned[index]) continue; // 本来就是空的，别每轮都重设封面
    assigned[index] = '';
    void player.stop(false);
    player.setCover('空闲', '这一格还没有在播的机位');
    player.setState('空闲');
  }
}

/** 切线路后按原样重放一遍（mode 变了，playRoom 会真的重连）。 */
export function reconnectAll() {
  const cams = lastCams;
  assigned = [];
  assign(cams);
}

/** 离开页面时全部停掉：媒体元素离开 DOM 不会自己停，四路一起响 / 一起解码都是浪费。 */
export function stopAll() {
  for (const player of players) void player.stop(false);
  assigned = [];
  lastCams = [];
  audioIndex = -1;
}

/** 主音切换：再点一次同一格 = 恢复全静音。 */
export function setAudio(index) {
  if (!players[index]) return;
  audioIndex = audioIndex === index ? -1 : index;
  applyAudio();
  log.info('导播台主音', audioIndex < 0 ? '(全静音)' : String(index));
}

/** 放大 / 还原某一格（纯样式，不动播放器）。 */
export function setFocus(index) {
  const grid = qs('#directorGrid');
  const cell = qs(`#dgCell${index}`);
  if (!grid || !cell) return;
  const wasSolo = cell.classList.contains('is-solo');
  for (let i = 0; i < CELLS; i += 1) qs(`#dgCell${i}`)?.classList.remove('is-solo');
  grid.classList.remove('is-solo-mode');
  if (wasSolo) return;
  cell.classList.add('is-solo');
  grid.classList.add('is-solo-mode');
}

/** 切换播放线路（与直播页共用同一个偏好，两边的选择保持一致）。 */
function setProto(proto) {
  App.liveProto = App.liveProto === proto ? '' : proto;
  try {
    localStorage.setItem(LIVE_PROTO_KEY, App.liveProto);
  } catch (err) {
    log.debug('线路偏好写入失败（忽略）', err);
  }
  document.querySelectorAll('[data-act="dg-proto"]').forEach((item) => {
    item.classList.toggle('btn--primary', (item.dataset.proto || '') === App.liveProto);
  });
  toast(
    App.liveProto
      ? `导播台已切到 ${proto === 'hls' ? 'HLS' : 'WebRTC'} 线路`
      : '导播台已恢复默认线路',
    'info',
    3600
  );
  reconnectAll();
}

/**
 * 点击委托：挂在 ``#directorBody`` 上——它同时包住顶部工具条（线路 / 刷新）
 * 与网格，两个区域共用一个委托，格子内部结构变化也不影响它。
 */
export function installDelegation() {
  const host = qs('#directorBody');
  if (!host || host.dataset.bound === '1') return;
  host.dataset.bound = '1';
  host.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const index = Number(btn.dataset.cell || '0');
    switch (btn.dataset.act) {
      case 'dg-audio':
        setAudio(index);
        break;
      case 'dg-focus':
        setFocus(index);
        break;
      case 'dg-reconnect':
        reconnect(index);
        break;
      case 'dg-proto':
        setProto(btn.dataset.proto || '');
        break;
      case 'dg-refresh':
        // 用户显式刷新：让服务端现场重新探测一次（可能要等媒体服务器超时）
        probeLiveHealth().then(() => {
          if (App.state) assign(lastCams);
        });
        break;
      default:
        break;
    }
  });
}
