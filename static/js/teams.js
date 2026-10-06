/* 组队台：拖拽编排队伍成员，并就地编辑队名 / 缩写 / 主题色 / 分组。
 *
 * 赛制说明：队伍一经生成便全程固定（随机分配队友 → 固定分组），
 * 因此这里编辑的是「队伍成员」，而不是某一场比赛的阵容。
 *
 * 数据约定：所有改动（拖拽、改名、换色、换组、删组）都只改**本地草案**；
 * 点「保存队伍」后一次性 PUT /api/teams。保存时**没有成员的分组会被自动删除**，
 * 而队伍的增删会让服务端清空赛程（比分一并清除），因此保存前会提示一次。
 */

import { App, Modal, api, esc, log, qs, qsa, toast } from './core.js';
import { avaHtml } from './ui.js';

/** 主题色调色板：与后端 app/tournament.py 的 PALETTE 同一套顺序（自动配色按队伍序号取）。 */
const PALETTE = ['#22e0e8', '#ff2f8e', '#ffd83d', '#7d5cff', '#43e58a', '#ff8a3d', '#3db2ff', '#e05cff'];

const T = {
  teams: [],
  pool: [],
  dragPid: null,
  bound: false,
};

const players = () => App.state?.players || [];
const teamSize = () => Math.max(1, App.state?.rules?.teamSize || 1);
const joinedIds = () => new Set(App.state?.participants || []);

const playerOf = (pid) => players().find((p) => p.id === pid);
const nameOf = (pid) => playerOf(pid)?.name || pid;

/** 自动配色：按队伍序号取调色板（与后端随机组队同一套顺序，看起来才一致）。 */
const autoColor = (index) => PALETTE[index % PALETTE.length];
/** 这支队伍实际显示的颜色：自己设了用自己的，否则用自动色。 */
const shownColor = (team, index) => team.color || autoColor(index);
/** 是否处于「自动」（没自己指定颜色）。 */
const isAutoColor = (team) => !team.color;

/** 未参与本届：只在**显式指定过名单**时才有意义（空名单 = 本届无人参与）。 */
function isOut(pid) {
  if (!App.state?.participantsSet) return false;
  return !joinedIds().has(pid);
}

/** 后端队伍数据的指纹：只有它变了才重新载入草案（否则保住正在拖的改动）。 */
const sourceKey = () => `${App.state?.revision ?? 0}|${(App.state?.teams || []).length}`;
let loadedFrom = '';

/** 从后端状态载入草案（丢弃未保存的改动）。 */
export function load() {
  loadedFrom = sourceKey();
  const list = App.state?.teams || [];
  T.teams = list.map((t) => ({
    id: t.id,
    name: t.name,
    short: t.short,
    color: t.color,
    group: t.group,
    playerIds: [...(t.playerIds || [])],
  }));
  const used = new Set(T.teams.flatMap((t) => t.playerIds));
  T.pool = players()
    .filter((p) => !used.has(p.id))
    .map((p) => p.id);
  log.debug('组队台载入', '队伍', T.teams.length, '候选', T.pool.length);
}

/* ------------------------------- 渲染 ------------------------------- */
function chipHtml(pid) {
  const p = playerOf(pid);
  if (!p) return '';
  const out = isOut(pid);
  const meta = [p.tag || p.id, out ? '未参与本届' : ''].filter(Boolean).join(' · ');
  return (
    `<div class="pchip${out ? ' pchip--out' : ''}" draggable="true" data-pid="${esc(p.id)}" ` +
    `title="${esc([p.name, meta].filter(Boolean).join(' · '))}">` +
    avaHtml(p, 'xs') +
    `<span class="pchip__txt"><span class="pchip__name">${esc(p.name || p.id)}</span>` +
    `<span class="pchip__meta">${esc(meta)}</span></span>` +
    (out ? '<span class="pchip__tag pchip__tag--out">未参与</span>' : '') +
    `</div>`
  );
}

//: 队名 / 缩写的长度上限（**字符数**，中文一个算一个）。
//: 队名短一点读着才像队名（「霓虹」而不是「甲 & 乙 & 丙」），赛程表与对阵图里也放得下。
const NAME_MAX = 6;
const SHORT_MAX = 4;

