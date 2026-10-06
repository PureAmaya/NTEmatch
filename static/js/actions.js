/* 管理动作层：赛程/比分/换人/选手/队伍/系统操作与弹窗表单。
 * 通过 data-act 与 data-form 被动调用；成功后依赖 WebSocket 推送刷新界面。
 */

import {
  App,
  LIVE_PROTO_KEY,
  hooks,
  Modal,
  TOKEN_KEY,
  api,
  MISSING,
  cmpVal,
  copyText,
  downloadFile,
  esc,
  fmtScore,
  fmtVal,
  hasEntered,
  hasResult,
  log,
  missingValue,
  nowLocalInput,
  parseHms,
  parseVal,
  qs,
  qsa,
  refreshPrivate,
  scoringOf,
  splitMilli,
  routePath,
  toLocalInput,
  toast,
} from './core.js';
import {
  PUSH_TIP_LINE,
  avaHtml,
  collectForm,
  fieldArea,
  fieldNum,
  fieldSelect,
  fieldSwitch,
  fieldText,
  isChannelLive,
  privateOf,
  pushTipsHtml,
  roundHasResult,
  roundStreamsOf,
} from './ui.js';
import { refreshDiagnostics, renderAdmin, startReadiness } from './admin.js';
import { resetClock, toggleClock } from './clock.js';
import { openCreditsModal } from './credits.js';
import {
  composeNotice,
  editEventInfo,
  editServerInfo,
  goNoticePage,
  openNoticeReader,
  removeNotice,
} from './notices.js';
import {
  hideSetupGuide,
  invalidateEvents,
  loadEvents,
  renderEventsGroups,
  renderHomeGroups,
} from './events.js';
import { reset as resetTeamBoard, save as saveTeamBoard } from './teams.js';
import { ChannelLive, Live, probeLiveHealth } from './live.js';
import { channelRooms, focusLive, renderChannels, renderPublic } from './views.js';
import { handleMemberAction, handleMemberForm, refreshMeData } from './members.js';

/** 按对局编号（WB-1-2）或序号定位一场比赛。 */
const roundOf = (ref) =>
  (App.state?.rounds || []).find((r) => r.code === ref) ||
  (App.state?.rounds || []).find((r) => String(r.index) === String(ref));

/* ------------------------------ 计分控件 ------------------------------ */
/**
 * 计分输入控件：**按计分类型给不同的控件**。
 *
 * * 时间型 → 「时 : 分 : 秒」三个框（比一整串 `1:23.456` 好填得多，也不会填错位）；
 * * 小数   → 数字框，步长 0.001（存的是千分之一）；
 * * 自然数 → 数字框，步长 1。
 */
function valueInputHtml(role, value, sc) {
  if (sc.timeBased) {
    const [h, m, s] = splitMilli(value || 0);
    const cell = (key, ph, text) =>
      `<input data-t="${key}" inputmode="decimal" placeholder="${ph}" value="${esc(text)}">`;
    return (
      `<span class="tvinput" data-role="${esc(role)}">` +
      cell('h', '时', h) +
      `<i>:</i>` +
      cell('m', '分', m) +
      `<i>:</i>` +
      cell('s', '秒', s) +
      `</span>`
    );
  }
    const step = sc.valueType === 'decimal' ? '0.001' : '1';
  // 有成绩就回填（**数值型的 0 也要填出来**：它是合法读数，留空会被当成「没填」）；
  // 没有成绩则留空，让人一眼看出还没录。
  const text = hasResult(value, sc) ? fmtVal(value, sc) : '';
  return (
    `<span class="nvinput" data-role="${esc(role)}">` +
    `<input type="number" min="0" step="${step}" placeholder="0" value="${esc(text)}">` +
    `</span>`
  );
}

/** 读回一个计分控件（时间型读三个框，其余读一个）；解析不了抛错，由调用方提示。 */
function readValueInput(host, role, sc) {
  if (!host) return missingValue(sc);
  const box = qs(`[data-role="${role}"]`, host);
  if (!box) return missingValue(sc);
  if (!sc.timeBased) {
    const input = qs('input', box);
    return parseVal(input ? input.value : '', sc);
  }
  const part = (key) => {
    const input = qs(`[data-t="${key}"]`, box);
    return input ? input.value : '';
  };
  return parseHms(part('h'), part('m'), part('s'));
}

/** 一行轮次：`第 N 轮  A 输入 : B 输入  ×`。 */
function roundRowHtml(a, b, sc) {
  return (
    `<div class="setrow" data-round>` +
    `<span class="setrow__no"></span>` +
    valueInputHtml('set-a', a, sc) +
    `<span class="setrow__sep">:</span>` +
    valueInputHtml('set-b', b, sc) +
    `<button class="btn btn--sm btn--ghost" type="button" data-set-del title="删除这一轮">×</button>` +
    `</div>`
  );
}

/** 轮次重新编号：删掉中间一轮后仍要显示连续的「第 1 / 2 / 3 轮」。 */
function renumberRounds(bodyEl) {
  qsa('[data-round]', bodyEl).forEach((row, index) => {
    const tag = row.querySelector('.setrow__no');
    if (tag) tag.textContent = `第 ${index + 1} 轮`;
  });
}

/** 读出弹窗里的轮次（整轮都没填的行丢掉）。 */
function readRoundRows(bodyEl, sc) {
  return qsa('[data-round]', bodyEl)
    .map((row) => ({ a: readValueInput(row, 'set-a', sc), b: readValueInput(row, 'set-b', sc) }))
    .filter((item) => hasResult(item.a, sc) || hasResult(item.b, sc));
}

/**
 * 由轮次推导大比分与总成绩（与后端 ``tournament.judge_round`` 同一套规则）。
 *
 * 每一轮谁赢看判断标准（数值高胜或数值低胜）；大比分是**赢的轮数**（计数），
 * 总成绩是各轮成绩合计——没填的那一方按 0 计，不能把哨兵 ``-1`` 减进去。
 */
function deriveFromRounds(rounds, sc) {
  const wins = [0, 0];
  const totals = [0, 0];
  rounds.forEach((item) => {
    totals[0] += Math.max(0, Number(item.a) || 0);
    totals[1] += Math.max(0, Number(item.b) || 0);
    const cmp = cmpVal(item.a, item.b, sc);
    if (cmp < 0) wins[0] += 1;
    else if (cmp > 0) wins[1] += 1;
  });
  return { wins, totals };
}

/**
 * 按当前填写内容实时推导比分 / 名次 / 胜负，并回写到界面上。
 *
 * 规则与后端 `tournament.judge_round` 完全一致：先看轮次（2 队），再看本场成绩，
 * 最后看小分；**每轮谁赢由判断标准决定**（数值高胜或数值低胜）。
 * 填了轮次之后 `score` 是「赢的轮数」（计数，多者胜），各轮合计在 `points` 里。
 */
function refreshResultPreview(bodyEl, sides, allowDraw) {
  const box = qs('#rq-preview', bodyEl);
  const sc = scoringOf(App.state);
  const multi = sides.length > 2;
  const manualHost = qs('#rq-manual', bodyEl);
  let rounds = [];
  let entries = [];
  try {
    rounds = multi ? [] : readRoundRows(bodyEl, sc);
    entries = sides.map((side) => {
      // 多队同场：成绩就填在每一方那一行上；两方对局：没有轮次时才读「本场成绩」那一组
      const host = multi
        ? qs(`[data-sid="${side.key}"]`, bodyEl)
        : qs(`[data-sid="${side.key}"]`, manualHost);
      return {
        key: side.key,
        label: side.label,
        score: readValueInput(host, 'score', sc),
        points: readValueInput(host, 'points', sc),
      };
    });
  } catch (err) {
    // 预览是「边打字边算」的：格式还没写完就报错很正常，
    // 这里必须提示而不是抛出去（抛出去会让整块预览停在上一帧，看起来像卡住）
    if (box) {
      box.innerHTML =
        `<span class="rpreview__warn">${esc(err.message)}</span>` +
        `<span class="rpreview__sub">改成 1:23.456 或 25 这样的写法会自动重算</span>`;
    }
    return;
  }

  // 两种录入方式互斥（折叠关系）：填了轮次就按轮次算，那一组「本场成绩」输入框收起来
  const counted = rounds.length > 0;
  const derived = counted ? deriveFromRounds(rounds, sc) : null;
  if (derived) {
    [0, 1].forEach((i) => {
      entries[i].score = derived.wins[i];
      entries[i].points = derived.totals[i];
    });
  }
  if (manualHost) manualHost.hidden = counted;

  // 两方对局把推导结果显示在那一行上（多队同场的成绩本身就是输入框，不做回显）
  if (!multi) {
    sides.forEach((side, index) => {
      const row = qs(`[data-sid="${side.key}"]`, bodyEl);
      if (!row) return;
      const put = (role, text) => {
        const node = row.querySelector(`[data-role="${role}"]`);
        if (node) node.textContent = text;
      };
      put('score-label', counted ? '大比分' : sc.label);
      put('points-label', counted ? `总${sc.label}` : '小分');
      put('score-view', fmtScore(entries[index].score, counted, sc));
      put('points-view', fmtVal(entries[index].points, sc));
    });
  }

  // 大比分是计数（多者胜）；没有轮次时 score 就是本场成绩（按判断标准比）
  const better = (x, y) =>
    counted
      ? y.score - x.score || cmpVal(x.points, y.points, sc)
      : cmpVal(x.score, y.score, sc) || cmpVal(x.points, y.points, sc);
  const order = [...entries].sort(better);
  const top = order[0];
  const tied = entries.filter((e) => !better(e, top) && !better(top, e));
  const labels = entries.map((e) => `${e.key} ${fmtScore(e.score, counted, sc)}`).join(' : ');
  let verdict;
  // 「填了没有」问的是**有没有成绩**：数值型的 0 也算（0:0 就是一场 0 比 0）
  if (!entries.some((e) => hasResult(e.score, sc) || e.points)) {
    verdict = '<span class="rpreview__mute">还没有填写成绩</span>';
  } else if (tied.length > 1) {
    verdict =
      allowDraw && !multi
        ? '<span class="rpreview__warn">平局（大比分与总成绩都相同）</span>'
        : '<span class="rpreview__warn">并列，请在下方指定胜方</span>';
  } else {
    verdict = `<b>${esc(top.label)}</b> 胜（${esc(labels)}）`;
  }
  const roundsText = counted
    ? ` · ${rounds
        .map((item, i) => `第${i + 1}轮 ${fmtVal(item.a, sc)}:${fmtVal(item.b, sc)}`)
        .join(' / ')}`
    : '';
  if (box) {
    const note = counted
      ? `大比分 = 赢的轮数 · 总${sc.label} = 各轮合计`
      : multi
        ? `按${sc.label}排名 · ${sc.betterLabel || sc.better}`
        : `${sc.label} · ${sc.betterLabel || sc.better}`;
    box.innerHTML = `${verdict}<span class="rpreview__sub">${esc(note)}${esc(roundsText)}</span>`;
  }
}

/**
 * 录分弹窗：**多轮次录入 + 自动大比分**。
 *
 * 折叠关系（从上往下读一遍就是录入顺序）：
 *   ① 实时预览：大比分 / 胜负 / 各轮明细             —— 常驻
 *   ② 每一方的「大比分 / 总成绩」                    —— 常驻（只读展示）
 *   ③ 轮次录入（默认一轮，可增删；仅两方对局）        —— 常驻
 *   ④ 「直接填本场成绩」（只在没有任何轮次时出现）     —— 与 ③ 互斥
 *   ⑤ 胜方 / 时长 / 起止时间 / 备注                  —— 折叠（details）
 */
