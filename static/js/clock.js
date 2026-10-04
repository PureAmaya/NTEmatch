/* 比赛计时器：现场主持 / 裁判掐时间用的小工具。
 *
 * 刻意**不进后端、也不进赛制**：赛制里没有「比赛时长」这个字段，也不该有——
 * 不同项目、不同赛制的时长没有可比性，存下来只会变成一份没人维护的数据。
 * 所以状态放在本机 localStorage（按场次编号各记一份），刷新页面不丢；
 * 换台设备当然看不到，这正符合「它就是块秒表」的定位。
 */

import { esc, qsa } from './core.js';

const KEY = 'nte.clock.v1';

/** 计时器状态：``{ [场次编号]: { startedAt: 毫秒|null, accumulated: 毫秒 } }`` */
let state = read();

function read() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function write() {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* 隐私模式下写不进去：本次会话照常能用，只是刷新会丢 */
  }
}

/** 某一场当前已用时（毫秒）。 */
function elapsed(code) {
  const it = state[code];
  if (!it) return 0;
  return (it.accumulated || 0) + (it.startedAt ? Date.now() - it.startedAt : 0);
}

const running = (code) => Boolean(state?.[code]?.startedAt);

/** 毫秒 → ``MM:SS``（超过一小时才显示小时）。 */
function fmt(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const body = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return h ? `${h}:${body}` : body;
}

/** 开始 / 暂停某一场。 */
export function toggleClock(code) {
  const it = state[code] || { startedAt: null, accumulated: 0 };
  if (it.startedAt) {
    it.accumulated = (it.accumulated || 0) + (Date.now() - it.startedAt);
    it.startedAt = null;
  } else {
    it.startedAt = Date.now();
  }
  state[code] = it;
  write();
}

/** 归零（顺带停下）。 */
export function resetClock(code) {
  delete state[code];
  write();
}

/** 计时器读数 + 开始 / 暂停 + 归零；挂在「正在进行」的每一场上。 */
export function clockHtml(code) {
  const key = esc(code);
  const on = running(code);
  return (
    `<div class="clock${on ? ' is-on' : ''}" data-clock="${key}">` +
    `<span class="clock__time" data-role="clock-time">${fmt(elapsed(code))}</span>` +
    `<button class="btn btn--sm${on ? '' : ' btn--primary'}" type="button" ` +
    `data-act="clock-toggle" data-code="${key}">${on ? '暂停' : '开始'}</button>` +
    `<button class="btn btn--sm btn--ghost" type="button" data-act="clock-reset" ` +
    `data-code="${key}">归零</button>` +
    `</div>`
  );
}

/**
 * 全局心跳：每 0.5 秒刷新页面上所有计时器。
 *
 * 为什么是「一个全局定时器 + 按 ``data-clock`` 找元素」，而不是每个计时器各起一个：
 * 面板会随 WebSocket 推送**整块重绘**，绑在旧 DOM 上的定时器会失效或泄漏；这样写
 * 无论重绘多少次都不必重新接管。按钮文案也在这里跟着状态走，省一次重渲染。
 */
export function startClockTicker() {
  if (startClockTicker.done) return;
  startClockTicker.done = true;
  setInterval(() => {
    qsa('[data-clock]').forEach((el) => {
      const code = el.dataset.clock || '';
      const on = running(code);
      const out = el.querySelector('[data-role="clock-time"]');
      if (out) out.textContent = fmt(elapsed(code));
      const btn = el.querySelector('[data-act="clock-toggle"]');
      if (btn) btn.textContent = on ? '暂停' : '开始';
      el.classList.toggle('is-on', on);
    });
  }, 500);
}
