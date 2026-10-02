/* 管理动作层：赛程/比分/换人/选手/队伍/系统操作与弹窗表单。
 * 通过 data-act 与 data-form 被动调用；成功后依赖 WebSocket 推送刷新界面。
 */

import {
  App,
  hooks,
  Modal,
  TOKEN_KEY,
  api,
  esc,
  log,
  nowLocalInput,
  qs,
  qsa,
  refreshPrivate,
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
  privateOf,
  pushTipsHtml,
  roundStreamsOf,
} from './ui.js';
import { refreshDiagnostics, renderAdmin, startReadiness } from './admin.js';
import { invalidateEvents, loadEvents, renderEventsView } from './events.js';
import { reset as resetTeamBoard, save as saveTeamBoard } from './teams.js';
import { focusLive, renderPublic } from './views.js';

/** 按对局编号（WB-1-2）或序号定位一场比赛。 */
const roundOf = (ref) =>
  (App.state?.rounds || []).find((r) => r.code === ref) ||
  (App.state?.rounds || []).find((r) => String(r.index) === String(ref));

/* ------------------------------ 弹窗 ---------------------------------- */
/** 读出弹窗里的各局小分（只保留填了内容的行）。 */
function readSetRows(bodyEl) {
  return qsa('.setrow', bodyEl)
    .map((row) => ({
      a: Number(qs('[data-role="set-a"]', row).value) || 0,
      b: Number(qs('[data-role="set-b"]', row).value) || 0,
    }))
    .filter((item) => item.a || item.b);
}

function setRowHtml(a = '', b = '') {
  return (
    `<div class="setrow">` +
    `<input type="number" min="0" data-role="set-a" value="${esc(a)}" placeholder="0">` +
    `<span class="setrow__sep">:</span>` +
    `<input type="number" min="0" data-role="set-b" value="${esc(b)}" placeholder="0">` +
    `<button class="btn btn--sm btn--ghost" type="button" data-set-del title="删除这一局">×</button>` +
    `</div>`
  );
}

/**
 * 按当前填写内容实时推导比分 / 名次 / 胜负，并回写到界面上。
 *
 * 规则与后端 `tournament.judge_round` 完全一致：
 * 先看各局小分（2 队），再看比分，最后看小分；并列时提示需要指定。
 */
function refreshResultPreview(bodyEl, sides, allowDraw) {
  const multi = sides.length > 2;
  const sets = readSetRows(bodyEl);
  const entries = sides.map((side) => ({
    key: side.key,
    label: side.label,
    score: Number(qs(`[data-sid="${side.key}"] [data-role="score"]`, bodyEl).value) || 0,
    points: Number(qs(`[data-sid="${side.key}"] [data-role="points"]`, bodyEl).value) || 0,
  }));

  if (!multi && sets.length) {
    const wins = [0, 0];
    const totals = [0, 0];
    sets.forEach((item) => {
      totals[0] += item.a;
      totals[1] += item.b;
      if (item.a > item.b) wins[0] += 1;
      else if (item.b > item.a) wins[1] += 1;
    });
    entries[0].score = wins[0];
    entries[0].points = totals[0];
    entries[1].score = wins[1];
    entries[1].points = totals[1];
    // 回写，让管理员直接看到推导出的局分与总得分
    qs('[data-sid="A"] [data-role="score"]', bodyEl).value = wins[0];
    qs('[data-sid="A"] [data-role="points"]', bodyEl).value = totals[0];
    qs('[data-sid="B"] [data-role="score"]', bodyEl).value = wins[1];
    qs('[data-sid="B"] [data-role="points"]', bodyEl).value = totals[1];
  }

  const order = [...entries].sort(
    (x, y) => y.score - x.score || y.points - x.points || 0
  );
  const top = order[0];
  const tied = entries.filter((e) => e.score === top.score && e.points === top.points);
  const labels = entries.map((e) => `${e.key} ${e.score}`).join(' : ');
  let verdict;
  if (!entries.some((e) => e.score || e.points)) {
    verdict = '<span class="rpreview__mute">还没有填写比分</span>';
  } else if (tied.length > 1) {
    verdict = allowDraw && !multi
      ? '<span class="rpreview__warn">平局</span>'
      : '<span class="rpreview__warn">并列，请在下方指定胜方</span>';
  } else {
    verdict = `<b>${esc(top.label)}</b> 胜（${esc(labels)}）`;
  }
  const setsText = sets.length
    ? ` · ${sets.map((item) => `${item.a}:${item.b}`).join(' / ')}`
    : '';
  const box = qs('#rq-preview', bodyEl);
  if (box) {
    box.innerHTML = `${verdict}<span class="rpreview__sub">${esc(
      multi ? '按得分排名' : '局分 / 总得分'
    )}${esc(setsText)}</span>`;
  }
}

