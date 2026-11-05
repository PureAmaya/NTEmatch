/* 模拟群（/bot）：**只在调试模式下存在**。
 *
 * 这就是一个假的 QQ 群：下面输入框里像群友一样打「比赛」「进度」「结果」，
 * 回车之后机器人把**真实会发到群里的东西**（文本分段 + 卡片图）回在这里。
 *
 * 回复从哪来：`/api/debug/bot/say` 认命令后走 `qqbot_api.build_message`——与真实推送
 * 同一条链路。所以这里看到的与群里收到的一致，不是另一套「预览文案」。
 *
 * 不伪造身份：报名 / 授权 / 我的直播间这类命令需要真实 QQ，页面只回一句说明。
 */

import { api, esc, log, qs } from './core.js';
import { icon } from './icons.js';

/** 快捷命令：点一下等于打了一句话。 */
const QUICK = ['比赛', '进度', '结果', '名单', '直播', '帮助'];

/** 页面状态（切走再回来还在）。 */
const state = {
  loaded: false,
  messages: [],
  error: '',
  sending: false,
  history: [],
  historyIndex: -1,
};

async function loadMeta() {
  const data = await api('/debug/bot', { auth: true });
  state.messages = data.messages || [];
  state.loaded = true;
}

/* ------------------------------- 渲染 ------------------------------- */

function botBubbleHtml(msg) {
  const parts = (msg.parts || []).map((part) => `<pre class="bot__text">${esc(part)}</pre>`).join('');
  const image = msg.image?.url
    ? `<a class="bot__imgwrap" href="${esc(msg.image.url)}" target="_blank" rel="noopener noreferrer">
         <img class="bot__img" src="${esc(msg.image.url)}" alt="卡片图" loading="lazy">
       </a>`
    : '';
  const mentions = (msg.mentions || []).length
    ? `<div class="bot__meta">@ ${msg.mentions.map((qq) => esc(qq)).join('、')}</div>`
    : '';
  // 图先、文本后：与真发出去的顺序一致
  return `${image}
    <div class="bot__bubble">
      ${parts || '<span class="bot__empty">（这条只有图）</span>'}
      ${mentions}
    </div>`;
}

function bubbleHtml(msg) {
  const mine = msg.role === 'user';
  const name = mine ? '你' : '比赛机器人';
  const time = `<span class="bot__time">${esc(msg.at || '')}</span>`;
  const tag =
    !mine && msg.source === 'push' ? '<span class="bot__tag bot__tag--push">真实推送</span>' : '';
  return `
  <div class="bot__msg ${mine ? 'bot__msg--me' : 'bot__msg--bot'}">
    <div class="bot__ava" aria-hidden="true">${mine ? '我' : '机'}</div>
    <div class="bot__side">
      <div class="bot__head">${mine ? '' : `<b>${name}</b>`}${tag}${time}</div>
      ${mine ? `<div class="bot__bubble bot__bubble--me"><pre class="bot__text">${esc((msg.parts || []).join('\n'))}</pre></div>` : botBubbleHtml(msg)}
    </div>
  </div>`;
}

function messagesHtml() {
  if (state.error) return `<div class="bot__sys">${esc(state.error)}</div>`;
  if (!state.messages.length) {
    return `<div class="bot__sys">
      <b>这里是模拟群</b>
      像在群里一样发一句「比赛」试试；发「帮助」看能听懂什么。
    </div>`;
  }
  return state.messages.map(bubbleHtml).join('');
}

function shellHtml() {
  return `
  <div class="panel bot__room">
    <div class="bot__bar">
      <div class="bot__roomname">NTE 比赛 · 调试群 <span>(${state.messages.length})</span></div>
      <div class="bot__roomhint">模拟群聊 · 只在本页显示，不会发到真实 QQ</div>
      <button class="btn btn--ghost btn--sm" id="botClear" type="button">
        ${icon('trash')}<span>清空</span>
      </button>
    </div>
    <div class="bot__msgs" id="botMsgs">${messagesHtml()}</div>
    <div class="bot__chips" id="botChips">
      ${QUICK.map((cmd) => `<button class="bot__chip" type="button" data-cmd="${esc(cmd)}">${esc(cmd)}</button>`).join('')}
    </div>
    <div class="bot__composer">
      <input class="bot__input" id="botInput" type="text" autocomplete="off"
             placeholder="像在群里一样发一句：比赛 / 进度 / 结果 / 名单 / 帮助">
      <button class="btn btn--primary" id="botSend" type="button">
        ${icon('send')}<span>发送</span>
      </button>
    </div>
  </div>`;
}

