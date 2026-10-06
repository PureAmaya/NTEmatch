/**
 * 热更新面板（服务器页）：**不中断服务**地换上新代码。
 *
 * 这里只做三件事：显示守护进程现在处于哪一步、点一下请求更新、更新完提示刷新。
 *
 * 两条刻意的设计：
 *
 * * **更新是异步的**：点下去只得到「已排入队列」。真换代的是持有监听端口的守护进程
 *   （见 `app/hotrun.py`），它会起新进程、等它能服务了再停旧的——正在处理这个请求的
 *   进程马上就会退出，同步等它做完就成了「自己等自己死」。所以这里点完就轮询状态；
 * * **新版本起不来不会造成事故**：父进程会把起不来的新进程丢掉，旧版本继续服务，
 *   状态里留一句为什么 + 一段日志尾巴。面板把这段话原样显示出来——比一句
 *   「更新失败」有用得多。
 */
import { api, esc, log, qs, toast } from './core.js';
import { panelHtml } from './ui.js';

/** 守护进程的阶段 → 人话。 */
const PHASE_LABEL = {
  starting: '启动中',
  running: '就绪',
  updating: '正在更新…',
  stopping: '正在退出',
  stopped: '已停止',
  failed: '未能启动',
};

/** 轮询：请求更新后盯一会儿状态，让人看得见过程。 */
const POLL_TIMES = [800, 1600, 2500, 4000, 6000, 8000, 10000];
let pollTimer = 0;