function openResultModal(rnd) {
  const sides = rnd.sides || [rnd.sideA, rnd.sideB];
  const multi = sides.length > 2;
  const allowDraw = App.state?.rules?.allowDraw && rnd.stage === 'group';
  const winnerOpts = [['', '自动判定']].concat(
    sides.map((side) => [side.key, `${side.label} 第 1`])
  );
  if (allowDraw && !multi) winnerOpts.push(['DRAW', '平局']);
  const setRows = (rnd.sets || []).map((item) => setRowHtml(item.a, item.b)).join('');

  const sideRows = sides
    .map(
      (side) =>
        `<div class="rrow" data-sid="${esc(side.key)}">` +
        `<span class="rrow__key" style="--c:${esc(side.color || 'var(--accent)')}">${esc(side.key)}</span>` +
        `<span class="rrow__name">${esc(side.label)}</span>` +
        `<label class="rrow__f"><span>${multi ? '得分' : '局分'}</span>` +
        `<input type="number" min="0" data-role="score" value="${side.score}"></label>` +
        `<label class="rrow__f"><span>${multi ? '小分' : '总得分'}</span>` +
        `<input type="number" min="0" data-role="points" value="${side.points}"></label>` +
        `</div>`
    )
    .join('');

  Modal.open({
    title: `录入结果 · ${rnd.label || rnd.code}`,
    body:
      `<div class="rrows">${sideRows}</div>` +
      (multi
        ? `<div class="notice" style="margin-top:10px">${sides.length} 队同场：按<b>得分</b>排名，` +
          `第 1 名即为本场胜者，名次分按 ${sides.length}/${sides.length - 1}/…/1 计入小组赛。</div>`
        : `<div class="rsets"><div class="rsets__head"><b>各局小分</b>` +
          `<span class="panel__hint">填了就自动算局分与总得分</span>` +
          `<button class="btn btn--sm" type="button" data-set-add>+ 添加一局</button></div>` +
          `<div class="rsets__body" id="rq-sets">${setRows}</div></div>`) +
      `<div id="rq-preview" class="rpreview"></div>` +
      `<div class="form form--2" style="margin-top:10px">` +
      `<div class="field"><label for="rq-winner">胜方</label><select id="rq-winner">${winnerOpts
        .map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`)
        .join('')}</select></div>` +
      `<div class="field"><label for="rq-duration">用时（分钟）</label><input id="rq-duration" type="number" min="0" value="${
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
      const refresh = () => refreshResultPreview(bodyEl, sides, allowDraw);
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
          qs('#rq-sets', bodyEl).insertAdjacentHTML('beforeend', setRowHtml());
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
        const read = (sel) => qs(sel, bodyEl).value.trim();
        const body = {
          winner: read('#rq-winner'),
          note: read('#rn'),
          sets: readSetRows(bodyEl),
          sides: sides.map((side) => ({
            key: side.key,
            score: Number(qs(`[data-sid="${side.key}"] [data-role="score"]`, bodyEl).value) || 0,
            points: Number(qs(`[data-sid="${side.key}"] [data-role="points"]`, bodyEl).value) || 0,
          })),
          durationMinutes: Number(read('#rq-duration')) || 0,
        };
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
    : `<div class="notice notice--warn">还没有配置直播根地址（WebRTC / RTMP / RTSP / HLS），` +
      `请到「直播配置」里填写。</div>`;

  Modal.open({
    title: `直播地址 · ${rnd.label || rnd.code}`,
    body:
      // 推流地址就在下面：先把「优先 WHIP / 关掉 B 帧」讲清楚
      pushTipsHtml() +
      tables +
      (cast.length
        ? `<div class="notice" style="margin-top:10px">本场有 ${cast.length} 路选手机位：` +
          `<b>推流优先用 WebRTC 套的 WHIP</b>（延迟最低），推不上去再用 TCP 套` +
          `（RTMP / RTSP 推，HLS / RTSP 播）。选手的地址整届固定，换比赛不用重推。</div>`
        : `<div class="notice notice--warn" style="margin-top:10px">本场选手都还没有推流流名，` +
          `可在「选手」页点该选手的「编辑」填推流流名（每位必须唯一），填好后这里会自动生成两套地址。</div>`) +
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
      `<b>仍然可用</b>：<b>直播开关</b>、<b>替补换人</b>（替补不在名单里会自动加入）、` +
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
 * 替补换人（锦标赛制）：队伍内 1:1 换人，不动赛程。
 *
 * 换上的人不在参与名单里时会由服务端**自动加入**；开赛后同样可用。
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
    title: '替补换人',
    body:
      `<div class="form form--2">` +
      `<div class="field"><label for="subTeam">队伍</label><select id="subTeam">${options}</select></div>` +
      `<div class="field"><label for="subFrom">换下（本队队员）</label><select id="subFrom"></select></div>` +
      `<div class="field"><label for="subTo">换上（替补）</label><select id="subTo"></select></div>` +
      fieldSwitch('subMark', '把换上的人标记为「替补」', false, {
        hint: '标记只作展示与统计口径；锦标赛制的上场人选由本窗口决定',
      }) +
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
            body: {
              fromId,
              toId,
              markSubstitute: qs('#f-subMark', bodyEl).checked,
            },
          });
          Modal.close();
          if (res.state) App.state = res.state;
          renderPublic();
          renderAdmin();
          toast(`${res.from.name} → ${res.to.name}：替补已上场`, 'ok', 6000);
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
  const joined = new Set(App.state?.participants || []);
  const players = (App.state?.players || [])
    .slice()
    .sort((a, b) => (a.substitute ? 1 : 0) - (b.substitute ? 1 : 0));
  const items = players
    .map((p) => {
      const cur = currentIds.includes(p.id);
      const out = joined.size > 0 && !joined.has(p.id);
      const meta = [
        p.tag,
        p.substitute ? '替补' : '',
        out ? '未参与本届' : '',
        p.active === false ? '停用' : '',
      ]
        .filter(Boolean)
        .join(' · ');
      return (
        `<button type="button" class="pick${cur ? ' pick--current' : ''}${p.substitute ? ' pick--sub' : ''}${out ? ' pick--out' : ''}" data-pid="${esc(p.id)}">` +
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
        { hint: '替补在锦标赛制下不参与组队' }
      ) +
      fieldNum('groupCount', '小组数（0 = 自动）', Number(rules.groupCount) || 0, {
        hint: '自动时约 4 队一组，并尽量排满整场',
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

function swapMember(ref, fromId) {
  const rnd = roundOf(ref);
  if (!rnd) return;
  const current = [...rnd.sideA.players.map((p) => p.id), ...rnd.sideB.players.map((p) => p.id)];
  const fromSide = rnd.sideA.players.some((p) => p.id === fromId) ? 'A' : 'B';
  openPlayerPicker({
    title: `${rnd.label || ref} · 换人 / 移出`,
    currentIds: current,
    onPick: async (toId) => {
      if (toId === fromId) return;
      try {
        const res = await api(`/rounds/${encodeURIComponent(ref)}/swap`, {
          method: 'POST',
          auth: true,
          body: { fromId, toId },
        });
        toast('已换人', 'ok');
        // 替补原本不在参与名单里时，服务端会把他补进本届名单
        (res.addedToParticipants || []).forEach((name) =>
          toast(`${name} 不在参与名单里，已自动加入本届名单`, 'info', 8000)
        );
      } catch (err) {
        toast(err.message, 'err');
      }
    },
    onRemove: async () => {
      const side = fromSide === 'A' ? rnd.sideA : rnd.sideB;
      const ids = side.players.map((p) => p.id).filter((id) => id !== fromId);
      try {
        await api(`/rounds/${encodeURIComponent(ref)}/lineup`, {
          method: 'POST',
          auth: true,
          body: { side: fromSide, playerIds: ids },
        });
        toast('已移出阵容', 'ok');
      } catch (err) {
        toast(err.message, 'err');
      }
    },
  });
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
  if (App.view === 'events') await renderEventsView();
  else if (App.view === 'admin') renderAdmin({ force: true });
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
      `<div class="notice" style="margin-top:10px">新建后会立即切换到新的一届，原赛事完整保留在「往届」中；` +
      `赛制决定比赛界面与赛程生成方式，之后也可在管理端切换（切换会清空赛程）。</div>`,
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
            `已创建并切换到「${res.name}」（${res.format === 'league' ? '积分制' : '锦标赛制'}）`,
            'ok',
            5000
          );
          await refreshEventsUI(true);
          // 新的一届直接成为主赛事：地址换成它的路由，继续吃实时推送
          if (hooks.goto) await hooks.goto('', App.view, { replace: true });
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

async function switchEvent(id) {
  if (!id) return;
  const event = App.events.find((e) => e.id === id);
  if (event?.current) {
    toast('已经是主赛事', 'info');
    return;
  }
  if (
    !window.confirm(
      `把「${event?.name || id}」设为主赛事？同期只会有一个主赛事，原来的那个自动让位；` +
        `根路径 / 与整站（总览 / 赛程 / 选手 / 直播）都会变成这一届。`
    )
  )
    return;
  try {
    const res = await api(`/events/${id}/switch`, { method: 'POST', auth: true });
    toast(`「${res.name || id}」已设为主赛事`, 'ok');
    await refreshEventsUI(true);
    // 它成了主赛事：地址换成它的路由（不再是「锁定的往届」），继续吃实时推送
    if (hooks.goto) await hooks.goto(id, App.view, { replace: true });
    else if (hooks.refreshState) await hooks.refreshState();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function deleteEvent(id) {
  const event = App.events.find((e) => e.id === id);
  if (!window.confirm(`确认删除「${event?.name || id}」？该届的名单、赛程与成绩会一并删除，无法恢复。`)) return;
  try {
    const res =     await api(`/events/${id}`, { method: 'DELETE', auth: true });
    toast('已删除该届赛事', 'ok');
    await refreshEventsUI(true);
    // 删掉的可能是正在看的那一届，甚至就是主赛事：统一回到主赛事的同一页
    if (hooks.goto) await hooks.goto('', App.view, { replace: true });
    else if (hooks.refreshState) await hooks.refreshState();
    log.info('已删除届次', id, '当前届', res.current);
  } catch (err) {
    toast(err.message, 'err');
  }
}

function openPlayerEditModal(player, defaults = {}) {
  const url = player?.avatar || '';
  const isSub = player ? Boolean(player.substitute) : Boolean(defaults.substitute);
  // 隐私字段（UUID / QQ / 推流流名）只在登录后的私有数据里
  const priv = privateOf(player?.id);
  const preview = url
    ? `<img src="${esc(url)}" alt=""><span class="ava__ring"></span>`
    : esc(String(player?.name || player?.id || '?').slice(0, 1));
  Modal.open({
    title: player ? `编辑选手 · ${player.name || player.id}` : isSub ? '新增替补' : '新增选手',
    body:
      `<div class="form form--2">` +
      fieldText('name', '姓名', player?.name || '') +
      fieldText('uuid', '游戏 UUID', priv.uuid || '', {
        hint: '玩家提供的游戏内 ID；UUID 与 QQ 都不会下发用户端',
      }) +
      fieldText('streamKey', '推流流名（必须唯一）', priv.streamKey || '', {
        hint: priv.endpoints?.whipPush
          ? `优先 WHIP：${priv.endpoints.whipPush}` +
            `；备选 RTMP：${priv.endpoints.rtmpPush || '未配置'}（整届固定，换比赛不用重推）。` +
            PUSH_TIP_LINE
          : `每位选手互不相同；它就是这位选手的推流地址，如 tom → …/tom/whip（整届固定）。${PUSH_TIP_LINE}`,
      }) +
      fieldText('qq', 'QQ（可选）', priv.qq || '', { hint: '仅服务端用于取头像' }) +
      fieldText('tag', '编号（可选）', player?.tag || '') +
      `<div class="field" style="grid-column:1/-1"><label>头像</label>` +
      `<div class="ava-edit" data-avatar-scope>` +
      `<span class="ava ava--md${url ? '' : ' ava--placeholder'}" data-role="avatar-preview">${preview}</span>` +
      `<div class="ava-edit__col">` +
      `<input type="file" accept="image/*" data-role="avatar-file">` +
      `<input name="avatar" type="text" value="${esc(url)}" placeholder="或直接填写图片 URL">` +
      `</div></div></div>` +
      `<div style="grid-column:1/-1">${fieldArea('note', '备注', priv.note || '')}</div>` +
      `<div class="field field--switch"><span class="switch">` +
      `<input id="f-substitute" name="substitute" type="checkbox"${isSub ? ' checked' : ''}><i></i></span><label for="f-substitute">替补</label></div>` +
      `<div class="field field--switch"><span class="switch">` +
      `<input id="f-active" name="active" type="checkbox"${player?.active !== false ? ' checked' : ''}><i></i></span><label for="f-active">启用</label></div>` +
      `</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
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
  if (App.view === 'admin') renderAdmin({ force: true });
}

function refreshDiagIfAdmin() {
  if (App.view === 'admin') refreshDiagnostics();
}

/* --------------------------- 动作分发 --------------------------- */
export async function handleAction(act, el) {
  log.debug('动作', act, el?.dataset);
  switch (act) {
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
    case 'route-home':
      // 回到「当前届」路由的同一页（根路径只跟管理员选定的当前届走）
      return hooks.goto?.('', App.view);
    case 'event-new':
      return openEventNewModal();
    case 'event-refresh':
      return refreshEventsUI(true);
    case 'event-view': {
      // 点卡片 = 换到这一届的路由（默认停在当前这一页，往届页正好切成它的详情）
      const id = el.dataset.id;
      if (!id) return;
      return hooks.goto?.(id, el.dataset.page || App.view);
    }
    case 'event-open': {
      // 卡片角上的「新窗口」：同一届同页面，另开一个窗口并排看
      const id = el.dataset.id;
      if (!id) return;
      const path = routePath(id, el.dataset.page || App.view);
      const win = window.open(path, '_blank');
      if (win) win.opener = null; // 别把本站的 window 引用交给新页面
      return;
    }
    case 'event-switch':
      return switchEvent(el.dataset.id);
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
      window.open(`/api/export?token=${encodeURIComponent(App.token)}`, '_blank', 'noopener');
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
      toast('已退出登录', 'info');
      renderAdmin({ force: true });
      renderPublic();
      return;
    case 'admin-login':
      return login(qs('#adminKey')?.value || '');
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
      return swapMember(el.dataset.code, el.dataset.pid);
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
    case 'player-add-sub':
      return openPlayerEditModal(null, { substitute: true });
    case 'player-edit':
      return openPlayerEditModal((App.state?.players || []).find((p) => p.id === el.dataset.id));
    case 'player-del':
      return deletePlayer(el.dataset.id);
    case 'avatar-refresh':
      return refreshAvatar(el.dataset.id);
    case 'participants-all':
      return setParticipants(true);
    case 'participants-none':
      return setParticipants(false);
    case 'participants-invert':
      return setParticipants(null);
    case 'participants-save':
      return saveParticipants();
    case 'team-del':
      return deleteTeam(el.dataset.id);
    case 'team-save':
      return saveTeams();
    // 注：复制按钮统一由 app.js 的 [data-copy] 委托处理（它先于 data-act 拦截），
    // 这里不再重复处理，否则会弹出两条 toast。
    default:
      log.warn('未处理的动作', act);
  }
}

export async function login(key) {
  if (!key.trim()) {
    toast('请输入管理 KEY', 'warn');
    return;
  }
  try {
    const res = await api('/auth', { method: 'POST', body: { key } });
    App.token = res.token;
    localStorage.setItem('nte:token', res.token);
    // 登录后才能取到选手隐私字段与推流地址
    await refreshPrivate();
    toast('登录成功', 'ok');
    log.info('管理登录成功');
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

function collectTeamRows() {
  const rows = qsa('[data-team-row]', qs('#adminPanel'));
  const base = new Map((App.state?.teams || []).map((t) => [t.id, t]));
  return rows.map((row) => {
    const id = row.dataset.teamRow;
    const prev = base.get(id) || {};
    const get = (n) => qs(`[name="${n}"]`, row);
    return {
      id,
      name: get('name').value.trim(),
      short: get('short').value.trim(),
      color: get('color').value.trim(),
      group: prev.group || '',
      playerIds: prev.playerIds || [],
    };
  });
}

async function saveTeams() {
  const teams = collectTeamRows().filter((t) => t.name);
  if (!teams.length) {
    toast('没有可保存的队伍', 'warn');
    return;
  }
  try {
    const res = await api('/teams', { method: 'PUT', auth: true, body: { teams } });
    toast(`已保存 ${res.count} 支队伍`, 'ok');
    (res.warnings || []).forEach((w) => toast(w, 'warn', 6000));
    if (res.state) App.state = res.state;
    setTimeout(hooksRenderAdmin, 300);
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function deleteTeam(id) {
  const teams = (App.state?.teams || []).filter((t) => t.id !== id);
  if (!teams.length) {
    toast('至少要保留一支队伍', 'warn');
    return;
  }
  if (!window.confirm('删除队伍属于结构性变更，会清空当前赛程。确认继续？')) return;
  try {
    const res = await api('/teams', { method: 'PUT', auth: true, body: { teams } });
    toast('已删除队伍', 'ok');
    (res.warnings || []).forEach((w) => toast(w, 'warn', 6000));
    if (res.state) App.state = res.state;
    setTimeout(hooksRenderAdmin, 300);
  } catch (err) {
    toast(err.message, 'err');
  }
}

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

  if (name === 'admin-key') {
    const values = collectForm(formEl);
    if (!String(values.key || '').trim()) {
      toast('请输入新的管理 KEY', 'warn');
      return;
    }
    try {
      const res = await api('/admin/key', {
        method: 'POST',
        auth: true,
        body: { key: values.key, storeHash: Boolean(values.storeHash) },
      });
      toast(
        res.mode === 'sha256' ? 'KEY 已更新（仅存哈希），请用新 KEY 重新登录' : 'KEY 已更新，请用新 KEY 重新登录',
        'ok',
        6000
      );
      App.token = '';
      localStorage.removeItem(TOKEN_KEY);
      renderAdmin();
      renderPublic();
    } catch (err) {
      toast(err.message, 'err');
    }
    return;
  }

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
