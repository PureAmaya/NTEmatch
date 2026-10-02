/* 公开视图渲染：总览 / 赛程 / 选手 / 直播 / HUD。
 * 仅生成 DOM，不绑定事件（按钮统一以 data-act 标记，由 actions/app 委托）。
 */

import {
  App,
  canEdit,
  esc,
  fmtDuration,
  fmtFull,
  fmtRange,
  fmtTime,
  hooks,
  liveAvailable,
  log,
  qs,
  qsa,
  reveal,
  sign,
  stateKey,
} from './core.js';
import {
  MAIN_ROOM_ID,
  PUSH_TIP_LINE,
  avaHtml,
  boardHeadHtml,
  formChips,
  isLivePlayer,
  isMainLive,
  kpiCard,
  livePlayers,
  liveTag,
  mainRoom,
  privateOf,
  pushEndpointsOf,
  pushTipsHtml,
  rankCell,
  roundBadge,
  rulesPanelHtml,
  whoHtml,
} from './ui.js';
import { renderEventsView } from './events.js';
import { Live } from './live.js';

const STAGE_LABEL = { group: '小组赛', wb: '胜者组', lb: '败者组', gf: '总决赛' };
const PHASE_LABEL = {
  idle: '尚未生成赛程',
  group: '小组赛进行中',
  knockout: '淘汰赛进行中',
  finished: '赛事已结束',
};
const STAGE_FILTERS = [
  ['all', '全部阶段'],
  ['group', '小组赛'],
  ['wb', '胜者组'],
  ['lb', '败者组'],
  ['gf', '总决赛'],
];
const STATUS_FILTERS = [
  ['all', '全部状态'],
  ['pending', '待赛'],
  ['live', '进行中'],
  ['done', '已结束'],
];
const STREAM_MODE_LABEL = { auto: '自动', webrtc: 'WebRTC', hls: 'HLS', flv: 'FLV', embed: '网页内嵌' };

const LEAGUE_FILTERS = [
  ['all', '全部'],
  ['pending', '待赛'],
  ['live', '进行中'],
  ['done', '已结束'],
];
const AVG_TIP = '均分 = 总得分 ÷ 场次，排名按均分从高到低；积分记在实际出场的选手名下。';
const BOARD_HEAD_WIDE = [
  '#', '选手', '场次', '胜', '负', '净胜',
  ['均分', AVG_TIP], '近况', '总得分',
];
const BOARD_HEAD_NARROW = ['#', '选手', '场次', ['均分', AVG_TIP], '总分'];

const stageFilter = () => App.filter || 'all';
const statusFilter = () => App.status || 'all';
const statusBadge = (st) =>
  ({ pending: 'badge--pending', live: 'badge--live', done: 'badge--done' })[st] || 'badge--pending';

/** 当前赛事是积分制还是锦标赛制。 */
const isLeague = (s = App.state) => (s?.rules?.format || 'tournament') === 'league';

/** 时间状态文案（赛事与比赛共用同一套说法）。 */
const TIME_STATE_TEXT = {
  draft: '筹备中',
  unscheduled: '时间待定',
  upcoming: '未开赛',
  scheduled: '未开始',
  running: '进行中',
  finished: '已结束',
};

/** 按需填充面板：空内容则整块隐藏（两套赛制共用同一批容器）。 */
function setPanel(id, html) {
  const el = qs(`#${id}`);
  if (!el) return;
  el.hidden = !html;
  el.innerHTML = html || '';
}

/* ------------------------------- HUD ---------------------------------- */
export function applyTheme(s) {
  const accent = s.ui?.accent || 'cyan';
  if (document.documentElement.dataset.accent !== accent) {
    document.documentElement.dataset.accent = accent;
  }
}

export function renderHeader(s) {
  const evt = s.event || {};
  const titleEl = qs('#evtTitle');
  titleEl.textContent = evt.title || 'NTE 比赛';
  titleEl.dataset.text = titleEl.textContent;
  qs('#evtSub').textContent = evt.subtitle || 'NEVERNESS TO EVERNESS · MATCH';
  const chipEvent = qs('#chipEvent');
  if (chipEvent) {
    const name = s.eventName || evt.name || s.eventId || '—';
    const kind = isLeague(s) ? '积分制' : '锦标赛制';
    chipEvent.textContent = `${name} · ${kind}`;
    chipEvent.title = `正在看：${name}（${kind} · ${s.eventStatus || evt.status || 'active'}）`;
  }
  document.title = `${evt.name || evt.title || '赛事平台'} | NTE 比赛`;

  renderEventTimeChip(s);
  renderPastChip(s);
}

/**
 * 「往届回看」芯片：按链接（``/<届 ID>``）看某一届时显示，点一下回当前届。
 *
 * 有了它，一条链接就能看一届，也能随时看清「现在看的是哪一届」。
 */