const fmtTime = (ts) => {
  const n = Number(ts);
  if (!n) return '—';
  const d = new Date(n * 1000);
  const pad = (v) => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

/** 一行「键：值」（键固定宽度，值可折行）。 */
const rowHtml = (key, value, cls = '') =>
  `<div class="hot__row"><span class="hot__key">${esc(key)}</span>` +
  `<span class="hot__val${cls ? ` ${cls}` : ''}">${value}</span></div>`;

/**
 * 启动自检清单 + 业务进程那边的事实。
 *
 * 两份数据来自两个进程：**父进程**（守护）查的是「换代机制能不能用」（端口 / 仓库 /
 * 依赖 / 预检），**业务进程**报的是「应用侧的事实」（会话落不落库、事件循环、卡片渲染）。
 * 分工的原因见 app/hot.py——各写各的，读的人合并。
 */
function checkHtml(st) {
  const items = Array.isArray(st.check) ? st.check : [];
  const app = st.app || {};
  const rows = items.map(
    (item) =>
      `<div class="hot__chk${item.ok ? '' : ' hot__chk--bad'}">` +
      `<b>${item.ok ? '✓' : '!'}</b><span class="hot__chk-name">${esc(item.name || '')}</span>` +
      `<span class="hot__chk-detail">${esc(item.detail || '')}</span></div>`
  );
  // 业务进程侧：会话落库是「换代不掉线」的命门，单独提出来说
  if (app.pid) {
    rows.push(
      `<div class="hot__chk${app.sessions ? '' : ' hot__chk--bad'}">` +
        `<b>${app.sessions ? '✓' : '!'}</b><span class="hot__chk-name">会话落库</span>` +
        `<span class="hot__chk-detail">${
          app.sessions
            ? `已开启（换代不会把人踢下线）· 当前在线 ${Number(app.online) || 0}`
            : '未开启：换代会把所有人踢下线'
        }</span></div>`
    );
    rows.push(
      `<div class="hot__chk">` +
        `<b>·</b><span class="hot__chk-name">业务进程</span>` +
        `<span class="hot__chk-detail">pid ${esc(String(app.pid))} · 事件循环 ${esc(
          String(app.loop || '—')
        )} · Python ${esc(String(app.python || '—'))} · 卡片渲染 ${
          app.cards ? '可用' : '不可用（没装 Pillow，推送走纯文本）'
        }</span></div>`
    );
  }
  if (!rows.length) return '';
  return `<div class="hot__label">启动自检</div>${rows.join('')}`;
}

/** 上次换代的结果（成功/失败、提交变化、耗时、是谁触发的）。 */
function lastReloadHtml(st) {
  const last = st.lastReload;
  if (!last) return '';
  const ok = Boolean(last.ok);
  const commits = last.from || last.to ? `${esc(String(last.from || '?'))} → ${esc(String(last.to || '?'))}` : '';
  const who = [last.reason, last.actor].filter(Boolean).join(' · ');
  return (
    `<div class="hot__label">上次换代</div>` +
    rowHtml('结果', ok ? '成功' : '未生效', ok ? 'hot__val--ok' : 'hot__val--bad') +
    (commits ? rowHtml('提交', commits) : '') +
    rowHtml('时间', `${fmtTime(last.at)}${last.seconds ? ` · 用时 ${last.seconds}s` : ''}`) +
    (who ? rowHtml('触发', esc(who)) : '') +
    (last.message ? rowHtml('说明', esc(String(last.message))) : '')
  );
}

function bodyHtml(st) {
  const supervised = Boolean(st.supervised);
  if (!supervised) {
    return (
      `<div class="notice notice--warn">当前服务<b>不是由热更新守护启动的</b>，所以不能热更新——` +
      `点「立即更新」会重启服务，所有人都会掉线。<br>` +
      `要用热更新请这样启动（旧进程会优雅退场，正在看比分的人只会重连一下）：<br>` +
      `<code class="hot__cmd">uv run python -m app hotrun</code>` +
      `${st.hint ? `<br><span class="hot__hint">${esc(st.hint)}</span>` : ''}</div>` +
      rowHtml('当前提交', esc(st.head || '—'))
    );
  }
  const phase = PHASE_LABEL[st.phase] || st.phase || '未知';
  const tail = String(st.tail || '').trim();
  // 两种换代语义要**说清楚**：Linux 上零中断，Windows 上是顺序换代（约 1 秒空窗）。
  // 让人以为「永远不会断」而实际断了 1 秒，比一开始就说明白糟糕得多。
  const seamless = st.holdSocket !== false;
  const modeNote = seamless
    ? `<div class="notice">换代怎么做到不断线：新进程先起来并<b>接管同一个监听端口</b>，` +
      `确认能服务了才让旧进程优雅退场——正在看比赛的人只是重连一下，` +
      `登录状态也不会掉（会话已落库）。新版本万一启动失败，旧版本会继续服务。</div>`
    : `<div class="notice notice--warn">当前是<b>顺序换代</b>模式：更新时先停旧进程、再起新进程，` +
      `中间约 1 秒连不上（本机开发环境的取舍）。<br>` +
      `Linux 上守护会自己持有监听套接字，新老进程同时 accept，换代<b>零中断</b>；` +
      `登录状态不会掉（会话已落库）。新代码起不来时会被预检拦下，旧版本继续服务。</div>`;
  return (
    rowHtml('守护进程', `运行中 · 端口 ${esc(String(st.port || '—'))} · 主进程 ${esc(String(st.pid || '—'))}`) +
    rowHtml(
      '当前状态',
      esc(phase) +
        (st.reloads ? ` · 已换代 ${Number(st.reloads)} 次` : '') +
        (st.watch === false ? ' · 未开启自动检测' : '') +
        (seamless ? ' · 零中断交接' : ' · 顺序换代（约 1 秒空窗）')
    ) +
    rowHtml('当前提交', esc(st.short || st.head || '—')) +
    rowHtml('上次变化', fmtTime(st.since)) +
    rowHtml('本进程 pid', esc(String(st.child || '—'))) +
    (st.message
      ? rowHtml('上次结果', esc(String(st.message)), st.phase === 'failed' ? 'hot__val--bad' : '')
      : '') +
    (tail ? `<pre class="hot__log">${esc(tail)}</pre>` : '') +
    (st.pending ? `<div class="notice">已有一个更新请求在队列里，正在处理。</div>` : '') +
    lastReloadHtml(st) +
    checkHtml(st)
  );
}

function actionsHtml(st) {
  if (!st.supervised) return '';
  return (
    `<div class="hot__acts">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="hot-reload">` +
    `重新加载代码</button>` +
    `<button class="btn btn--sm" type="button" data-act="hot-pull">拉取更新并换代</button>` +
    `<button class="btn btn--sm btn--ghost" type="button" data-act="hot-status">刷新状态</button>` +
    `</div>`
  );
}

async function load(host) {
  let st = {};
  try {
    st = await api('/hot', { auth: true });
  } catch (err) {
    host.innerHTML = panelHtml(
      '热更新',
      '代码更新不中断服务',
      `<div class="notice notice--warn">读取状态失败：${esc(err.message || '未知错误')}</div>`
    );
    return;
  }
  host.innerHTML = panelHtml(
    '热更新',
    '改代码 / git pull 都不中断服务',
    bodyHtml(st) + actionsHtml(st)
  );
}

/** 渲染面板（服务器页调用；异步填内容，不拖慢整页）。 */
export function renderHotPanel(host) {
  if (!host) return;
  void load(host);
}

/** 原地刷新状态（点「刷新状态」或轮询时用）。 */
export async function refreshHotPanel() {
  const host = qs('#hotBox');
  if (host) await load(host);
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = 0;
  }
}

function poll(index = 0) {
  stopPolling();
  if (index >= POLL_TIMES.length) return;
  pollTimer = setTimeout(async () => {
    await refreshHotPanel();
    poll(index + 1);
  }, POLL_TIMES[index]);
}

/**
 * 请求一次更新（``reload`` 只换代码，``pull`` 先 git pull 再换）。
 *
 * 请求体里的 mode 由服务端再校验一次；失败（例如没有守护进程）会带着原因回 400，
 * 这里把原因原样告诉人。
 */
export async function requestHotUpdate(mode) {
  if (mode === 'pull') {
    const ok = window.confirm(
      '在服务器上执行 git pull 并换上新的代码？\n\n' +
        '只会快进合并（--ff-only）；工作区有未提交改动时会被拒绝，不会覆盖任何人的改动。\n' +
        '更新期间服务不中断：新版本先起来，确认能服务了旧版本才退场。'
    );
    if (!ok) return;
  }
  try {
    const res = await api('/hot/update', { method: 'POST', auth: true, body: { mode } });
    toast(res.message || '已排入更新队列', 'ok', 9000);
    log.info('已请求热更新', mode);
  } catch (err) {
    toast(err.message || '请求更新失败', 'err', 9000);
    return;
  }
  poll();
}