/** 队伍卡片里的一行输入（改名 / 改缩写）。 */
const fieldHtml = (field, label, value, placeholder, extra = '') =>
  `<label class="zfield"><span>${esc(label)}</span>` +
  `<input data-team-field="${field}" value="${esc(value || '')}" placeholder="${esc(placeholder)}" ` +
  `maxlength="${field === 'short' ? SHORT_MAX : NAME_MAX}">${extra}</label>`;

//: 自动队名的素材（2 字词根 + 后缀，拼出来正好 2~4 字）
const NAME_STEMS = [
  '霓虹', '深渊', '星轨', '夜刃', '赤鳞', '幻影', '铁翼', '量子', '迷雾', '雷鸣',
  '苍蓝', '灼羽', '虚空', '逆流', '零号', '极昼', '猎风', '暗涌', '折光', '碎片',
  '白噪', '黑潮', '蚀月', '浮空',
];
const NAME_TAILS = ['队', '团', '盟'];
const NAME_SUFFIXES = ['小队', '战队', '分队'];

/** 随机一个 2~4 字的队名（尽量不与已有队名重复）。 */
function randomTeamName() {
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
  const taken = new Set(T.teams.map((t) => (t.name || '').trim()));
  for (let i = 0; i < 20; i += 1) {
    const stem = pick(NAME_STEMS);
    const roll = Math.random();
    const name = roll < 0.55 ? stem : roll < 0.85 ? stem + pick(NAME_TAILS) : stem + pick(NAME_SUFFIXES);
    if (!taken.has(name)) return name;
  }
  return pick(NAME_STEMS);
}

/**
 * 一支队伍的卡片。
 *
 * 卡片左边框就是它的主题色（``--tc``）：改了颜色立刻能看见，不必重绘整块面板
 * ——重绘会打断正在输入的内容。
 */
function teamCardHtml(team, index) {
  const size = teamSize();
  const ids = team.playerIds;
  const full = ids.length >= size;
  let body = ids.map(chipHtml).join('');
  for (let i = ids.length; i < size; i += 1) body += `<div class="zone__slot">空位</div>`;
  if (!ids.length) {
    body = `<div class="zone__empty">拖选手到此处 · <b>保存时会自动删除这个空分组</b></div>`;
  }
  const color = shownColor(team, index);
  const auto = isAutoColor(team);
  return (
    `<div class="zone zone--team${full ? ' zone--full' : ''}" data-zone="team:${esc(team.id)}" ` +
    `data-team="${esc(team.id)}" style="--tc:${esc(color)}">` +
    `<div class="zone__head"><b>${esc(team.name || team.short || team.id)}</b>` +
    `<span class="zone__count">${ids.length}/${size}</span></div>` +
    `<div class="zone__edit">` +
    `${fieldHtml(
      'name',
      '队名',
      team.name,
      '如 霓虹',
      `<button class="btn btn--xs btn--ghost" type="button" data-team-op="name-auto" ` +
        `title="随机生成一个 2~4 字的队名">自动</button>`
    )}` +
    `${fieldHtml('short', '缩写', team.short, '如 NH')}</div>` +
    `<div class="zone__ops">` +
    `<label class="zcolor" title="主题色（卡片左边框）">` +
    `<input type="color" data-team-field="color" value="${esc(color)}"${auto ? ' disabled' : ''}>` +
    `</label>` +
    `<label class="zauto" title="按队伍序号自动取色">` +
    `<input type="checkbox" data-team-field="auto"${auto ? ' checked' : ''}>自动</label>` +
    `<span class="zone__group" title="分组">${team.group ? `${esc(team.group)} 组` : '未分组'}</span>` +
    `<button class="btn btn--sm btn--ghost" type="button" data-team-op="swap" ` +
    `title="与另一个分组的队伍互换分组">换组</button>` +
    `<button class="btn btn--sm btn--ghost" type="button" data-team-op="drop" ` +
    `title="删除这个分组，队员回到候选池">删除分组</button>` +
    `</div>` +
    `<div class="zone__body">${body}</div></div>`
  );
}