function renderPastChip(s) {
  const chip = qs('#chipPast');
  if (!chip) return;
  if (!App.routeEvent) {
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  chip.textContent = `往届回看 · ${s.eventName || App.routeEvent}`;
  chip.title = `正在回看「${s.eventName || App.routeEvent}」；点这里回到主赛事`;
}

/* ---------------------------- 赛事时间 -------------------------------- */
/** 赛事时间的简短描述：已结束则给出起止时间，进行中则给出开始时间。 */
function eventTimeText(t) {
  if (!t) return '';
  if (t.state === 'finished') {
    if (t.startAt && t.endAt) return fmtRange(t.startAt, t.endAt);
    if (t.endAt) return `结束于 ${fmtTime(t.endAt)}`;
    if (t.startAt) return `${fmtTime(t.startAt)} 开始 · 结束未登记`;
    return '起止时间未登记';
  }
  if (t.state === 'running') return t.startAt ? `${fmtTime(t.startAt)} 起` : '开始时间未登记';
  if (t.state === 'upcoming') return t.startAt ? `${fmtTime(t.startAt)} 开赛` : '开赛时间未登记';
  return t.startAt ? `${fmtTime(t.startAt)} 开赛` : '时间待定';
}

/** 顶栏芯片：一眼看出本届是否已结束、以及起止时间。 */
function renderEventTimeChip(s) {
  const chip = qs('#chipEventTime');
  if (!chip) return;
  const t = s.eventTime;
  if (!t) {
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  chip.dataset.state = t.state;
  chip.textContent = `${TIME_STATE_TEXT[t.state] || ''} · ${eventTimeText(t)}`;
  chip.title = [
    `本届赛事：${TIME_STATE_TEXT[t.state] || ''}`,
    t.startAt ? `开始 ${fmtFull(t.startAt)}` : '开始时间未登记',
    t.endAt ? `结束 ${fmtFull(t.endAt)}` : '结束时间未登记',
    t.durationMinutes != null ? `用时 ${fmtDuration(t.durationMinutes)}` : '',
  ]
    .filter(Boolean)
    .join('｜');
}

/** 总览顶部的赛事时间条：状态 + 开始 / 结束 / 用时 + 赛程进度。 */
function eventTimePanelHtml(s) {
  const t = s.eventTime;
  if (!t) return '';
  if (!t.total && !t.startAt && !t.endAt) return ''; // 什么都没登记时不占版面
  const cells = [
    ['开始', t.startAt ? fmtFull(t.startAt) : '未登记'],
    ['结束', t.endAt ? fmtFull(t.endAt) : t.state === 'finished' ? '未登记' : '待定'],
    ['用时', t.durationMinutes != null ? fmtDuration(t.durationMinutes) : '—'],
    ['赛程', t.total ? `${t.done} / ${t.total} 场` : '未生成'],
  ];
  const showHint = t.state !== 'finished' && t.scheduleDone;
  return (
    `<div class="etime">` +
    `<span class="etime__pill etime__pill--${esc(t.state)}">${esc(TIME_STATE_TEXT[t.state] || '')}</span>` +
    `<div class="etime__cells">` +
    cells
      .map(
        ([k, v]) =>
          `<div class="etime__cell"><span>${esc(k)}</span><b>${esc(v)}</b></div>`
      )
      .join('') +
    `</div>` +
    (showHint
      ? `<div class="etime__note">赛程已全部结束，请在「管理 → 赛事信息」登记结束时间</div>`
      : '') +
    `</div>`
  );
}

/* ------------------------------ 总览 ---------------------------------- */
export function renderOverview(s) {
  if (isLeague(s)) {
    renderLeagueOverview(s);
    return;
  }
  renderTournamentOverview(s);
}

/* —— 锦标赛制总览：小组赛 + 双败对阵图 —— */
function renderTournamentOverview(s) {
  const fmt = s.format || {};
  const prog = s.progress || [];
  const done = prog.reduce((n, p) => n + p.done, 0);
  const total = prog.reduce((n, p) => n + p.total, 0);
  const live = (s.rounds || []).filter((r) => r.status === 'live').length;
  const percent = total ? Math.round((done / total) * 100) : 0;

  qs('#kpiRow').innerHTML = [
    kpiCard(
      '赛事进程',
      `${done}<small> / ${total}</small>`,
      `${PHASE_LABEL[s.phase] || '—'} · 进行中 ${live} 场`,
      percent
    ),
    kpiCard(
      '固定队伍',
      String(fmt.teams || 0),
      (fmt.size || 0) >= 4
        ? `淘汰赛 ${fmt.size} 强 · ${s.rules?.loserBracket === false ? '单败' : '双败'}淘汰`
        : '队伍不足 4 支'
    ),
    kpiCard(
      '小组赛',
      `${fmt.groupCount || 0} 组`,
      `${fmt.groupMatches || 0} 场单循环${fmt.groupStageDone ? ' · 已结束' : ''}`
    ),
    kpiCard('淘汰赛', `${fmt.knockoutMatches || 0} 场`, '胜者组冠军 vs 败者组冠军'),
  ].join('');

  renderNowPlaying(s);
  setPanel('eventTimePanel', eventTimePanelHtml(s));
  setPanel('championBox', championHtml(s));
  // 小组赛与淘汰赛合成一张「对阵总览」
  setPanel('bracketBoard', bracketPanelHtml(s));
  setPanel('standingsBoard', '');
  setPanel('formBoard', '');
  setPanel('rulesBoard', rulesPanelHtml(s));
}

/** 总决赛结束后的冠军横幅。 */
function championHtml(s) {
  const champ = s.champion;
  if (!champ) return '';
  const members = (champ.playerIds || [])
    .map((pid) => (s.players || []).find((p) => p.id === pid))
    .filter(Boolean);
  return (
    `<div class="champion">` +
    `<div class="champion__txt"><span class="champion__tag">总冠军</span>` +
    `<div class="champion__name">${esc(champ.name || champ.id)}</div>` +
    `<div class="champion__members">${members
      .map((p) => `<span class="champion__member">${avaHtml(p, 'xs')}${esc(p.name || p.id)}</span>`)
      .join('')}</div></div>` +
    `<span class="champion__cup" aria-hidden="true">CHAMPION</span>` +
    `</div>`
  );
}

/* —— 积分制总览：均分榜 + 选手近况 —— */
function renderLeagueOverview(s) {
  const prog = s.standings?.progress || { played: 0, total: 0, live: 0, pending: 0, percent: 0 };
  const leader = s.standings?.leader;
  const players = s.players || [];
  const subs = players.filter((p) => p.substitute).length;
  const quality = s.schedule || { partnerRepeats: 0, opponentRepeats: 0, groupRepeats: 0 };

  qs('#kpiRow').innerHTML = [
    kpiCard(
      '赛程进度',
      `${prog.played}<small> / ${prog.total}</small>`,
      `进行中 ${prog.live} · 待赛 ${prog.pending} · 积分制`,
      prog.percent
    ),
    leader
      ? kpiCard(
          '均分领先',
          `${leader.average ?? 0}<small> 均分</small>`,
          `${leader.player?.name || leader.playerId} · 总得分 ${leader.points} / ${leader.played} 场`
        )
      : kpiCard('均分领先', '—', '暂无已结算对局'),
    kpiCard(
      '参赛选手',
      String((s.participants || []).length),
      `替补 ${subs} 人 · 报名池 ${players.length} 人`
    ),
    kpiCard(
      '重复搭档',
      String(quality.partnerRepeats),
      `重复对手 ${quality.opponentRepeats} · 组合重复 ${quality.groupRepeats}`
    ),
  ].join('');

  renderNowPlaying(s);
  setPanel('eventTimePanel', eventTimePanelHtml(s));
  setPanel('championBox', '');
  setPanel('bracketBoard', '');
  setPanel('standingsBoard', leagueStandingsHtml(s));
  setPanel('formBoard', leagueFormHtml(s));
  setPanel('rulesBoard', rulesPanelHtml(s));
}

/** 积分榜（按均分排名，满 N 场才参与名次）。 */
function leagueStandingsHtml(s) {
  const rows = s.standings?.players || [];
  const wide = window.matchMedia('(min-width: 1024px)').matches;
  const canReveal = reveal();
  let body;
  if (!rows.length) {
    body = `<div class="empty"><b>暂无选手数据</b>请在管理端添加参赛选手</div>`;
  } else {
    body = rows
      .map((row) => {
        const qualified = row.qualified !== false;
        const top = qualified && row.rank <= 3 ? ` board__row--top${row.rank}` : '';
        const unranked = qualified ? '' : ' board__row--unranked';
        const avg = row.average ?? 0;
        const total = canReveal ? row.points : '—';
        const avgTip = !qualified
          ? `${row.played} 场 · 未达门槛`
          : canReveal
            ? `${row.winRate}% 胜率`
            : '已封存';
        const cells = wide
          ? `<div class="score-cell">${row.played}</div>` +
            `<div class="score-cell">${row.win}</div>` +
            `<div class="score-cell">${row.lose}</div>` +
            `<div class="score-cell">${canReveal ? sign(row.diff) : '—'}</div>` +
            `<div class="points-cell">${canReveal ? avg : '—'}<small>${esc(avgTip)}</small></div>` +
            `<div>${formChips(row.form)}</div>` +
            `<div class="score-cell score-cell--total">${total}</div>`
          : `<div class="score-cell">${row.played}</div>` +
            `<div class="points-cell">${canReveal ? avg : '—'}<small>${esc(avgTip)}</small></div>` +
            `<div class="score-cell score-cell--total">${total}</div>`;
        return `<div class="board__row${top}${unranked}">${rankCell(row.rank)}${whoHtml(row.player)}${cells}</div>`;
      })
      .join('');
  }
  const minRank = s.standings?.minRankPlayed ?? s.rules?.minRankPlayed ?? 5;
  return (
    `<div class="panel__head"><h2>积分榜</h2>` +
    `<span class="panel__hint">按均分排名 · 满 ${minRank} 场参与排名 · ` +
    `胜 ${s.rules?.pointsWin ?? 3} / 负 ${s.rules?.pointsLose ?? 0} / 平 ${s.rules?.pointsDraw ?? 1}</span></div>` +
    `<div class="panel__body panel__body--flush">${boardHeadHtml(
      wide ? BOARD_HEAD_WIDE : BOARD_HEAD_NARROW
    )}${body}</div>`
  );
}

function leagueFormHtml(s) {
  const rows = (s.standings?.players || []).filter((r) => r.played > 0);
  const body = rows.length
    ? `<div class="duo__members">${rows
        .map((r) => {
          const name = r.player?.name || r.playerId;
          return (
            `<div class="member">${avaHtml(r.player || { name }, 'xs')}` +
            `<span class="member__name">${esc(name)}</span>${formChips(r.form)}</div>`
          );
        })
        .join('')}</div>`
    : `<div class="empty"><b>暂无对战记录</b>完成对局后显示近况</div>`;
  return (
    `<div class="panel__head"><h2>选手近况</h2><span class="panel__hint">最近 6 局战绩</span></div>` +
    `<div class="panel__body">${body}</div>`
  );
}

/* —— 树状淘汰赛对阵图 ——
 *
 * 全部坐标（列宽 / 行距 / 连线折点）都在 JS 里算成整数像素后写进内联 style，
 * CSS 只负责配色与边框，避免「JS 算的坐标」和「CSS 里的尺寸」互相脱节。
 *
 * 父子关系以对局自带的 ``srcA`` / ``srcB`` 为准（如 WB-2-1 的 srcA = ``WB-1-1:W``），
 * 所以单败 / 双败、4 强到 32 强都是同一套代码：拿到谁传给谁，就按它排。
 */
const BT = {
  box: 250,       // 对阵框宽（要放得下：两位选手头像 + 队名 + 小组战绩 + 比分）
  boxH: 118,      // 对阵框高（顶栏 + 两行「头像 + 队名」，留 1~2px 余量不裁切）
  gapX: 42,       // 列间距（连线走这里；收窄一点，八强双败常见宽度下刚好不用横向滚动）
  champ: 184,     // 冠军框宽
  head: 42,       // 每条带上方：带标题 + 列标题占用的纵向空间
  bandGap: 70,    // 胜者组与败者组之间的空隙
  labelH: 24,     // 列标题行高
  // 画布四周的固定留白：**加在内容坐标里**，不是加在滚动容器的 padding 上。
  // 滚动容器自己的 padding 在滚到两端时会被浏览器吞掉（框就贴边/被裁），
  // 把留白算进内容以后，无论滚到哪里，框离容器边缘都有这一圈间距。
  pad: 16,
  bandBleed: 12,  // 带底块比框左右各外扩多少（仍要小于 pad，不然会被裁掉）
};

/** 叶子行距：至少要容得下一个对阵框，否则相邻两场会叠在一起。 */
function leafGapOf() {
  return BT.boxH + 10;
}

/** 把 bracket 的三段整理成「列 + 节点」；单败时胜者组与决赛算同一条带。 */
function treeNodes({ wb, lb, gf, single }) {
  const byCode = new Map();
  const cols = [];
  const bandOf = { wb: single ? 'main' : 'wb', lb: 'lb', gf: single ? 'main' : 'gf' };
  const push = (list, stage) => {
    (list || []).forEach((col) => {
      const nodes = (col.matches || []).map((m, i) => {
        const node = { m, band: bandOf[stage], stage, x: 0, y: 0, kids: [], slot: i + 1 };
        byCode.set(m.code, node);
        return node;
      });
      cols.push({ title: col.title || '', band: bandOf[stage], stage, nodes, x: 0 });
    });
  };
  push(wb, 'wb');
  push(lb, 'lb');
  push(gf, 'gf');

  // 连父子：只连同一条带内的对局；总决赛（band = gf）允许跨带汇聚两组冠军。
  // 胜者组落败者掉进败者组这类跨带引用不画线（否则满屏长线），
  // 它们的来源由对阵框里的「席位来源」文案体现（如「WB-1-1 败者」）。
  cols.forEach((col) =>
    col.nodes.forEach((node) => {
      [node.m.srcA, node.m.srcB].forEach((ref) => {
        const kid = byCode.get(String(ref || '').split(':')[0]);
        if (!kid) return;                      // seed:3 / 组内名次 → 没有上游对局
        if (kid.band !== node.band && node.band !== 'gf') return;
        node.kids.push({ node: kid, win: String(ref || '').endsWith(':W') });
      });
    })
  );
  return { byCode, cols };
}

const mid = (list) => (list.length ? list.reduce((n, v) => n + v, 0) / list.length : NaN);

/** 计算每列的 x 与每个节点的 y（叶子均分、父节点取两个孩子的中点）。 */
function layoutTree(cols, single) {
  const pitch = BT.box + BT.gapX;
  const gap = leafGapOf(cols);
  const bands = single ? ['main'] : ['wb', 'lb', 'gf'];
  const top = {};
  // 起点整体右下移 BT.pad：这一圈留白跟着内容走，滚动时不会被吞
  let cursor = BT.pad + BT.head;

  bands.forEach((band) => {
    const bandCols = cols.filter((c) => c.band === band);
    if (!bandCols.length) return;
    top[band] = cursor;
    const leafTop = cursor;
    bandCols.forEach((col, ci) => {
      // 总决赛落在「胜者组冠军」与「败者组冠军」的中点，正好夹在两条带之间
      col.x =
        BT.pad +
        (band === 'gf'
          ? Math.max(
              ...bands.filter((b) => b !== 'gf').map((b) => cols.filter((c) => c.band === b).length)
            ) * pitch
          : ci * pitch);
      col.nodes.forEach((node, i) => {
        node.x = col.x;
        const kidY = node.kids.map((k) => k.node.y).filter((v) => Number.isFinite(v));
        node.y = kidY.length ? mid(kidY) : leafTop + i * gap;
      });
    });
    const leaves = bandCols[0].nodes.length;
    cursor += (leaves - 1) * gap + BT.boxH + BT.bandGap;
  });

  // 冠军框接在最后一列右侧（单败 = 决赛列，双败 = 总决赛列）
  const lastCols = single ? cols : cols.filter((c) => c.band === 'gf');
  const last = lastCols[lastCols.length - 1];
  const champX = (last ? last.x : BT.pad) + BT.box + BT.gapX + 14;
  const champY = last && last.nodes[0] ? last.nodes[0].y : BT.pad + BT.head;

  const nodes = cols.flatMap((c) => c.nodes);
  return {
    top,
    nodes,
    cols,
    champX,
    champY,
    width: champX + BT.champ + BT.pad,
    height: Math.max(...nodes.map((n) => n.y + BT.boxH), 0) + BT.pad,
  };
}

const BT_BAND_META = {
  main: ['淘汰赛', '输一场即淘汰，自左向右逐级汇入决赛'],
  wb: ['胜者组', '输一场掉进败者组'],
  lb: ['败者组', '再输一场即淘汰'],
};

function bracketTreeHtml(s, { wb, lb, gf, single }) {
  const { cols } = treeNodes({ wb, lb, gf, single });
  const geo = layoutTree(cols, single);
  // 对阵图里的队伍只有 id / 缩写：颜色、头像、小组战绩都要回公开状态里取
  const colors = new Map((s.teams || []).map((t) => [t.id, t.color]));
  const players = new Map((s.players || []).map((p) => [p.id, p]));
  const groups = new Map();
  (s.groups || []).forEach((g) =>
    (g.rows || []).forEach((row) => row.teamId && groups.set(row.teamId, row))
  );
  const colorOf = (side) => side.color || colors.get(side.teamId) || 'var(--accent)';
  const ctx = {
    colorOf,
    playerOf: (pid) => players.get(pid) || null,
    // 第二行小字：小组 + 战绩（这就是「小组信息 / 成绩」），没有队伍时退回席位来源
    infoOf: (side) => {
      const row = side.teamId ? groups.get(side.teamId) : null;
      if (!row) return side.source || '';
      const bits = [`${row.group || 'A'} 组`];
      if (row.played) bits.push(`${row.win}胜${row.lose}负`);
      return bits.join(' · ');
    },
  };

  // 每条带的底色块 + 标题（总决赛列不铺块，它夹在两条带之间）
  const bandHtml = (single ? ['main'] : ['wb', 'lb'])
    .map((band) => {
      const bandCols = geo.cols.filter((c) => c.band === band);
      if (!bandCols.length) return '';
      const [title, note] = BT_BAND_META[band];
      // 带底块比框左右各外扩 bandBleed，但整体仍在画布留白之内
      const left = BT.pad - BT.bandBleed;
      const right = bandCols[bandCols.length - 1].x + BT.box + BT.bandBleed;
      const bottom = Math.max(...bandCols.flatMap((c) => c.nodes.map((n) => n.y + BT.boxH)));
      return (
        `<div class="btree__band btree__band--${band}" style="left:${left}px;top:${geo.top[band] - BT.head}px;` +
        `width:${right - left}px;height:${bottom - geo.top[band] + BT.head + BT.bandBleed}px"></div>` +
        `<div class="btree__band-label btree__band-label--${band}" ` +
        `style="left:${BT.pad - 4}px;top:${geo.top[band] - BT.head + 4}px">` +
        `<b>${esc(title)}</b><span>${esc(note)}</span></div>`
      );
    })
    .join('');

  // 连线：孩子右缘 → 折向中点 → 竖线 → 父节点左缘（标准对阵表折线）
  const paths = [];
  geo.cols.forEach((col) =>
    col.nodes.forEach((node) => {
      node.kids.forEach((kid) => {
        const x1 = kid.node.x + BT.box;
        const x2 = node.x;
        const y1 = kid.node.y + BT.boxH / 2;
        const y2 = node.y + BT.boxH / 2;
        const mx = x2 - BT.gapX / 2;
        const cls =
          `btree__link${kid.node.m.status === 'live' ? ' btree__link--live' : ''}` +
          `${kid.node.m.status === 'done' ? ' btree__link--won' : ''}`;
        paths.push(`<path class="${cls}" d="M${x1} ${y1}H${mx}V${y2}H${x2}"/>`);
      });
    })
  );

  const colLabels = geo.cols
    .map((col) => {
      const y = col.band === 'gf' && !single ? col.nodes[0].y - BT.labelH - 4 : geo.top[col.band] - BT.labelH;
      return (
        `<div class="btree__col-label" style="left:${col.x}px;top:${y}px;width:${BT.box}px">` +
        `${esc(col.title)}</div>`
      );
    })
    .join('');

  return (
    `<div class="btree" style="width:${geo.width}px;height:${geo.height}px">` +
    bandHtml +
    `<svg class="btree__svg" width="${geo.width}" height="${geo.height}" ` +
    `viewBox="0 0 ${geo.width} ${geo.height}" aria-hidden="true">${paths.join('')}</svg>` +
    colLabels +
    geo.nodes.map((node) => treeBoxHtml(node, ctx)).join('') +
    treeChampHtml(s, geo.champX, geo.champY) +
    `</div>`
  );
}

function treeBoxHtml(node, ctx) {
  const m = node.m;
  const sides = (m.sides || []).slice(0, 2);
  const ready = sides.length === 2 && sides.every((side) => side.teamId);
  // 管理端且双方已就位时，整框可点 → 直接录比分（弃权在录分弹窗里）
  const actionable = canEdit() && ready && m.status !== 'done';
  const act = actionable
    ? ` data-act="round-result" data-code="${esc(m.code)}" role="button" tabindex="0"`
    : '';
  // 空席位平时不写字，把「这一席等谁」放进整框的悬停提示里
  const slots = sides
    .filter((side) => !side.teamId && side.source)
    .map((side) => side.source)
    .join(' / ');
  const tip = `${m.label || m.code}${slots ? ` · 等待 ${slots}` : ''}${actionable ? ' · 点击录入比分' : ''}`;
  return (
    `<div class="btree__box btree__box--${esc(m.status)}${actionable ? ' btree__box--act' : ''}" ` +
    `style="left:${node.x}px;top:${node.y}px;width:${BT.box}px;height:${BT.boxH}px" ` +
    `data-code="${esc(m.code)}" title="${esc(tip)}"${act}>` +
    `<div class="btree__top"><span class="btree__code">${esc(m.code)}</span>` +
    // 对阵图节点只有简略字段，回公开状态里取同一场来数「在播机位」
    `${liveBadgeHtml(roundByCode(m.code) || m)}` +
    (m.status === 'done' && m.duration
      ? `<span class="btree__top-info" title="用时">${esc(fmtDuration(m.duration))}</span>`
      : '') +
    `<span class="badge ${statusBadge(m.status)}">` +
    `${esc({ pending: '待赛', live: '进行中', done: '已结束' }[m.status] || m.status)}</span></div>` +
    sides.map((side, i) => treeSideHtml(side, String.fromCharCode(65 + i), m, ctx)).join('') +
    `</div>`
  );
}

/** 树状图里的一方：选手头像 + 队名 + 小组战绩 + 比分。 */
function treeSideHtml(side, key, rnd, ctx) {
  const decided = rnd.status === 'done';
  // 谁打这一席还没定下来：留空。不写「待定」「A 组第 1」「A 队」这类占位，
  // 席位来源放在整个方框的 title 上（悬停可见，平时不干扰）。
  if (!side.teamId) return `<div class="btree__side btree__side--empty"></div>`;
  const members = (side.playerIds || []).map((pid) => ctx.playerOf(pid)).filter(Boolean);
  const label = side.label || side.teamId;
  const info = ctx.infoOf(side);
  const cls =
    `btree__side${decided && rnd.winner === key ? ' btree__side--win' : ''}` +
    `${side.forfeit ? ' btree__side--fo' : ''}`;
  return (
    `<div class="${cls}">` +
    `<span class="btree__avas">` +
    (members.length
      ? members.map((p) => avaHtml(p, 'xs')).join('')
      : `<i class="btree__dot" style="background:${esc(ctx.colorOf(side))}"></i>`) +
    `</span>` +
    `<span class="btree__who">` +
    `<b class="btree__name" title="${esc(label)}">${esc(label)}</b>` +
    (info ? `<i class="btree__info" title="${esc(info)}">${esc(info)}</i>` : '') +
    `</span>` +
    `<b class="btree__score">${decided ? side.score : ''}</b></div>`
  );
}

/** 树状图最右端的冠军框（决赛没打完就只留框，不写占位名字）。 */
function treeChampHtml(s, x, y) {
  const champ = s.champion;
  const name = champ ? champ.short || champ.name || champ.id : '';
  return (
    `<div class="btree__champ${champ ? ' btree__champ--on' : ''}" ` +
    `style="left:${x}px;top:${y}px;width:${BT.champ}px;height:${BT.boxH}px" ` +
    `title="${esc(name || '决赛打完后揭晓')}">` +
    `<span class="btree__champ-tag">CHAMPION</span>` +
    `<b class="btree__champ-name">${esc(name)}</b></div>`
  );
}

/* —— 对阵总览：小组赛（分组排名）+ 淘汰赛树状图（导出便于用 Node 验证布局）—— */
export function bracketPanelHtml(s) {
  const b = s.bracket || {};
  const wb = b.wb || [];
  const lb = b.lb || [];
  const gf = ((b.gf || [])[0] || {}).matches || [];
  // 单败 / 双败看赛制开关；历史数据没这个字段时按双败渲染
  const single = s.rules?.loserBracket === false;
  const groups = groupSectionHtml(s);
  const hasTree = Boolean(wb.length || lb.length || gf.length);
  const hint = hasTree
    ? `${single ? '单败淘汰 · 输一场即淘汰' : '双败淘汰 · 胜者组落败者进败者组'}` +
      `${canEdit() ? ' · 点方框录比分' : ''} · 图可横向滚动`
    : '小组赛分组排名 → 淘汰赛对阵';
  // 淘汰赛骨架（还没生成时给一句说明，而不是留白）
  const tree = hasTree
    ? `<h3 class="btree__sec">淘汰赛</h3><div class="btree-wrap">` +
      bracketTreeHtml(s, { wb, lb, gf: ((b.gf || [])[0] ? [{ ...b.gf[0], matches: gf }] : []), single }) +
      `</div>`
    : `<h3 class="btree__sec">淘汰赛</h3><div class="empty">` +
      (groups
        ? `<b>小组赛结束后生成</b>按小组赛总排名取前 ${s.format?.size || 0} 名进入淘汰赛`
        : `<b>尚未生成赛程</b>确定参与名单后执行「随机组队 → 生成赛程」`) +
      `</div>`;
  return (
    `<div class="panel__head"><h2>对阵总览</h2><span class="panel__hint">${esc(hint)}</span></div>` +
    `<div class="panel__body">${groups}${tree}</div>`
  );
}

/* —— 小组赛积分表 + 晋级顺位（作为「对阵总览」里的一段，不再单独占面板）—— */
function groupSectionHtml(s) {
  const groups = s.groups || [];
  if (!groups.length) return '';
  const ranking = s.ranking || [];
  const perMatch = Number(s.rules?.teamsPerMatch) || 2;
  const shape = perMatch === 2 ? '组 vs 组' : `${perMatch} 队同场`;
  const head =
    `<h3 class="btree__sec">小组赛` +
    `<span class="panel__hint">${groups.length} 组轮转 · 每场 ${shape} · 按名次分 / 净胜分排名 · ` +
    `前 ${s.format?.size || 0} 名晋级</span></h3>`;
  const advMap = new Map(ranking.map((r) => [r.team.id, r]));
  const tables = groups
    .map(
      (g) =>
        `<div class="gtable"><div class="gtable__head"><b>${esc(g.key)} 组</b>` +
        `<span>${(g.rows || []).length} 支队 · 每场 ${shape}</span></div>` +
        `<div class="gtable__cols"><div>#</div><div>队伍</div><div>场次</div><div>胜</div><div>负</div>` +
        `<div title="第 1 名得分最高">名次分</div><div>净胜</div><div>名次</div></div>` +
        (g.rows || [])
          .map((row) => {
            const adv = advMap.get(row.teamId);
            return (
              `<div class="gtable__row${row.rank === 1 ? ' gtable__row--top' : ''}">` +
              `<div class="gtable__rank">${row.rank}</div>` +
              `<div class="gtable__team"><i style="background:${esc(row.color || 'var(--accent)')}"></i>` +
              `<span title="${esc(row.name)}">${esc(row.short || row.name)}</span>` +
              `${perMatch > 2 && row.bestRank ? `<small class="gtable__best">最好 #${row.bestRank}</small>` : ''}` +
              `</div>` +
              `<div>${row.played}</div><div>${row.win}</div><div>${row.lose}</div>` +
              `<div class="gtable__pts">${row.placement ?? 0}</div>` +
              `<div>${sign(row.diff)}</div>` +
              `<div>${adv && adv.advanced ? `<span class="badge badge--done">晋级 #${adv.seed}</span>` : '<span class="panel__hint">—</span>'}</div>` +
              `</div>`
            );
          })
          .join('') +
        `</div>`
    )
    .join('');
  const order =
    `<div class="gorder"><div class="gorder__title">晋级顺位</div><div class="gorder__list">` +
    (ranking.length
      ? ranking
          .map(
            (r) =>
              `<span class="gorder__item${r.advanced ? ' gorder__item--in' : ''}">` +
              `<b>${r.seed}</b>${esc(r.team.short || r.team.name)}</span>`
          )
          .join('')
      : '<span class="panel__hint">小组赛结束后生成</span>') +
    `</div></div>`;
  return head + `<div class="gtables">${tables}</div>` + order;
}

/* 多组并行：以卡片列出所有正在进行的对局 */

/**
 * 本场在播的机位数。
 *
 * 判定只有一条：**设置了推流流名，且媒体服务器确认在推流**（``isLivePlayer``）。
 * 光配了流名、人还没开播的不算——界面上因此不会出现点了没反应的「直播入口」。
 */
const roundLiveCams = (rnd) =>
  (rnd?.streams?.cast || []).filter((c) => isLivePlayer(c.playerId)).length;

function renderNowPlaying(s) {
  const host = qs('#nowPlaying');
  const live = (s.rounds || []).filter((r) => r.status === 'live');
  if (!live.length) {
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  const streaming = live.reduce((n, r) => n + roundLiveCams(r), 0);
  host.innerHTML =
    `<div class="panel__head"><h2>正在进行</h2>` +
    // 没人真在推流时，一个字都不提「直播」
    `<span class="panel__hint">${[`${live.length} 场并行`, streaming ? `${streaming} 路直播中` : '']
      .filter(Boolean)
      .join(' · ')}</span></div>` +
    `<div class="panel__body"><div class="now-grid">${live.map((r) => nowCardHtml(r)).join('')}</div></div>`;
}

function nowSideHtml(side) {
  const names = side.players.map((p) => esc(p.name || p.id)).join(' / ') || '—';
  return (
    `<span class="vs-line__side" style="--c:${esc(side.color || 'var(--accent)')}">` +
    `<b>${esc(side.label)}</b>${names}</span>`
  );
}

function nowCardHtml(r) {
  const sides = r.sides || [r.sideA, r.sideB];
  const all = sides.flatMap((side) => side.players || []);
  // 只有**真的在推流**的选手才给观看入口；一个人都没推就不摆这一行
  const ops = all
    .filter((p) => isLivePlayer(p.id))
    .map(
      (p) =>
        `<button class="btn btn--sm btn--primary" type="button" ` +
        `data-act="watch" data-pid="${esc(p.id)}">直播中 · ${esc(p.name || p.id)}</button>`
    )
    .join('');
  const cast =
    r.liveNote && (roundLiveCams(r) || canEdit())
      ? `<div class="now-card__cast">直播提示：${esc(r.liveNote)}</div>`
      : '';
  return (
    `<div class="now-card">` +
    `<div class="now-card__head"><span class="round__no">${esc(r.label || r.code)}</span>` +
    `${liveBadgeHtml(r)}${roundBadge('live')}</div>` +
    roundTimeHtml(r) +
    `<div class="now-card__vs">${sides.map((side) => nowSideHtml(side)).join('<span class="vs-line__vs">VS</span>')}</div>` +
    cast +
    `<div class="now-card__ops">${ops}</div>` +
    `</div>`
  );
}

/* ------------------------------ 赛程 ---------------------------------- */
export function renderSchedule(s) {
  renderScheduleTools(s);
  renderScheduleGrid(s);
}

function renderScheduleTools(s) {
  if (isLeague(s)) return renderLeagueTools(s);
  const stageBtns = STAGE_FILTERS.map(
    ([key, label]) =>
      `<button class="btn btn--sm${stageFilter() === key ? ' btn--primary' : ''}" ` +
      `type="button" data-act="filter" data-filter="${key}">${label}</button>`
  ).join('');
  const statusBtns = STATUS_FILTERS.map(
    ([key, label]) =>
      `<button class="btn btn--sm${statusFilter() === key ? ' btn--primary' : ''}" ` +
      `type="button" data-act="status-filter" data-status="${key}">${label}</button>`
  ).join('');
  const adminOps = canEdit()
    ? `<div class="tool-group" style="margin-left:auto">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="tournament-generate">生成赛程</button>` +
      `<button class="btn btn--sm" type="button" data-act="reload">同步配置</button></div>`
    : '';
  const done = (s.rounds || []).filter((r) => r.status === 'done').length;
  qs('#scheduleTools').innerHTML =
    `<div class="tool-group" role="group" aria-label="阶段筛选">${stageBtns}</div>` +
    `<div class="tool-group" role="group" aria-label="状态筛选">${statusBtns}` +
    `<span class="tool-group__sep"></span>` +
    `<span class="panel__hint">共 ${(s.rounds || []).length} 场 · 已完成 ${done} 场` +
    ` · 小组赛 ${s.format?.groupMatches || 0} · 淘汰赛 ${s.format?.knockoutMatches || 0}</span></div>` +
    adminOps;
}

function filterRounds(s) {
  const stage = stageFilter();
  const status = statusFilter();
  return (s.rounds || []).filter(
    (r) => (stage === 'all' || r.stage === stage) && (status === 'all' || r.status === status)
  );
}

export function renderScheduleGrid(s) {
  if (isLeague(s)) return renderLeagueGrid(s);
  const host = qs('#scheduleGrid');
  const rounds = filterRounds(s);
  const all = s.rounds || [];
  // 还没定下谁打这一席的场次不出卡片（对阵骨架看总览的「对阵总览」）
  const decided = (r) => (r.sides || []).every((side) => side.teamId);
  const ready = rounds.filter(decided);
  const pending = rounds.length - ready.length;
  const pendingHtml = (n) =>
    `<div class="panel"><div class="empty"><b>${n} 场对阵还没定</b>` +
    `上游比赛出结果后自动出现在这里；对阵骨架见总览的「对阵总览」</div></div>`;
  if (!ready.length) {
    host.innerHTML = rounds.length
      ? pendingHtml(pending)
      : all.length
        ? `<div class="panel"><div class="empty"><b>当前筛选没有比赛</b>切换阶段或状态筛选查看其它比赛</div></div>`
        : `<div class="panel"><div class="empty"><b>尚未生成赛程</b>${
            canEdit() ? '在管理端执行「随机组队 → 生成赛程」' : '请等待管理员生成赛程'
          }</div></div>`;
    return;
  }
  host.innerHTML =
    ['group', 'wb', 'lb', 'gf']
    .filter((stage) => ready.some((r) => r.stage === stage))
    .map((stage) => {
      const items = ready.filter((r) => r.stage === stage);
      // 关键：**先按阶段过滤，再按轮次分组**。若先按轮次分好组再挑，
      // 小组赛第 1 轮会把胜者组第 1 轮（半决赛）一起吸进来——出现
      // 「小组赛里冒出半决赛」的错乱。
      const byRound = new Map();
      items.forEach((r) => {
        const key = r.bracketRound || 0;
        if (!byRound.has(key)) byRound.set(key, []);
        byRound.get(key).push(r);
      });
      const buckets = [...byRound.entries()].sort((a, b) => a[0] - b[0]);
      const done = items.filter((r) => r.status === 'done').length;
      return (
        `<section class="panel stage-block" data-stage="${stage}">` +
        `<div class="panel__head"><h2>${esc(STAGE_LABEL[stage])}</h2>` +
        `<span class="panel__hint">${items.length} 场 · 已完成 ${done} 场</span></div>` +
        `<div class="panel__body">` +
        buckets
          .map(
            ([, rows]) =>
              `<div class="stage-round"><div class="stage-round__title">` +
              `<b>${esc(roundBlockTitle(rows))}</b>` +
              `<span>${rows.length} 场 · 已完成 ${rows.filter((r) => r.status === 'done').length} 场</span>` +
              `</div><div class="match-grid">${rows.map((r) => matchCardHtml(s, r)).join('')}</div></div>`
          )
          .join('') +
        `</div></section>`
      );
    })
    .join('') +
    (pending ? pendingHtml(pending) : '');
}

/**
 * 一轮的标题（动态分类）：
 *
 * * 小组赛：同一轮里只有一组时 ``A 组 · 第 1 轮``；**多组并行**时写
 *   ``小组赛 · 第 1 轮``（否则标题写着 A 组，格子里却是 A/B 两组的比赛）；
 * * 胜者组：``十六强`` / ``八强`` / ``半决赛`` / ``胜者组决赛``；
 * * 败者组：``败者组第 N 轮`` / ``败者组决赛``；
 * * 总决赛：``总决赛``。
 */
function roundBlockTitle(rows) {
  const first = rows[0] || {};
  const parts = (first.label || '').split(' · ');
  const fallback = `第 ${first.bracketRound || 1} 轮`;
  if (first.stage === 'group') {
    const groups = [...new Set(rows.map((r) => (r.label || '').split(' · ')[0]).filter(Boolean))];
    const round = parts[1] || fallback;
    if (groups.length > 1) return `小组赛 · ${round}`;
    return `${groups[0] || parts[0] || 'A 组'} · ${round}`;
  }
  return parts[0] || fallback;
}

/* —— 积分制赛程：逐局卡片（可点击选手换人 / 空位补人） —— */
/** 积分制的筛选是「状态」维度；切赛制后遗留的阶段筛选值一律回落到 all。 */
const leagueFilter = () =>
  ['all', 'pending', 'live', 'done'].includes(stageFilter()) ? stageFilter() : 'all';

function renderLeagueTools(s) {
  const filters = LEAGUE_FILTERS.map(
    ([key, label]) =>
      `<button class="btn btn--sm${leagueFilter() === key ? ' btn--primary' : ''}" ` +
      `type="button" data-act="filter" data-filter="${key}">${label}</button>`
  ).join('');
  const adminOps = canEdit()
    ? `<div class="tool-group" style="margin-left:auto">` +
      `<button class="btn btn--sm btn--primary" type="button" data-act="schedule-generate">生成赛程</button>` +
      `<button class="btn btn--sm" type="button" data-act="schedule-append">追加补赛</button>` +
      `<button class="btn btn--sm" type="button" data-act="round-append">追加空局</button>` +
      // 允许一场不剩：清空后可以重新生成
      ((s.rounds || []).length
        ? `<button class="btn btn--sm btn--danger" type="button" data-act="rounds-clear">清空全部比赛</button>`
        : '') +
      `<button class="btn btn--sm" type="button" data-act="reload">同步配置</button></div>`
    : '';
  const q = s.schedule || { partnerRepeats: 0, opponentRepeats: 0 };
  const played = s.standings?.progress?.played || 0;
  qs('#scheduleTools').innerHTML =
    `<div class="tool-group" role="group" aria-label="赛程筛选">${filters}` +
    `<span class="tool-group__sep"></span>` +
    `<span class="panel__hint">共 ${(s.rounds || []).length} 局 · 已结算 ${played} 局` +
    ` · 重复搭档 ${q.partnerRepeats} · 重复对手 ${q.opponentRepeats}</span></div>` +
    adminOps;
}

function renderLeagueGrid(s) {
  const host = qs('#scheduleGrid');
  const status = leagueFilter();
  const rounds = (s.rounds || []).filter((r) => status === 'all' || r.status === status);
  if (!rounds.length) {
    host.innerHTML = (s.rounds || []).length
      ? `<div class="panel"><div class="empty"><b>当前筛选无对局</b>切换筛选条件查看其它局</div></div>`
      : `<div class="panel"><div class="empty"><b>尚未生成赛程</b>${
          canEdit() ? '点击「生成赛程」按参与名单自动分组' : '请等待管理员生成赛程'
        }</div></div>`;
    return;
  }
  const q = s.schedule || { partnerRepeats: 0 };
  const notice = q.partnerRepeats
    ? `<div class="notice notice--warn" style="grid-column:1/-1">检测到 ${q.partnerRepeats} 组重复搭档，` +
      `可重新「生成赛程」或「追加补赛」改善排布。</div>`
    : '';
  host.innerHTML = `<div class="round-grid">${notice}${rounds
    .map((r) => leagueRoundCardHtml(s, r))
    .join('')}</div>`;
}

function leagueSideClass(side, rnd) {
  if (rnd.status !== 'done' || !reveal() || rnd.winner === 'DRAW') return '';
  return rnd.winner === side.key ? ' side--win' : ' side--lose';
}

function leagueMembersHtml(s, rnd, side) {
  const teamSize = Math.max(1, s.rules?.teamSize || 1);
  const editable = canEdit();
  const items = [];
  for (let i = 0; i < teamSize; i += 1) {
    const p = side.players[i];
    if (p) {
      items.push(
        `<button type="button" class="member${editable ? ' member--editable' : ''}${p.substitute ? ' member--sub' : ''}" ` +
          `data-act="member" data-code="${esc(rnd.code)}" data-side="${side.key}" data-pid="${esc(p.id)}"` +
          (editable ? '' : ' disabled') +
          `>${avaHtml(p, 'xs')}<span class="member__name">${esc(p.name || p.id)}</span>` +
          (isLivePlayer(p.id) ? liveTag('直播') : '') +
          (p.substitute ? '<span class="member__sub">替补</span>' : '') +
          `</button>`
      );
    } else if (editable) {
      items.push(
        `<button type="button" class="member member--editable member--empty" ` +
          `data-act="empty-slot" data-code="${esc(rnd.code)}" data-side="${side.key}">` +
          `<span class="member__name">+ 空位</span></button>`
      );
    } else {
      items.push(`<span class="member member--empty"><span class="member__name">+ 空位</span></span>`);
    }
  }
  return `<div class="duo__members">${items.join('')}</div>`;
}

function leagueDuoHtml(s, rnd, side) {
  const winTag =
    rnd.status === 'done' && reveal() && rnd.winner === side.key ? `<span class="win-tag">WIN</span>` : '';
  return (
    `<div class="duo${leagueSideClass(side, rnd)}" data-side="${side.key}">` +
    `<div class="duo__label"><i style="background:${esc(side.color || 'var(--accent)')}"></i>` +
    // 还没排定阵容时不写「A 队 / B 队」这种占位，留空
    `${esc(side.label || '')}${winTag}</div>${leagueMembersHtml(s, rnd, side)}</div>`
  );
}

const leagueScore = (rnd, side) => (reveal() || rnd.status !== 'done' ? side.score : '–');

function leagueOpsHtml(rnd) {
  if (!canEdit()) return '';
  const ops = [];
  if (rnd.status === 'pending') {
    ops.push(
      `<button class="btn btn--sm btn--primary" type="button" data-act="round-status" ` +
        `data-code="${esc(rnd.code)}" data-status="live">开始</button>`
    );
  } else if (rnd.status === 'live') {
    ops.push(
      `<button class="btn btn--sm btn--ok" type="button" data-act="round-status" ` +
        `data-code="${esc(rnd.code)}" data-status="done">结束</button>`
    );
  }
  ops.push(
    `<button class="btn btn--sm" type="button" data-act="round-times" data-code="${esc(rnd.code)}">时间</button>` +
      `<button class="btn btn--sm" type="button" data-act="round-result" data-code="${esc(rnd.code)}">录入比分</button>`
  );
  if (rnd.status !== 'pending') {
    ops.push(
      `<button class="btn btn--sm" type="button" data-act="round-reset" data-code="${esc(rnd.code)}">重置</button>`
    );
  }
  ops.push(
    `<button class="btn btn--sm btn--danger" type="button" data-act="round-delete" data-code="${esc(rnd.code)}">删除本局</button>` +
      `<span class="panel__hint">点击选手换人 / 移出 · 空位补人</span>`
  );
  return `<div class="round__ops">${ops.join('')}</div>`;
}

function leagueRoundCardHtml(s, rnd) {
  const cls = rnd.status === 'live' ? ' round--live' : rnd.status === 'done' ? ' round--done' : '';
  const foot = [];
  if (rnd.status === 'done' && rnd.winner) {
    const label =
      rnd.winner === 'DRAW' ? '平局' : `${rnd.winner === 'A' ? rnd.sideA.label : rnd.sideB.label} 胜`;
    foot.push(`<span class="badge badge--win">${esc(reveal() ? label : '结果已封存')}</span>`);
  }
  // 打完的局要把成绩说全：各局小分 + 双方总得分（时间行已经给了起止时间与用时）
  foot.push(resultStatsHtml(rnd));
  return (
    `<article class="round${cls}" data-code="${esc(rnd.code)}">` +
    `<header class="round__head"><div class="round__no">${esc(rnd.label)}</div>${roundBadge(rnd.status)}</header>` +
    roundTimeHtml(rnd) +
    `<div class="round__body"><div class="versus">` +
    leagueDuoHtml(s, rnd, rnd.sideA) +
    `<div class="vs-mid">${leagueScore(rnd, rnd.sideA)}<span class="vs-mid__tag">VS</span>${leagueScore(rnd, rnd.sideB)}</div>` +
    leagueDuoHtml(s, rnd, rnd.sideB) +
    `</div></div>` +
    (foot.length ? `<div class="round__foot">${foot.join('')}</div>` : '') +
    (rnd.note ? `<div class="round__note">${esc(rnd.note)}</div>` : '') +
    watchRoundHtml(rnd) +
    leagueOpsHtml(rnd) +
    `</article>`
  );
}

/**
 * 比赛时间行：明示「是否已结束」。
 *
 * * 已结束：起止时间 + 用时（结束时间未登记时明确说出来）
 * * 进行中：开始时间 + 「结束待定」（结束时间为可选，登记后即为已结束）
 * * 未开始：计划时间，或「时间待定」
 */
function roundTimeHtml(rnd) {
  const state = rnd.timeState || 'unscheduled';
  const bits = [];
  if (state === 'finished') {
    const range = fmtRange(rnd.startedAt, rnd.finishedAt);
    bits.push(`<span class="rtime__main">${esc(range || '起止时间未登记')}</span>`);
    if (!rnd.finishedAt && rnd.startedAt) {
      bits.push(`<span class="rtime__sub">结束时间未登记</span>`);
    }
    if (rnd.durationMinutes != null) {
      bits.push(`<span class="rtime__dur">用时 ${esc(fmtDuration(rnd.durationMinutes))}</span>`);
    }
    if (rnd.pendingSettlement) {
      bits.push(`<span class="rtime__warn">比分待录入</span>`);
    }
  } else if (state === 'running') {
    bits.push(
      `<span class="rtime__main">${esc(rnd.startedAt ? `${fmtTime(rnd.startedAt)} 开始` : '进行中')}</span>`
    );
    bits.push(
      `<span class="rtime__sub">${esc(rnd.finishedAt ? `结束 ${fmtTime(rnd.finishedAt)}` : '结束待定')}</span>`
    );
  } else if (state === 'scheduled') {
    bits.push(`<span class="rtime__main">计划 ${esc(fmtTime(rnd.scheduledAt))}</span>`);
  } else {
    bits.push(`<span class="rtime__main rtime__main--mute">时间待定</span>`);
  }
  return `<div class="rtime rtime--${esc(state)}">${bits.join('')}</div>`;
}

function matchSideHtml(side, key, rnd) {
  const ready = Boolean(side.teamId);
  const done = rnd.status === 'done';
  const win = done && rnd.winner === key;
  const forfeit = Boolean(side.forfeit);
  const score = done ? side.score : '';
  // 多队同场：显示本场名次与（可选的）小分
  const rank = done && side.rank ? `<span class="mside__rank">#${side.rank}</span>` : '';
  const points = done && side.points ? `<span class="mside__pts">小分 ${side.points}</span>` : '';
  const members = (side.players || [])
    .map(
      (p) =>
        `<span class="mside__member">${avaHtml(p, 'xs')}${esc(p.name || p.id)}` +
        `${isLivePlayer(p.id) ? liveTag('直播') : ''}</span>`
    )
    .join('');
  return (
    `<div class="mside${win ? ' mside--win' : ''}${ready ? '' : ' mside--empty'}${
      forfeit ? ' mside--forfeit' : ''
    }">` +
    `<div class="mside__top"><i class="mside__dot" style="background:${esc(side.color || 'var(--accent)')}"></i>` +
    `<b class="mside__team" title="${esc(side.label || '')}">${esc(side.label || '')}</b>` +
    (forfeit ? `<span class="mside__forfeit">弃权</span>` : '') +
    rank +
    points +
    `<span class="mside__score">${score}</span></div>` +
    `<div class="mside__members">${
      members || `<span class="panel__hint">${esc(side.source || '等待上游比赛结果')}</span>`
    }</div></div>`
  );
}

/** 各局小分：25:20 · 22:25 · 15:12。 */
function setsChipHtml(rnd) {
  const sets = rnd.sets || [];
  if (!sets.length) return '';
  return `<span class="chip chip--sets" title="各局小分">${sets
    .map((s) => `${s.a}:${s.b}`)
    .join(' · ')}</span>`;
}

/**
 * 已结束比赛的「成绩」：各局小分 + 双方总得分。
 *
 * 只结算了胜负（没填小分 / 总得分）时返回空串，不占位置；
 * 多队同场（3~4 队）不显示「总得分 A:B」——那种场次看各队名次与得分。
 */
function resultStatsHtml(rnd) {
  if (rnd.status !== 'done') return '';
  const sides = (rnd.sides || []).slice(0, 2);
  const bits = [];
  const sets = setsChipHtml(rnd);
  if (sets) bits.push(sets);
  if (sides.length === 2) {
    const points = sides.map((side) => side.points || 0);
    if (points.some((n) => n > 0)) {
      bits.push(
        `<span class="chip chip--total" title="双方总得分">总得分 ${points[0]} : ${points[1]}</span>`
      );
    }
  }
  return bits.join('');
}

/** 按比赛编号取公开状态里的那一场（对阵图只带简略字段，需要回这里取机位明细）。 */
const roundByCode = (code) => (App.state?.rounds || []).find((r) => r.code === code) || null;

/**
 * 本场直播标识。
 *
 * **真的有人在推流**才叫「直播中」；「已排直播」只是管理端的排期提示，
 * 观众看不到——否则赛程上会挂着一堆点了没反应的直播入口。
 */
function liveBadgeHtml(rnd) {
  if (roundLiveCams(rnd)) {
    return `<span class="badge badge--cast"><i class="dot"></i>直播中</span>`;
  }
  if (rnd?.live && canEdit()) return `<span class="badge badge--cast-plan">已排直播</span>`;
  return '';
}

function matchOpsHtml(rnd) {
  if (!canEdit()) return '';
  const sides = rnd.sides || [];
  if (!sides.every((side) => side.teamId)) {
    return (
      `<div class="round__ops">` +
      `<span class="panel__hint">对阵确定后自动出现在这里</span>` +
      `<button class="btn btn--sm" type="button" data-act="round-live" data-code="${esc(rnd.code)}" ` +
      `title="查看 / 复制本场的推流与播放地址">直播地址</button></div>`
    );
  }
  const ops = [];
  if (rnd.status === 'pending') {
    ops.push(
      `<button class="btn btn--sm btn--primary" type="button" data-act="round-status" ` +
        `data-code="${esc(rnd.code)}" data-status="live">开始</button>`
    );
  }
  ops.push(
    `<button class="btn btn--sm" type="button" data-act="round-times" data-code="${esc(rnd.code)}">时间</button>` +
      `<button class="btn btn--sm" type="button" data-act="round-result" data-code="${esc(rnd.code)}">录入比分</button>` +
      `<button class="btn btn--sm" type="button" data-act="round-walkover" data-code="${esc(rnd.code)}" ` +
      `title="长期没人 / 人数不足：判一方弃权，对方直接晋级">弃权</button>` +
      // 这里只提供推流地址，不放直播开关（开关在「直播配置」的总开关上）
      `<button class="btn btn--sm" type="button" data-act="round-live" data-code="${esc(rnd.code)}" ` +
      `title="查看 / 复制本场的推流与播放地址">直播地址</button>`
  );
  if (rnd.status !== 'pending') {
    ops.push(
      `<button class="btn btn--sm" type="button" data-act="round-reset" data-code="${esc(rnd.code)}">重置</button>`
    );
  }
  return `<div class="round__ops">${ops.join('')}</div>`;
}

function matchCardHtml(s, rnd) {
  const cls = rnd.status === 'live' ? ' round--live' : rnd.status === 'done' ? ' round--done' : '';
  const sides = rnd.sides || [rnd.sideA, rnd.sideB];
  const multi = sides.length > 2;
  const foot = [];
  const watch = watchRoundHtml(rnd);
  if (rnd.status === 'done' && rnd.winner) {
    const winner = sides.find((side) => side.key === rnd.winner);
    const label = rnd.winner === 'DRAW' ? '平局' : `${winner?.label || rnd.winner} 胜`;
    foot.push(`<span class="badge badge--win">${esc(reveal() ? label : '结果已封存')}</span>`);
  }
  if (multi) foot.push(`<span class="chip chip--multi">${sides.length} 队同场</span>`);
  // 直播标记只按**真实推流**算（配了流名 + 媒体服务器确认在推）；
  // 「已排直播」这种排期提示只在卡片头部给管理员看，观众这边一个字都不提
  const cams = roundLiveCams(rnd);
  if (cams) {
    foot.push(
      `<span class="chip chip--cast" title="${esc(rnd.liveNote || '本场有多路信号在推流')}">` +
        `${cams} 路直播中${rnd.liveNote ? ` · ${esc(rnd.liveNote)}` : ''}</span>`
    );
  }
  foot.push(resultStatsHtml(rnd));
  if (rnd.winnerTo) foot.push(`<span class="chip">胜者 → ${esc(rnd.winnerTo)}</span>`);
  if (rnd.loserTo) foot.push(`<span class="chip">败者 → ${esc(rnd.loserTo)}</span>`);
  return (
    `<article class="round${cls}" data-code="${esc(rnd.code)}">` +
    `<header class="round__head"><div class="round__no">${esc(rnd.label || rnd.code)}` +
    // 同一轮里有多场，编号必须露出来，否则几张卡看起来一模一样
    `<span class="round__code" title="对局编号">${esc(rnd.code)}</span></div>` +
    `${liveBadgeHtml(rnd)}${roundBadge(rnd.status)}</header>` +
    roundTimeHtml(rnd) +
    `<div class="round__body"><div class="msides${multi ? ' msides--multi' : ''}">` +
    sides.map((side) => matchSideHtml(side, side.key, rnd)).join('') +
    `</div></div>` +
    (foot.length ? `<div class="round__foot">${foot.filter(Boolean).join('')}</div>` : '') +
    (rnd.note ? `<div class="round__note">${esc(rnd.note)}</div>` : '') +
    watch +
    matchOpsHtml(rnd) +
    `</article>`
  );
}

/**
 * 观众入口：这场**真的有人在推流**时，才给一个「看这场直播」按钮。
 *
 * 点了就切到直播页并选中这场——观众因此可以看**任意一场**，
 * 而不只是「正在进行」的那场。只是「配了流名 / 排了直播」不算，
 * 否则点进去只有一张「没有任何人在直播」的封面。
 */
function watchRoundHtml(rnd) {
  const cams = roundLiveCams(rnd);
  if (!cams) return '';
  return (
    `<div class="round__ops round__ops--public">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="watch-round" ` +
    `data-code="${esc(rnd.code)}">看这场直播（${cams} 路在播）</button></div>`
  );
}

/* ------------------------------ 选手 ---------------------------------- */
export function renderRoster(s) {
  renderRosterTools(s);
  renderRosterGrid(s);
}

function filteredPlayers(s) {
  const kw = App.search.trim().toLowerCase();
  if (!kw) return s.players || [];
  // 只搜索用户端可见的字段（UUID / QQ 不下发，也不该作为检索维度）
  return (s.players || []).filter((p) =>
    [p.name, p.tag, p.id].filter(Boolean).some((v) => String(v).toLowerCase().includes(kw))
  );
}

function renderRosterTools(s) {
  const focused = document.activeElement && document.activeElement.id === 'rosterSearch';
  qs('#rosterTools').innerHTML =
    `<div class="field" style="min-width:200px"><input id="rosterSearch" type="search" ` +
    `placeholder="搜索姓名 / 编号" value="${esc(App.search)}" autocomplete="off"></div>` +
    `<div class="tool-group" style="margin-left:auto">` +
    `<span class="panel__hint">${filteredPlayers(s).length} / ${(s.players || []).length} 人</span>` +
    (canEdit() ? `<button class="btn btn--sm btn--primary" type="button" data-act="player-add">新增选手</button>` : '') +
    // 积分制才谈得上替补（锦标赛制用「替补换人」按队换）
    (canEdit() && isLeague(s)
      ? `<button class="btn btn--sm" type="button" data-act="player-add-sub">新增替补</button>`
      : '') +
    `</div>`;
  if (focused) qs('#rosterSearch').focus();
}

/** 积分制：个人积分榜数据；锦标赛制：所属队伍与胜负（playerProgress）。 */
const statById = (id) => (App.state?.standings?.players || []).find((r) => r.playerId === id);
const progressById = (id) => (App.state?.playerProgress || {})[id];

/** 后端下发的「本届参与名单」为生效名单，未指定时即全部启用选手。 */
const joinedIds = () => new Set(App.state?.participants || []);

const isOut = (id) => {
  const set = joinedIds();
  return set.size > 0 && !set.has(id);
};

function playerCardHtml(p) {
  const league = isLeague();
  const pr = league ? null : progressById(p.id);
  const st = league
    ? statById(p.id) || { played: 0, win: 0, points: 0, rank: null, winRate: 0, bestStreak: 0 }
    : null;
  const played = league ? st.played : pr?.played || 0;
  const win = league ? st.win : pr?.win || 0;
  const rate = league ? st.winRate || 0 : played ? Math.round((win / played) * 100) : 0;
  const out = isOut(p.id);
  const cls = `${p.substitute ? ' pcard--sub' : ''}` +
    `${p.active === false ? ' pcard--inactive' : ''}${out ? ' pcard--out' : ''}`;
  const tags = [];
  if (out) tags.push(`<span class="badge badge--out">未参与本届</span>`);
  if (isLivePlayer(p.id)) tags.push(liveTag('直播中'));
  if (league && st.bestStreak >= 2) tags.push(`<span class="badge badge--win">连胜×${st.bestStreak}</span>`);
  if (!league && pr?.teamName) tags.push(`<span class="badge badge--done">${esc(pr.teamName)}</span>`);
  if (p.tag) tags.push(`<span class="badge badge--pending">${esc(p.tag)}</span>`);
  if (p.substitute) tags.push(`<span class="badge badge--sub">替补</span>`);
  if (p.active === false) tags.push(`<span class="badge badge--lose">停用</span>`);

  const stats = league
    ? `<div class="pcard__stat"><b>${played}</b><span>场次</span></div>` +
      `<div class="pcard__stat"><b>${win}</b><span>胜</span></div>` +
      `<div class="pcard__stat"><b>${st.points}</b><span>积分</span></div>` +
      `<div class="pcard__stat"><b>${st.rank ?? '—'}</b><span>排名</span></div>`
    : `<div class="pcard__stat"><b>${played}</b><span>场次</span></div>` +
      `<div class="pcard__stat"><b>${win}</b><span>胜</span></div>` +
      `<div class="pcard__stat"><b>${pr?.lose || 0}</b><span>负</span></div>` +
      `<div class="pcard__stat"><b>${esc(pr?.group || '—')}</b><span>小组</span></div>`;

  const forms = league ? st.form : pr?.forms;
  const ops = canEdit()
    ? `<div class="round__ops">` +
      // 在 QQ 里换过头像后点这里：跳过服务端 + 浏览器缓存重新拉取
      (privateOf(p.id).qq
        ? `<button class="btn btn--sm" type="button" data-act="avatar-refresh" data-id="${esc(p.id)}" ` +
          `title="跳过缓存重新从 QQ 拉取头像">刷新头像</button>`
        : '') +
      `<button class="btn btn--sm" type="button" data-act="player-edit" data-id="${esc(p.id)}">编辑</button>` +
      `<button class="btn btn--sm btn--danger" type="button" data-act="player-del" data-id="${esc(p.id)}">删除</button></div>`
    : '';
  return (
    `<article class="pcard${cls}"><div class="pcard__band"></div>` +
    `<div class="pcard__top">${avaHtml(p, 'md')}<div>` +
    `<div class="pcard__name">${esc(p.name || p.id)}</div>` +
    `<div class="pcard__sub" title="${esc(p.tag || p.id)}">${esc(p.tag || p.id)}</div>` +
    `</div></div>` +
    `<div class="pcard__stats">${stats}</div>` +
    `<div class="pcard__tags">${
      [...tags, ...(forms?.length ? [formChips(forms)] : [])].join('') ||
      '<span class="panel__hint">无标签</span>'
    }</div>` +
    `<div class="pcard__rate"><i style="width:${Math.max(0, Math.min(100, rate))}%"></i></div>` +
    ops +
    `</article>`
  );
}

export function renderRosterGrid(s) {
  const grid = qs('#rosterGrid');
  const players = filteredPlayers(s);
  if (players.length) {
    grid.innerHTML = players.map((p) => playerCardHtml(p)).join('');
    return;
  }
  // 区分「名单本来就是空的」与「搜索没匹配上」——出厂不带任何示例选手
  const none = (s.players || []).length === 0;
  grid.innerHTML = none
    ? `<div class="empty"><b>还没有选手</b>${
        canEdit()
          ? '点右上角「新增选手」开始录入（姓名 + 游戏 UUID，QQ 可选）'
          : '请等待管理员录入选手名单'
      }</div>`
    : `<div class="empty"><b>没有匹配的选手</b>调整搜索条件，或清空搜索框</div>`;
}

/* ------------------------------ 直播 ----------------------------------
 *
 * 一条硬规则：**只有「配了推流流名 + 媒体服务器确认在推流」才出现在界面上**。
 * 没人推流时，直播页不摆任何机位，只留一句「没有任何人在直播」；
 * 主直播间（直播配置里的「默认流名」）也只有它真的在推流时，
 * 才作为一路独立机位出现——一个人都没播时，它就是唯一可选的那一路。
 */
/** 在播的选手机位：配了流名 **且** 真的在推流。 */
function liveCandidates(s) {
  return (s.players || [])
    .filter((p) => p.hasStream && isLivePlayer(p.id))
    .map((p) => ({
      id: p.id,
      name: p.name || p.id,
      player: p,
      live: true,
      room: (s.streams || {})[p.id] || {},
      round: roundOfPlayer(s, p.id),
    }));
}

/**
 * 主直播间这一路机位（不属于任何选手）。
 *
 * 只有「默认流名」真的在推流、并且配了源地址时才给；
 * ``id`` 用常量而不是流名，免得跟选手 ID 撞车，真正的流名在 ``room.key`` 里。
 */
function mainCandidate() {
  if (!isMainLive()) return null;
  const room = mainRoom();
  if (!room) return null;
  return {
    id: MAIN_ROOM_ID,
    name: '主直播间',
    player: null,
    live: true,
    room,
    round: null,
    main: true,
  };
}

/** 直播页的全部可播机位：主直播间（在播时）+ 在播的选手机位。 */
const livePool = (s) => [mainCandidate(), ...liveCandidates(s)].filter(Boolean);

const inRound = (r, pid) => [...r.sideA.players, ...r.sideB.players].some((p) => p.id === pid);

/**
 * 直播页的比赛候选：**有在播机位的那些对局**（没人播的比赛不上条）。
 *
 * ``cams`` 数的是「真的在推流」的机位，而不是配了几路流名——
 * 否则选择条上会挂一堆点进去没画面的比赛。
 * 排序：进行中的最前 → 在播机位多的 → 场次靠前。
 */
function liveRoundCandidates(s) {
  const order = { live: 0, pending: 1, done: 2 };
  return (s.rounds || [])
    .map((r) => ({ ...r, cams: roundLiveCams(r) }))
    .filter((r) => r.cams > 0)
    .sort(
      (a, b) =>
        (order[a.status] ?? 9) - (order[b.status] ?? 9) ||
        b.cams - a.cams ||
        (a.index || 0) - (b.index || 0)
    );
}

/** 比赛选择条：选中一场就只看这场的机位（主直播间不受它影响）。 */
function roundChipsHtml(rounds, total) {
  if (!rounds.length) return '';
  const chip = (code, label, live, count) =>
    `<button type="button" class="live-chip live-chip--round${
      code === App.liveRound ? ' live-chip--active' : ''
    }${live ? ' live-chip--live' : ''}" data-act="live-round" data-code="${esc(code)}" ` +
    `title="${esc(label)}">` +
    `${live ? '<i class="dot"></i>' : ''}<span class="live-chip__name">${esc(label)}</span>` +
    `<span class="live-chip__count">${count} 路在播</span></button>`;
  return (
    `<span class="live-pick__label">比赛</span>` +
    chip('', '全部机位', false, total) +
    rounds.map((r) => chip(r.code, r.label || r.code, r.status === 'live', r.cams)).join('')
  );
}

function roundOfPlayer(s, pid) {
  const rounds = s.rounds || [];
  return (
    rounds.find((r) => r.status === 'live' && inRound(r, pid)) ||
    rounds.find((r) => r.status === 'pending' && inRound(r, pid)) ||
    rounds.find((r) => inRound(r, pid)) ||
    null
  );
}

/** 没有任何在播机位时的提示（区分「确实没人播」与「探测不到」）。 */
function liveEmptyHtml(s) {
  const known = App.liveHealth ? App.liveHealth.streamingKnown : s.liveStatus?.known;
  const hint =
    known === false
      ? '暂时无法判断有没有人在直播：媒体服务器 API 不可达，请管理员到「直播配置」里检查 API 地址与账号。'
      : canEdit()
        ? '让主播在 OBS 里推他自己的流名（选手名单里填的那个）；推上来后这里会自动出现，平时不摆空机位。'
        : '等主播开播后再来看。';
  return `<div class="live-pick__empty live-pick__empty--none"><b>没有任何人在直播</b>${esc(hint)}</div>`;
}

export function renderLive(s) {
  const all = livePool(s); // 主直播间（在播时）+ 在播的选手机位
  const rounds = liveRoundCandidates(s);
  // 选中的比赛（'' = 全部机位）；这场已经没人播了就回到全部
  if (App.liveRound && !rounds.some((r) => r.code === App.liveRound)) App.liveRound = '';
  // 选中某场：只看这场的选手机位（用后端的 cast，保证与赛程卡一致）。
  // 主直播间不属于任何一场，切到某场比赛时它照样可选。
  const castIds = new Set(
    App.liveRound
      ? ((s.rounds || []).find((r) => r.code === App.liveRound)?.streams?.cast || []).map(
          (item) => item.playerId
        )
      : []
  );
  const pool = App.liveRound ? all.filter((c) => c.main || castIds.has(c.id)) : all;
  let picked = pool.find((c) => c.id === App.livePlayerId) || null;
  if (!picked) {
    // 没选、或原来选的那路已经下播：自动落到第一路在播信号
    // （否则观众打开直播页只能对着一张封面，得先自己猜着点一下）
    picked = pool[0] || null;
    if (App.livePlayerId !== (picked?.id ?? null)) {
      log.debug('当前机位', picked?.id || '(没有任何人在直播)');
    }
    App.livePlayerId = picked?.id ?? null;
  }

  App.livePicked = picked;
  const st = s.stream || {};
  const sig = [
    st.mode,
    st.enabled,
    App.liveProto,
    // 管理端登录状态也要进签名：否则登录后「复制推流」按钮不会补出来
    canEdit() ? 'admin' : 'guest',
    App.livePlayerId || '',
    picked ? picked.room.key || '' : '',
  ].join('|');
  const stage = qs('#liveStage');
  let rebuilt = false;
  if (stage.dataset.sig !== sig) {
    stage.dataset.sig = sig;
    stage.innerHTML = stageHtml(s);
    rebuilt = true;
    if (App.view === 'live') Live.playSelected(s);
  }

  // 比赛条只在「有比赛有人在播」时才出现（没人播就整条收起来）
  const roundBar = qs('#liveRounds');
  roundBar.innerHTML = roundChipsHtml(rounds, all.length);
  roundBar.hidden = !rounds.length;
  qs('#livePick').innerHTML = pool.length
    ? pool.map((c) => liveChipHtml(c, picked)).join('')
    : liveEmptyHtml(s);
  qs('#stageMeta').innerHTML = picked ? vsLineHtml(picked) : '';
  qs('#pushPanel').innerHTML = pushPanelHtml(s, picked);
  qs('#liveInfoPanel').innerHTML = liveInfoHtml(s, picked);
  return rebuilt;
}

/** 让直播视图聚焦当前机位：必要时重建舞台，否则直接播放。 */
export function focusLive(s) {
  const rebuilt = renderLive(s);
  if (!rebuilt) Live.playSelected(s);
  return rebuilt;
}

function liveChipHtml(c, picked) {
  const active = picked && picked.id === c.id;
  const cls =
    `live-chip${active ? ' live-chip--active' : ''}${c.live ? ' live-chip--live' : ''}` +
    `${c.main ? ' live-chip--main' : ''}`;
  return (
    `<button type="button" class="${cls}" data-act="live-select" data-pid="${esc(c.id)}" ` +
    `title="${esc(c.main ? '主直播间（直播配置里的默认流名）' : c.name)}">` +
    (c.player ? avaHtml(c.player, 'xs') : '<span class="ava ava--xs ava--placeholder">主</span>') +
    `<span class="live-chip__name">${esc(c.name)}</span>` +
    (c.live ? liveTag('LIVE') : '') +
    `</button>`
  );
}

/** 直播间顶部的「谁 vs 谁」。 */
function vsLineHtml(picked) {
  const r = picked.round;
  if (!r) {
    // 主直播间是「全场那一路」，不绑定某一场比赛
    return picked.main
      ? `<div class="vs-line vs-line--idle"><span class="vs-line__tag">主直播间</span>` +
          `全场总机位（默认流名） · 不绑定某一场</div>`
      : `<div class="vs-line vs-line--idle"><span class="vs-line__tag">当前机位</span>${esc(picked.name)} · 暂无进行中的对局</div>`;
  }
  const side = (sd) => {
    const own = sd.players.some((p) => p.id === picked.id);
    const names = sd.players.map((p) => esc(p.name || p.id)).join(' / ') || '—';
    return (
      `<span class="vs-line__side${own ? ' vs-line__side--own' : ''}" style="--c:${esc(sd.color || 'var(--accent)')}">` +
      `<b>${esc(sd.label)}</b>${names}</span>`
    );
  };
  return (
    `<div class="vs-line"><span class="vs-line__tag">${esc(r.label || r.code)}</span>${side(r.sideA)}` +
    `<span class="vs-line__vs">VS</span>${side(r.sideB)}</div>`
  );
}

function stageHtml(s) {
  const st = s.stream || {};
  return (
    `<div class="panel__head"><h2>赛事直播</h2>` +
    `<span class="panel__hint">${esc(st.provider || 'mediamtx')} · 多机位 · ${esc(STREAM_MODE_LABEL[st.mode] || st.mode || 'auto')}</span></div>` +
    `<div class="live-pick live-pick--rounds" id="liveRounds"></div>` +
    `<div class="live-pick" id="livePick"></div>` +
    `<div class="stage-frame" id="stageFrame">` +
    `<video id="liveVideo" playsinline autoplay controls muted></video>` +
    `<div class="stage-frame__bars"><span></span><span></span><span></span><span></span></div>` +
    `<span class="stage-badge"><span class="chip chip--live"><i class="dot"></i><b>LIVE</b></span></span>` +
    `<span class="stage-state" id="liveState">待连接</span>` +
    `<div class="stage-cover" id="liveCover" hidden></div></div>` +
    // 对阵条放在画面**下面**：原来压在画面底部，既挡住视频又和原生控制条抢位置
    `<div class="stage-meta" id="stageMeta"></div>` +
    `<div class="stage-bar"><div class="stage-bar__left">` +
    `<button class="btn btn--primary btn--sm" type="button" data-act="live-play">播放</button>` +
    `<button class="btn btn--sm" type="button" data-act="live-stop">停止</button></div>` +
    `<div class="stage-bar__mid" role="group" aria-label="播放线路">` +
    `<span class="stage-bar__label">线路</span>` +
    `<button class="btn btn--sm${App.liveProto === 'webrtc' ? ' btn--primary' : ''}" type="button" ` +
    `data-act="live-proto" data-proto="webrtc" title="优先：WebRTC / WHEP，走 UDP，延迟最低">WebRTC<sup>优先</sup></button>` +
    `<button class="btn btn--sm${App.liveProto === 'hls' ? ' btn--primary' : ''}" type="button" ` +
    `data-act="live-proto" data-proto="hls" title="备选：HLS 走 TCP，抗抖动，延迟略高">HLS</button>` +
    `</div>` +
    `<div class="stage-bar__right">` +
    `<button class="btn btn--sm" type="button" data-act="live-open">打开源页</button>` +
    // 推流地址属于凭据，只给登录后的管理端；内容跟随当前线路（WebRTC→WHIP / TCP→RTMP）
    (canEdit()
      ? `<button class="btn btn--sm" type="button" data-act="live-copy-push" ` +
        `title="复制当前线路对应的推流地址 · ${esc(PUSH_TIP_LINE)}">` +
        `复制推流（${App.liveProto === 'hls' ? 'RTMP' : 'WHIP'}）</button>`
      : '') +
    `<button class="btn btn--sm" type="button" data-act="live-copy">复制播放地址</button>` +
    `<button class="btn btn--sm" type="button" data-act="live-refresh">刷新信号</button>` +
    `</div></div>`
  );
}

function pushPanelHtml(s, picked) {
  const st = s.stream || {};
  const room = (picked && picked.room) || {};
  const admin = canEdit();
  const main = Boolean(picked?.main);
  // 主直播间走「默认流名」那一套推流地址，选手机位走选手自己的
  const endpoints = pushEndpointsOf(picked?.id);
  // [标签, 值, 是否可复制, 复制提示类型]
  const rows = [];
  if (room.key) {
    rows.push(['机位', picked.name, false]);
    // 推流标识 = 选手自己的流名（主直播间 = 配置里的默认流名）：整届都用同一个地址
    rows.push([main ? '主直播间流名' : '推流标识', room.key, true]);
    if (room.roundLabel) rows.push(['当前比赛', room.roundLabel, false]);
    // 内嵌观看页（源站）：https://live.shiyora.net:8889/<流名>/
    if (room.page) rows.push(['观看页（内嵌）', room.page, true]);
    // 推流（仅管理端；WHIP = WebRTC 套 = 优先，RTMP / RTSP = TCP 套 = 备选）
    if (admin && endpoints.whipPush) rows.push(['推流 WHIP（优先）', endpoints.whipPush, true, 'push']);
    if (admin && endpoints.rtmpPush) rows.push(['推流 RTMP（备选）', endpoints.rtmpPush, true, 'push']);
    if (admin && endpoints.rtspPush)
      rows.push(['推 RTSP·播（备选）', endpoints.rtspPush, true, 'push']);
    // 播放（源站直连）：WebRTC 套用 WHEP，TCP 套用 HLS。
    // HLS 给两条：地址到 /<流名>/ 为止的**播放页**（贴浏览器就能看）+ 播放列表（播放器用）
    rows.push(['播放 WHEP', room.whep, true]);
    rows.push(['HLS 播放页', room.hlsPage, true]);
    rows.push(['HLS 播放列表', room.hls, true]);
  }
  const body = rows.length
    ? rows
        .map(([label, value, copyable, tip]) =>
          !value
            ? ''
            : `<div class="url-row"><span class="url-row__label">${esc(label)}</span>` +
              `<span class="url-row__value">${esc(value)}</span>` +
              (copyable
                ? `<button class="btn btn--sm" type="button" data-copy="${esc(value)}"` +
                  `${tip ? ` data-tip="${esc(tip)}"` : ''}>复制</button>`
                : '') +
              `</div>`
        )
        .join('')
    : `<div class="empty"><b>没有任何人在直播</b>${
        admin
          ? '目前没有信号在推，所以这里不摆地址；有人开播后会自动出现。'
          : '等主播开播后再来看。'
      }</div>`;
  const note = [
    st.note ? `<div class="notice" style="margin-top:10px">${esc(st.note)}</div>` : '',
    admin
      ? `<div class="panel__hint" style="margin-top:8px">推流<b>优先用 WHIP</b>（WebRTC 套，UDP，延迟最低），` +
        `推不上去再用 RTMP / RTSP（TCP 套，抗抖动）。${
          main
            ? `主直播间推的是直播配置里的<b>默认流名</b>（这里 …/${esc(room.key || '<流名>')}）。`
            : `每位选手只要推自己的流名（这里 …/${esc(room.key || '<流名>')}），` +
              `整届赛事都用同一个地址，换比赛不用改。`
        }仅管理员可见。</div>` +
        pushTipsHtml()
      : `<div class="panel__hint" style="margin-top:8px">播放线路可在播放器上方切换：` +
        `WebRTC 延迟低、HLS 更稳（推流地址属于凭据，仅登录管理员可见）。</div>`,
  ].join('');
  return (
    `<div class="panel__head"><h2>${admin ? '推流 / 播放地址' : '播放地址'}</h2>` +
    `<span class="panel__hint">${admin ? '选手自行推流 · 观众拉流' : '观众拉流 · 源站直连'}</span></div>` +
    `<div class="panel__body">${body}${note}</div>`
  );
}

export function liveInfoHtml(s, picked = null) {
  const e = App.liveInfo || {};
  const health = App.liveHealth;
  const liveRounds = (s.rounds || []).filter((r) => r.status === 'live');
  // 配了流名的机位数（能不能播要看媒体服务器上报，见下面的「正在推流」）
  const configured = (s.players || []).filter((p) => p.hasStream).length;
  // 两个端口分开回报：MediaMTX 的 WebRTC(8889) 与 HLS(8888) 是独立监听，
  // 地址写错时能一眼看出是哪一条要改
  const probes = health?.probes || {};
  const probeText = (name) => {
    const probe = probes[name];
    if (!probe) return '—';
    if (probe.ok) {
      return probe.status === 404 ? '端口通（当前无此流）' : `就绪（HTTP ${probe.status}）`;
    }
    return `异常：${probe.reason || '不可达'}`;
  };
  // 「正在推流」以媒体服务器上报为准：拿不到就直说，别给假的直播标记
  const live = s.liveStatus || {};
  // 在播 = 在推流的选手 + 主直播间（整数字）
  const nowCount = livePlayers().size + (isMainLive() ? 1 : 0);
  const streamingText = !health
    ? `${nowCount} 路（未探测）`
    : live.known
      ? `${nowCount} 路 · 媒体服务器上报`
      : `无法判断 · ${live.reason || '媒体服务器 API 不可达'}（只影响「直播中」标记，不影响播放）`;
  const kv = [
    ['状态', s.stream?.enabled ? '已启用' : '已关闭'],
    ['WebRTC 端口', health ? probeText('webrtc') : '未探测'],
    ['HLS 端口', health ? probeText('hls') : '未探测'],
    ['进行中', `${liveRounds.length} 场`],
    ['已配置机位', `${configured} 路`],
    ['正在推流', streamingText],
    ['当前机位', picked ? `${picked.name} · ${picked.room.key || '—'}` : '没人直播'],
    ['源地址', e.origin || s.stream?.baseUrl || '—'],
  ];
  return (
    `<div class="panel__head"><h2>直播信息</h2><span class="panel__hint">源站直连（不做反代）</span></div>` +
    `<div class="panel__body"><dl class="kv">` +
    kv.map(([k, v]) => `<div class="kv__row"><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('') +
    `</dl></div>`
  );
}

/* ------------------------------ 总调度 -------------------------------- */
const VIEW_RENDERERS = {
  overview: renderOverview,
  schedule: renderSchedule,
  roster: renderRoster,
  live: renderLive,
  events: renderEventsView,
};

/** 只渲染指定视图；切页时按需渲染，避免隐藏视图做无谓的 DOM 重建。 */
export function renderView(view, s = App.state) {
  if (!s) return;
  const render = VIEW_RENDERERS[view];
  if (render) render(s);
}

/**
 * 页签可用性：已完结的届、回看的往届都没有直播页。
 *
 * 每次状态刷新都对一遍（包括 WebSocket 推来的变更：管理员刚把这一届标记结束，
 * 观众这边的直播页签也要立刻收掉）。
 */
function syncTabs(s) {
  const liveTab = qsa('.tab').find((t) => t.dataset.view === 'live');
  if (!liveTab) return;
  const allow = liveAvailable(s);
  if (liveTab.hidden === !allow) return; // 已经是目标状态
  liveTab.hidden = !allow;
  // 正停在直播页却被收走（届被标记结束了）：回总览，别留一个够不着的页面
  if (!allow && App.view === 'live') hooks.goto?.(App.routeEvent, 'overview', { replace: true });
}

/** 状态推送入口：HUD/公告始终刷新，正文只刷新当前可见视图。 */
export function renderPublic() {
  const s = App.state;
  if (!s) return;
  // 记下「已渲染的指纹 + 视图」，供 app.js 判断重复推送时是否可以跳过重建
  App.renderedKey = stateKey(s);
  App.renderedView = App.view;
  applyTheme(s);
  renderHeader(s);
  syncTabs(s);
  renderView(App.view, s);
}
