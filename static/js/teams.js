/* 组队台：用拖拽把选手编排进固定队伍（每队 teamSize 人），或放回候补池。
 *
 * 赛制说明：队伍一经生成便全程固定（随机分配队友 → 固定分组），
 * 因此这里编辑的是「队伍成员」，而不是某一场比赛的阵容。
 *
 * 数据约定：拖拽只改本地草案；点「保存队伍」后一次性 PUT /api/teams。
 * 若队伍结构（数量 / ID）发生变化，服务端会清空赛程，保存时会提示。
 */

import { App, api, esc, log, qs, qsa, toast } from './core.js';
import { avaHtml } from './ui.js';

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

function isOut(pid) {
  const set = joinedIds();
  return set.size > 0 && !set.has(pid);
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
  log.debug('组队台载入', '队伍', T.teams.length, '候补', T.pool.length);
}

/* ------------------------------- 渲染 ------------------------------- */
function chipHtml(pid) {
  const p = playerOf(pid);
  if (!p) return '';
  const out = isOut(pid);
  const meta = [p.tag || p.id, p.substitute ? '替补' : '', out ? '未参与本届' : '']
    .filter(Boolean)
    .join(' · ');
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

function zoneHtml(key, title, subtitle, ids, slots) {
  const full = slots > 0 && ids.length >= slots;
  let body = ids.map(chipHtml).join('');
  if (slots > 0) {
    for (let i = ids.length; i < slots; i += 1) body += `<div class="zone__slot">空位</div>`;
  }
  if (!body) body = `<div class="zone__empty">拖选手到此处</div>`;
  return (
    `<div class="zone zone--${key.startsWith('team:') ? 'team' : 'pool'}${full ? ' zone--full' : ''}" ` +
    `data-zone="${esc(key)}">` +
    `<div class="zone__head"><b>${esc(title)}</b>` +
    `<span class="zone__count">${slots > 0 ? `${ids.length}/${slots}` : `${ids.length} 人`}</span></div>` +
    (subtitle ? `<div class="zone__sub">${esc(subtitle)}</div>` : '') +
    `<div class="zone__body">${body}</div></div>`
  );
}

function boardHtml() {
  const size = teamSize();
  const zones = T.teams.map((t) =>
    zoneHtml(
      `team:${t.id}`,
      t.name || t.short || t.id,
      [t.short, t.group ? `${t.group} 组` : ''].filter(Boolean).join(' · '),
      t.playerIds,
      size
    )
  );
  zones.push(zoneHtml('pool', '候补池', '未编入队伍的选手', T.pool, 0));
  return `<div class="team-board">${zones.join('')}</div>`;
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
    `<div class="notice">队友随机分配后<strong>全程固定</strong>；每个组的人数可以不同（默认 ${size} 人），` +
    `在「重新随机组队」里可改人数或选择零头并入。赛程按当前队伍生成，因此调整队伍后需要重新「生成赛程」。</div>` +
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
/** 把选手拖到某支队伍或候补池；队伍满员时挤出一人回候补池。 */
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
    toast(`${team.short || team.id} 已满（${size} 人），${nameOf(out)} 回到候补池`, 'info', 4000);
  }
  team.playerIds.push(pid);
  refresh();
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

  log.debug('组队台拖拽已启用');
}

export function reset() {
  load();
  refresh();
  toast('已还原为当前保存的队伍', 'info');
}

export async function save() {
  const size = teamSize();
  const teams = T.teams.filter((t) => t.playerIds.length);
  if (!teams.length) {
    toast('至少要有一支队伍', 'warn');
    return;
  }
  const dropped = T.teams.length - teams.length;
  const uneven = teams.filter((t) => t.playerIds.length !== size);
  const notices = [
    dropped ? `${dropped} 支空队伍会被删除` : '',
    uneven.length
      ? `${uneven.length} 支队伍人数不是 ${size} 人（各队人数可以不同，但会以少打多）`
      : '',
    '队伍结构变化会清空当前赛程',
  ].filter(Boolean);
  if ((dropped || uneven.length) && !window.confirm(`${notices.join('；')}，继续保存？`)) {
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
    toast(`已保存 ${res.count} 支队伍`, 'ok');
    (res.warnings || []).forEach((w) => toast(w, 'warn', 6000));
    if (res.state) App.state = res.state;
    load();
    refresh();
  } catch (err) {
    toast(err.message, 'err');
  }
}