function poolHtml() {
  const ids = T.pool;
  const body = ids.length
    ? ids.map(chipHtml).join('')
    : `<div class="zone__empty">没有候补选手</div>`;
  return (
    `<div class="zone zone--pool" data-zone="pool">` +
    `<div class="zone__head"><b>候选池</b><span class="zone__count">${ids.length} 人</span></div>` +
    `<div class="zone__sub">未编入队伍的选手</div>` +
    `<div class="zone__body">${body}</div></div>`
  );
}

function boardHtml() {
  return (
    `<div class="team-board">` +
    T.teams.map((team, index) => teamCardHtml(team, index)).join('') +
    poolHtml() +
    `</div>`
  );
}

function shellHtml() {
  const size = teamSize();
  const count = T.teams.length;
  return (
    `<div class="group-bar">` +
    `<div class="tool-group">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="quick-group">快速创建分组</button>` +
    `<button class="btn btn--sm" type="button" data-act="teams-auto">重新随机组队</button>` +
    `<button class="btn btn--sm" type="button" data-act="teams-reset">还原</button>` +
    `<button class="btn btn--sm" type="button" data-act="teams-save">保存队伍</button>` +
    `</div>` +
    `<span class="panel__hint" style="margin-left:auto">${count} 支队伍 · 默认每队 ${size} 人 · 拖拽调整队友</span>` +
    `</div>` +
    `<div class="notice">队友随机分配后<b>全程固定</b>（不换队、不换队友）。` +
    `卡片上可<b>改队名（≤6 字，点「自动」随机取一个 2~4 字的）/ 缩写 / 主题色</b>、` +
    `<b>换组</b>（与另一组的队伍互换）、<b>删除分组</b>（队员回到候选池）；` +
    `保存时<b>没有成员的分组会被自动删除</b>，改完队伍需重新「生成赛程」。</div>` +
    `<div id="teamBoard">${boardHtml()}</div>`
  );
}

export function mount(host) {
  if (!host) return;
  // 管理面板重绘会重新挂载组队台：只有后端队伍数据真变了才重载草案，
  // 否则保住正在拖的改动（保存 / 随机组队后版本号会变，那时才重载）
  if (loadedFrom !== sourceKey()) load();
  host.innerHTML = shellHtml();
  log.debug('组队台已挂载', T.teams.length);
}

export function refresh() {
  const board = qs('#teamBoard');
  if (!board) return;
  // 选手可能被删除，剔除失效 ID
  T.teams.forEach((t) => {
    t.playerIds = t.playerIds.filter((pid) => playerOf(pid));
  });
  T.pool = T.pool.filter((pid) => playerOf(pid));
  board.innerHTML = boardHtml();
}

/* ------------------------------- 交互 ------------------------------- */
/** 把选手拖到某支队伍或候选池；队伍满员时挤出一人回候选池。 */
export function moveTo(pid, zone) {
  if (!pid || !zone) return;
  T.teams.forEach((t) => {
    t.playerIds = t.playerIds.filter((x) => x !== pid);
  });
  T.pool = T.pool.filter((x) => x !== pid);

  if (zone === 'pool') {
    T.pool.push(pid);
    refresh();
    return;
  }

  const team = T.teams.find((t) => `team:${t.id}` === zone);
  if (!team) return;
  const size = teamSize();
  if (team.playerIds.length >= size) {
    const out = team.playerIds.pop();
    if (out) T.pool.push(out);
    toast(`${team.short || team.name || team.id} 已满（${size} 人），${nameOf(out)} 回到候选池`, 'info', 4000);
  }
  team.playerIds.push(pid);
  refresh();
}

/** 就地改卡片边框色（不重绘，免得打断正在输入的队名）。 */
function paintCard(card, team, index) {
  card.style.setProperty('--tc', shownColor(team, index));
}

/** 删除一个分组：队员回到候选池（保存前都还能「还原」）。 */
function dropTeam(teamId) {
  const index = T.teams.findIndex((t) => t.id === teamId);
  if (index < 0) return;
  const team = T.teams[index];
  const count = team.playerIds.length;
  const label = team.name || team.short || team.id;
  if (count && !window.confirm(`删除分组「${label}」？${count} 名队员会回到候选池（保存后才生效）。`)) {
    return;
  }
  T.teams.splice(index, 1);
  T.pool.push(...team.playerIds);
  refresh();
  toast(count ? `已删除「${label}」，${count} 名队员回到候选池` : `已删除空分组「${label}」`, 'info', 5000);
}