function openResultModal(rnd) {
  const sides = rnd.sides || [rnd.sideA, rnd.sideB];
  const sc = scoringOf(App.state);
  const multi = sides.length > 2;
  const stored = rnd.sets || [];
  // 已经「直接填过成绩」的场次仍按原样编辑（想改用轮次就点「添加一轮」）
  // 「填过没填」看录入痕迹：数值型的 0 是合法读数，不能用真假值糊过去
  const hasPlainScore = !stored.length && sides.some((side) => hasEntered(side.score) || side.points);
  const initial = stored.length
    ? stored.map((item) => ({ a: item.a, b: item.b }))
    : multi || hasPlainScore
      ? []
      : [{ a: MISSING, b: MISSING }]; // 全新的一场：默认一轮（空）
  const allowDraw = App.state?.rules?.allowDraw && rnd.stage === 'group';
  const winnerOpts = [['', '自动判定']].concat(
    sides.map((side) => [side.key, `${side.label} 第 1`])
  );
  if (allowDraw && !multi) winnerOpts.push(['DRAW', '平局']);

  const sideRows = sides
    .map((side) => {
      const head =
        `<span class="rrow__key" style="--c:${esc(side.color || 'var(--accent)')}">${esc(side.key)}</span>` +
        `<span class="rrow__name">${esc(side.label)}</span>`;
      // 多队同场没有「轮次」：成绩就是每一方自己那一格 → 直接给输入框
      if (multi) {
        return (
          `<div class="rrow" data-sid="${esc(side.key)}">` +
          head +
          `<label class="rrow__f"><span>${esc(sc.label)}</span>` +
          valueInputHtml('score', side.score, sc) +
          `</label>` +
          `<label class="rrow__f"><span>细则分</span>` +
          valueInputHtml('points', side.points, sc) +
          `</label></div>`
        );
      }
      return (
        `<div class="rrow" data-sid="${esc(side.key)}">` +
        head +
        `<span class="rrow__f"><span data-role="score-label">大比分</span>` +
        `<b data-role="score-view">—</b></span>` +
        `<span class="rrow__f"><span data-role="points-label">总${esc(sc.label)}</span>` +
        `<b data-role="points-view">—</b></span></div>`
      );
    })
    .join('');

  // 没有轮次时才需要手填「本场成绩」（与轮次互斥：显隐由预览里的 refresh 切换）
  const manualRows = sides
    .map(
      (side) =>
        `<div class="rrow" data-sid="${esc(side.key)}">` +
        `<span class="rrow__key" style="--c:${esc(side.color || 'var(--accent)')}">${esc(side.key)}</span>` +
        `<span class="rrow__name">${esc(side.label)}</span>` +
        `<label class="rrow__f"><span>${esc(sc.label)}</span>` +
        valueInputHtml('score', side.score, sc) +
        `</label>` +
        `<label class="rrow__f"><span>小分</span>` +
        valueInputHtml('points', side.points, sc) +
        `</label></div>`
    )
    .join('');

  const roundsBlock = multi
    ? `<div class="notice" style="margin-top:10px">${sides.length} 队同场：按 <b>${esc(
        sc.label
      )}</b>（${esc(sc.betterLabel || sc.better)}）排名，第 1 名即为本场胜者，` +
      `名次分按 ${sides.length}/${sides.length - 1}/…/1 计入小组赛。</div>`
    : `<div class="rsets"><div class="rsets__head"><b>轮次</b>` +
      `<span class="panel__hint">默认一轮；每轮 ${esc(sc.label)}，赢的轮数就是大比分` +
      `${sc.timeBased ? '（可写 1:23.456 或 83.45）' : ''}</span>` +
      `<button class="btn btn--sm" type="button" data-set-add>+ 添加一轮</button></div>` +
      `<div class="rsets__body" id="rq-sets">${initial
        .map((row) => roundRowHtml(row.a, row.b, sc))
        .join('')}</div></div>`;
  const manualBlock = multi
    ? ''
    : `<div id="rq-manual"${initial.length ? ' hidden' : ''}>` +
      `<div class="notice" style="margin-top:10px">没有轮次时按下面这一组「本场成绩」判定；` +
      `点上面的「添加一轮」就改用轮次录入。</div>` +
      `<div class="rrows">${manualRows}</div></div>`;
  const foldOpen = Boolean(
    rnd.duration || rnd.startedAt || rnd.finishedAt || rnd.note || rnd.winner
  );

  Modal.open({
    title: `录入结果 · ${rnd.label || rnd.code}`,
    body:
      `<div id="rq-preview" class="rpreview"></div>` +
      `<div class="rrows">${sideRows}</div>` +
      roundsBlock +
      manualBlock +
      `<details class="rfold"${foldOpen ? ' open' : ''}>` +
      `<summary>胜方 / 时长 / 时间 / 备注</summary>` +
      `<div class="form form--2" style="margin-top:10px">` +
      `<div class="field"><label for="rq-winner">胜方</label><select id="rq-winner">${winnerOpts
        .map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`)
        .join('')}</select></div>` +
      // 这是「这场比赛持续了多久」，与计分里的成绩不是一回事
      `<div class="field"><label for="rq-duration">本场时长（分钟）</label><input id="rq-duration" type="number" min="0" value="${
        rnd.duration || ''
      }" placeholder="可留空"></div>` +
      `<div class="field"><label for="rt-start">开始时间</label><input id="rt-start" type="datetime-local" value="${esc(
        toLocalInput(rnd.startedAt)
      )}"></div>` +
      `<div class="field"><label for="rt-end">结束时间（留空 = 用当前时间）</label><input id="rt-end" type="datetime-local" value="${esc(
        toLocalInput(rnd.finishedAt)
      )}"></div>` +
      `<div class="field" style="grid-column:1/-1"><label for="rn">备注</label><input id="rn" value="${esc(
        rnd.note || ''
      )}"></div>` +
      `</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm" type="button" data-now="rt-start">开始=现在</button>` +
      `<button class="btn btn--sm" type="button" data-now="rt-end">结束=现在</button>` +
      `</div>` +
      `</details>` +
      `<div class="notice" style="margin-top:10px">结算后：${
        rnd.stage === 'group'
          ? '小组赛只影响名次分与排名'
          : `胜者进入下一轮${App.state?.rules?.loserBracket === false ? '，败者直接淘汰' : '，败者进入败者组'}`
      }。留空的开始 / 结束时间会用当前时刻补全。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      // 判弃权（对方直接晋级）也从这里进：总览的树状对阵图整框点击打开的就是本弹窗，
      // 否则为了判弃权还得专门跑一趟「赛程」页
      (sides.filter((side) => side.label || side.source).length >= 2
        ? `<button class="btn btn--sm btn--danger" type="button" data-wo-open>判弃权</button>`
        : '') +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存结果</button>`,
    onMount(bodyEl, footEl) {
      const refresh = () => {
        renumberRounds(bodyEl);
        refreshResultPreview(bodyEl, sides, allowDraw, sc);
      };
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      const woBtn = footEl.querySelector('[data-wo-open]');
      if (woBtn) {
        woBtn.onclick = () => {
          Modal.close();
          openWalkoverModal(rnd);
        };
      }
      bodyEl.querySelectorAll('[data-now]').forEach((btn) => {
        btn.onclick = () => {
          const input = qs(`#${btn.dataset.now}`, bodyEl);
          if (input) input.value = nowLocalInput();
        };
      });
      const addBtn = bodyEl.querySelector('[data-set-add]');
      if (addBtn) {
        addBtn.onclick = () => {
          const host = qs('#rq-sets', bodyEl);
          // 新行留空：0 是合法读数，拿它当「空」会让人一保存就记下两个 0 分
          if (host) host.insertAdjacentHTML('beforeend', roundRowHtml(MISSING, MISSING, sc));
          refresh();
        };
      }
      bodyEl.addEventListener('input', (e) => {
        if (e.target.closest('[data-set-del]')) return;
        refresh();
      });
      bodyEl.addEventListener('click', (e) => {
        const del = e.target.closest('[data-set-del]');
        if (!del) return;
        del.closest('.setrow')?.remove();
        refresh();
      });
      refresh();

      footEl.querySelector('[data-submit]').onclick = async () => {
        const read = (sel) => (qs(sel, bodyEl) || {}).value || '';
        let body;
        try {
          const sets = multi ? [] : readRoundRows(bodyEl, sc);
          // 填了轮次时大比分是「赢的轮数」（计数）：不能拿时间/小数的解析器去解它
          const counted = sets.length > 0;
          const derived = counted ? deriveFromRounds(sets, sc) : null;
          const manualHost = qs('#rq-manual', bodyEl);
          body = {
            winner: read('#rq-winner'),
            note: read('#rn'),
            sets,
            sides: sides.map((side, index) => {
              const host = multi
                ? qs(`[data-sid="${side.key}"]`, bodyEl)
                : qs(`[data-sid="${side.key}"]`, manualHost);
              return {
                key: side.key,
                score: derived
                  ? derived.wins[index]
                  : readValueInput(host, 'score', sc),
                points: derived
                  ? derived.totals[index]
                  : readValueInput(host, 'points', sc),
              };
            }),
            durationMinutes: Number(read('#rq-duration')) || 0,
          };
        } catch (err) {
          toast(err.message || '成绩格式不对', 'err');
          return;
        }
        const startedAt = read('#rt-start');
        const finishedAt = read('#rt-end');
        // 留空 = 交给后端补全（开始沿用已有值、结束用当前时间）
        if (startedAt) body.startedAt = startedAt;
        if (finishedAt) body.finishedAt = finishedAt;
        try {
          const res = await api(`/rounds/${encodeURIComponent(rnd.code)}/result`, {
            method: 'POST',
            auth: true,
            body,
          });
          Modal.close();
          toast(
            res.eventClosed
              ? '结果已保存 · 总冠军已决出，本届自动标记为已结束（要补改先「恢复进行」）'
              : res.finished
                ? '结果已保存 · 赛程已全部结束'
                : '结果已保存',
            'ok',
            res.eventClosed ? 9000 : 3600
          );
          (res.ranking || []).length &&
            log.info(
              '录入结果',
              res.winner,
              res.ranking.map((r) => `${r.key}#${r.rank}`)
            );
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

/**
 * 判某一方弃权（长期没人 / 人数不足）。
 *
 * 弃权方名次垫底，对手直接晋级；淘汰赛的对阵图会自动往下推进，
 * 备注里留一条痕迹，重置该场即可撤销。
 */
function openWalkoverModal(rnd) {
  const sides = rnd.sides || [];
  Modal.open({
    title: `判弃权 · ${rnd.label || rnd.code}`,
    body:
      `<div class="notice">弃权方<b>名次垫底</b>，对手直接晋级` +
      (sides.length > 2
        ? `；本场还有 ${sides.length - 1} 支队，他们继续比赛。`
        : '，淘汰赛对阵图会自动推进。') +
      `</div>` +
      `<div class="field" style="margin-top:10px"><label for="woReason">原因（会写进备注）</label>` +
      `<input id="woReason" value="长期无人到场" placeholder="例如：人数不足 / 超时未到场"></div>` +
      `<div class="wo-list" style="margin-top:10px">` +
      (sides.length
        ? sides
            .map(
              (side) =>
                `<button class="btn btn--block btn--danger" type="button" data-wo="${esc(side.key)}">` +
                `${esc(side.key)} · ${esc(side.label || side.source || '待定')} 弃权</button>`
            )
            .join('')
        : '<div class="empty">本场还没有对阵</div>') +
      `</div>`,
    footer: `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      bodyEl.querySelectorAll('[data-wo]').forEach((btn) => {
        btn.onclick = async () => {
          const key = btn.dataset.wo;
          const label = sides.find((side) => side.key === key)?.label || key;
          const reason = qs('#woReason', bodyEl).value.trim() || '弃权';
          try {
            const res = await api(`/rounds/${encodeURIComponent(rnd.code)}/walkover`, {
              method: 'POST',
              auth: true,
              body: { side: key, reason },
            });
            Modal.close();
            const winner = (res.state?.rounds || [])
              .find((r) => r.code === rnd.code)
              ?.sides.find((side) => side.key === res.winner);
            toast(
              winner ? `${label} 弃权 → ${winner.label} 晋级` : `${label} 已判弃权`,
              'ok',
              6000
            );
            if (res.state) App.state = res.state;
            hooksRenderAdmin();
            renderPublic();
          } catch (err) {
            toast(err.message, 'err', 7000);
          }
        };
      });
    },
  });
}

/* ---------------------- 小组赛对阵调整（开赛前） ---------------------- */

/**
 * 开赛前调整小组赛对阵：**点两个队徽即对调**。
 *
 * 两种对调（都保持「场次数与编号不变、每轮每队只打一场」）：
 *
 * * **同一组、同一轮**：只换「谁碰谁」；
 * * **跨组**：两支队**整队互换**（各自在原来那个组里的每一场都换过去），
 *   于是它们的分组也跟着变——服务端会把 ``group`` 一起改掉，分组名单 / 选手页
 *   跟着同步。两组的出场次数不一样（轮空 / 组大小不同）时换不了，会当场说明。
 *
 * 同一组里跨轮换人仍然禁止：多队同场会有轮空，跨轮换会让某轮的出场次数对不上。
 * 每个组给出「组合覆盖 X/Y」（按**当前草案**算，所以跨组换完立刻看得出结果）：
 * 重复组合会标出来但**不拦**——要不要保留重复组合是组织者的自由。
 * 「恢复默认」＝请服务端按分组算法重排，放弃手改。
 */
function openGroupPairingModal() {
  const rounds = (App.state?.rounds || []).filter((r) => r.stage === 'group');
  if (!rounds.length) {
    toast('本届还没有小组赛', 'warn');
    return;
  }
  if (App.state?.event?.locked) {
    toast('比赛已开始（赛程结构已锁定），要改对阵请先解除锁定', 'warn', 7000);
    return;
  }
  if (rounds.some((r) => roundHasResult(r))) {
    toast('小组赛已经开打，对阵不能再改（先「重置」已录入的比赛）', 'warn', 7000);
    return;
  }
  const teams = App.state?.teams || [];
  const perMatch = Number(App.state?.rules?.teamsPerMatch) || 2;
  const teamById = new Map(teams.map((t) => [t.id, t]));
  const nameOf = (id) => teamById.get(id)?.short || teamById.get(id)?.name || id;
  const colorOf = (id) => teamById.get(id)?.color || 'var(--accent)';
  const groupOf = (rnd) => String(rnd.code || '').split('-')[1] || 'A';
  const list = [...rounds].sort(
    (a, b) =>
      groupOf(a).localeCompare(groupOf(b)) ||
      (a.bracketRound || 0) - (b.bracketRound || 0) ||
      (a.slot || 0) - (b.slot || 0)
  );
  // 草案：code → 队伍 ID 顺序（与 sides 一一对应）；原值用来算「改了哪些」
  let draft = new Map(list.map((r) => [r.code, (r.sides || []).map((s) => s.teamId)]));
  let picked = null; // {code, index}：当前选中的队徽

  const pairKeys = (ids) => {
    const out = [];
    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) out.push([ids[i], ids[j]].sort().join('~'));
    }
    return out;
  };
  /** 某个组的进度提示：2 队对阵报「组合覆盖」，多队同场按场次数报（两两组合对不齐）。 */
  const groupHint = (key) => {
    if (perMatch !== 2) {
      const n = list.filter((r) => groupOf(r) === key).length;
      return { text: `${n} 场 · 每场最多 ${perMatch} 队同场`, warn: false };
    }
    // 「这组有哪些队」按**草案**算：跨组换队之后分组会变，按 team.group 算就是旧名单
    const inGroup = new Set();
    list
      .filter((r) => groupOf(r) === key)
      .forEach((r) => (draft.get(r.code) || []).forEach((id) => id && inGroup.add(id)));
    const need = new Set(pairKeys([...inGroup]));
    const seen = [];
    list
      .filter((r) => groupOf(r) === key)
      .forEach((r) => pairKeys(draft.get(r.code) || []).forEach((k) => seen.push(k)));
    const covered = new Set(seen.filter((k) => need.has(k)));
    const dup = seen.length - covered.size;
    return {
      text: `组合覆盖 ${covered.size}/${need.size}${dup ? ` · 重复 ${dup} 组` : ' · 无重复'}`,
      warn: covered.size < need.size,
    };
  };

  const renderBody = (bodyEl) => {
    const blocks = [];
    let key = '';
    list.forEach((rnd) => {
      const g = groupOf(rnd);
      if (g !== key) {
        key = g;
        const hint = groupHint(g);
        blocks.push(
          `<div class="pair__head"><b>${esc(g)} 组</b>` +
            `<span class="pair__cover${hint.warn ? ' pair__cover--dup' : ''}">${esc(hint.text)}</span></div>`
        );
      }
      blocks.push(
        `<div class="pair__row"><span class="pair__label">第 ${rnd.bracketRound} 轮</span>` +
          (draft.get(rnd.code) || [])
            .map((id, i) => {
              const on = picked && picked.code === rnd.code && picked.index === i;
              return (
                `<button class="pair__team${on ? ' pair__team--on' : ''}" type="button" ` +
                `data-pair="${esc(rnd.code)}" data-index="${i}" title="${esc(nameOf(id))}">` +
                `<i style="background:${esc(colorOf(id))}"></i>${esc(nameOf(id))}</button>`
              );
            })
            .join('<span class="pair__vs">vs</span>') +
          `</div>`
      );
    });
    bodyEl.innerHTML =
      `<div class="notice">开赛前可以换对手：先点一个队徽，再点另一个队徽即可对调。` +
      `<b>同一组同一轮</b>＝只换「谁碰谁」；<b>跨组</b>＝两支队<b>整队互换</b>` +
      `（分组跟着变，分组名单 / 选手页一起同步）。场次数与编号不变，每轮每队仍然只打一场。</div>` +
      `<div class="pair__wrap">${blocks.join('')}</div>`;
  };

  /** 某支队在草案里出现的所有位置——跨组换队要**整队**互换，看的就是它。 */
  const positionsOf = (id) => {
    const out = [];
    list.forEach((r) => {
      (draft.get(r.code) || []).forEach((tid, i) => {
        if (tid === id) out.push({ code: r.code, index: i });
      });
    });
    return out;
  };

  const clickTeam = (bodyEl, code, index) => {
    if (!picked) {
      picked = { code, index };
      renderBody(bodyEl);
      return;
    }
    if (picked.code === code && picked.index === index) {
      picked = null;
      renderBody(bodyEl);
      return;
    }
    const a = list.find((r) => r.code === picked.code);
    const b = list.find((r) => r.code === code);
    if (!a || !b) return;
    const idA = (draft.get(picked.code) || [])[picked.index] || '';
    const idB = (draft.get(code) || [])[index] || '';
    if (!idA || !idB || idA === idB) {
      picked = null;
      renderBody(bodyEl);
      return;
    }
    if (groupOf(a) === groupOf(b)) {
      // 组内：只在同一轮内对调（多队同场有轮空，跨轮会让某轮的出场次数对不上）
      if ((a.bracketRound || 0) !== (b.bracketRound || 0)) {
        toast('同一组里只能在**同一轮**内对调：跨轮换人会让某轮的出场次数对不上', 'warn', 8000);
        picked = { code, index };
        renderBody(bodyEl);
        return;
      }
      const listA = draft.get(picked.code);
      const listB = draft.get(code);
      const tmp = listA[picked.index];
      listA[picked.index] = listB[index];
      listB[index] = tmp;
    } else {
      // 跨组：两支队整队互换（各自组里的每一场一起换），分组随之变化
      const posA = positionsOf(idA);
      const posB = positionsOf(idB);
      if (posA.length !== posB.length) {
        toast(
          `跨组换队要两边场次一样多：${nameOf(idA)} 出场 ${posA.length} 次、` +
            `${nameOf(idB)} 出场 ${posB.length} 次，换不了（可先「恢复默认对阵」或重新生成赛程）`,
          'warn',
          9000
        );
        picked = { code, index };
        renderBody(bodyEl);
        return;
      }
      posA.forEach((p) => {
        draft.get(p.code)[p.index] = idB;
      });
      posB.forEach((p) => {
        draft.get(p.code)[p.index] = idA;
      });
    }
    picked = null;
    renderBody(bodyEl);
  };

  /** 基线：上一次保存（或恢复默认）之后的样子。未保存的改动 = 草案与它不同。 */
  let baseline = new Map(list.map((r) => [r.code, (r.sides || []).map((s) => s.teamId)]));
  const changedRows = () =>
    list
      .filter((r) => (draft.get(r.code) || []).join('~') !== (baseline.get(r.code) || []).join('~'))
      .map((r) => ({ code: r.code, team_ids: draft.get(r.code) }));

  /** 保存改过的场次；成功后基线跟着走，这样「还没保存」的判断才不会一直为真。 */
  const saveRows = async (rows) => {
    const res = await api('/tournament/group-pairings', {
      method: 'POST',
      auth: true,
      body: { rounds: rows },
    });
    if (res.state) App.state = res.state;
    baseline = new Map(draft);
    renderPublic();
    hooksRenderAdmin();
    toast(`对阵已保存：${rows.length} 场（用户端已刷新）`, 'ok', 6000);
    return res;
  };

  // 关闭前拦一道：改过还没保存就问一句（保存成功 / 放弃改动都能关）
  let closing = false;

  Modal.open({
    title: '调整小组赛对阵',
    body: '<div id="pairBody"></div>',
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm" type="button" data-reset>恢复默认对阵</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-save>保存对阵</button>`,
    onBeforeClose: () => {
      if (closing) return true;
      const rows = changedRows();
      if (!rows.length) return true;
      const save = window.confirm(
        `有 ${rows.length} 场对阵改过但还没保存。\n\n` +
          `「确定」= 保存后关闭；「取消」= 放弃这些改动并关闭。`
      );
      if (!save) return true; // 放弃改动，直接关
      // 先拦住这一次关闭，保存完再自己关（失败就留在弹窗里，别把改动丢了）
      closing = true;
      saveRows(rows)
        .then(() => Modal.close())
        .catch((err) => {
          closing = false;
          toast(err.message, 'err', 8000);
        });
      return false;
    },
    onMount(bodyEl, footEl) {
      renderBody(bodyEl);
      bodyEl.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-pair]');
        if (btn) clickTeam(bodyEl, btn.dataset.pair, Number(btn.dataset.index));
      });
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-save]').onclick = async () => {
        const rows = changedRows();
        if (!rows.length) {
          toast('对阵没有变化', 'info');
          return;
        }
        const btn = footEl.querySelector('[data-save]');
        if (btn) btn.disabled = true;
        try {
          await saveRows(rows);
          closing = true;
          Modal.close();
        } catch (err) {
          toast(err.message, 'err', 8000);
        } finally {
          if (btn) btn.disabled = false;
        }
      };
      footEl.querySelector('[data-reset]').onclick = async () => {
        if (!window.confirm('恢复成算法默认排法？手改过的对阵会被覆盖。')) return;
        try {
          const res = await api('/tournament/group-pairings', {
            method: 'POST',
            auth: true,
            body: { reset: true },
          });
          if (res.state) App.state = res.state;
          const now = new Map((res.state?.rounds || []).map((r) => [r.code, r]));
          draft = new Map(
            list.map((r) => [r.code, ((now.get(r.code) || r).sides || []).map((s) => s.teamId)])
          );
          baseline = new Map(draft);
          picked = null;
          renderBody(bodyEl);
          renderPublic();
          hooksRenderAdmin();
          toast('已恢复默认对阵（用户端已刷新）', 'ok', 5000);
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

/**
 * 本场直播地址：**只提供推流 / 播放地址**（不放直播开关，也没有比赛编号的地址）。
 *
 * 推流标识只有**选手自己的唯一流名**（``tom``）：整届赛事都是同一个地址，
 * 换比赛不用重推。这里按「本场有哪些选手」列出他们各自的地址。
 */
function openRoundLiveModal(rnd) {
  const rs = roundStreamsOf(rnd.code);
  const fallback = rnd.streams || {};
  const sets = App.private?.protocols || [];
  const cast = rs?.cast || fallback.cast || [];
  // 每位选手一行，各带他自己的完整地址表（推流地址只按选手区分）
  const rows = cast.map((item) => ({
    name: item.name || item.playerId,
    endpoints: item.endpoints || item.play || {},
  }));

  // 推流格的复制按钮带 data-tip="push"：复制后由统一动作再提醒一次「别开 B 帧」
  const cell = (url, kind = 'play') =>
    url
      ? `<span class="streams__url"><span class="streams__val">${esc(url)}</span>` +
        `<button class="btn btn--sm" type="button" data-act="copy" data-copy="${esc(url)}"` +
        `${kind === 'play' ? '' : ' data-tip="push"'}` +
        ` title="${esc(
          kind === 'play' ? '复制播放地址' : `复制推流地址 · ${PUSH_TIP_LINE}`
        )}">复制</button></span>`
      : `<span class="streams__url"><i>—</i></span>`;

  // 一套协议一张表：列由后端下发（只列已配置的协议）
  const block = (set) => {
    const cols = set.columns || [];
    const grid = `style="grid-template-columns:minmax(0,.85fr) repeat(${cols.length}, minmax(0,1.7fr))"`;
    return (
      `<div class="streams streams--${esc(set.id)}">` +
      `<div class="streams__title"><b>${esc(set.label)}</b>` +
      (set.badge
        ? `<span class="streams__badge streams__badge--${esc(set.id)}">${esc(set.badge)}</span>`
        : '') +
      `<span>${esc(set.note || '')}</span></div>` +
      `<div class="streams__row streams__row--head" ${grid}><span>对象</span>` +
      cols
        .map(
          (col) =>
            `<span class="streams__col" title="${esc(col.hint || '')}">` +
            `${esc(col.label)}</span>`
        )
        .join('') +
      `</div>` +
      rows
        .map(
          (row) =>
            `<div class="streams__row" ${grid}><span class="streams__who">${esc(row.name)}</span>` +
            cols.map((col) => cell(row.endpoints?.[col.key] || '', col.kind)).join('') +
            `</div>`
        )
        .join('') +
      `</div>`
    );
  };

  const tables = sets.length
    ? sets.map(block).join('')
    : `<div class="notice notice--warn">还没有配置直播根地址（WebRTC / HLS），` +
      `请到「直播配置」里填写。</div>`;

  Modal.open({
    title: `直播地址 · ${rnd.label || rnd.code}`,
    body:
      // 推流地址就在下面：先把「WHIP / 关掉 B 帧」讲清楚
      pushTipsHtml() +
      tables +
      (cast.length
        ? `<div class="notice" style="margin-top:10px">本场有 ${cast.length} 路选手机位：` +
          `<b>推流用 WHIP</b>（WebRTC / UDP，延迟最低）；观看有 WebRTC 与 HLS 两条线路，` +
          `观众可自行切换。选手的地址整届固定，换比赛不用重推。</div>`
        : `<div class="notice notice--warn" style="margin-top:10px">本场选手都还没有推流流名，` +
          `可在「选手」页点该选手的「编辑」填推流流名（每位必须唯一），填好后这里会自动生成推流与播放地址。</div>`) +
      `<div class="notice" style="margin-top:10px">推流地址只在管理端显示，观众只会拿到播放地址；` +
      `观众在直播页按<b>比赛</b>筛选机位。</div>`,
    footer: `<button class="btn btn--sm btn--ghost" type="button" data-close>关闭</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
    },
  });
}

/**
 * 单独编辑一场比赛的时间。
 *
 * 与录分弹窗的差别：这里空值 = **清空**（可以直接撤销误填的时间），
 * 且不会改动比分与状态之外的任何东西。
 */
function openRoundTimesModal(rnd) {
  Modal.open({
    title: `比赛时间 · ${rnd.label || rnd.code}`,
    body:
      `<div class="form form--2">` +
      `<div class="field"><label for="rt-sched">计划时间</label><input id="rt-sched" type="datetime-local" value="${esc(
        toLocalInput(rnd.scheduledAt)
      )}"></div>` +
      `<div class="field"><label for="rt-start">开始时间</label><input id="rt-start" type="datetime-local" value="${esc(
        toLocalInput(rnd.startedAt)
      )}"></div>` +
      `<div class="field"><label for="rt-end">结束时间（可选）</label><input id="rt-end" type="datetime-local" value="${esc(
        toLocalInput(rnd.finishedAt)
      )}"></div>` +
      `<div class="field"><label>当前状态</label><div class="panel__hint">${
        rnd.timeState === 'finished'
          ? '已结束'
          : rnd.timeState === 'running'
            ? '进行中 · 结束待定'
            : rnd.timeState === 'scheduled'
              ? '未开始（已排时间）'
              : '未开始（时间待定）'
      }</div></div>` +
      `</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<button class="btn btn--sm" type="button" data-now="rt-sched">计划=现在</button>` +
      `<button class="btn btn--sm" type="button" data-now="rt-start">开始=现在</button>` +
      `<button class="btn btn--sm" type="button" data-now="rt-end">结束=现在</button>` +
      `<button class="btn btn--sm" type="button" data-clear-all>清空全部</button>` +
      `</div>` +
      `<div class="notice" style="margin-top:10px">只填开始时间 = 进行中（结束时间留空即<b>结束待定</b>）；` +
      `填了结束时间即视为已结束，但仍需<b>录入比分</b>才会结算晋级。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存时间</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      bodyEl.querySelectorAll('[data-now]').forEach((btn) => {
        btn.onclick = () => {
          const input = qs(`#${btn.dataset.now}`, bodyEl);
          if (input) input.value = nowLocalInput();
        };
      });
      const clearBtn = bodyEl.querySelector('[data-clear-all]');
      if (clearBtn) {
        clearBtn.onclick = () => {
          ['rt-sched', 'rt-start', 'rt-end'].forEach((id) => {
            const input = qs(`#${id}`, bodyEl);
            if (input) input.value = '';
          });
        };
      }
      footEl.querySelector('[data-submit]').onclick = async () => {
        const read = (id) => qs(`#${id}`, bodyEl).value.trim();
        try {
          const res = await api(`/rounds/${encodeURIComponent(rnd.code)}/times`, {
            method: 'POST',
            auth: true,
            body: {
              scheduledAt: read('rt-sched'),
              startedAt: read('rt-start'),
              finishedAt: read('rt-end'),
            },
          });
          Modal.close();
          toast('时间已更新', 'ok');
          (res.warnings || []).forEach((w) => toast(w, 'warn', 7000));
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

/* ------------------------- 开赛与锁定（二次确认） ------------------------- */

/** 开赛前的准备情况摘要（与面板共用同一份判断）。 */
function readinessHtml(r) {
  return (
    `<div class="kv kv--inline" style="margin-top:10px">` +
    `<div class="kv__row"><dt>赛制</dt><dd>${esc(r.format)}</dd></div>` +
    `<div class="kv__row"><dt>参与选手</dt><dd>${r.joined} 人</dd></div>` +
    `<div class="kv__row"><dt>队伍</dt><dd>${r.league ? '—' : `${r.teams} 支`}</dd></div>` +
    `<div class="kv__row"><dt>赛程</dt><dd>${r.rounds} 场</dd></div></div>` +
    (r.warnings.length
      ? `<div class="notice notice--warn" style="margin-top:10px">${r.warnings.map(esc).join('<br>')}</div>`
      : '')
  );
}

/**
 * 开始比赛（二次确认）。
 *
 * 二次确认不是走过场：必须先勾选「我已确认」，按钮才会变成可用，
 * 服务端也要求 ``confirm: true``，两边都拦一道。
 */
function startEvent() {
  const r = startReadiness(App.state);
  if (r.locked) {
    toast('比赛已经开始', 'info');
    return;
  }
  Modal.open({
    title: '开始比赛（二次确认）',
    body:
      `<div class="notice">开始后 <b>赛制与参赛名单将锁定</b>：赛制、每队人数、每场同场队伍数、` +
      `败者组开关、参与名单、重新组队、赛程重建与清空、删除选手都会被拒绝。<br>` +
      `<b>仍然可用</b>：<b>直播开关</b>、<b>对局替补 / 队伍换人</b>（替上的人不在名单里会自动加入）、` +
      `录分与重置、时间登记、弃权、赛事信息与界面配置。</div>` +
      readinessHtml(r) +
      `<div style="margin-top:12px">${fieldSwitch(
        'confirmStart',
        '我已确认参与名单与赛制无误，开始后不再调整',
        false
      )}</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit disabled>确认开始比赛</button>`,
    onMount(bodyEl, footEl) {
      const box = qs('#f-confirmStart', bodyEl);
      const submit = footEl.querySelector('[data-submit]');
      // 必须勾选确认才能点：这就是二次确认的那一步
      box.onchange = () => {
        submit.disabled = !box.checked;
        submit.title = box.checked ? '' : '请先勾选确认';
      };
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      submit.onclick = async () => {
        try {
          const res = await api('/event/start', {
            method: 'POST',
            auth: true,
            body: { confirm: true },
          });
          Modal.close();
          if (res.state) App.state = res.state;
          await refreshPrivate();
          renderPublic();
          renderAdmin();
          toast('比赛已开始：赛制与参赛名单已锁定', 'ok', 6000);
          (res.warnings || []).forEach((w) => toast(w, 'warn', 8000));
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

/** 解除锁定（同样二次确认）：重新允许调整赛制与名单。 */
function unlockEvent() {
  Modal.open({
    title: '解除锁定（二次确认）',
    body:
      `<div class="notice notice--warn">解除后可以再次调整<b>赛制、每队人数、参赛名单、重新组队与重建赛程</b>。` +
      `已录入的比分不会被清除，但结构性改动会让相关对局作废重来。</div>` +
      `<div class="field" style="margin-top:10px"><label for="unlockReason">原因（可选，会写进日志）</label>` +
      `<input id="unlockReason" placeholder="例如：有选手临时缺席，需要增补名单"></div>` +
      `<div style="margin-top:10px">${fieldSwitch('confirmUnlock', '我确认要解除锁定', false)}</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--danger" type="button" data-submit disabled>确认解除锁定</button>`,
    onMount(bodyEl, footEl) {
      const box = qs('#f-confirmUnlock', bodyEl);
      const submit = footEl.querySelector('[data-submit]');
      box.onchange = () => {
        submit.disabled = !box.checked;
      };
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      submit.onclick = async () => {
        try {
          const res = await api('/event/unlock', {
            method: 'POST',
            auth: true,
            body: { confirm: true, reason: qs('#unlockReason', bodyEl).value.trim() },
          });
          Modal.close();
          if (res.state) App.state = res.state;
          renderPublic();
          renderAdmin();
          toast('已解除锁定，可以再次调整赛制与名单', 'warn', 6000);
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

/**
 * 队伍换人（固定队伍）：队内 1:1 换人，不动赛程结构。
 *
 * 用于「到不齐人」：换上的人不在参与名单里时会由服务端**自动加入**，开赛后同样可用。
 * 只换某一场、或从某一场起换人，请用积分制赛程里的「对局替补」。
 */
function openTeamSubModal() {
  const s = App.state || {};
  const teams = s.teams || [];
  if (!teams.length) {
    toast('本届还没有固定队伍：请先随机组队，或在对局里点选手换人', 'warn', 7000);
    return;
  }
  const players = s.players || [];
  const joined = new Set(s.participants || []);
  const options = teams
    .map((t) => `<option value="${esc(t.id)}">${esc(t.label || t.name || t.id)}</option>`)
    .join('');

  Modal.open({
    title: '队伍换人',
    body:
      `<div class="form form--2">` +
      `<div class="field"><label for="subTeam">队伍</label><select id="subTeam">${options}</select></div>` +
      `<div class="field"><label for="subFrom">换下（本队队员）</label><select id="subFrom"></select></div>` +
      `<div class="field"><label for="subTo">换上（候选池 / 其他人）</label><select id="subTo"></select></div>` +
      `</div>` +
      `<div class="notice" style="margin-top:10px">只换这支队伍里的一个人，<b>不动赛程结构</b>：` +
      `该队未结算的对局会同步成新阵容，已打完的对局保留当时的阵容与比分。` +
      `换上的人若不在参与名单里会<b>自动加入</b>。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>确认换人</button>`,
    onMount(bodyEl, footEl) {
      const teamSel = qs('#subTeam', bodyEl);
      const fromSel = qs('#subFrom', bodyEl);
      const toSel = qs('#subTo', bodyEl);

      const fill = () => {
        const team = teams.find((t) => t.id === teamSel.value) || teams[0];
        const members = team.playerIds || [];
        const nameOf = (pid) => players.find((p) => p.id === pid)?.name || pid;
        fromSel.innerHTML =
          members.map((pid) => `<option value="${esc(pid)}">${esc(nameOf(pid))}</option>`).join('') ||
          `<option value="">（该队没有队员）</option>`;
        // 换上的人：本队以外的所有选手，标出「未参与本届」与「已停用」
        toSel.innerHTML =
          players
            .filter((p) => !members.includes(p.id))
            .map(
              (p) =>
                `<option value="${esc(p.id)}">${esc(p.name || p.id)}` +
                `${joined.has(p.id) ? '' : ' · 未参与本届（自动加入）'}` +
                `${p.active === false ? ' · 已停用' : ''}</option>`
            )
            .join('') || `<option value="">（没有可换的选手，请先到「选手」页新增）</option>`;
      };
      teamSel.onchange = fill;
      fill();

      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const fromId = fromSel.value;
        const toId = toSel.value;
        if (!fromId || !toId) {
          toast('请选择要换下与换上的选手', 'warn');
          return;
        }
        try {
          const res = await api(`/teams/${encodeURIComponent(teamSel.value)}/substitute`, {
            method: 'POST',
            auth: true,
            body: { fromId, toId },
          });
          Modal.close();
          if (res.state) App.state = res.state;
          renderPublic();
          renderAdmin();
          toast(`${res.from.name} → ${res.to.name}：队伍已换人`, 'ok', 6000);
          (res.addedToParticipants || []).forEach((name) =>
            toast(`${name} 不在参与名单里，已自动加入本届名单`, 'info', 8000)
          );
          if (res.keptRounds?.length) {
            toast(`已打完的 ${res.keptRounds.length} 场对局保留原阵容与比分`, 'info', 7000);
          }
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

/** 把赛事信息表单中的时间输入框填成当前时间（仅改输入框，保存才落库）。 */
function fillEventTime(name) {
  const input = qs(`#f-${name}`, qs('#adminPanel'));
  if (!input) return;
  input.value = nowLocalInput();
  toast('已填入当前时间，别忘了点「保存赛事信息」', 'info', 4000);
}

function clearEventTimeField(name) {
  const input = qs(`#f-${name}`, qs('#adminPanel'));
  if (!input) return;
  input.value = '';
  toast('已清空该时间，保存后生效', 'info');
}

/* --------------------------- 积分制操作 --------------------------- */
function openPlayerPicker({ title, currentIds = [], onPick, onRemove }) {
  const rosterKnown = Boolean(App.state?.participantsSet);
  const joined = new Set(App.state?.participants || []);
  const items = (App.state?.players || [])
    .map((p) => {
      const cur = currentIds.includes(p.id);
      const out = rosterKnown && !joined.has(p.id);
      const meta = [p.tag, out ? '未参与本届' : '', p.active === false ? '停用' : '']
        .filter(Boolean)
        .join(' · ');
      return (
        `<button type="button" class="pick${cur ? ' pick--current' : ''}${out ? ' pick--out' : ''}" data-pid="${esc(p.id)}">` +
        avaHtml(p, 'sm') +
        `<span class="who__txt"><span class="who__name">${esc(p.name || p.id)}</span>` +
        `<span class="pick__meta">${esc(meta || p.id)}</span></span></button>`
      );
    })
    .join('');
  Modal.open({
    title,
    body: `<div class="pick-list">${items || '<div class="empty"><b>无可选选手</b></div>'}</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      (onRemove ? `<button class="btn btn--sm btn--danger" type="button" data-remove>移出阵容</button>` : ''),
    onMount(bodyEl, footEl) {
      const removeBtn = footEl.querySelector('[data-remove]');
      if (removeBtn) {
        removeBtn.onclick = () => {
          Modal.close();
          onRemove();
        };
      }
      bodyEl.querySelectorAll('.pick').forEach((btn) => {
        btn.onclick = () => {
          const pid = btn.dataset.pid;
          Modal.close();
          onPick(pid);
        };
      });
    },
  });
}

/** 积分制：生成赛程（动态轮换 / 固定队伍）。 */
function openScheduleModal() {
  Modal.open({
    title: '生成赛程 · 积分制',
    body:
      `<div class="form form--2">` +
      fieldSelect('mode', '分组模式', 'rotate', [
        ['rotate', '动态轮换（按参与名单）'],
        ['fixed', '固定队伍（按队伍配置）'],
      ]) +
      fieldNum('totalRounds', '总轮次', App.state?.rules?.totalRounds || 5) +
      fieldNum('seed', '随机种子（改数字可重新排布）', Math.floor(Math.random() * 900000) + 100000) +
      `</div>` +
      `<div class="notice" style="margin-top:10px">按参与名单自动 2v2，尽量不重复搭档与四人组合，` +
      `并均衡出场与轮空。生成会<strong>覆盖现有全部对局与比分</strong>。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>生成</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const mode = qs('#f-mode', bodyEl).value;
        const totalRounds = Number(qs('#f-totalRounds', bodyEl).value) || 0;
        const seedRaw = qs('#f-seed', bodyEl).value;
        const seed = seedRaw === '' ? null : Number(seedRaw);
        try {
          const res = await api('/schedule/generate', {
            method: 'POST',
            auth: true,
            body: { mode, totalRounds, seed },
          });
          Modal.close();
          const repeats = res.quality?.partnerRepeats || 0;
          toast(
            `已生成 ${res.count} 局 · ${repeats === 0 ? '无重复搭档' : `${repeats} 组重复搭档`}`,
            repeats === 0 ? 'ok' : 'warn',
            5000
          );
          (res.warnings || []).forEach((w) => toast(w, 'info', 6000));
          if (res.state) App.state = res.state;
          hooksRenderAdmin();
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

/** 积分制：追加补赛。 */
function openAppendModal() {
  Modal.open({
    title: '追加补赛',
    body:
      `<div class="form form--2">` +
      fieldNum('count', '追加局数', 1) +
      fieldNum('seed', '随机种子（改数字可换队友）', Math.floor(Math.random() * 900000) + 100000) +
      `</div>` +
      `<div class="notice" style="margin-top:10px">优先安排目前出场次数最少的选手（含替补），` +
      `队友随机并尽量避免重复搭档；已有的对局与比分不受影响。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>追加</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const count = Number(qs('#f-count', bodyEl).value) || 1;
        const seedRaw = qs('#f-seed', bodyEl).value;
        const seed = seedRaw === '' ? null : Number(seedRaw);
        try {
          const res = await api('/schedule/append', { method: 'POST', auth: true, body: { count, seed } });
          Modal.close();
          toast(`已追加 ${res.added} 局，当前共 ${res.count} 局`, 'ok');
          (res.warnings || []).forEach((w) => toast(w, 'info', 6000));
          if (res.state) App.state = res.state;
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

/** 把预估结构渲染成一行提示（快速创建分组弹窗用）。 */
function renderPlan(plan, box) {
  if (!box) return;
  if (!plan) {
    box.className = 'notice';
    box.textContent = '正在估算…';
    return;
  }
  if (!plan.ok) {
    box.className = 'notice notice--warn';
    box.innerHTML =
      `<b>还开不了赛</b>${esc((plan.warnings || []).join('；'))}` +
      `<br>当前参赛 ${plan.players} 人 · 每队 ${plan.teamSize} 人`;
    return;
  }
  const groups = (plan.groupSizes || []).map((g) => `${g.key} 组 ${g.teams} 队`).join(' / ');
  const shape = plan.teamsPerMatch === 2 ? '组 vs 组' : `${plan.teamsPerMatch} 队同场`;
  box.className = 'notice';
  box.innerHTML =
    `<b>${plan.players} 人 → ${plan.teams} 支队</b>（每队 ${plan.teamSize} 人）` +
    `<br>小组赛：${plan.groupCount} 组${groups ? `（${esc(groups)}）` : ''} · 每场 ${shape}` +
    ` → ${plan.groupMatches} 场` +
    `<br>淘汰赛：${plan.size} 强${plan.loserBracket ? '双败' : '单败'}（` +
    `${esc((plan.knockoutRounds || []).join(' → '))}） → ${plan.knockoutMatches} 场` +
    `<br><b>合计 ${plan.total} 场</b>` +
    (plan.warnings && plan.warnings.length
      ? `<br><span class="plan__warn">${esc(plan.warnings.join('；'))}</span>`
      : '');
}

/**
 * 快速创建分组：开赛前（还没有比赛结果）一键「重新随机组队 + 生成赛程」。
 *
 * 弹窗里可微调每个组的人数、小组数、淘汰赛规模、每场同场队伍数与败者组开关，
 * 结构会实时预估：人少自动少分组、淘汰赛从 4/8 强起步；人多则拉长赛程。
 */
export function openQuickGroupModal() {
  const s = App.state || {};
  const rules = s.rules || {};
  const players = (s.participants || []).length || (s.players || []).length;
  const done = (s.rounds || []).filter((r) => r.status === 'done' || r.winner).length;
  const opts = s.format?.sizeOptions || [];
  const sizeOpts = [['0', '自动（按队伍数）']].concat(opts.map((n) => [String(n), `${n} 强`]));
  let timer = null;
  let seq = 0;

  Modal.open({
    title: '快速创建分组 · 锦标赛制',
    body:
      `<div class="notice${done ? ' notice--warn' : ''}">` +
      (done
        ? `已经产生 <b>${done}</b> 场比赛结果，重新分组会<b>清空全部比分与晋级关系</b>。`
        : '比赛尚未开始（还没有任何结果），可以随时重新分组。') +
      `　当前参赛 <b>${players}</b> 人。</div>` +
      `<div class="form form--2" style="margin-top:10px">` +
      fieldSelect(
        'teamSize',
        '每个组的人数',
        Number(rules.teamSize) || 2,
        [['1', '1 人'], ['2', '2 人（默认）'], ['3', '3 人'], ['4', '4 人'], ['5', '5 人'], ['6', '6 人']],
        { hint: '凑不满一组的人留在候选池，可在组队台里顶上' }
      ) +
      fieldNum('groupCount', '小组数（0 = 自动）', Number(rules.groupCount) || 0, {
        hint: '自动 = 在「每组排得满一场」的前提下尽量多分组（队伍越多组越多，小组赛更短）；下面会按当前人数实时预估',
      }) +
      fieldSelect('size', '淘汰赛规模', '0', sizeOpts, { hint: '人少自动短赛程（如 4/8 强）' }) +
      fieldSelect(
        'teamsPerMatch',
        '每场比赛',
        Number(rules.teamsPerMatch) || 2,
        [['2', '组 vs 组'], ['3', '组 vs 组 vs 组'], ['4', '组 vs 组 vs 组 vs 组']]
      ) +
      fieldSwitch('loserBracket', '启用败者组（双败淘汰）', rules.loserBracket !== false, {
        hint: '关 = 输一场即淘汰',
      }) +
      fieldNum('seed', '随机种子', Math.floor(Math.random() * 900000) + 100000, {
        hint: '换个数就重新洗一次牌',
      }) +
      `</div>` +
      `<div id="qgPreview" class="notice" style="margin-top:10px">正在估算…</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>重新分组并生成赛程</button>`,
    onMount(bodyEl, footEl) {
      const read = () => ({
        teamSize: Number(qs('#f-teamSize', bodyEl).value) || 2,
        groupCount: Number(qs('#f-groupCount', bodyEl).value) || 0,
        size: Number(qs('#f-size', bodyEl).value) || 0,
        teamsPerMatch: Number(qs('#f-teamsPerMatch', bodyEl).value) || 2,
        loserBracket: qs('#f-loserBracket', bodyEl).checked,
      });
      const box = qs('#qgPreview', bodyEl);
      const refresh = async () => {
        const mine = ++seq;
        try {
          const plan = await api('/tournament/preview', {
            method: 'POST',
            auth: true,
            body: read(),
          });
          if (mine === seq) renderPlan(plan, box);
        } catch (err) {
          if (mine === seq) {
            box.className = 'notice notice--warn';
            box.textContent = err.message;
          }
        }
      };
      bodyEl.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(refresh, 260);
      });
      bodyEl.addEventListener('change', () => {
        clearTimeout(timer);
        timer = setTimeout(refresh, 120);
      });
      refresh();

      footEl.querySelector('[data-close]').onclick = () => {
        clearTimeout(timer);
        Modal.close();
      };
      footEl.querySelector('[data-submit]').onclick = async () => {
        const params = read();
        if (done && !window.confirm(`已产生 ${done} 场结果，确认重新分组？全部比分会被清空。`)) {
          return;
        }
        try {
          const res = await api('/tournament/generate', {
            method: 'POST',
            auth: true,
            body: { ...params, reform: true },
          });
          clearTimeout(timer);
          Modal.close();
          toast(
            `已创建 ${res.teams} 支队 / ${res.count} 场（${res.size || 0} 强 · ${
              res.loserBracket ? '双败' : '单败'
            }）`,
            'ok',
            5000
          );
          (res.warnings || []).forEach((w) => toast(w, 'info', 9000));
          if (res.state) App.state = res.state;
          hooksRenderAdmin();
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

/** 锦标赛制：生成赛程并选择淘汰赛规模。 */
function openTournamentModal() {
  const s = App.state || {};
  const opts = s.format?.sizeOptions || [];
  const maxSize = s.format?.maxSize || 0;
  const cur = String(s.rules?.knockoutSize || 0);
  const sizeOpts = [['0', maxSize ? `自动（当前上限 ${maxSize} 强）` : '自动']].concat(
    opts.map((n) => [String(n), `${n} 强${n === maxSize ? '（上限）' : ''}`])
  );
  const perMatch = Number(s.rules?.teamsPerMatch) || 2;
  const loser = s.rules?.loserBracket !== false;
  Modal.open({
    title: '生成赛程 · 锦标赛制',
    body:
      `<div class="form form--2">` +
      fieldSelect('size', '淘汰赛规模', cur, sizeOpts) +
      fieldSelect(
        'teamsPerMatch',
        '小组赛每场',
        perMatch,
        [['2', '组 vs 组'], ['3', '组 vs 组 vs 组'], ['4', '组 vs 组 vs 组 vs 组']],
        { hint: '淘汰赛恒为 2 队对阵' }
      ) +
      fieldSwitch('loserBracket', '启用败者组（双败淘汰）', loser, {
        hint: '关 = 单败，输一场即淘汰',
      }) +
      fieldNum('seed', '随机种子（改数字可重新排布）', Math.floor(Math.random() * 900000) + 100000) +
      `</div>` +
      `<div class="notice" style="margin-top:10px">赛程 = 小组赛轮转（按名次分排名）+ ` +
      `${loser ? '双败淘汰（含败者组，最后胜者组冠军 vs 败者组冠军）' : '单败淘汰（最后一轮即决赛）'}。` +
      `规模越小，小组赛淘汰的队伍越多；生成会<strong>覆盖现有全部对局与比分</strong>。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>生成</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const size = Number(qs('#f-size', bodyEl).value) || 0;
        const teamsPerMatch = Number(qs('#f-teamsPerMatch', bodyEl).value) || 2;
        const loserBracket = qs('#f-loserBracket', bodyEl).checked;
        const seedRaw = qs('#f-seed', bodyEl).value;
        const seed = seedRaw === '' ? null : Number(seedRaw);
        try {
          const res = await api('/tournament/generate', {
            method: 'POST',
            auth: true,
            body: { size, seed, teamsPerMatch, loserBracket },
          });
          Modal.close();
          toast(
            `已生成 ${res.count} 场（${res.teams} 支队伍 · ${res.size || 0} 强` +
              ` · 每场 ${res.teamsPerMatch || 2} 队 · ${res.loserBracket ? '双败' : '单败'}）`,
            'ok',
            5000
          );
          (res.warnings || []).forEach((w) => toast(w, 'info', 9000));
          if (res.state) App.state = res.state;
          hooksRenderAdmin();
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

async function roundAppend() {
  try {
    const res = await api('/rounds', { method: 'POST', auth: true });
    toast(`已追加一局，当前共 ${res.count} 局`, 'ok');
    if (res.state) App.state = res.state;
    renderPublic();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function roundDelete(ref) {
  if (!window.confirm(`确认删除 ${ref}？删除后其余局号会自动前移，已录入的比分一并丢失。`)) return;
  try {
    const res = await api(`/rounds/${encodeURIComponent(ref)}`, { method: 'DELETE', auth: true });
    toast(`已删除，剩余 ${res.count} 局`, 'ok');
    renderPublic();
  } catch (err) {
    toast(err.message, 'err');
  }
}

/**
 * 积分制替补：点某位选手 → 换下他、选定替补与生效范围。
 *
 * 三档范围：仅当前比赛 / 本场及之后 / 全场。同一选手在同一范围再次指定即为**改人**，
 * 页面上只保留最后一次；已在生效的替补可在本窗口或「总览 → 本届替补」里取消。
 */
function openSubstituteModal(ref, fromId) {
  const s = App.state || {};
  const rnd = roundOf(ref);
  const from = (s.players || []).find((p) => p.id === fromId);
  if (!rnd || !from) return;
  const sides = [rnd.sideA, rnd.sideB].filter(Boolean);
  const onCourt = sides.flatMap((side) => (side.players || []).map((p) => p.id));
  const rosterKnown = Boolean(s.participantsSet);
  const joined = new Set(s.participants || []);
  const existing = (s.substitutions || []).filter((item) => item.fromId === fromId);
  const who = (p) => (p ? p.name || p.tag || p.id : '');
  let chosen = '';

  const existingHtml = existing.length
    ? `<div class="notice notice--warn" style="margin-top:10px">已有替补：` +
      existing
        .map(
          (item) =>
            `<b>${esc(item.toName || item.toId)}</b>（${esc(item.scopeLabel || '')}）` +
            `<button class="btn btn--sm btn--danger" type="button" data-act="sub-cancel" ` +
            `data-id="${esc(item.id)}">取消</button>`
        )
        .join('') +
      `</div>`
    : '';

  const candidates = (s.players || [])
    .filter((p) => p.id !== fromId)
    .sort((a, b) => Number(onCourt.includes(a.id)) - Number(onCourt.includes(b.id)))
    .map((p) => {
      const clash = onCourt.includes(p.id);
      const out = rosterKnown && !joined.has(p.id);
      const meta = [
        p.tag || p.id,
        clash ? '已在本场阵容' : '',
        out ? '未参与本届（自动加入）' : '',
        p.active === false ? '停用' : '',
      ]
        .filter(Boolean)
        .join(' · ');
      return (
        `<button type="button" class="pick${clash ? ' pick--out' : ''}" data-pid="${esc(p.id)}"` +
        (clash ? ' disabled' : '') +
        `>${avaHtml(p, 'sm')}<span class="who__txt"><span class="who__name">${esc(who(p))}</span>` +
        `<span class="pick__meta">${esc(meta)}</span></span></button>`
      );
    })
    .join('');

  Modal.open({
    title: `安排替补 · ${who(from)}`,
    body:
      `<div class="notice"><b>${esc(who(from))}</b> 下场，由你选的人顶上（按实际出场计入积分榜）；` +
      `已结算的比赛不改写。</div>` +
      existingHtml +
      `<div class="form form--2" style="margin-top:10px">` +
      fieldSelect('scope', '影响范围', 'round', [
        ['round', `仅当前比赛（${rnd.label || rnd.code}）`],
        ['rest', '本场及之后（从这场起全部替换）'],
        ['event', '全场（本届所有比赛）'],
      ]) +
      `</div>` +
      `<div class="field" style="margin-top:8px"><label>选择替补选手</label></div>` +
      `<div class="pick-list">${
        candidates || '<div class="empty"><b>没有可选的选手</b>请先在「选手」页新增</div>'
      }</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--danger" type="button" data-remove>移出阵容</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit disabled>确定替补</button>`,
    onMount(bodyEl, footEl) {
      const submit = footEl.querySelector('[data-submit]');
      bodyEl.querySelectorAll('.pick').forEach((btn) => {
        btn.onclick = () => {
          chosen = btn.dataset.pid;
          bodyEl.querySelectorAll('.pick').forEach((item) => item.classList.remove('pick--current'));
          btn.classList.add('pick--current');
          submit.disabled = false;
        };
      });
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-remove]').onclick = async () => {
        const side = sides.find((item) => (item.players || []).some((p) => p.id === fromId));
        if (!side) {
          // 已经被替补换下（或本场没上场）：没有可移出的位置
          toast('这位选手已经不在本场阵容里了', 'warn', 5000);
          return;
        }
        const ids = (side.players || []).map((p) => p.id).filter((id) => id !== fromId);
        Modal.close();
        try {
          await api(`/rounds/${encodeURIComponent(ref)}/lineup`, {
            method: 'POST',
            auth: true,
            body: { side: side.key, playerIds: ids },
          });
          toast('已移出阵容', 'ok');
        } catch (err) {
          toast(err.message, 'err');
        }
      };
      submit.onclick = async () => {
        if (!chosen) {
          toast('请先选择替补选手', 'warn');
          return;
        }
        const scope = (qs('#f-scope', bodyEl) || {}).value || 'round';
        submit.disabled = true;
        try {
          const res = await api(`/rounds/${encodeURIComponent(ref)}/substitute`, {
            method: 'POST',
            auth: true,
            body: { fromId, toId: chosen, scope },
          });
          Modal.close();
          if (res.state) App.state = res.state;
          renderPublic();
          hooksRenderAdmin();
          toast(`${res.from.name} → ${res.to.name}（${res.scopeLabel}）替补已生效`, 'ok', 6000);
          // 替补原本不在参与名单里时，服务端会把他补进本届名单
          (res.addedToParticipants || []).forEach((name) =>
            toast(`${name} 不在参与名单里，已自动加入本届名单`, 'info', 8000)
          );
          if (res.lockedRounds?.length) {
            toast(`已打完的 ${res.lockedRounds.length} 场保留原阵容与比分`, 'info', 7000);
          }
          if (res.conflictRounds?.length) {
            toast(`${res.conflictRounds.length} 场因替补已在该场阵容中而跳过`, 'warn', 7000);
          }
        } catch (err) {
          submit.disabled = false;
          toast(err.message, 'err', 7000);
        }
      };
    },
  });
}

/** 取消一处替补：把换上的选手换回原主（已结算的比赛不改写）。 */
async function cancelSubstitution(id) {
  if (!window.confirm('取消这处替补？被换下的选手会回到他的位置（已结算的比赛不改写）。')) return;
  Modal.close();
  try {
    const res = await api(`/substitutions/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
      auth: true,
    });
    if (res.state) App.state = res.state;
    renderPublic();
    hooksRenderAdmin();
    toast('已取消这处替补', 'ok');
    if (res.lockedRounds?.length) {
      toast(`已打完的 ${res.lockedRounds.length} 场保留原阵容与比分`, 'info', 7000);
    }
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
}

function fillSlot(ref, side) {
  const rnd = roundOf(ref);
  if (!rnd) return;
  const target = side === 'B' ? rnd.sideB : rnd.sideA;
  const other = side === 'B' ? rnd.sideA : rnd.sideB;
  const current = target.players.map((p) => p.id);
  const used = [...current, ...other.players.map((p) => p.id)];
  if (current.length >= (App.state?.rules?.teamSize || 1)) {
    toast('该侧阵容已满，请先替换或移除选手', 'warn');
    return;
  }
  openPlayerPicker({
    title: `${rnd.label || ref} · ${target.label} 补位`,
    currentIds: used,
    onPick: async (pid) => {
      try {
        const res = await api(`/rounds/${encodeURIComponent(ref)}/lineup`, {
          method: 'POST',
          auth: true,
          body: { side, playerIds: [...current, pid] },
        });
        toast('已加入阵容', 'ok');
        (res.addedToParticipants || []).forEach((name) =>
          toast(`${name} 不在参与名单里，已自动加入本届名单`, 'info', 8000)
        );
      } catch (err) {
        toast(err.message, 'err');
      }
    },
  });
}

/** 随机组队：可选每队人数与「零头并入」（会清空赛程）。 */
function openTeamsAutoModal() {
  const s = App.state || {};
  const size = Number(s.rules?.teamSize) || 2;
  const joined = (s.participants || []).length || (s.players || []).length;
  Modal.open({
    title: '随机组队',
    body:
      `<div class="form form--2">` +
      fieldSelect(
        'teamSize',
        '每个组的人数',
        size,
        [['1', '1 人'], ['2', '2 人（默认）'], ['3', '3 人'], ['4', '4 人'], ['5', '5 人'], ['6', '6 人']],
        { hint: `当前参与 ${joined} 人` }
      ) +
      fieldSwitch('mergeRemainder', '零头并入（不落下任何人）', false, {
        hint: '开启后凑不满整队的人会平均并入前面的队伍，队伍人数可能不相等',
      }) +
      fieldNum('seed', '随机种子（改数字可重新洗牌）', Math.floor(Math.random() * 900000) + 100000) +
      `</div>` +
      `<div class="notice" style="margin-top:10px">队友随机分配后<b>全程固定</b>；` +
      `重新组队会清空当前赛程与比分。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>开始组队</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const teamSize = Number(qs('#f-teamSize', bodyEl).value) || 2;
        const mergeRemainder = qs('#f-mergeRemainder', bodyEl).checked;
        const seedRaw = qs('#f-seed', bodyEl).value;
        try {
          const res = await api('/teams/auto', {
            method: 'POST',
            auth: true,
            body: {
              teamSize,
              mergeRemainder,
              seed: seedRaw === '' ? null : Number(seedRaw),
            },
          });
          Modal.close();
          toast(`已随机生成 ${(res.teams || []).length} 支固定队伍（每队 ${res.teamSize} 人）`, 'ok', 5000);
          (res.warnings || []).forEach((w) => toast(w, 'warn', 8000));
          if (res.state) App.state = res.state;
          hooksRenderAdmin();
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

/** 切换赛制（积分制 / 锦标赛制）。两套赛程不通用，因此切换会清空现有对局。 */
async function setFormat(fmt) {
  if (fmt !== 'league' && fmt !== 'tournament') return;
  const s = App.state || {};
  if ((s.rules?.format || 'tournament') === fmt) {
    toast('当前已经是该赛制', 'info');
    return;
  }
  const label = fmt === 'league' ? '积分制' : '锦标赛制';
  if (
    !window.confirm(
      `切换到「${label}」？两套赛制的赛程互不通用，现有对局与比分会被清空（报名池与队伍保留）。`
    )
  ) {
    return;
  }
  try {
    const res = await api('/format', { method: 'POST', auth: true, body: { format: fmt } });
    toast(`已切换到${label}`, 'ok', 4000);
    if (res.state) App.state = res.state;
    else if (hooks.refreshState) await hooks.refreshState();
    App.filter = 'all';
    App.status = 'all';
    hooksRenderAdmin();
    renderPublic();
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
}

/**
 * 清空**全部比赛**（两套赛制通用）。
 *
 * 允许一场不剩——赛程为空是合法状态，之后可以重新生成；
 * 固定队伍与参与名单保留，比分与用时一并丢弃。
 */
async function clearAllRounds() {
  const total = (App.state?.rounds || []).length;
  if (!total) {
    toast('当前没有比赛', 'info');
    return;
  }
  const ok = window.confirm(
    `清空全部 ${total} 场比赛？\n` +
      `比分与用时一并丢失，固定队伍与参与名单会保留；清空后可以重新生成赛程。`
  );
  if (!ok) return;
  try {
    const res = await api('/rounds', { method: 'DELETE', auth: true });
    toast(`已清空 ${res.removed || total} 场比赛`, 'ok');
    if (res.state) App.state = res.state;
    else if (hooks.refreshState) await hooks.refreshState();
    App.filter = 'all';
    App.status = 'all';
    hooksRenderAdmin();
    renderPublic();
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
}

/* --------------------------- 赛事届次 --------------------------- */
async function refreshEventsUI(force = false) {
  invalidateEvents();
  await loadEvents(force);
  if (App.view === 'events') renderEventsGroups();
  else if (App.view === 'home') renderHomeGroups(App.state);
  else if (App.view === 'manage') renderAdmin({ force: true });
}

function openEventNewModal() {
  Modal.open({
    title: '新建一届赛事',
    body:
      `<div class="form form--2">` +
      fieldText('name', '届名', '', {
        ph: '例如 2026 秋季联赛',
        hint: '用于区分多届赛事，用户与管理员都按它识别',
      }) +
      fieldSelect('format', '赛制', 'tournament', [
        ['tournament', '锦标赛制（固定队伍 + 双败淘汰）'],
        ['league', '积分制（动态轮换 + 均分排名）'],
      ]) +
      fieldSwitch('copyRoster', '沿用当前届的选手 / 参与名单 / 队伍 / 规则 / 直播 / 界面配置', true) +
      `</div>` +
      `<div class="notice" style="margin-top:10px">新建后直接进入新的一届，原赛事完整保留在「全部赛事」里；` +
      `赛制决定比赛界面与赛程生成方式，之后也可在赛事管理里切换（切换会清空赛程）。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>创建并切换</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const values = collectForm(bodyEl);
        if (!String(values.name || '').trim()) {
          toast('请填写届名', 'warn');
          return;
        }
        try {
          const res = await api('/events', {
            method: 'POST',
            auth: true,
            body: {
              name: values.name,
              copyRoster: Boolean(values.copyRoster),
              format: values.format || 'tournament',
            },
          });
          Modal.close();
          toast(
            `已创建「${res.name}」（${res.format === 'league' ? '积分制' : '锦标赛制'}），正在进入`,
            'ok',
            5000
          );
          await refreshEventsUI(true);
          // 直接进入新建的这一届（它同时成了后端「当前届」，所以进去就能编辑）
          if (hooks.goto) await hooks.goto(res.eventId || '', 'overview');
          else if (hooks.refreshState) await hooks.refreshState();
        } catch (err) {
          toast(err.message, 'err');
        }
      };
    },
  });
}

function openEventRenameModal(id) {
  const event = App.events.find((e) => e.id === id);
  Modal.open({
    title: `重命名 · ${event?.name || id}`,
    body:
      `<div class="form"><div class="field"><label for="f-ename">届名</label>` +
      `<input id="f-ename" value="${esc(event?.name || '')}" placeholder="例如 2026 秋季联赛"></div></div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const name = qs('#f-ename', bodyEl).value.trim();
        if (!name) {
          toast('请填写届名', 'warn');
          return;
        }
        Modal.close();
        await patchEvent(id, { name });
      };
    },
  });
}

async function patchEvent(id, patch) {
  try {
    await api(`/events/${id}`, { method: 'PATCH', auth: true, body: patch });
    toast('已更新', 'ok');
    await refreshEventsUI(true);
    // 改名 / 标记结束 / 恢复进行都要立刻反映到当前页面（只读态与页签可用性会跟着变）
    if (hooks.refreshState) await hooks.refreshState();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function deleteEvent(id) {
  const event = App.events.find((e) => e.id === id);
  if (!window.confirm(`确认删除「${event?.name || id}」？该届的名单、赛程与成绩会一并删除，无法恢复。`)) return;
  try {
    const res = await api(`/events/${id}`, { method: 'DELETE', auth: true });
    toast('已删除该届赛事', 'ok');
    await refreshEventsUI(true);
    // 删掉的若正是正在看的那一届（或它已是后端的当前届）：回主页；否则就地刷新
    if (App.routeEvent === id || res.current === id) {
      await hooks.goto?.('', 'home', { replace: true });
    } else if (hooks.refreshState) {
      await hooks.refreshState();
    }
    log.info('已删除届次', id, '当前届', res.current);
  } catch (err) {
    toast(err.message, 'err');
  }
}

function openPlayerEditModal(player) {
  const url = player?.avatar || '';
  // 隐私字段（UUID / QQ / 推流流名）只在登录后的私有数据里
  const priv = privateOf(player?.id);
  const preview = url
    ? `<img src="${esc(url)}" alt=""><span class="ava__ring"></span>`
    : esc(String(player?.name || player?.id || '?').slice(0, 1));
  Modal.open({
    title: player ? `编辑选手 · ${player.name || player.id}` : '新增选手',
    body:
      `<div class="form form--2">` +
      fieldText('name', '姓名', player?.name || '') +
      fieldText('uuid', '游戏 UUID', priv.uuid || '', {
        hint: '玩家提供的游戏内 ID；UUID 与 QQ 都不会下发用户端',
      }) +
      fieldText('streamKey', '推流流名（必须唯一）', priv.streamKey || '', {
        hint:
          (priv.endpoints?.whipPush
            ? `该选手的 WHIP 推流地址：${priv.endpoints.whipPush}` +
              `（整届固定，换比赛不用重推）。`
            : `每位选手互不相同；它就是这位选手的推流地址，如 tom → …/tom/whip（整届固定）。`) +
          `${PUSH_TIP_LINE}。流名只能用字母、数字、连字符(-)与下划线(_)。`,
      }) +
      fieldText('qq', 'QQ（可选）', priv.qq || '', { hint: '仅服务端用于取头像' }) +
      fieldText('tag', '编号（可选）', player?.tag || '') +
      // 关联全局成员（「选手就是成员」）：选上后推流 / 封禁状态与成员同步
      `<div class="field"><label for="f-memberUid">关联成员</label>` +
      `<select id="f-memberUid" name="memberUid"><option value="">（自动匹配 / 新建）</option></select>` +
      `<span class="field__hint">留空即按游戏 UUID 或「姓名 + QQ」自动匹配，匹配不到会新建成员</span></div>` +
      `<div class="field" style="grid-column:1/-1"><label>头像</label>` +
      `<div class="ava-edit" data-avatar-scope>` +
      `<span class="ava ava--md${url ? '' : ' ava--placeholder'}" data-role="avatar-preview">${preview}</span>` +
      `<div class="ava-edit__col">` +
      `<input type="file" accept="image/*" data-role="avatar-file">` +
      `<input name="avatar" type="text" value="${esc(url)}" placeholder="或直接填写图片 URL">` +
      `</div></div></div>` +
      `<div style="grid-column:1/-1">${fieldArea('note', '备注', priv.note || '')}</div>` +
      `<div class="field field--switch"><span class="switch">` +
      `<input id="f-active" name="active" type="checkbox"${player?.active !== false ? ' checked' : ''}><i></i></span><label for="f-active">启用</label></div>` +
      `</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      // 异步填充「关联成员」下拉（成员列表需登录，独立选手留空）
      const sel = bodyEl.querySelector('#f-memberUid');
      const currentUid = priv.memberUid || '';
      api('/members', { auth: true })
        .then((res) => {
          (res.members || []).forEach((m) => {
            const opt = document.createElement('option');
            opt.value = m.uid;
            const role =
              m.permission === 'server_admin' ? '（服务器管理员）' : m.permission === 'event_admin' ? '（赛事管理员）' : '';
            opt.textContent = `${m.name || m.uid}${m.streamId ? ` · ${m.streamId}` : ''}${role}`;
            if (m.uid === currentUid) opt.selected = true;
            sel.appendChild(opt);
          });
        })
        .catch((err) => log.debug('成员列表加载失败（忽略）', err));
      footEl.querySelector('[data-submit]').onclick = async () => {
        const data = collectForm(bodyEl);
        if (!String(data.name || '').trim()) {
          toast('请填写姓名', 'warn');
          return;
        }
        if (player?.id) data.id = player.id;
        try {
          await api('/players', { method: 'POST', auth: true, body: data });
          Modal.close();
          toast('选手已保存', 'ok');
          await refreshPrivate(); // 流名可能变了，推流地址需要重取
          setTimeout(hooksRenderAdmin, 400);
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('读取失败'));
    reader.readAsDataURL(file);
  });
}

/** 上传头像：读取本地图片 -> data:URL -> 服务端落盘 -> 回填 URL 与预览。 */
export async function uploadAvatarFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) {
    toast('头像不能超过 2 MB', 'warn');
    input.value = '';
    return;
  }
  let dataUrl;
  try {
    dataUrl = await readAsDataUrl(file);
  } catch (err) {
    log.error('读取头像文件失败', err);
    toast('无法读取该文件', 'err');
    input.value = '';
    return;
  }
  try {
    const res = await api('/avatar/upload', { method: 'POST', auth: true, body: { dataUrl } });
    const scope = input.closest('[data-avatar-scope]') || document;
    const urlInput = scope.querySelector('input[name="avatar"]');
    if (urlInput) urlInput.value = res.url;
    const prev = scope.querySelector('[data-role="avatar-preview"]');
    if (prev) {
      prev.classList.remove('ava--placeholder');
      prev.innerHTML = `<img src="${esc(res.url)}" alt=""><span class="ava__ring"></span>`;
    }
    toast('头像已上传', 'ok');
    log.info('头像已上传', res.url);
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    input.value = '';
  }
}

function hooksRenderAdmin() {
  // 数据刚变过：强制刷新面板（普通重绘会被「指纹没变就不重建」挡掉，
  // 而这里要的正是把服务端的新值显示出来；用户没保存的输入仍会被保住）
  if (App.view === 'manage') renderAdmin({ force: true });
}

function refreshDiagIfAdmin() {
  if (App.view === 'manage') refreshDiagnostics();
}

/* --------------------------- 成员频道（日常直播） --------------------------- */
// 频道页的房间既可能是成员直播间（合成对象），也可能是传统频道
const channelOf = (id) => channelRooms(App.state).find((c) => c.id === id) || null;

/** 选中并播放某个成员频道（未开播时给一句说明，而不是去连一个空流）。 */
function watchChannel(id) {
  App.channelId = id || null;
  const channel = channelOf(id);
  if (App.state) renderChannels(App.state);
  // 地址跟着换成 /channels/<流名>（用 replaceState，不新增历史）
  hooks.syncChannelUrl?.(channel?.play?.key || channel?.biliRoom?.key || '');
  if (!channel) return;
  if (channel.bili) {
    // B站 那一路由舞台**直嵌官方播放器**（见 views.js 的 channelStageHtml）：
    // 这里不能再去连本站媒体服务器，否则会把 B站 的画面顶掉、封面还写着「未开播」。
    ChannelLive.stop(false);
    return;
  }
  if (isChannelLive(channel.id)) {
    ChannelLive.playRoom(channel.play || null);
  } else {
    ChannelLive.stop(false);
    ChannelLive.setCover('当前未开播', `${channel.name} 现在没有推流；开播后这里会自动有画面。`);
  }
}

/** 新增 / 编辑成员频道（频道与赛事无关，任何届次下都能管理）。 */
function openChannelModal(channel) {
  const isNew = !channel;
  // 推流流名 / QQ 属于私有数据，登录后从 /api/private 取
  const priv = channel ? (App.private?.channels?.[channel.id] || {}) : {};
  const url = channel?.avatar || '';
  const preview = url
    ? `<img src="${esc(url)}" alt=""><span class="ava__ring"></span>`
    : esc(String(channel?.name || '?').slice(0, 1));
  Modal.open({
    title: isNew ? '新增成员频道' : `编辑频道 · ${channel.name}`,
    body:
      `<div class="form form--2">` +
      fieldText('name', '频道名 / 主播名', channel?.name || '') +
      fieldText('title', '直播间标题', channel?.title || '', { ph: '一句话，例如「每晚八点开播」' }) +
      fieldText('server', '游戏区服', channel?.server || '', { ph: '如「国服 / 国际服」' }) +
      fieldText('role', '常驻角色 / 称号', channel?.role || '', { ph: '展示用，可留空' }) +
      fieldText('streamKey', '推流流名（必须唯一）', priv.streamKey || '', {
        hint:
          (priv.endpoints?.whipPush
            ? `该频道的 WHIP 推流地址：${priv.endpoints.whipPush}。`
            : `全局唯一（与任何选手流名也不能重复）；它就是这位群友的推流地址，如 tom → …/tom/whip（常驻，不用改）。`) +
          `${PUSH_TIP_LINE}。流名只能用字母、数字、连字符(-)与下划线(_)。`,
      }) +
      fieldText('qq', 'QQ（可选）', priv.qq || '', { hint: '仅服务端用于取头像' }) +
      fieldText('link', '外部链接（可选）', channel?.link || '', { ph: '个人主页 / 其它平台' }) +
      fieldNum('sort', '排序（小的在前）', channel?.sort ?? 0) +
      fieldText('tags', '标签（逗号分隔）', (channel?.tags || []).join(', ')) +
      `<div style="grid-column:1/-1">${fieldArea('description', '简介 / 内容说明', channel?.description || '')}</div>` +
      `<div class="field" style="grid-column:1/-1"><label>头像</label>` +
      `<div class="ava-edit" data-avatar-scope>` +
      `<span class="ava ava--md${url ? '' : ' ava--placeholder'}" data-role="avatar-preview">${preview}</span>` +
      `<div class="ava-edit__col">` +
      `<input type="file" accept="image/*" data-role="avatar-file">` +
      `<input name="avatar" type="text" value="${esc(url)}" placeholder="或直接填写图片 URL">` +
      `</div></div></div>` +
      fieldSwitch('active', '在用户端展示', channel ? channel.active !== false : true) +
      fieldSwitch('featured', '置顶推荐', Boolean(channel?.featured)) +
      `</div>` +
      `<div class="notice" style="margin-top:10px">成员频道是<b>常驻</b>的日常直播位，` +
      `与赛事届次无关：没有比赛时也能一直开着播，开赛锁定也不影响它。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const data = collectForm(bodyEl);
        if (!String(data.name || '').trim()) {
          toast('请填写频道名', 'warn');
          return;
        }
        data.tags = String(data.tags || '')
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean);
        if (channel?.id) data.id = channel.id;
        try {
          const res = await api('/channels', { method: 'POST', auth: true, body: data });
          Modal.close();
          toast(isNew ? '频道已创建' : '频道已保存', 'ok');
          await refreshPrivate(); // 流名可能变了，推流地址需要重取
          if (res.state) App.state = res.state;
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

/** 编辑「频道」板块的公告（全局，放异环相关说明 / 活动文案）。 */
function openChannelNoticeModal() {
  const text = App.state?.channelNotice || '';
  Modal.open({
    title: '编辑频道公告',
    body:
      `<div class="field"><label for="f-channelNotice">公告内容（留空即清除）</label>` +
      `<textarea id="f-channelNotice" rows="6" placeholder="例如：异环活动期间频道照常开播，每晚 8 点联机">${esc(text)}</textarea>` +
      `<span class="field__hint">展示在「频道」页顶部，与赛事届次无关；换行会保留。</span></div>` +
      `<div class="notice" style="margin-top:10px">公告只做展示。异环没有面向第三方的官方数据接口，` +
      `游戏相关内容（区服 / 角色 / 活动）需管理员自行填写，本站不会自动抓取游戏数据。</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存公告</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const value = qs('#f-channelNotice', bodyEl).value;
        try {
          const res = await api('/channels/notice', { method: 'PUT', auth: true, body: { text: value } });
          Modal.close();
          toast('频道公告已保存', 'ok');
          if (res.state) App.state = res.state;
          renderPublic();
        } catch (err) {
          toast(err.message, 'err', 6000);
        }
      };
    },
  });
}

async function deleteChannel(id) {
  const channel = channelOf(id);
  if (!window.confirm(`确认删除频道「${channel?.name || id}」？`)) return;
  try {
    const res = await api(`/channels/${encodeURIComponent(id)}`, { method: 'DELETE', auth: true });
    toast('已删除频道', 'ok');
    if (App.channelId === id) App.channelId = null;
    if (res.state) App.state = res.state;
    renderPublic();
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* --------------------------- 动作分发 --------------------------- */
export async function handleAction(act, el) {
  log.debug('动作', act, el?.dataset);
  // 成员 / 服务器相关动作先交给 members.js 处理；未命中再走赛事动作
  if (await handleMemberAction(act, el)) return;
  switch (act) {
  // —— 通知与信息（Markdown，见 notices.js）——
  case 'notice-new':
    composeNotice(el.dataset.scope || 'event');
    return;
  case 'notice-edit':
    composeNotice(el.dataset.scope || 'event', el.dataset.id || '');
    return;
  case 'notice-del':
    removeNotice(el.dataset.scope || 'event', el.dataset.id || '');
    return;
  case 'notice-read':
    openNoticeReader(el.dataset.id || '', el.dataset.scope || 'event');
    return;
  case 'notice-page':
    goNoticePage(el.dataset.scope || 'event', Number(el.dataset.page) || 1);
    return;
  case 'event-info-edit':
    editEventInfo();
    return;
  case 'server-info-edit':
    editServerInfo();
    return;
  // —— 页脚（署名与开源组件清单，见 credits.js）——
  case 'credits':
    openCreditsModal();
    return;
  // —— 比赛计时器（本机秒表，不进赛制数据，见 clock.js）——
  case 'clock-toggle':
    toggleClock(el.dataset.code || '');
    return;
  case 'clock-reset':
    resetClock(el.dataset.code || '');
    return;
  case 'guide-hide':
    // 关掉主页的「三步开赛」引导卡（记忆在本机，见 events.js 的 GUIDE_KEY）
    hideSetupGuide();
    renderPublic();
    return;
  case 'filter':
    App.filter = el.dataset.filter || 'all';
    renderPublic();
    return;
    case 'status-filter':
      App.status = el.dataset.status || 'all';
      renderPublic();
      return;
    case 'live-select':
      App.livePlayerId = el.dataset.pid || null;
      Live.stoppedByUser = false; // 主动选台 = 想看，解除「别再自动播」的标记
      log.info('切换直播机位', App.livePlayerId);
      if (App.state) focusLive(App.state);
      return;
    case 'watch':
      if (hooks.gotoLive) hooks.gotoLive(el.dataset.pid || null);
      return;
    case 'watch-round': {
      // 看某一场的机位：切到直播页并选中这场（直播页会自动挑这场的第一个机位）
      App.liveRound = el.dataset.code || '';
      App.livePlayerId = null;
      if (hooks.gotoLive) hooks.gotoLive(null);
      return;
    }
    // —— 成员频道（日常直播）——
    case 'channel-watch':
      return watchChannel(el.dataset.id);
    case 'channel-play': {
      const channel = channelOf(App.channelId);
      // B站 那一路已经自动嵌在舞台里了，没有「播放」这一步
      if (channel?.bili) return;
      ChannelLive.stoppedByUser = false; // 主动点「播放」：恢复自动开播的资格
      if (channel) ChannelLive.playRoom(channel.play || null);
      return;
    }
    case 'channel-stop':
      ChannelLive.stop(true);
      return;
    case 'channel-open': {
      const channel = channelOf(App.channelId);
      if (channel?.bili) {
        // B站 那一路没有本站推流地址，能打开的就是 B站 直播间
        const jump = channel.biliRoom?.jump || '';
        if (jump) window.open(jump, '_blank', 'noopener');
        return;
      }
      const room = channel?.play || {};
      const url = (App.liveProto === 'hls' ? room.hls : room.webrtc) || room.webrtc || '';
      if (url) window.open(url, '_blank', 'noopener');
      return;
    }
    case 'channel-copy': {
      const channel = channelOf(App.channelId);
      const room = channel?.play || {};
      // B站 那一路没有本站播放地址，复制的是 B站 直播间地址（方便直接分享）
      const url = channel?.bili
        ? channel.biliRoom?.jump || ''
        : (App.liveProto === 'hls' ? room.hls : room.webrtc) || room.webrtc || '';
      if (!url) return;
      copyText(url).then((ok) =>
        toast(ok ? `已复制播放地址：${url}` : '复制失败', ok ? 'ok' : 'err', ok ? 6000 : 3600)
      );
      return;
    }
    case 'channel-proto': {
      const proto = el.dataset.proto || '';
      ChannelLive.stoppedByUser = false; // 换线路也是「我想看」
      App.liveProto = App.liveProto === proto ? '' : proto;
      try {
        localStorage.setItem(LIVE_PROTO_KEY, App.liveProto);
      } catch (err) {
        log.debug('线路偏好写入失败（忽略）', err);
      }
      if (App.state) renderChannels(App.state);
      return;
    }
    case 'channel-refresh':
      // 显式刷新：清零失败计数并让服务端现场重新探测一次信号
      probeLiveHealth().then(() => watchChannel(App.channelId));
      return;
    case 'live-health-retry':
      // 「获取失败，等待服务器修复」里的重试按钮（直播页走舞台委托，这里是频道页）
      probeLiveHealth().then(() => {
        if (App.view === 'channels' && App.state) renderChannels(App.state);
        else renderPublic();
      });
      return;
    case 'channel-notice-edit':
      return openChannelNoticeModal();
    case 'channel-add':
      return openChannelModal(null);
    case 'channel-edit':
      return openChannelModal(channelOf(el.dataset.id));
    case 'channel-del':
      return deleteChannel(el.dataset.id);
    case 'route-home':
      // 主页是唯一总入口：比赛 / 频道 / 全部赛事 / 我的 / 服务器都从这里进出
      // （频道 / 全部赛事的入口卡都是真链接，不再走动作）
      return hooks.goto?.('', 'home');
    case 'route-events':
      return hooks.goto?.('', 'events');
    case 'route-user':
      return hooks.goto?.('', 'user');
    case 'group-toggle': {
      // 主页 / 全部赛事页的分组折叠：只切一个 class，交给 CSS 过渡，不重绘
      const key = el.dataset.group;
      if (!key) return;
      const open = !(App.homeOpen[key] !== false);
      App.homeOpen[key] = open;
      el.setAttribute('aria-expanded', String(open));
      el.closest('.home-group')?.classList.toggle('is-open', open);
      return;
    }
    case 'event-new':
      return openEventNewModal();
    case 'event-refresh':
      return refreshEventsUI(true);
    // 注：届次卡片已是**真链接**（`<a data-route>` + 拉伸覆盖整张卡），
    // 「进入这一届 / 新窗口打开」都由浏览器自己处理，这里不再需要对应的动作。
    case 'event-rename':
      return openEventRenameModal(el.dataset.id);
    case 'event-close':
      return patchEvent(el.dataset.id, { status: 'closed' });
    case 'event-reopen':
      return patchEvent(el.dataset.id, { status: 'active' });
    case 'event-delete':
      return deleteEvent(el.dataset.id);
    case 'format-set':
      return setFormat(el.dataset.format);
    case 'reload':
      try {
        await api('/reload', { method: 'POST', auth: true });
        toast('配置已重载', 'ok');
      } catch (err) {
        toast(err.message, 'err');
      }
      refreshDiagIfAdmin();
      return;
    case 'export':
      // 带请求头取 blob 另存：链接里不再出现会话令牌（见 core.downloadFile）
      try {
        await downloadFile('/export', 'export.json');
        toast('已开始下载导出文件', 'ok');
      } catch (err) {
        toast(err.message, 'err', 8000);
      }
      return;
    case 'logout':
      try {
        await api('/auth/logout', { method: 'POST' });
      } catch (err) {
        log.warn('注销请求失败（忽略）', err);
      }
      App.token = '';
      localStorage.removeItem(TOKEN_KEY);
      App.private = null; // 清掉隐私数据（UUID / QQ / 推流地址）
      App.me = null;
      App.server = null;
      toast('已退出登录', 'info');
      renderAdmin({ force: true });
      renderPublic();
      return;
    case 'admin-login': {
      // 赛事管理 / 服务器 / 我的 三个门禁各有一个密钥框，必须取「按钮所在门禁」里的那个；
      // 全局 qs('#adminKey') 会命中文档里最靠前的（隐藏的赛事管理页）空框，永远提示「请输入密钥」。
      const gate = el.closest('.gate') || document;
      const input = gate.querySelector('input[type="password"]') || qs('#adminKey');
      return login(input?.value || '');
    }
    case 'event-start':
      return startEvent();
    case 'event-unlock':
      return unlockEvent();
    case 'team-sub':
      return openTeamSubModal();
    case 'teams-auto':
      return openTeamsAutoModal();
    case 'teams-save':
      return saveTeamBoard();
    case 'teams-reset':
      return resetTeamBoard();
    case 'tournament-generate':
      return openTournamentModal();
    case 'group-pairings':
      // 开赛前换小组赛对手（开打后前后端都会拒绝）
      return openGroupPairingModal();
    case 'rounds-clear':
    case 'tournament-clear': // 旧入口，两者都是「清空全部比赛」
      return clearAllRounds();
    case 'schedule-generate':
      return openScheduleModal();
    case 'schedule-append':
      return openAppendModal();
    case 'round-append':
      return roundAppend();
    case 'round-delete':
      return roundDelete(el.dataset.code);
    case 'member':
      return openSubstituteModal(el.dataset.code, el.dataset.pid);
    case 'sub-cancel':
      return cancelSubstitution(el.dataset.id);
    case 'empty-slot':
      return fillSlot(el.dataset.code, el.dataset.side);
    case 'round-status':
      return roundStatus(el.dataset.code, el.dataset.status);
    case 'round-reset':
      return roundReset(el.dataset.code);
    case 'round-result': {
      const rnd = roundOf(el.dataset.code);
      if (rnd) openResultModal(rnd);
      return;
    }
    case 'round-times': {
      const rnd = roundOf(el.dataset.code);
      if (rnd) openRoundTimesModal(rnd);
      return;
    }
    case 'round-live': {
      const rnd = roundOf(el.dataset.code);
      if (rnd) openRoundLiveModal(rnd);
      return;
    }
    case 'round-walkover': {
      const rnd = roundOf(el.dataset.code);
      if (rnd) openWalkoverModal(rnd);
      return;
    }
    case 'quick-group':
      return openQuickGroupModal();
    case 'event-now':
      return fillEventTime(el.dataset.field);
    case 'event-now-clear':
      return clearEventTimeField(el.dataset.field);
    case 'player-add':
      return openPlayerEditModal(null);
    case 'player-edit':
      return openPlayerEditModal((App.state?.players || []).find((p) => p.id === el.dataset.id));
    case 'player-del':
      return deletePlayer(el.dataset.id);
    case 'avatar-refresh':
      return refreshAvatar(el.dataset.id);
    case 'participants-members':
      return openRosterMembersModal();
    case 'participants-all':
      return setParticipants(true);
    case 'participants-none':
      return setParticipants(false);
    case 'participants-invert':
      return setParticipants(null);
    case 'participants-save':
      return saveParticipants();
    // 注：队伍编辑（队名 / 配色 / 分组 / 成员）统一在组队台上就地完成，见 teams.js，
    // 这里不再有「队伍信息」面板的保存与删除。
    // 注：复制按钮统一由 app.js 的 [data-copy] 委托处理（它先于 data-act 拦截），
    // 这里不再重复处理，否则会弹出两条 toast。
    default:
      log.warn('未处理的动作', act);
  }
}

export async function login(key) {
  if (!key.trim()) {
    toast('请输入密钥', 'warn');
    return;
  }
  try {
    const res = await api('/auth', { method: 'POST', body: { key } });
    App.token = res.token;
    localStorage.setItem(TOKEN_KEY, res.token);
    App.me = {
      uid: res.uid || '',
      name: res.name || '',
      permission: res.permission || 'member',
      isServer: res.permission === 'server_admin',
      canManageEvents: res.permission === 'event_admin' || res.permission === 'server_admin',
    };
    // 登录后才能取到选手隐私字段与推流地址；/api/me 补上成员视图
    await Promise.all([refreshPrivate(), refreshMeData()]);
    toast('登录成功', 'ok');
    log.info('登录成功', App.me.permission);
    renderAdmin({ force: true });
    renderPublic();
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ------------------------------ 对局 ------------------------------ */
const STATUS_TEXT = { pending: '待赛', live: '进行中', done: '已结束' };

async function roundStatus(ref, status) {
  try {
    await api(`/rounds/${encodeURIComponent(ref)}/status`, { method: 'POST', auth: true, body: { status } });
    toast(`${ref} → ${STATUS_TEXT[status] || status}`, 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function roundReset(ref) {
  if (!window.confirm(`确认重置 ${ref} 的比分与状态？依赖它的后续对局会自动作废。`)) return;
  try {
    await api(`/rounds/${encodeURIComponent(ref)}/reset`, { method: 'POST', auth: true });
    toast('已重置，下游对局已同步回退', 'ok', 5000);
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ------------------------------ 选手 ------------------------------ */
async function deletePlayer(id) {
  const p = (App.state?.players || []).find((x) => x.id === id);
  if (!window.confirm(`确认删除选手「${p?.name || id}」？该操作会同时从阵容中移除。`)) return;
  try {
    await api(`/players/${id}`, { method: 'DELETE', auth: true });
    toast('已删除', 'ok');
    setTimeout(hooksRenderAdmin, 400);
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* 选手名单的批量表单已移除：名单统一在「选手」页按人编辑（/api/config 那次整体 PUT 不再需要） */

/* 队伍的保存 / 删除也已移除：全部在「组队台」上就地完成（static/js/teams.js 的 save()）——
 * 「队伍信息」面板与组队台功能重叠，重复的表单只会各自漂移。 */

/* ------------------------------ 头像 ------------------------------ */
/**
 * 刷新某位选手的 QQ 头像。
 *
 * 服务端有内存（10 分钟）与磁盘（12 小时）两级缓存，浏览器还会自己缓存一小时；
 * 在 QQ 里换过头像后点这里：强制回源 + 给图片地址加时间戳，三层缓存一起绕过。
 * 回执里的 ``X-NTE-Avatar`` 说明这次到底拿到了什么，据此给出准确的提示。
 */
async function refreshAvatar(pid) {
  if (!pid) return;
  try {
    const res = await fetch(
      `/api/avatar/p/${encodeURIComponent(pid)}?size=100&refresh=1&t=${Date.now()}`,
      { cache: 'no-store' }
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      toast(body.detail || `头像刷新失败（HTTP ${res.status}）`, 'err', 6000);
      return;
    }
    const source = res.headers.get('X-NTE-Avatar') || '';
    // 给图片地址加时间戳：否则浏览器还会拿自己缓存里的旧图
    App.avatarBust = { ...(App.avatarBust || {}), [pid]: Date.now() };
    hooksRenderAdmin();
    renderPublic();
    if (source === 'fetched') toast('头像已从 QQ 重新拉取', 'ok');
    else if (source === 'stale') toast('QQ 暂时不可达，仍在用上一次的图', 'warn', 6000);
    else if (source === 'placeholder') {
      toast('QQ 没返回可用头像（可能该号未设置头像），已用占位图', 'warn', 7000);
    } else {
      toast(`头像仍是缓存内容（来源：${source || '未知'}）`, 'info', 5000);
    }
  } catch (err) {
    toast(err.message || '头像刷新失败', 'err', 6000);
  }
}

/* --------------------------- 本届参与名单 --------------------------- */
/**
 * 「从成员列表选择参赛者」：勾一位成员 = 他参加这一届。
 *
 * 与下面那排勾选框的区别：这里列的是**成员**（全局那一份，含本届还没有档案的人）。
 * 勾上之后服务端按成员资料把选手档案建好（姓名 / QQ / 头像 / 游戏 UUID），
 * 所以不必先去「选手」页手工登记一遍；取消勾选只是把他移出名单，**档案留着**
 * （档案一删，赛程与比分就断了）。
 */
async function openRosterMembersModal() {
  let data;
  try {
    data = await api('/roster/members', { auth: true });
  } catch (err) {
    toast(err.message || '读取成员列表失败', 'err');
    return;
  }
  const members = data.members || [];
  const loose = data.loosePlayers || [];
  const explicit = Boolean(data.participantsSet);
  const avaOf = (m) =>
    m.hasAvatar
      ? `<img class="ava ava--xs" src="/api/avatar/m/${encodeURIComponent(m.uid)}?size=40"` +
        ` alt="" loading="lazy">`
      : `<span class="ava ava--xs ava--placeholder">${esc(String(m.name || '?').slice(0, 1))}</span>`;
  const memberRow = (m) => {
    const idle = m.active === false;
    const meta = (m.playerId ? m.tag || m.playerId : '本届还没有档案') + (idle ? ' · 已停用' : '');
    return (
      `<label class="pick${m.selected ? '' : ' pick--off'}${idle ? ' pick--disabled' : ''}"` +
      ` data-name="${esc(String(m.name || '').toLowerCase())}">` +
      `<input type="checkbox" data-role="roster-member" value="${esc(m.uid)}"` +
      `${m.selected ? ' checked' : ''}${idle ? ' disabled' : ''}>` +
      avaOf(m) +
      `<span class="pick__txt"><span class="who__name">${esc(m.name || m.uid)}</span>` +
      `<span class="pick__meta">${esc(meta)}</span></span></label>`
    );
  };
  const looseRow = (p) =>
    `<label class="pick${p.selected ? '' : ' pick--off'}"` +
    ` data-name="${esc(String(p.name || '').toLowerCase())}">` +
    `<input type="checkbox" data-role="roster-loose" value="${esc(p.id)}"${p.selected ? ' checked' : ''}>` +
    `<span class="pick__txt"><span class="who__name">${esc(p.name || p.id)}</span>` +
    `<span class="pick__meta">${esc(p.tag || p.id)} · 没有账号</span></span></label>`;

  Modal.open({
    title: '从成员列表选择参赛者',
    body:
      `<div class="notice">名单来自<b>成员列表</b>：勾上就参加这一届；本届还没有档案的会` +
      `<b>按成员资料自动建好</b>（姓名 / QQ / 头像 / 游戏 UUID），以后改成员资料这里跟着更新。` +
      `取消勾选只把他移出名单，<b>档案会留着</b>（赛程与比分不受影响）。` +
      (explicit ? '' : '当前<b>未指定名单</b>（默认全员参与），保存后会成为一份显式名单。') +
      `</div>` +
      `<div class="tool-group" style="margin-top:10px">` +
      `<div class="field" style="max-width:220px;margin:0">` +
      `<input id="rosterMemberSearch" placeholder="搜姓名…"></div>` +
      `<button class="btn btn--sm" type="button" data-roster="all">全选</button>` +
      `<button class="btn btn--sm" type="button" data-roster="none">全不选</button>` +
      `<span class="panel__hint" style="margin-left:auto">已选 ` +
      `<b data-role="roster-count">0</b> 人</span>` +
      `</div>` +
      `<div class="pick-grid" data-role="roster-grid">` +
      (members.map(memberRow).join('') || '<div class="empty"><b>还没有成员</b>先在「服务器 → 成员管理」里添加</div>') +
      `</div>` +
      (loose.length
        ? `<div class="notice" style="margin-top:10px">` +
          `没有账号的客串选手（手工登记的，不受成员列表影响）</div>` +
          `<div class="pick-grid">${loose.map(looseRow).join('')}</div>`
        : ''),
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存参赛名单</button>`,
    onMount(bodyEl, footEl) {
      const boxes = () =>
        Array.from(
          bodyEl.querySelectorAll('[data-role="roster-member"], [data-role="roster-loose"]')
        );
      const countEl = bodyEl.querySelector('[data-role="roster-count"]');
      const sync = () => {
        boxes().forEach((box) =>
          box.closest('.pick')?.classList.toggle('pick--off', !box.checked)
        );
        if (countEl) countEl.textContent = String(boxes().filter((b) => b.checked).length);
      };
      boxes().forEach((box) => box.addEventListener('change', sync));
      sync();
      bodyEl.querySelectorAll('[data-roster]').forEach((btn) => {
        btn.onclick = () => {
          const on = btn.dataset.roster === 'all';
          boxes()
            .filter((b) => !b.disabled)
            .forEach((b) => {
              b.checked = on;
            });
          sync();
        };
      });
      const search = bodyEl.querySelector('#rosterMemberSearch');
      if (search) {
        search.oninput = () => {
          const term = search.value.trim().toLowerCase();
          bodyEl.querySelectorAll('.pick[data-name]').forEach((row) => {
            row.hidden = Boolean(term) && !row.dataset.name.includes(term);
          });
        };
      }
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const btn = footEl.querySelector('[data-submit]');
        const memberUids = Array.from(
          bodyEl.querySelectorAll('[data-role="roster-member"]:checked')
        ).map((b) => b.value);
        const playerIds = Array.from(
          bodyEl.querySelectorAll('[data-role="roster-loose"]:checked')
        ).map((b) => b.value);
        btn.disabled = true;
        try {
          const res = await api('/roster/members', {
            method: 'POST',
            auth: true,
            body: { memberUids, playerIds },
          });
          Modal.close();
          const created = (res.created || []).length;
          toast(
            `参赛名单已更新：${res.count} 人` +
              (created ? `（新建 ${created} 份选手档案）` : ''),
            'ok',
            6000
          );
          (res.warnings || []).forEach((w) => toast(w, 'warn', 8000));
          if (res.state) App.state = res.state;
          if (hooks.refreshState) await hooks.refreshState();
          hooksRenderAdmin();
          refreshDiagIfAdmin();
        } catch (err) {
          toast(err.message || '保存失败', 'err', 8000);
          btn.disabled = false;
        }
      };
    },
  });
}

const participantBoxes = () => qsa('[data-role="participant"]', qs('#adminPanel'));

function syncPickStyle(box) {
  box.closest('.pick')?.classList.toggle('pick--off', !box.checked);
}

/** mode: true 全选 / false 全不选 / null 反选（停用的选手不参与勾选）。 */
function setParticipants(mode) {
  const boxes = participantBoxes().filter((b) => !b.disabled);
  if (!boxes.length) {
    toast('没有可勾选的选手', 'warn');
    return;
  }
  boxes.forEach((box) => {
    box.checked = mode === null ? !box.checked : mode;
    syncPickStyle(box);
  });
  log.debug('参与名单勾选', mode === null ? 'invert' : mode, boxes.length);
}

/**
 * 保存本届参与名单：服务端会同时按新名单重排未开赛对局，
 * 因此保存后必须重新载入分组台草案，避免显示过期阵容。
 */
async function saveParticipants() {
  const boxes = participantBoxes();
  if (!boxes.length) {
    toast('暂无选手，请先在「选手」页添加', 'warn');
    return;
  }
  const ids = boxes.filter((b) => b.checked).map((b) => b.value);
  if (!ids.length && !window.confirm('没有勾选任何选手，保存后本届将没有参与者。确认继续？')) {
    return;
  }
  try {
    const res = await api('/participants', {
      method: 'POST',
      auth: true,
      body: { playerIds: ids },
    });
    toast(`参与名单已保存：${res.count} 人`, 'ok', 4000);
    (res.warnings || []).forEach((w) => toast(w, 'warn', 7000));
    log.info('参与名单已保存', res.count, res.warnings);
    if (res.state) App.state = res.state;
    if (hooks.refreshState) await hooks.refreshState();
    hooksRenderAdmin();
    refreshDiagIfAdmin();
  } catch (err) {
    toast(err.message, 'err', 6000);
  }
}

/* ------------------------------ 表单 ------------------------------ */
const NUMERIC_RULE_KEYS = [
  'teamSize', 'totalRounds', 'pointsWin', 'pointsLose', 'pointsDraw',
  'targetScore', 'minRankPlayed', 'groupCount', 'knockoutSize',
];

const PATCH_BUILDERS = {
  event: (v) => ({ event: v }),
  rules: (v) => {
    const out = { ...v };
    NUMERIC_RULE_KEYS.forEach((k) => {
      if (!(k in out)) return;
      out[k] = out[k] === '' || out[k] === null ? 0 : Number(out[k]);
    });
    return { rules: out };
  },
  ui: (v) => ({ ui: v }),
  stream: (v) => ({ stream: v }),
};

export async function handleForm(formEl) {
  const name = formEl.dataset.form;

  // 成员 / 服务器相关表单先交给 members.js
  if (await handleMemberForm(formEl)) return;

  // 注：`admin-key`（主管理 KEY）表单已随那套凭据一起移除，见 README「登录与权限」。
  const builder = PATCH_BUILDERS[name];
  if (!builder) {
    log.warn('未知表单', name);
    return;
  }
  const values = collectForm(formEl);
  try {
    await api('/config', { method: 'PUT', auth: true, body: builder(values) });
    toast('已保存', 'ok');
    log.info('配置已提交', name);
    refreshDiagIfAdmin();
  } catch (err) {
    toast(err.message, 'err');
  }
}