function paint() {
  const host = qs('#botBody');
  if (!host) return;
  host.innerHTML = shellHtml();
  scrollBottom();
  bind();
}

function scrollBottom() {
  const box = qs('#botMsgs');
  if (box) box.scrollTop = box.scrollHeight;
}

function append(html, { scroll = true } = {}) {
  const box = qs('#botMsgs');
  if (!box) return;
  if (box.querySelector('.bot__sys')) box.innerHTML = '';
  box.insertAdjacentHTML('beforeend', html);
  if (scroll) box.scrollTop = box.scrollHeight;
}

function typingHtml(on) {
  const box = qs('#botMsgs');
  if (!box) return;
  const old = qs('#botTyping');
  if (old) old.remove();
  if (!on) return;
  box.insertAdjacentHTML(
    'beforeend',
    `<div class="bot__msg bot__msg--bot" id="botTyping">
       <div class="bot__ava" aria-hidden="true">机</div>
       <div class="bot__side"><div class="bot__bubble bot__bubble--typing">正在输入…</div></div>
     </div>`,
  );
  box.scrollTop = box.scrollHeight;
}

/* ------------------------------- 交互 ------------------------------- */

async function send(text) {
  const content = String(text || '').trim();
  if (!content || state.sending) return;
  const input = qs('#botInput');
  if (input) input.value = '';
  state.sending = true;
  state.history.push(content);
  state.historyIndex = -1;
  typingHtml(true);
  const started = Date.now();
  try {
    const data = await api('/debug/bot/say', { method: 'POST', auth: true, body: { text: content } });
    // 本地接口常常 30ms 就回来了，太快会闪一下：至少让「正在输入」露个面
    const wait = Math.max(0, 240 - (Date.now() - started));
    await new Promise((resolve) => setTimeout(resolve, wait));
    typingHtml(false);
    append(bubbleHtml(data.user), { scroll: false });
    (data.replies || []).forEach((reply) => append(bubbleHtml(reply)));
    state.messages.push(data.user, ...(data.replies || []));
    const count = qs('.bot__roomname span');
    if (count) count.textContent = `(${state.messages.length})`;
  } catch (err) {
    typingHtml(false);
    log.warn('模拟群发言失败', err);
    append(`<div class="bot__sys">发送失败：${esc(err.message || '未知错误')}</div>`);
  } finally {
    state.sending = false;
    qs('#botInput')?.focus();
  }
}

function bind() {
  const sendBtn = qs('#botSend');
  if (sendBtn) sendBtn.addEventListener('click', () => send(qs('#botInput')?.value));

  const input = qs('#botInput');
  if (input) {
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        send(input.value);
        return;
      }
      // ↑ / ↓ 翻自己发过的命令，像终端一样
      if (event.key === 'ArrowUp' && state.history.length) {
        event.preventDefault();
        state.historyIndex =
          state.historyIndex < 0 ? state.history.length - 1 : Math.max(0, state.historyIndex - 1);
        input.value = state.history[state.historyIndex] || '';
      } else if (event.key === 'ArrowDown' && state.historyIndex >= 0) {
        event.preventDefault();
        state.historyIndex += 1;
        if (state.historyIndex >= state.history.length) {
          state.historyIndex = -1;
          input.value = '';
        } else {
          input.value = state.history[state.historyIndex] || '';
        }
      }
    });
    input.focus();
  }

  qs('#botChips')?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-cmd]');
    if (chip) send(chip.dataset.cmd);
  });

  qs('#botClear')?.addEventListener('click', async () => {
    try {
      await api('/debug/bot/clear', { method: 'POST', auth: true, body: {} });
      state.messages = [];
      paint();
    } catch (err) {
      log.warn('清空失败', err);
    }
  });
}

export async function renderBotPage() {
  const host = qs('#botBody');
  if (!host) return;
  if (state.loaded) {
    paint();
    return;
  }
  host.innerHTML = '<div class="panel bot__room"><div class="bot__sys">正在连接模拟群…</div></div>';
  try {
    await loadMeta();
    state.error = '';
  } catch (err) {
    log.warn('模拟群读取失败', err);
    if (err.status === 404) {
      state.error = '调试模式未开启：用 `uv run main.py --debug` 启动后这里才有内容';
    } else if (err.status === 403) {
      state.error = '需要服务器管理员登录后才能使用模拟群';
    } else {
      state.error = `读取失败：${err.message || '未知错误'}`;
    }
  }
  paint();
}