/**
 * 换组：与**另一个分组**的队伍互换 ``group``。
 *
 * 分组名只在生成赛程时被用来搭对阵，所以换组之后要重新「生成赛程」才生效——
 * 提示里说明了这一点，避免有人换完以为对阵自己会变。
 */
function openSwapGroup(teamId) {
  const team = T.teams.find((t) => t.id === teamId);
  if (!team) return;
  const others = T.teams.filter((t) => t.id !== teamId && (t.group || '') !== (team.group || ''));
  if (!others.length) {
    toast('没有其它分组的队伍可以交换；先「快速创建分组」或「生成赛程」把队伍分好组', 'warn', 7000);
    return;
  }
  const label = (t) => esc(t.name || t.short || t.id);
  const body =
    `<div class="notice">把 <b>${label(team)}</b>（${team.group ? `${esc(team.group)} 组` : '未分组'}）` +
    `与另一支队伍<b>互换分组</b>。分组只在生成赛程时生效，换完请重新「生成赛程」。</div>` +
    `<div class="pick-list" style="margin-top:10px">` +
    others
      .map(
        (t, i) =>
          `<button type="button" class="pick" data-swap="${esc(t.id)}">` +
          `<span class="who__txt"><span class="who__name">${label(t)}</span>` +
          `<span class="pick__meta">${t.group ? `${esc(t.group)} 组` : '未分组'} · ` +
          `${t.playerIds.length} 人</span></span></button>`
      )
      .join('') +
    `</div>`;
  Modal.open({
    title: `换组 · ${team.name || team.short || team.id}`,
    body,
    footer: `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      bodyEl.querySelectorAll('[data-swap]').forEach((btn) => {
        btn.onclick = () => {
          const other = T.teams.find((t) => t.id === btn.dataset.swap);
          if (!other) return;
          const mine = team.group;
          team.group = other.group;
          other.group = mine;
          Modal.close();
          refresh();
          toast(
            `${team.name || team.id} → ${team.group || '未分组'} 组，` +
              `${other.name || other.id} → ${other.group || '未分组'} 组（重新生成赛程后生效）`,
            'ok',
            7000
          );
        };
      });
    },
  });
}

export function installDnD() {
  const host = qs('#adminPanel');
  if (!host || T.bound) return;
  T.bound = true;

  host.addEventListener('dragstart', (e) => {
    const chip = e.target.closest('.pchip');
    if (!chip) return;
    T.dragPid = chip.dataset.pid;
    chip.classList.add('pchip--dragging');
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', T.dragPid);
    }
  });

  host.addEventListener('dragend', () => {
    qsa('.pchip--dragging', host).forEach((el) => el.classList.remove('pchip--dragging'));
    qsa('.zone--over', host).forEach((el) => el.classList.remove('zone--over'));
    T.dragPid = null;
  });

  host.addEventListener('dragover', (e) => {
    const zone = e.target.closest('.zone');
    if (!zone) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    qsa('.zone--over', host).forEach((el) => {
      if (el !== zone) el.classList.remove('zone--over');
    });
    zone.classList.add('zone--over');
  });

  host.addEventListener('dragleave', (e) => {
    const zone = e.target.closest('.zone');
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove('zone--over');
  });

  host.addEventListener('drop', (e) => {
    const zone = e.target.closest('.zone');
    if (!zone) return;
    e.preventDefault();
    zone.classList.remove('zone--over');
    const pid = T.dragPid || (e.dataTransfer ? e.dataTransfer.getData('text/plain') : '');
    if (pid) moveTo(pid, zone.dataset.zone);
  });

  // ---- 卡片上的就地编辑（改文本只动草案，**不重绘**，否则输入框会失焦）----
  const teamOfCard = (target) => {
    const card = target.closest?.('.zone--team');
    if (!card) return null;
    const index = T.teams.findIndex((t) => t.id === card.dataset.team);
    return index < 0 ? null : { card, team: T.teams[index], index };
  };

  host.addEventListener('input', (e) => {
    const hit = teamOfCard(e.target);
    if (!hit) return;
    const field = e.target.dataset.teamField;
    if (field === 'name') hit.team.name = e.target.value;
    else if (field === 'short') hit.team.short = e.target.value;
    else if (field === 'color') {
      hit.team.color = e.target.value;
      paintCard(hit.card, hit.team, hit.index);
    }
  });

  host.addEventListener('change', (e) => {
    const hit = teamOfCard(e.target);
    if (!hit || e.target.dataset.teamField !== 'auto') return;
    // 勾上「自动」= 清掉自定色，回到按序号取色
    hit.team.color = e.target.checked ? '' : autoColor(hit.index);
    hit.card.querySelector('[data-team-field="color"]').value = shownColor(hit.team, hit.index);
    hit.card.querySelector('[data-team-field="color"]').disabled = e.target.checked;
    paintCard(hit.card, hit.team, hit.index);
  });

  host.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-team-op]');
    if (!btn) return;
    e.preventDefault();
    const card = btn.closest('.zone--team');
    if (!card) return;
    if (btn.dataset.teamOp === 'drop') dropTeam(card.dataset.team);
    else if (btn.dataset.teamOp === 'swap') openSwapGroup(card.dataset.team);
    else if (btn.dataset.teamOp === 'name-auto') {
      // 随机 2~4 字队名：只改**本地草案**（点「保存队伍」才落库），所以随便点着换着看
      const hit = teamOfCard(card.querySelector('[data-team-field="name"]'));
      if (!hit) return;
      hit.team.name = randomTeamName();
      const input = card.querySelector('[data-team-field="name"]');
      if (input) input.value = hit.team.name;
      const head = card.querySelector('.zone__head b');
      if (head) head.textContent = hit.team.name || hit.team.short || hit.team.id;
    }
  });

  log.debug('组队台拖拽与就地编辑已启用');
}

export function reset() {
  load();
  refresh();
  toast('已还原为当前保存的队伍', 'info');
}

/**
 * 保存队伍。
 *
 * **没有成员的分组会被自动删除**（这是约定，不再需要手动清理）；队伍如果被增删，
 * 服务端会清空赛程（比分一并清除）——那种情况保存前会确认一次。
 */
export async function save() {
  const size = teamSize();
  const beforeIds = new Set((App.state?.teams || []).map((t) => t.id));
  const empty = T.teams.filter((t) => !t.playerIds.length);
  const teams = T.teams.filter((t) => t.playerIds.length);
  if (!teams.length) {
    toast('至少要有一支队伍', 'warn');
    return;
  }
  // 增删队伍 → 服务端清空赛程（只改名字 / 颜色 / 成员不影响）
  const structural =
    teams.length !== beforeIds.size || teams.some((t) => !beforeIds.has(t.id));
  const uneven = teams.filter((t) => t.playerIds.length !== size);
  const notices = [
    empty.length ? `${empty.length} 个没有成员的分组会被删除` : '',
    uneven.length
      ? `${uneven.length} 支队伍人数不是 ${size} 人（各队人数可以不同，但会以少打多）`
      : '',
    structural ? '队伍有增删，当前赛程与比分会被清空' : '',
  ].filter(Boolean);
  if (notices.length && !window.confirm(`${notices.join('；')}。继续保存？`)) {
    return;
  }
  try {
    const res = await api('/teams', {
      method: 'PUT',
      auth: true,
      body: {
        teams: teams.map((t) => ({
          id: t.id,
          name: t.name,
          short: t.short,
          color: t.color,
          group: t.group,
          playerIds: t.playerIds,
        })),
      },
    });
    const cleaned = (res.droppedEmpty || 0) + empty.length;
    toast(`已保存 ${res.count} 支队伍${cleaned ? `（自动清理 ${cleaned} 个空分组）` : ''}`, 'ok', 5000);
    (res.warnings || []).forEach((w) => toast(w, 'warn', 6000));
    if (res.state) App.state = res.state;
    load();
    refresh();
  } catch (err) {
    toast(err.message, 'err');
  }
}
