/* 「对阵总览」树状图自检（纯 Node，无依赖）。
 *
 * 为什么需要它：树状图的坐标、框高、画布高度全是 JS 算完写进内联 style 的，
 * CSS 只负责配色（见 views.js 里 BT 那一段的注释）。这类「算出来的数字」出错的
 * 表现特别隐蔽——框被裁掉一角、冠军框压在别人身上，页面看起来只是「有点怪」，
 * 而 Python 测试完全够不到。
 *
 * 这里钉住几条：
 *
 * 1. **冠军框里要有成员头像**：总览顶部的冠军横幅有，树里没有就成了「一处有一处没有」；
 *    头像必须与横幅同源（上传头像用原图，没配的回落首字），不是自己另画一套；
 * 2. 框高与画布高度要**把头像那一行算进去**，否则会超出容器被 overflow 裁掉；
 * 3. 队伍人多时头像**不换行**（框只有 184px 宽），名字一个不少地进悬停提示；
 * 4. 还没决出冠军时：框里没有头像、高度回到一个对阵框。
 *
 * 用法：``node tools/check_bracket_tree.mjs``（与 check_live_player.mjs 一样进 CI）。
 */

import { installBrowserStub } from './_browser_stub.mjs';

/* ------------------------------ 浏览器桩 ------------------------------ */

installBrowserStub();
const { App } = await import('../static/js/core.js');
const { bracketPanelHtml } = await import('../static/js/views.js');

/* ------------------------------ 断言与工具 ------------------------------ */

const failures = [];
function check(name, cond, detail = '') {
  console.log(`  ${cond ? 'OK ' : '✗  '} ${name}${detail ? `（${detail}）` : ''}`);
  if (!cond) failures.push(name);
}

/** 造一份「决赛已打完」的最小公开状态；``members`` 传选手对象数组。 */
function treeState({ members = [], champion = true } = {}) {
  App.state = { ui: { showAvatar: true }, players: members };
  App.me = null;
  const side = (label, ids, rank) => ({
    teamId: 't1',
    label,
    playerIds: ids,
    rank,
    score: 3,
  });
  const ids = members.map((p) => p.id);
  return {
    event: { name: '用例届' },
    players: members,
    teams: [{ id: 't1', label: '仙台猫粮', short: '仙台猫粮', color: '#ffd83d' }],
    rules: { teamsPerMatch: 2, loserBracket: false },
    format: { size: 2, groupCount: 0, groupMatches: 0, knockoutMatches: 1, groupStageDone: true },
    groups: [],
    rounds: [],
    bracket: {
      wb: [
        {
          title: '决赛',
          matches: [
            {
              code: 'GF',
              label: '决赛',
              status: 'done',
              winner: 'A',
              sides: [side('仙台猫粮', ids, 1), side('对手队', [], 2)],
            },
          ],
        },
      ],
      lb: [],
      gf: [{ title: '总决赛', matches: [] }],
    },
    champion: champion ? { id: 't1', name: '仙台猫粮', short: '仙台猫粮', playerIds: ids } : null,
  };
}

/** 从渲染出来的 HTML 里抠出冠军框的 style 与内容。 */
function champOf(html) {
  const box = html.match(/<div class="btree__champ[^"]*"[^>]*>[\s\S]*?<\/div>/)?.[0] || '';
  const style = box.match(/style="([^"]+)"/)?.[1] || '';
  const num = (key) => Number((style.match(new RegExp(`${key}:(-?\\d+)px`)) || [, NaN])[1]);
  return {
    box,
    top: num('top'),
    height: num('height'),
    avatars: (box.match(/class="ava ava--xs/g) || []).length,
    imgs: (box.match(/<img src="([^"]+)"/g) || []).length,
    title: box.match(/title="([^"]*)"/)?.[1] || '',
  };
}

const wrapperHeight = (html) =>
  Number((html.match(/<div class="btree" style="[^"]*height:(\d+)px/) || [, NaN])[1]);

/* ------------------------------ 场景 ------------------------------ */

console.log('=== 冠军框要有成员头像（与冠军横幅同一份数据 / 同一套头像） ===');
const two = [
  { id: 'p1', name: '甲', avatar: '/api/avatar/file/aaa.png', hasAvatar: true },
  { id: 'p2', name: '乙', avatar: '', hasAvatar: false }, // 没配头像 → 回落首字
];
let html = bracketPanelHtml(treeState({ members: two }));
let champ = champOf(html);
check('排出两个成员头像', champ.avatars === 2, `${champ.avatars} 个`);
check('上传头像用原图', champ.box.includes('src="/api/avatar/file/aaa.png"'));
check('没配头像的回落首字占位', champ.box.includes('ava--placeholder'));
check('悬停提示里带上成员名', champ.title.includes('甲') && champ.title.includes('乙'), champ.title);
check('框高含头像那一行（118 + 36）', champ.height === 154, `${champ.height}px`);
check(
  '画布高度包得住冠军框（不会被裁）',
  wrapperHeight(html) >= champ.top + champ.height,
  `画布 ${wrapperHeight(html)} ≥ ${champ.top + champ.height}`
);

console.log('');
console.log('=== 人多也不换行：最多 5 个头像，名字一个不少 ===');
const many = Array.from({ length: 7 }, (_, i) => ({ id: `p${i}`, name: `选手${i}`, avatar: '' }));
html = bracketPanelHtml(treeState({ members: many }));
champ = champOf(html);
check('头像数封顶在 5', champ.avatars === 5, `${champ.avatars} 个`);
check(
  '悬停提示里 7 个名字都在',
  many.every((p) => champ.title.includes(p.name)),
  champ.title.slice(0, 40) + '…'
);

console.log('');
console.log('=== 还没决出冠军：框里不摆头像，高度回到一个对阵框 ===');
html = bracketPanelHtml(treeState({ members: two, champion: false }));
champ = champOf(html);
check('没有头像', champ.avatars === 0 && champ.imgs === 0);
check('框高 = 118', champ.height === 118, `${champ.height}px`);
check(
  '画布仍然包得住',
  wrapperHeight(html) >= champ.top + champ.height,
  `画布 ${wrapperHeight(html)} ≥ ${champ.top + champ.height}`
);

console.log('');
if (failures.length) {
  console.log(`✗ 对阵树自检未通过：${failures.join('；')}`);
  process.exit(1);
}
console.log('对阵树自检全部通过');
