/* 成员与服务器管理：独立页 /admin（服务器）与 /user（我的）。
 *
 * 与赛事页的关系：
 *   * 成员是**全局账号**（跨届共享），密钥 / Bearer 令牌只由服务端生成，
 *     明文只在生成/轮换那一次出现，之后一律不可再取（只能轮换）；
 *   * 服务器管理员在 /admin 管成员、届次、全局封禁与自定义 HTML；
 *   * 普通成员在 /user 改自己的资料、直播间名字并轮换自己的凭据。
 *
 * 本模块只负责渲染与交互，数据读写全部走 REST；动作由 actions.js 转发进来。
 */

import {
  API,
  App,
  TOKEN_KEY,
  api,
  copyText,
  esc,
  fmtFull,
  hooks,
  isServerAdmin,
  log,
  Modal,
  myPermission,
  qs,
  routePath,
  toast,
} from './core.js';
import {
  collectForm,
  fieldArea,
  fieldNum,
  fieldSelect,
  fieldSwitch,
  fieldText,
  isMemberLive,
  liveTag,
  memberAvaHtml,
  ownerHtml,
  panelHtml,
  PUSH_TIP_LINE,
} from './ui.js';
import { renderNoticeBoard, renderServerInfo } from './notices.js';
import { loadEvents, statusBadge } from './events.js';
import { refreshLiveHealth } from './live.js';

const PERMISSION_LABEL = {
  member: '成员',
  event_admin: '赛事管理员',
  server_admin: '服务器管理员',
};

const BAN_SCOPE_LABEL = { global: '全局封禁', event: '赛事封禁' };

/* ------------------------------ 数据 ------------------------------ */
/** 拉取服务器管理数据（成员 + 服务器配置 + 登录限制）。仅服务器管理员需要。 */
export async function refreshServerData({ silent = false } = {}) {
  if (!isServerAdmin()) {
    App.server = null;
    return null;
  }
  try {
    const [members, config, guard, backups, qqbot, activity] = await Promise.all([
      api('/members', { auth: true }),
      api('/server/config', { auth: true }),
      api('/server/login-guard', { auth: true }),
      api('/backups', { auth: true }),
      api('/qqbot', { auth: true }),
      // 操作日志是「顺带看看」的东西：它挂了不该把整页拖垮，所以单独兜底
      api('/activity', { auth: true }).catch(() => ({ items: [] })),
    ]);
    App.server = {
      members: members.members || [],
      duplicates: members.duplicates || {},
      config,
      guard,
      backups,
      qqbot,
      activity: activity.items || [],
      at: Date.now(),
    };
    log.debug('服务器数据已加载', App.server.members.length, '位成员');
  } catch (err) {
    if (!silent) toast(err.message, 'err');
    log.warn('服务器数据加载失败', err);
    App.server = null;
  }
  return App.server;
}

/* ------------------------------ /admin 页 ------------------------------ */
const serverGateHtml = () =>
  `<div class="panel"><div class="gate">` +
  `<div class="gate__title">服务器管理</div>` +
  `<p class="gate__desc">这里是服务器级管理（成员 / 届次 / 直播封禁 / 自定义 HTML）。` +
  `请用<b>服务器管理员密钥</b>登录后使用。</p>` +
  `<div class="field"><label for="serverKey">登录密钥</label>` +
  `<input id="serverKey" type="password" autocomplete="current-password" placeholder="请输入服务器管理员密钥"></div>` +
  `<button class="btn btn--primary btn--block" type="button" data-act="admin-login">登录</button>` +
  `<p class="gate__hint">密钥在服务启动日志里（首次启动自动生成）；` +
  `服务器管理员有且只有一个，忘记可在成员管理里轮换。</p>` +
  `</div></div>`;

function memberSearchMatch(m) {
  const kw = App.memberSearch.trim().toLowerCase();
  if (!kw) return true;
  return [m.name, m.uid, m.gameUuid, m.streamId, m.roomTitle]
    .filter(Boolean)
    .some((v) => String(v).toLowerCase().includes(kw));
}

function memberFilterMatch(m) {
  const f = App.memberFilter || 'all';
  if (f === 'server_admin' || f === 'event_admin' || f === 'member') return m.permission === f;
  if (f === 'live') return isMemberLive(m.uid);
  if (f === 'banned') return Boolean(m.banned);
  return true;
}

/** 该成员的推流 ID 是否与别人重复；返回占用者列表（无重复返回 null）。 */
const duplicateOf = (m) => (m.streamId ? (App.server?.duplicates || {})[m.streamId] || null : null);

function memberCardHtml(m) {
  const live = isMemberLive(m.uid);
  const dup = duplicateOf(m);
  const tags = [];
  if (live) tags.push(liveTag('直播中'));
  if (dup) {
    tags.push(
      `<span class="badge badge--lose" title="${esc(dup.join('、'))}">推流 ID 重复</span>`
    );
  }
  tags.push(
    `<span class="badge ${m.permission === 'server_admin' ? 'badge--live' : 'badge--done'}">` +
      `${esc(PERMISSION_LABEL[m.permission] || m.permission)}</span>`
  );
  if (m.banned) {
    tags.push(
      `<span class="badge badge--lose" title="${esc(m.banned.reason || '')}">` +
        `${esc(m.banned.until ? `封禁至 ${fmtFull(m.banned.until)}` : '永久封禁')}</span>`
    );
  }
  if (m.active === false) tags.push('<span class="badge badge--lose">停用</span>');
  if (m.hasKey) tags.push('<span class="badge badge--pending" title="已配置登录密钥（加盐哈希）">密钥</span>');
  if (m.hasBearer) tags.push('<span class="badge badge--pending" title="已配置 Bearer 令牌（加盐哈希）">令牌</span>');
  if (m.legacyCredential) {
    // 老库里的凭据是无盐 sha256；服务端拿不到明文，只能靠轮换升级
    tags.push(
      '<span class="badge badge--lose" title="凭据仍是历史无盐格式，点「换密钥 / 换令牌」轮换一次即可升级为加盐哈希">凭据待升级</span>'
    );
  }

  const meta = [m.uid, m.gameUuid ? `游戏 ${m.gameUuid}` : '', m.streamId ? `推流 ${m.streamId}` : '']
    .filter(Boolean)
    .join(' · ');
  const ops =
    `<div class="round__ops">` +
    `<button class="btn btn--sm" type="button" data-act="member-edit" data-uid="${esc(m.uid)}">编辑</button>` +
    `<button class="btn btn--sm" type="button" data-act="member-rotate" data-uid="${esc(m.uid)}" data-what="key">换密钥</button>` +
    `<button class="btn btn--sm" type="button" data-act="member-rotate" data-uid="${esc(m.uid)}" data-what="bearer">换令牌</button>` +
    (m.streamId
      ? `<button class="btn btn--sm btn--danger" type="button" data-act="member-ban" data-uid="${esc(m.uid)}">` +
        `掐断 / 封禁</button>`
      : '') +
    (m.permission === 'server_admin'
      ? ''
      : `<button class="btn btn--sm btn--danger" type="button" data-act="member-del" data-uid="${esc(m.uid)}">删除</button>`) +
    `</div>`;
  return (
    `<article class="pcard${live ? ' pcard--live' : ''}${dup ? ' pcard--warn' : ''}` +
    `${m.active === false ? ' pcard--inactive' : ''}">` +
    `<div class="pcard__band"></div>` +
    `<div class="pcard__top">${memberAvaHtml(m, 'md')}<div>` +
    `<div class="pcard__name">${esc(m.name || m.uid)}</div>` +
    `<div class="pcard__sub" title="${esc(meta)}">${esc(m.roomTitle || m.uid)}</div>` +
    `</div></div>` +
    (dup
      ? `<div class="pcard__tags"><span class="panel__hint">推流 ID「${esc(m.streamId)}」与 ${esc(
          dup.filter((x) => !x.includes(m.name || m.uid)).join('、') || dup.join('、')
        )} 重复，请修改</span></div>`
      : '') +
    (m.roundLabel
      ? `<div class="pcard__tags"><span class="badge badge--live">比赛中 · ${esc(m.roundLabel)}</span></div>`
      : '') +
    `<div class="pcard__tags">${tags.join('')}</div>` +
    ops +
    `</article>`
  );
}

/** 成员列表每页几行（超过就分页）。 */
const MEMBER_ROWS = 4;

/**
 * 量出成员网格当前有几列。
 *
 * ``auto-fill`` 会**把占满宽度的轨道全部生成出来**（哪怕没有卡片），
 * 所以只渲染一页也能量准，不必先把全部成员铺出来。
 */
function memberCols() {
  const grid = qs('#memberGrid .roster-grid');
  if (!grid) return 0;
  const tpl = getComputedStyle(grid).gridTemplateColumns;
  if (!tpl || tpl === 'none') return 0;
  return tpl.split(/\s+/).filter(Boolean).length;
}

/** 分页条：只在真的需要翻页时才出现（成员少的时候一条都不多）。 */
function memberPagerHtml(page, pages) {
  const btn = (target, label, disabled) =>
    `<button class="btn btn--sm" type="button" data-act="member-page" data-page="${target}"` +
    `${disabled ? ' disabled' : ''}>${label}</button>`;
  return (
    `<div class="pager tool-group">` +
    btn(page - 1, '上一页', page <= 1) +
    `<span class="pager__now">第 ${page} / ${pages} 页</span>` +
    btn(page + 1, '下一页', page >= pages) +
    `</div>`
  );
}

/** 只重绘成员网格（搜索时避免整页重建，保住输入焦点）。 */
export function renderMemberGrid() {
  const host = qs('#memberGrid');
  if (!host) return;
  const data = App.server?.members || [];
  const shown = data.filter((m) => memberSearchMatch(m) && memberFilterMatch(m));

  // 一页 = 4 行 × 列数。列数先用上次量到的值（首次默认 3），渲染完再量一次；
  // 量出来不一样（换窗口宽度 / 换设备）就重绘一次——之后值稳定，不会来回抖。
  const perPage = Math.max(1, (App.memberCols || 3) * MEMBER_ROWS);
  const pages = Math.max(1, Math.ceil(shown.length / perPage));
  const page = Math.min(Math.max(1, App.memberPage || 1), pages);
  App.memberPage = page;
  const slice = shown.slice((page - 1) * perPage, page * perPage);

  host.innerHTML =
    (slice.length
      ? `<div class="roster-grid">${slice.map(memberCardHtml).join('')}</div>`
      : `<div class="empty"><b>没有匹配的成员</b>调整搜索或筛选条件</div>`) +
    (pages > 1 ? memberPagerHtml(page, pages) : '');

  const count = qs('#memberCount');
  if (count) {
    count.textContent =
      `${shown.length} / ${data.length} 人` + (pages > 1 ? ` · 第 ${page} / ${pages} 页` : '');
  }

  const cols = memberCols();
  if (cols && cols !== App.memberCols) {
    App.memberCols = cols;
    renderMemberGrid(); // 列数变了：重算一页装多少人（下次量到同值即停）
  }
}

function membersPanelHtml() {
  const dups = Object.entries(App.server?.duplicates || {});
  const legacy = App.server?.legacyCredentials || [];
  const legacyNotice = legacy.length
    ? `<div class="notice notice--warn" style="margin-bottom:10px">有 <b>${legacy.length}</b> 位成员的凭据还是` +
      `<b>历史无盐格式</b>（老版本写的）。服务端拿不到明文、无法自动升级——在成员卡上点一次` +
      `「换密钥 / 换令牌」轮换即可切换到<b>加盐哈希</b>（新值会弹窗显示，记得转告本人）。</div>`
    : '';
  const dupNotice = dups.length
    ? `<div class="notice notice--warn" style="margin-bottom:10px">有 <b>${dups.length}</b> 个推流 ID 重复：` +
      `${esc(dups.slice(0, 3).map(([k]) => k).join('、'))}${dups.length > 3 ? ' 等' : ''}。` +
      `重复会串流，请改掉其中一个。</div>`
    : '';
  const body =
    legacyNotice +
    dupNotice +
    `<div class="tool-group" style="margin-bottom:10px">` +
    `<div class="field" style="min-width:200px"><input id="memberSearch" type="search" ` +
    `placeholder="搜索名字 / uid / 游戏 UUID / 推流 ID" value="${esc(App.memberSearch)}" autocomplete="off"></div>` +
    `<select id="memberFilter">` +
    [
      ['all', '全部'],
      ['member', '成员'],
      ['event_admin', '赛事管理员'],
      ['server_admin', '服务器管理员'],
      ['live', '直播中'],
      ['banned', '封禁中'],
    ]
      .map(([v, t]) => `<option value="${v}"${App.memberFilter === v ? ' selected' : ''}>${t}</option>`)
      .join('') +
    `</select>` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="member-add">新增成员</button>` +
    `<span class="panel__hint" style="margin-left:auto" id="memberCount"></span>` +
    `</div>` +
    `<div id="memberGrid"></div>`;
  return panelHtml('成员管理', '全局账号 · 密钥与令牌仅服务器可见', body);
}

function eventAdminCard(e) {
  const ops =
    `<div class="evt-card__ops">` +
    `<button class="btn btn--sm" type="button" data-act="event-rename" data-id="${esc(e.id)}">重命名</button>` +
    (e.status === 'closed'
      ? `<button class="btn btn--sm" type="button" data-act="event-reopen" data-id="${esc(e.id)}">恢复进行</button>`
      : `<button class="btn btn--sm" type="button" data-act="event-close" data-id="${esc(e.id)}">标记结束</button>`) +
    (e.hidden
      ? `<button class="btn btn--sm" type="button" data-act="event-hidden" data-id="${esc(e.id)}" data-hidden="0">取消隐藏</button>`
      : `<button class="btn btn--sm" type="button" data-act="event-hidden" data-id="${esc(e.id)}" data-hidden="1">隐藏</button>`) +
    `<button class="btn btn--sm btn--danger" type="button" data-act="event-delete" data-id="${esc(e.id)}">删除</button>` +
    `</div>`;
  return (
    `<article class="evt-card evt-card--act">` +
    `<div class="evt-card__head">` +
    `<a class="evt-card__name" href="${esc(routePath(e.id, 'overview'))}" data-route>` +
    `${esc(e.name || e.id)}</a>` +
    statusBadge(e.status) +
    (e.hidden ? '<span class="badge badge--pending">已隐藏</span>' : '') +
    // 显示**头像 + 名字**而不是 uid 前 8 位：那串随机字符对谁都说明不了问题，
    // uid 留在 tooltip 里给排查用。
    `<span class="panel__hint" title="归属 uid：${esc(e.ownerUid || '—')}">` +
    `${e.ownerName ? ownerHtml(e.ownerName, e.ownerAvatar) : '举办者 服务器管理员'}</span>` +
    `</div>` +
    `<div class="evt-card__meta">${e.players || 0} 人 · 已赛 ${e.played || 0} / ${e.rounds || 0} 局` +
    (e.champion ? ` · 榜首 ${esc(e.champion)}` : '') +
    `</div>` +
    ops +
    `</article>`
  );
}

function eventsPanelHtml() {
  const events = App.events || [];
  const body =
    `<div class="tool-group" style="margin-bottom:10px">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="event-new">新建一届</button>` +
    `<button class="btn btn--sm" type="button" data-act="event-refresh">刷新届次</button>` +
    `<span class="panel__hint">全部届次（含进行中与未来）：可封存 / 隐藏 / 重命名 / 删除</span>` +
    `</div>` +
    (events.length
      ? `<div class="evt-grid">${events.map(eventAdminCard).join('')}</div>`
      : `<div class="empty"><b>暂无赛事</b>点「新建一届」开始</div>`);
  return panelHtml('届次管理', '全部届次 · 服务器统一管理', body);
}

/** 站点名称（全局）：顶栏、浏览器标签与主页都用它。 */
function sitePanelHtml() {
  const body =
    `<form class="form" data-form="site">` +
    fieldText('siteName', '站点名称', App.state?.siteName || '', {
      ph: 'NTE 比赛',
      hint: '显示在顶栏左侧、浏览器标签与主页标题；与比赛无关的页面都用它。留空回落默认值。',
    }) +
    `<div class="form-actions"><button class="btn btn--primary" type="submit">保存站点名称</button></div>` +
    `</form>`;
  return panelHtml('站点', '全局 · 服务器级', body);
}

/** 备份里的「原因」怎么念给用户看。 */
const BACKUP_REASON = { manual: '手动', auto: '自动', 'pre-restore': '还原前' };

function backupReasonText(it) {
  const reason = String(it.reason || '');
  if (reason.startsWith('upload')) return '上传的备份';
  return BACKUP_REASON[reason] || reason || '—';
}

function backupSizeText(bytes) {
  const n = Number(bytes) || 0;
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** 数据备份：打包 / 下载 / 还原 / 上传还原 / 周期性自动备份。 */
function backupPanelHtml() {
  const b = App.server?.backups;
  if (!b) return '';
  const s = b.settings || {};
  const items = b.backups || [];
  const rows = items.length
    ? `<div class="bak-list">` +
      items
        .map(
          (it) =>
            `<div class="bak">` +
            `<div class="bak__main">` +
            `<div class="bak__name">${esc(it.name)}</div>` +
            `<div class="bak__meta">${esc(fmtFull(it.createdAt))} · ${backupSizeText(it.size)} · ` +
            `${esc(backupReasonText(it))}` +
            (it.avatars ? ` · 头像 ${it.avatars}` : '') +
            (it.ok ? '' : ' · <b>清单损坏</b>') +
            `</div></div>` +
            `<div class="tool-group">` +
            `<button class="btn btn--sm" type="button" data-act="backup-download" data-name="${esc(it.name)}">下载</button>` +
            `<button class="btn btn--sm" type="button" data-act="backup-restore" data-name="${esc(it.name)}">还原</button>` +
            `<button class="btn btn--sm btn--danger" type="button" data-act="backup-delete" data-name="${esc(it.name)}">删除</button>` +
            `</div></div>`
        )
        .join('') +
      `</div>`
    : `<div class="empty"><b>还没有备份</b>点上面的「立即备份」生成第一份——` +
      `备份会打包整个数据库与头像目录。</div>`;

  const nextText = !s.enabled ? '未开启' : b.nextRunAt ? fmtFull(b.nextRunAt) : '尽快';
  const body =
    `<div class="notice">一份备份 = <b>全部数据</b>：所有届次与赛程、成员与凭据、直播封禁、` +
    `自定义 HTML、本地上传的头像（QQ 头像缓存不算，它可再生）。` +
    `文件保存在服务器上的 <code>${esc(b.dir || '')}</code>。</div>` +
    `<div class="notice notice--warn" style="margin-top:8px"><b>还原会覆盖当前全部数据</b>：` +
    `服务端会先自动打一份「还原前」的安全备份，还原完成后<b>所有会话失效</b>，需要重新登录。</div>` +
    `<form class="form form--2" data-form="backup" style="margin-top:10px">` +
    fieldSwitch('enabled', '开启周期性自动备份', s.enabled === true) +
    fieldNum('intervalHours', '备份间隔（小时）', s.intervalHours ?? 24, { hint: '1 ~ 720' }) +
    fieldNum('keep', '最多保留份数', s.keep ?? 7, { hint: '超出后按时间从旧到新自动删除' }) +
    `<div style="grid-column:1/-1" class="form-actions">` +
    `<button class="btn btn--primary" type="submit">保存备份设置</button></div>` +
    `</form>` +
    `<div class="notice" style="margin-top:10px">下次自动备份：<b>${esc(nextText)}</b>` +
    (s.lastRunAt ? `（上次 ${esc(fmtFull(s.lastRunAt))}）` : '') +
    `</div>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="backup-create">立即备份</button>` +
    `<button class="btn btn--sm" type="button" data-act="backup-upload">上传备份并还原</button>` +
    `<input id="backupFile" data-role="backup-file" type="file" accept=".zip,application/zip" hidden>` +
    `<span class="panel__hint" style="margin-left:auto">上传上限 ${b.maxUploadMb || 256} MB</span>` +
    `</div>` +
    `<div style="margin-top:10px">${rows}</div>`;
  return panelHtml('备份', items.length ? `${items.length} 份` : '尚无备份', body);
}

/**
 * 查询接口令牌：给配套的 AstrBot 插件用（群命令，不依赖任何大模型）。
 *
 * 令牌只存**加盐哈希**，明文只在生成那一次弹出；重置后旧令牌立即失效。
 */
function botTokenBlockHtml(q) {
  return (
    `<div class="panel__divider" style="margin:12px 0;border-top:1px solid var(--line)"></div>` +
    `<div class="notice">配套插件 <code>astrbot_plugin_nte_match</code>（见项目里 <code>integrations/</code>）` +
    `可以把赛事数据做成 <b>群命令</b>（不经过大模型，答案稳定）。` +
    `把帮助图放成 <code>static/help.jpg</code>，本域 <code>/help.jpg</code> 即可访问，` +
    `「比赛帮助」会自动改成回这张图（插件无需配置；没放图就回文字说明）。` +
    `它需要一个只读查询令牌：</div>` +
    `<dl class="kv" style="margin-top:10px">` +
    `<div class="kv__row"><dt>查询 API</dt><dd>${q.hasBotToken ? '已启用（令牌不可查看）' : '未启用'}</dd></div>` +
    `<div class="kv__row"><dt>接口前缀</dt><dd><code>/api/bot/query</code> · <code>/api/bot/events</code> · <code>/api/bot/participants</code></dd></div>` +
    `</dl>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="qqbot-token-new">` +
    `${q.hasBotToken ? '重置令牌' : '生成令牌'}</button>` +
    `<button class="btn btn--sm btn--danger" type="button" data-act="qqbot-token-clear"` +
    `${q.hasBotToken ? '' : ' disabled'}>清除令牌（关闭查询）</button>` +
    `</div>` +
    `<div class="notice" style="margin-top:8px">把站点地址与令牌填进插件配置即可。` +
    `令牌<b>只显示一次</b>，重置后旧令牌立即失效。<br>` +
    `插件的「比赛召集」用消息链发 <b>At 组件</b>，是<b>真正的 @</b>；` +
    `而页面上「发送到群」受 AstrBot OpenAPI 限制只能把 @ 写进文本。</div>`
  );
}

/** 限流额度一行（服务器面板用；数据来自 /api/qqbot 的 limit）。 */
function limitLineHtml(limit = App.server?.qqbot?.limit) {
  if (!limit) return '';
  if (limit.cooldownLeft > 0) {
    return ` · 限流中：还需等 <b>${limit.cooldownLeft}</b> 秒`;
  }
  return ` · 本小时已推 <b>${limit.usedLastHour}</b>／${limit.maxPerHour} 次`;
}

/* ------------------------------ 推送到群 ------------------------------ */
const PUSH_KINDS = [
  ['event', '比赛信息', '名字 / 赛制 / 时间 / 人数 / 简介 / 是否排名'],
  ['live', '当前直播', '主直播间是否开播 + 正在推流的选手 / 成员机位（全局信息，不挑届次）'],
  ['progress', '赛程进度', '已赛多少、正在打谁 vs 谁'],
  ['call', '召集参赛', '@ 参与名单里的 QQ，请他们到场准备'],
  ['result', '比赛结果', '冠军 / 榜单 + 逐场比分'],
  ['detail', '单届详情', '信息 + 进度 + 结果（选定场次就细说那一场）'],
  ['list', '全部比赛列表', '所有届次；过长会自动分段，可分页'],
];

/**
 * 赛事管理页里的「推送到群」面板（赛事管理员 / 服务器管理员都能用）。
 *
 * 只做两件事：**预览**与**发送**；发送目标就是当前这一届。
 */
export function qqbotPushPanelHtml(s) {
  const rounds = (s.rounds || []).filter((r) => r.code).slice(0, 60);
  const body =
    `<div class="notice" id="qqbotStatus">正在检查群推送配置…</div>` +
    `<div class="form form--2" style="margin-top:10px">` +
    `<div class="field"><label for="qqbotKind">推送内容</label><select id="qqbotKind">` +
    PUSH_KINDS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('') +
    `</select><span class="field__hint" id="qqbotKindHint">${esc(PUSH_KINDS[0][2])}</span></div>` +
    `<div class="field"><label for="qqbotRef">指定场次（可选）</label><select id="qqbotRef">` +
    `<option value="">（不指定）</option>` +
    rounds
      .map((r) => `<option value="${esc(r.code)}">${esc(r.label || r.code)}</option>`)
      .join('') +
    `</select><span class="field__hint">选「单届详情」时用它细说某一场</span></div>` +
    `<div class="field"><label for="qqbotPage">列表页码</label>` +
    `<input id="qqbotPage" type="number" min="1" value="1">` +
    `<span class="field__hint">仅「全部比赛列表」用</span></div>` +
    `</div>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm" type="button" data-act="qqbot-preview">预览</button>` +
    `<button class="btn btn--sm btn--primary" type="button" data-act="qqbot-push">发送到群</button>` +
    `<span class="panel__hint" style="margin-left:auto">「预览」不会真的发送</span>` +
    `</div>` +
    `<textarea id="qqbotPreview" class="qqbot-preview" readonly rows="8" ` +
    `placeholder="点「预览」看看要发什么…"></textarea>`;
  return panelHtml('推送到群', '当前这一届 · QQ 机器人', body);
}

/** 拉一次推送状态并填进状态条（面板渲染后调用）。 */
export async function refreshQqbotStatusBox() {
  const box = qs('#qqbotStatus');
  if (!box) return;
  try {
    const st = await api('/qqbot/status', { auth: true });
    const quota = st.limit
      ? st.limit.cooldownLeft > 0
        ? ` · 限流中：还需等 <b>${st.limit.cooldownLeft}</b> 秒`
        : ` · 本小时已推 <b>${st.limit.usedLastHour}</b>／${st.limit.maxPerHour} 次`
      : '';
    if (st.ready) {
      box.className = 'notice';
      box.innerHTML =
        `已就绪 · 目标会话 <code>${esc(st.umo)}</code> · @ 方式 <b>${esc(st.atMode || 'cq')}</b>` +
        quota;
      return;
    }
    const missing = [
      st.enabled ? '' : '未启用（到「服务器 → QQ 机器人」打开开关）',
      st.hasKey ? '' : '缺少 AstrBot API Key',
      st.umo ? '' : '缺少目标群',
    ].filter(Boolean);
    box.className = 'notice notice--warn';
    box.innerHTML = `还不能发送：${esc(missing.join(' · '))}。仅服务器管理员可改这些设置。`;
  } catch (err) {
    box.className = 'notice notice--warn';
    box.textContent = `推送状态获取失败：${err.message}`;
  }
}

function qqbotPushQuery() {
  return {
    kind: qs('#qqbotKind')?.value || 'event',
    ref: qs('#qqbotRef')?.value || '',
    page: Number(qs('#qqbotPage')?.value) || 1,
  };
}

/** QQ 机器人（AstrBot）推送设置：仅服务器管理员。 */
function qqbotPanelHtml() {
  const q = App.server?.qqbot?.settings || {};
  const body =
    `<div class="notice">把<b>比赛信息 / 赛程进度 / 召集参赛 / 比赛结果 / 比赛列表</b>推到 QQ 群。` +
    `对接的是 <b>AstrBot OpenAPI</b>：API Key 在 AstrBot 的「设置 → OpenAPI」里创建（形如 <code>abk_xxx</code>）。` +
    `Key 只存服务端，接口不回显、也不进「导出配置」。</div>` +
    `<form class="form form--2" data-form="qqbot" style="margin-top:10px">` +
    fieldSwitch('enabled', '启用群推送', q.enabled === true) +
    // 赛前提醒：站点侧定时巡检（app/remind.py），到点在群里 @ 举办者
    fieldSwitch('remindEnabled', '赛前提醒（开赛前 @ 举办者）', q.remindEnabled !== false) +
    fieldText('remindLeads', '提前量（分钟，逗号分隔）', q.remindLeads || '1440,120', {
      hint: '默认「前一天 + 前 2 小时」；只在【提前量 − 1 小时, 提前量】窗口内发，避免服务重启后把「明天开赛」补发成错话',
    }) +
    // 出厂**不预填**任何地址：别人的 AstrBot 地址留在这里，新装的人会「看着配好了、
    // 其实把消息推给了别人」，与直播地址同一个坑。
    fieldText('baseUrl', 'AstrBot 地址', q.baseUrl || '', {
      ph: 'https://你的-AstrBot-地址',
      hint: '不带尾部斜杠，例如 https://bot.example.com',
    }) +
    fieldText('apiKey', 'AstrBot API Key', '', {
      type: 'password',
      ph: q.hasKey ? '已配置（留空 = 不修改）' : 'abk_xxx',
      hint: '留空表示保持已保存的 Key 不变',
    }) +
    fieldText('umo', '目标群', q.umoRaw || '', {
      ph: '123456789 或 aiocqhttp:GroupMessage:123456789',
      hint: '只填群号时会按下面的平台拼成完整 UMO',
    }) +
    fieldText('platform', '平台适配器', q.platform || 'aiocqhttp', {
      hint: '只填群号时用它拼 UMO；QQ（OneBot v11 / NapCat）用 aiocqhttp',
    }) +
    fieldSelect(
      'atMode',
      '@ 的方式',
      q.atMode || 'cq',
      [
        ['cq', 'CQ 码 [CQ:at,qq=…]（OneBot v11 推荐）'],
        ['text', '纯文本 @QQ号'],
        ['none', '不 @，只列名字'],
      ],
      { hint: 'AstrBot 的 OpenAPI 没有 at 消息段，所以 @ 写在文本里；点「发送测试消息」一试就知道哪种生效' }
    ) +
    fieldNum('maxChars', '单条上限（字符）', q.maxChars ?? 1200, { hint: '超出自动分段发送' }) +
    fieldNum('timeout', '请求超时（秒）', q.timeout ?? 10) +
    fieldNum('cooldownSeconds', '两次推送的最小间隔（秒）', q.cooldownSeconds ?? 20, {
      hint: '防止手滑连点；0 = 不限制间隔',
    }) +
    fieldNum('maxPerHour', '每小时最多推送（次）', q.maxPerHour ?? 30, {
      hint: '按自然小时计；防长时间高频刷群',
    }) +
    fieldNum('maxParts', '单次最多分段（段）', q.maxParts ?? 8, {
      hint: '一次推送最多切几段；超出会被拒绝（改用分页更合适）',
    }) +
    `<div class="form-actions" style="grid-column:1/-1">` +
    `<button class="btn btn--primary" type="submit">保存推送设置</button>` +
    `<button class="btn" type="button" data-act="qqbot-test">发送测试消息</button></div>` +
    `</form>` +
    `<div class="notice" style="margin-top:10px">当前目标会话：<code>${esc(q.umo || '未设置')}</code>` +
    (q.hasKey ? ' · API Key 已配置' : ' · <b>未配置 API Key</b>') +
    (q.enabled ? '' : ' · <b>未启用</b>') +
    limitLineHtml() +
    `</div>` +
    `<div class="notice" style="margin-top:8px"><b>限流</b>：预览不占额度；「发送到群」与「发送测试消息」都占。` +
    `一次推送无论切成几段只算 <b>1 次</b>，分段之间另各停 0.5 秒。</div>` +
    `<div class="notice" style="margin-top:8px">所有出站消息都会<b>纯文本化</b>：QQ 不认 Markdown，` +
    `星号 / 井号 / 表格竖线在发送前就被收拾干净。</div>` +
    botTokenBlockHtml(q);
  return panelHtml('QQ 机器人', 'AstrBot 群推送 · 服务器级', body);
}

function serverConfigPanelHtml() {
  const cfg = App.server?.config || {};
  const body =
    `<form class="form" data-form="server-html">` +
    fieldArea('customHtml', '自定义 HTML（用于接入统计 / 数据采集）', cfg.customHtml || '', {
      hint: '原样注入到页面（会执行其中的 <script>），只在受信任时填写；留空即关闭。',
    }) +
    `<div class="form-actions"><button class="btn btn--primary" type="submit">保存自定义 HTML</button></div>` +
    `</form>`;
  return panelHtml('自定义 HTML', '服务器级 · 全局注入', body);
}

/** 登录失败限制（类 fail2ban）：配置 + 当前封禁列表。 */
function loginGuardPanelHtml() {
  const g = App.server?.guard;
  if (!g) return '';
  const s = g.settings || {};
  const bans = (g.status || {}).bans || [];
  const failing = Object.entries((g.status || {}).failing || {});
  const proxyHint = g.proxyConfigured
    ? ''
    : `<br><b>未配置可信反向代理</b>：只能拿到直连 IP。若站点前面有反向代理 / CDN（如 EdgeOne），` +
      `请把代理的 IP / CIDR 填进「可信反向代理」，否则所有请求会被当成同一个 IP（一处失败会误伤所有人）。`;
  const body =
    `<div class="notice">同一客户端 IP 在时间窗内登录失败达到阈值即<b>临时封禁</b>（类 fail2ban），` +
    `期间登录回 429 并带 Retry-After。${proxyHint}` +
    `<br>本次请求解析到的客户端 IP：<code>${esc(g.clientIp || '—')}</code></div>` +
    `<form class="form form--2" data-form="login-guard" style="margin-top:10px">` +
    fieldSwitch('enabled', '启用登录失败限制', s.enabled !== false) +
    fieldNum('maxAttempts', '失败次数阈值', s.maxAttempts ?? 5, {
      hint: '统计时间窗内失败达到该次数即封禁',
    }) +
    fieldNum('windowSeconds', '统计时间窗（秒）', s.windowSeconds ?? 300) +
    fieldNum('banSeconds', '封禁时长（秒，0 = 只计数不封禁）', s.banSeconds ?? 900) +
    `<div style="grid-column:1/-1">${fieldText('trustedProxies', '可信反向代理 IP / CIDR', s.trustedProxies || '', {
      hint: '逗号分隔，如 10.0.0.0/8, 172.16.0.0/12；留空 = 不信任任何转发头（最安全）',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldText('whitelist', '永不封禁的 IP / CIDR', s.whitelist || '', {
      hint: '逗号分隔，例如你自己固定的出口 IP',
    })}</div>` +
    `<div style="grid-column:1/-1">${fieldSwitch(
      'localTrust',
      '本机（localhost / 127.0.0.1）访问不设防',
      s.localTrust !== false,
      { hint: '回环地址直连时免登录，也不受限流与封禁；经反向代理进来的不算本机' }
    )}</div>` +
    `<div class="form-actions" style="grid-column:1/-1">` +
    `<button class="btn btn--primary" type="submit">保存登录限制</button></div></form>` +
    `<div class="tool-group" style="margin-top:12px">` +
    `<span class="panel__hint">当前封禁 ${bans.length} 个 IP</span>` +
    (bans.length
      ? `<button class="btn btn--sm btn--danger" type="button" data-act="login-guard-clear">解除全部封禁</button>`
      : '') +
    `</div>` +
    (bans.length
      ? bans
          .map(
            (b) =>
              `<div class="url-row"><span class="url-row__value">${esc(b.ip)}</span>` +
              `<span class="url-row__label">剩余约 ${Math.max(1, Math.ceil(b.remaining / 60))} 分钟</span>` +
              `<button class="btn btn--sm" type="button" data-act="login-guard-unban" ` +
              `data-ip="${esc(b.ip)}">解除</button></div>`
          )
          .join('')
      : `<div class="panel__hint" style="margin-top:8px">当前没有被封禁的 IP。</div>`) +
    (failing.length
      ? `<div class="panel__hint" style="margin-top:8px">失败中：${esc(
          failing.map(([ip, n]) => `${ip}(${n})`).join('、')
        )}</div>`
      : '');
  return panelHtml('登录限制', '类 fail2ban · 服务器级', body);
}

function serverStatusPanelHtml() {
  const cfg = App.server?.config;
  if (!cfg) return '';
  const kv = [
    ['站点名称', esc(App.state?.siteName || '—')],
    ['服务器管理员', esc(App.me?.name || '—')],
    ['成员总数', cfg.members ?? 0],
    ['赛事管理员', cfg.admins ?? 0],
    ['届次总数', (App.events || []).length],
    ['直播封禁', cfg.bans ?? 0],
  ];
  const body =
    `<dl class="kv">` +
    kv.map(([k, v]) => `<div class="kv__row"><dt>${esc(k)}</dt><dd>${v}</dd></div>`).join('') +
    `</dl>`;
  return panelHtml('服务器状态', '实时', body);
}

export function renderServerPage() {
  const host = qs('#serverBody');
  if (!host) return;
  if (!App.token) {
    host.innerHTML = serverGateHtml();
    return;
  }
  if (!isServerAdmin()) {
    host.innerHTML = panelHtml(
      '服务器管理',
      `当前权限：${PERMISSION_LABEL[myPermission()] || myPermission() || '未登录'}`,
      `<div class="notice notice--warn">服务器管理仅限<b>服务器管理员</b>。` +
        `你当前是「${esc(PERMISSION_LABEL[myPermission()] || myPermission())}」，` +
        `可前往赛事管理页管理自己创建的赛事，或在「我的」页修改个人资料。</div>`
    );
    return;
  }
  if (!App.server) {
    const loading = panelHtml('服务器管理', '加载中', `<div class="empty"><b>正在加载</b>请稍候</div>`);
    if (App.serverLoading) {
      host.innerHTML = loading;
      return;
    }
    if (!App.serverTried) {
      // 首次进本页拉一次；失败后不再自动重试（否则网络/鉴权异常时会无限重拉）
      App.serverTried = true;
      App.serverLoading = true;
      host.innerHTML = loading;
      void refreshServerData({ silent: true }).finally(() => {
        App.serverLoading = false;
        if (App.view === 'server') renderServerPage();
      });
      return;
    }
    host.innerHTML = panelHtml(
      '服务器管理',
      '加载失败',
      `<div class="notice notice--warn">服务器数据加载失败（可能是网络或登录状态问题）。</div>` +
        `<div class="tool-group" style="margin-top:10px">` +
        `<button class="btn btn--sm btn--primary" type="button" data-act="server-refresh">重试</button></div>`
    );
    return;
  }
  // 届次列表为空时补拉一次（同样只试一次，避免 0 届时反复重绘）
  if (!(App.events || []).length && !App.eventsTried) {
    App.eventsTried = true;
    void loadEvents(true).finally(() => {
      if (App.view === 'server') renderServerPage();
    });
  }
  host.innerHTML =
    serverStatusPanelHtml() +
    sitePanelHtml() +
    membersPanelHtml() +
    eventsPanelHtml() +
    backupPanelHtml() +
    qqbotPanelHtml() +
    loginGuardPanelHtml() +
    serverConfigPanelHtml() +
    // 服务器信息（一篇说明）与服务器通知（一条条要人马上看到的）放一起：
    // 都是站点级 Markdown，编辑与渲染在 notices.js / mdeditor.js 里共用一套
    `<div class="panel" id="serverInfoBox"></div>` +
    `<div class="panel" id="serverNotices"></div>` +
    activityPanelHtml();
  renderMemberGrid();
  // 这两块要请求接口，异步填（不拖慢整页渲染）
  renderServerInfo(qs('#serverInfoBox'), { title: '服务器信息', manage: true });
  renderNoticeBoard('server', qs('#serverNotices'), {
    manage: true,
    hint: 'Markdown · 打开任何页面都弹',
  });
}

/* ------------------------------ 最近操作 ------------------------------ */

/** 写接口 → 人话（认不出来就退回原路径，至少能看出动过哪一块）。 */
const ACTIVITY_PATH_LABEL = [
  [/^\/api\/config$/, '保存配置'],
  [/^\/api\/events$/, '新建届次'],
  [/^\/api\/events\/[^/]+\/switch$/, '切换届次'],
  [/^\/api\/events\/[^/]+$/, '修改 / 删除届次'],
  [/^\/api\/format$/, '切换赛制'],
  [/^\/api\/event\/(start|unlock)$/, '开赛 / 解锁'],
  [/^\/api\/reload$/, '重载数据'],
  [/^\/api\/players/, '选手改动'],
  [/^\/api\/participants$/, '保存参赛名单'],
  [/^\/api\/teams/, '队伍改动'],
  [/^\/api\/tournament/, '淘汰赛操作'],
  [/^\/api\/schedule/, '生成 / 追加赛程'],
  [/^\/api\/rounds\/[^/]+\/result$/, '录入比分'],
  [/^\/api\/rounds\/[^/]+\/walkover$/, '判罚弃权'],
  [/^\/api\/rounds/, '赛程改动'],
  [/^\/api\/members/, '成员改动'],
  [/^\/api\/me(\/rotate)?$/, '我的资料'],
  [/^\/api\/live\/bans/, '直播封禁'],
  [/^\/api\/backups/, '备份操作'],
  [/^\/api\/qqbot/, 'QQ 机器人'],
  [/^\/api\/server\//, '服务器设置'],
  [/^\/api\/site\/name$/, '站点名称'],
  [/^\/api\/channels/, '频道改动'],
  [/^\/api\/avatar\/upload$/, '上传头像'],
];

const activityLabel = (path) => {
  const hit = ACTIVITY_PATH_LABEL.find(([re]) => re.test(path));
  return hit ? hit[1] : path;
};

/** 操作日志面板：谁、什么时候、动了哪一块、成没成。 */
function activityPanelHtml() {
  const items = App.server?.activity || [];
  if (!items.length) {
    return panelHtml(
      '最近操作',
      '本机',
      `<div class="empty"><b>还没有记录</b>有权限的人每次改动都会在这里留一条</div>`
    );
  }
  const rows = items
    .map((it) => {
      const status = Number(it.status) || 0;
      const bad = status >= 400;
      // 时间戳是「2026-10-04 11:21:50」：截到分钟就够读了
      const ts = String(it.ts || '').slice(5, 16);
      return (
        `<div class="act__row${bad ? ' act__row--bad' : ''}">` +
        `<span class="act__ts">${esc(ts)}</span>` +
        `<span class="act__who">${esc(it.actor || '未登录')}</span>` +
        `<span class="act__what">${esc(activityLabel(String(it.path || '')))}</span>` +
        `<span class="act__code">${bad ? status : '✓'}</span>` +
        `<span class="act__path" title="${esc(it.path || '')}">${esc(it.method || '')} ${esc(it.path || '')}</span>` +
        `</div>`
      );
    })
    .join('');
  return panelHtml(
    '最近操作',
    `${items.length} 条 · 只记接口不记内容`,
    `<div class="act">${rows}</div>` +
      `<p class="act__foot">由服务端中间件记录：只保存「谁 / 何时 / 动了哪个接口 / 结果」，` +
      `不保存提交内容（里面可能有密钥）。只保留最近 600 条。</p>`
  );
}

/* ------------------------------ /user 页 ------------------------------ */
const userGateHtml = () =>
  `<div class="panel"><div class="gate">` +
  `<div class="gate__title">我的</div>` +
  `<p class="gate__desc">用你的<b>成员密钥</b>登录后，可修改头像 / 名字 / 游戏 UUID / 推流 ID / 直播间名字，` +
  `并轮换自己的密钥与 Bearer 令牌。</p>` +
  `<div class="field"><label for="userKey">成员密钥</label>` +
  `<input id="userKey" type="password" autocomplete="current-password" placeholder="请输入成员密钥"></div>` +
  `<button class="btn btn--primary btn--block" type="button" data-act="admin-login">登录</button>` +
  `<p class="gate__hint">没有密钥？请联系服务器管理员在成员管理里为您创建。</p>` +
  `</div></div>`;

function banBanner(ban) {
  if (!ban) return '';
  return (
    `<div class="notice notice--warn">` +
    `<b>直播封禁中</b>：${esc(ban.reason || '未填写原因')} · ` +
    `${ban.until ? `解禁时间 ${esc(fmtFull(ban.until))}` : '永久'} · ` +
    `范围 ${esc(BAN_SCOPE_LABEL[ban.scope] || ban.scope)}。` +
    `封禁期间无法推流；解除封禁请联系服务器管理员。</div>`
  );
}

function meProfilePanel(m) {
  const body =
    `<form class="form form--2" data-form="me">` +
    fieldText('name', '名字', m.name, { hint: '公开展示的名字' }) +
    fieldText('gameUuid', '游戏 UUID', m.gameUuid, { hint: '游戏内 ID；不会下发给访客' }) +
    fieldText('streamId', '推流 ID（全局唯一）', m.streamId, {
      hint:
        (m.streamId
          ? `OBS 推流地址：${esc(pushAddress(m, m.play || {}) || '（站点还没填媒体服务器地址）')}；` +
            `Bearer 令牌填到 OBS 的「Bearer 令牌」字段。`
          : '设置后即可用「推流 ID + Bearer 令牌」开播。') +
        `${PUSH_TIP_LINE}。推流 ID 只能用字母、数字、连字符(-)与下划线(_)。`,
    }) +
    fieldText('qq', 'QQ（可选）', m.qq || '', { hint: '仅服务端用于取头像' }) +
    `<div class="field" style="grid-column:1/-1"><label>头像</label>` +
    `<div class="ava-edit" data-avatar-scope>` +
    `<span class="ava ava--md${m.avatar ? '' : ' ava--placeholder'}" data-role="avatar-preview">` +
    (m.avatar
      ? `<img src="${esc(m.avatar)}" alt=""><span class="ava__ring"></span>`
      : esc(String(m.name || '?').slice(0, 1))) +
    `</span>` +
    `<div class="ava-edit__col">` +
    `<input type="file" accept="image/*" data-role="avatar-file">` +
    `<input name="avatar" type="text" value="${esc(m.avatar || '')}" placeholder="或直接填写图片 URL">` +
    `</div></div></div>` +
    `<div style="grid-column:1/-1">${fieldArea('note', '备注', m.note || '')}</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存资料</button></div>` +
    `</form>`;
  return panelHtml('我的资料', `uid ${m.uid}`, banBanner(m.banned) + body);
}

/**
 * 推流地址（完整）。
 *
 * 优先用后端在「本人视角」里下发的完整地址；拿不到就从观看地址反推
 * （``<base>/<流名>`` → ``<base>/<流名>/whip``）；都没有就返回空串。
 * **界面上不留 `<WebRTC 根地址>` 这种占位符**——地址本来就在配置里。
 */
function pushAddress(m, play) {
  if (m.push?.whipPush) return m.push.whipPush;
  const suffix = m.streamId ? `/${m.streamId}` : '';
  if (play.webrtc && suffix && play.webrtc.endsWith(suffix)) {
    return `${play.webrtc.slice(0, -suffix.length)}${suffix}/whip`;
  }
  return '';
}

function meRoomPanel(m) {
  const play = m.play || {};
  const pushUrl = pushAddress(m, play);
  const body =
    `<form class="form form--2" data-form="me-room">` +
    fieldText('roomTitle', '直播间名字', m.roomTitle, {
      hint: '开播后展示在频道里的标题（可随时改）',
    }) +
    `<div style="grid-column:1/-1" class="notice">` +
    (m.streamId
      ? `OBS → 推流：服务选 <b>WHIP</b>，服务器填 ` +
        `<code>${esc(pushUrl || '（站点还没填媒体服务器地址）')}</code>，` +
        `Bearer 令牌填到 OBS 的「Bearer 令牌」字段。` +
        (play.webrtc ? `<br>观看（WebRTC）：<code>${esc(play.webrtc)}</code>` : '') +
        (play.hls ? `<br>观看（HLS）：<code>${esc(play.hls)}</code>` : '') +
        `<br><b>推流建议</b>：${PUSH_TIP_LINE}。` +
        `<br>令牌是<b>每人一把</b>、且只能推你自己的推流 ID：拿别人的令牌推不动，` +
        `没带令牌会被媒体服务器直接拒绝。` +
        (pushUrl
          ? ''
          : `<br><b>注意</b>：站点还没填媒体服务器地址（「服务器 → 直播配置」），` +
            `所以这里给不出完整推流地址。`)
      : '尚未设置推流 ID；设置后直播间会出现在「频道」里。') +
    `</div>` +
    `<div class="form-actions" style="grid-column:1/-1"><button class="btn btn--primary" type="submit">保存直播间名字</button></div>` +
    `</form>`;
  return panelHtml(
    '我的直播间',
    isMemberLive(m.uid) ? '直播中' : '未开播',
    body
  );
}

function meCredentialPanel(m) {
  const body =
    `<div class="notice">密钥与 Bearer 令牌<b>只由服务器随机生成</b>，生成后仅显示<b>一次</b>，` +
    `之后以密文保存、无法再查看，只能轮换；轮换后旧值立即失效。</div>` +
    `<dl class="kv" style="margin-top:10px">` +
    `<div class="kv__row"><dt>登录密钥</dt><dd>${m.hasKey ? '已配置（不可查看）' : '未配置'}</dd></div>` +
    `<div class="kv__row"><dt>Bearer 令牌</dt><dd>${m.hasBearer ? '已配置（不可查看）' : '未配置'}</dd></div>` +
    `</dl>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--danger" type="button" data-act="me-rotate" data-what="key">轮换登录密钥</button>` +
    `<button class="btn btn--sm" type="button" data-act="me-rotate" data-what="bearer">轮换 Bearer 令牌</button>` +
    `</div>` +
    `<div class="notice" style="margin-top:10px">轮换密钥会<b>立即注销本人登录</b>，需用新密钥重新登录；` +
    `轮换令牌会<b>立即让旧令牌失效</b>（正在推流需用新令牌重推）。</div>`;
  return panelHtml('凭据管理', '密钥 / Bearer 令牌', body);
}

/** 账户：当前身份与注销（注销入口放在 /user 页）。 */
function meAccountPanel() {
  const body =
    `<div class="notice">当前身份：<b>${esc(
      PERMISSION_LABEL[myPermission()] || myPermission() || '未登录'
    )}</b>${App.me?.uid ? ` · 网站用户 UUID <code>${esc(App.me.uid)}</code>` : ''}</div>` +
    `<div class="tool-group" style="margin-top:10px">` +
    `<button class="btn btn--sm btn--danger" type="button" data-act="logout">退出登录</button>` +
    `</div>` +
    `<div class="notice" style="margin-top:10px">退出后需重新输入密钥才能继续编辑资料。</div>`;
  return panelHtml('账户', '登出', body);
}

export function renderUserPage() {
  const host = qs('#userBody');
  if (!host) return;
  if (!App.token) {
    host.innerHTML = userGateHtml();
    return;
  }
  const m = App.me?.member;
  if (!m) {
    // 正常不会走到这里：登录（含本机免登录）都会绑到某位成员上
    host.innerHTML =
      panelHtml(
        '我的',
        App.me?.name || '服务器管理员',
        `<div class="notice">当前会话没有绑定成员资料。` +
          `正常情况（成员密钥登录、本机免登录）都会绑定到那位成员；` +
          `若看到这条提示，说明服务器上还没有管理员成员，请到「服务器 → 成员管理」创建一个。</div>`
      ) + meAccountPanel();
    return;
  }
  host.innerHTML = meProfilePanel(m) + meRoomPanel(m) + meCredentialPanel(m) + meAccountPanel();
}

/* ------------------------------ 弹窗 ------------------------------ */
/** 展示「仅此一次」的密钥 / 令牌明文。 */
function showSecretModal(secretKey, bearerToken, title = '请立即保存') {
  const row = (label, value) =>
    value
      ? `<div class="field"><label>${esc(label)}</label>` +
        `<div class="tool-group"><input type="text" readonly value="${esc(value)}" style="flex:1">` +
        `<button class="btn btn--sm" type="button" data-copy="${esc(value)}">复制</button></div></div>`
      : '';
  Modal.open({
    title,
    body:
      `<div class="notice notice--warn"><b>这些内容只会显示这一次</b>，离开后不再可见，` +
      `请立即复制保存。</div>` +
      row('登录密钥', secretKey) +
      row('Bearer 令牌', bearerToken),
    footer: `<button class="btn btn--sm btn--primary" type="button" data-close>我已保存</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      bodyEl.querySelectorAll('[data-copy]').forEach((b) => {
        b.onclick = () => copyText(b.dataset.copy).then((ok) => toast(ok ? '已复制' : '复制失败', ok ? 'ok' : 'err'));
      });
    },
  });
}

function openMemberModal(member) {
  const isNew = !member;
  const m = member || {};
  Modal.open({
    title: isNew ? '新增成员' : `编辑成员 · ${m.name || m.uid}`,
    body:
      `<div class="form form--2">` +
      fieldText('name', '名字', m.name || '') +
      fieldText('gameUuid', '游戏 UUID', m.gameUuid || '') +
      fieldText('streamId', '推流 ID（全局唯一）', m.streamId || '', {
        hint: duplicateOf(m)
          ? `该推流 ID 与 ${duplicateOf(m).join('、')} 重复，会串流，请改成唯一值`
          : '成员用「推流 ID + Bearer 令牌」推流；留空则不能推流。只能用字母、数字、- 与 _',
      }) +
      fieldText('roomTitle', '直播间名字（可选）', m.roomTitle || '') +
      fieldText('qq', 'QQ（可选）', m.qq || '', { hint: '仅服务端用于取头像' }) +
      fieldSelect(
        'permission',
        '权限',
        m.permission || 'member',
        [
          ['member', '成员'],
          ['event_admin', '赛事管理员'],
          ['server_admin', '服务器管理员（有且只有一个）'],
        ],
        { hint: '赛事管理员可创建并管理自己的赛事；服务器管理员拥有全部权限' }
      ) +
      `<div class="field" style="grid-column:1/-1"><label>头像</label>` +
      `<div class="ava-edit" data-avatar-scope>` +
      `<span class="ava ava--md${m.avatar ? '' : ' ava--placeholder'}" data-role="avatar-preview">` +
      (m.avatar
        ? `<img src="${esc(m.avatar)}" alt=""><span class="ava__ring"></span>`
        : esc(String(m.name || '?').slice(0, 1))) +
      `</span>` +
      `<div class="ava-edit__col">` +
      `<input type="file" accept="image/*" data-role="avatar-file">` +
      `<input name="avatar" type="text" value="${esc(m.avatar || '')}" placeholder="或直接填写图片 URL">` +
      `</div></div></div>` +
      `<div style="grid-column:1/-1">${fieldArea('note', '备注', m.note || '')}</div>` +
      `<div class="field field--switch"><span class="switch">` +
      `<input id="f-active" name="active" type="checkbox"${m.active !== false ? ' checked' : ''}><i></i></span>` +
      `<label for="f-active">启用</label></div>` +
      (isNew
        ? `<div class="notice" style="grid-column:1/-1">保存后会自动生成随机的<b>登录密钥</b>与 ` +
          `<b>Bearer 令牌</b>，并只显示一次。</div>`
        : '') +
      `</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--primary" type="button" data-submit>保存</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const data = collectForm(bodyEl);
        if (!String(data.name || '').trim()) return toast('请填写名字', 'warn');
        if (!isNew) data.uid = m.uid;
        try {
          const res = await api('/members', { method: 'POST', auth: true, body: data });
          Modal.close();
          toast('成员已保存', 'ok');
          await refreshServerData();
          renderServerPage();
          if (res.secretKey || res.bearerToken) {
            showSecretModal(res.secretKey, res.bearerToken, `成员「${data.name}」的凭据`);
          }
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

function openBanModal(member) {
  Modal.open({
    title: `掐断 / 封禁 · ${member.name || member.uid}`,
    body:
      `<div class="form form--2">` +
      fieldSelect(
        'duration',
        '处置方式',
        'kick',
        [
          ['kick', '仅掐断（不封禁）'],
          ['minutes', '封禁一段时间'],
          ['permanent', '永久封禁'],
        ],
        { hint: '掐断会尝试立即踢掉正在推流的会话；封禁期间该令牌无法再推流' }
      ) +
      fieldNum('minutes', '封禁时长（分钟）', 60, { hint: '仅在「封禁一段时间」时生效' }) +
      `<div style="grid-column:1/-1">${fieldArea('reason', '理由', '', {
        hint: '会展示在该成员的直播间与成员信息上',
      })}</div>` +
      `</div>`,
    footer:
      `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
      `<button class="btn btn--sm btn--danger" type="button" data-submit>执行</button>`,
    onMount(bodyEl, footEl) {
      footEl.querySelector('[data-close]').onclick = () => Modal.close();
      footEl.querySelector('[data-submit]').onclick = async () => {
        const v = collectForm(bodyEl);
        const body = {
          memberUid: member.uid,
          reason: v.reason || '',
          kick: true,
          permanent: v.duration === 'permanent',
          minutes: v.duration === 'minutes' ? Number(v.minutes) || 0 : 0,
        };
        try {
          const res = await api('/live/bans', { method: 'POST', auth: true, body });
          const kick = res.kick || {};
          Modal.close();
          toast(
            res.banned
              ? `已封禁${kick.ok ? '并掐断' : ''}（${kick.ok ? `踢掉 ${kick.kicked} 路` : kick.reason || '未在推流'}）`
              : `已请求掐断${kick.ok ? '' : `：${kick.reason || '未在推流'}`}`,
            res.banned ? 'warn' : 'info',
            7000
          );
          await refreshLiveHealth().catch(() => {});
          if (isServerAdmin()) {
            await refreshServerData();
            renderServerPage();
          } else {
            hooks.refreshState?.();
          }
        } catch (err) {
          toast(err.message, 'err', 8000);
        }
      };
    },
  });
}

/* ------------------------------ 动作 ------------------------------ */
async function doMemberRotate(uid, what) {
  if (!window.confirm(`确认轮换该成员的${what === 'key' ? '登录密钥' : ' Bearer 令牌'}？旧值将立即失效。`)) return;
  try {
    const res = await api(`/members/${encodeURIComponent(uid)}/rotate`, {
      method: 'POST',
      auth: true,
      body: { key: what === 'key', bearer: what === 'bearer' },
    });
    toast('已轮换', 'ok');
    showSecretModal(res.secretKey, res.bearerToken, '新的凭据（仅此一次）');
    await refreshServerData();
    renderServerPage();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function doMeRotate(what) {
  if (!window.confirm(`确认轮换你的${what === 'key' ? '登录密钥（会立即退出登录）' : ' Bearer 令牌'}？`)) return;
  try {
    const res = await api('/me/rotate', {
      method: 'POST',
      auth: true,
      body: { key: what === 'key', bearer: what === 'bearer' },
    });
    showSecretModal(res.secretKey, res.bearerToken, '新的凭据（仅此一次）');
    if (res.reauth) {
      toast('密钥已轮换，请用新密钥重新登录', 'warn', 6000);
      App.token = '';
      localStorage.removeItem(TOKEN_KEY);
      App.me = null;
      hooks.onAuthLost?.();
      renderUserPage();
    } else {
      toast('令牌已轮换', 'ok');
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

/** 在成员列表里找一位成员：服务器管理数据优先，其次公开状态（赛事管理员用）。 */
function findMember(uid) {
  if (!uid) return null;
  return (
    (App.server?.members || []).find((m) => m.uid === uid) ||
    (App.state?.members || []).find((m) => m.uid === uid) ||
    null
  );
}

/** 处理成员 / 服务器相关动作；返回是否已处理（未处理则交回 actions.js）。 */
export async function handleMemberAction(act, el) {
  switch (act) {
    case 'server-refresh':
      App.serverTried = false;
      App.eventsTried = false;
      await refreshServerData();
      renderServerPage();
      return true;
    // 成员分页（只在成员超过 4 行时才画得出这两个按钮）
    case 'member-page':
      App.memberPage = Math.max(1, Number(el.dataset.page) || 1);
      renderMemberGrid();
      return true;
    case 'backup-create': {
      try {
        await api('/backups', { method: 'POST', auth: true });
        toast('已创建备份', 'ok');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err', 8000);
      }
      return true;
    }
    case 'backup-upload':
      qs('#backupFile')?.click();
      return true;
    case 'backup-download':
      window.open(
        `${API}/backups/${encodeURIComponent(el.dataset.name || '')}/download?token=${encodeURIComponent(App.token)}`,
        '_blank',
        'noopener'
      );
      return true;
    case 'backup-restore': {
      const name = el.dataset.name || '';
      if (!window.confirm(restoreConfirmText(`「${name}」`))) return true;
      try {
        await api(`/backups/${encodeURIComponent(name)}/restore`, { method: 'POST', auth: true });
        afterRestore();
      } catch (err) {
        toast(err.message, 'err', 8000);
      }
      return true;
    }
    case 'backup-delete': {
      const name = el.dataset.name || '';
      if (!window.confirm(`确认删除备份「${name}」？`)) return true;
      try {
        await api(`/backups/${encodeURIComponent(name)}`, { method: 'DELETE', auth: true });
        toast('备份已删除', 'ok');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err');
      }
      return true;
    }
    case 'qqbot-token-new': {
      if (
        App.server?.qqbot?.settings?.hasBotToken &&
        !window.confirm('重置令牌会让旧令牌立即失效（插件需同步更新），继续？')
      ) {
        return true;
      }
      try {
        const res = await api('/qqbot/bot-token', { method: 'POST', auth: true });
        showSecretModal(res.token, '', '查询接口令牌（仅此一次）');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err', 8000);
      }
      return true;
    }
    case 'qqbot-token-clear': {
      if (!window.confirm('清除令牌后插件将无法查询，继续？')) return true;
      try {
        await api('/qqbot/bot-token', { method: 'DELETE', auth: true });
        toast('查询令牌已清除', 'ok');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err');
      }
      return true;
    }
    case 'qqbot-test': {
      try {
        const res = await api('/qqbot/test', { method: 'POST', auth: true, body: {} });
        toast(
          res.ok ? `测试消息已发送到 ${res.umo}` : `发送失败：${res.detail || '未知原因'}`,
          res.ok ? 'ok' : 'err',
          8000
        );
      } catch (err) {
        toast(err.message, 'err', 8000);
      }
      return true;
    }
    case 'qqbot-preview': {
      const q = qqbotPushQuery();
      try {
        const res = await api(`/qqbot/preview?${new URLSearchParams(q).toString()}`, { auth: true });
        const box = qs('#qqbotPreview');
        if (box) {
          box.value = res.parts
            .map((part, i) => (res.parts.length > 1 ? `— 第 ${i + 1} 段 —\n${part}` : part))
            .join('\n\n');
        }
        toast(
          `预览完成：${res.parts.length} 段` + (res.pages > 1 ? `（列表共 ${res.pages} 页）` : ''),
          'ok'
        );
      } catch (err) {
        toast(err.message, 'err', 7000);
      }
      return true;
    }
    case 'qqbot-push': {
      const q = qqbotPushQuery();
      if (!window.confirm('确认把这条消息发送到群里？')) return true;
      try {
        const res = await api('/qqbot/push', { method: 'POST', auth: true, body: q });
        toast(`已发送到群（${res.sent}/${res.total} 段）`, 'ok', 6000);
      } catch (err) {
        toast(err.message, 'err', 9000);
      }
      return true;
    }
    case 'member-add':
      openMemberModal(null);
      return true;
    case 'member-edit': {
      const m = findMember(el.dataset.uid);
      if (m) openMemberModal(m);
      return true;
    }
    case 'member-rotate':
      await doMemberRotate(el.dataset.uid, el.dataset.what || 'key');
      return true;
    case 'member-del': {
      const m = findMember(el.dataset.uid);
      if (!window.confirm(`确认删除成员「${m?.name || el.dataset.uid}」？其会话与凭据一并失效。`)) return true;
      try {
        await api(`/members/${encodeURIComponent(el.dataset.uid)}`, { method: 'DELETE', auth: true });
        toast('成员已删除', 'ok');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err');
      }
      return true;
    }
    case 'member-ban': {
      const m = findMember(el.dataset.uid);
      if (m) openBanModal(m);
      return true;
    }
    case 'me-rotate':
      await doMeRotate(el.dataset.what || 'key');
      return true;
    case 'login-guard-unban': {
      try {
        await api(`/server/login-guard/${encodeURIComponent(el.dataset.ip || '')}`, {
          method: 'DELETE',
          auth: true,
        });
        toast('已解除该 IP 的登录封禁', 'ok');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err');
      }
      return true;
    }
    case 'login-guard-clear': {
      if (!window.confirm('确认解除全部登录封禁？')) return true;
      try {
        await api('/server/login-guard', { method: 'DELETE', auth: true });
        toast('已解除全部登录封禁', 'ok');
        await refreshServerData();
        renderServerPage();
      } catch (err) {
        toast(err.message, 'err');
      }
      return true;
    }
    case 'event-hidden': {
      try {
        await api(`/events/${encodeURIComponent(el.dataset.id)}`, {
          method: 'PATCH',
          auth: true,
          body: { hidden: el.dataset.hidden === '1' },
        });
        toast(el.dataset.hidden === '1' ? '已隐藏该届' : '已取消隐藏', 'ok');
        await loadEvents(true);
        renderServerPage();
        hooks.refreshState?.();
      } catch (err) {
        toast(err.message, 'err');
      }
      return true;
    }
    default:
      return false;
  }
}

/** 处理成员 / 服务器相关表单；返回是否已处理。 */
export async function handleMemberForm(formEl) {
  const name = formEl.dataset.form;
  if (name === 'server-html') {
    const v = collectForm(formEl);
    try {
      await api('/server/config', { method: 'PUT', auth: true, body: { customHtml: v.customHtml } });
      toast('自定义 HTML 已保存', 'ok');
      await refreshServerData();
      renderServerPage();
    } catch (err) {
      toast(err.message, 'err');
    }
    return true;
  }
  if (name === 'site') {
    const v = collectForm(formEl);
    try {
      const res = await api('/site/name', { method: 'PUT', auth: true, body: { name: v.siteName } });
      toast(`站点名称已更新为「${res.siteName}」`, 'ok');
      // 服务端会广播一次状态，这里再主动同步一遍，保证顶栏与标签页立刻换名字
      await refreshServerData({ silent: true });
      if (hooks.refreshState) await hooks.refreshState();
      else renderServerPage();
    } catch (err) {
      toast(err.message, 'err');
    }
    return true;
  }
  if (name === 'login-guard') {
    const v = collectForm(formEl);
    try {
      await api('/server/login-guard', { method: 'PUT', auth: true, body: v });
      toast('登录限制已保存', 'ok');
      await refreshServerData();
      renderServerPage();
    } catch (err) {
      toast(err.message, 'err', 7000);
    }
    return true;
  }
  if (name === 'backup') {
    const v = collectForm(formEl);
    try {
      await api('/backups/settings', {
        method: 'PUT',
        auth: true,
        body: {
          enabled: v.enabled === true,
          intervalHours: Number(v.intervalHours) || 24,
          keep: Number(v.keep) || 7,
        },
      });
      toast('备份设置已保存', 'ok');
      await refreshServerData();
      renderServerPage();
    } catch (err) {
      toast(err.message, 'err');
    }
    return true;
  }
  if (name === 'qqbot') {
    const v = collectForm(formEl);
    try {
      await api('/qqbot', {
        method: 'PUT',
        auth: true,
        body: {
          enabled: v.enabled === true,
          baseUrl: v.baseUrl,
          apiKey: v.apiKey,
          umo: v.umo,
          platform: v.platform,
          atMode: v.atMode,
          maxChars: Number(v.maxChars) || 1200,
          timeout: Number(v.timeout) || 10,
          cooldownSeconds: Number(v.cooldownSeconds) || 0,
          maxPerHour: Number(v.maxPerHour) || 30,
          maxParts: Number(v.maxParts) || 8,
          // 赛前提醒：开关 + 提前量（分钟，逗号分隔）。服务端会再规整一次，
          // 这里原样传字符串即可（写错了不会存成坏配置）。
          remindEnabled: v.remindEnabled === true,
          remindLeads: v.remindLeads,
        },
      });
      toast('群推送设置已保存', 'ok');
      await refreshServerData();
      renderServerPage();
    } catch (err) {
      toast(err.message, 'err');
    }
    return true;
  }
  if (name === 'me') {
    const v = collectForm(formEl);
    try {
      await api('/me', { method: 'PUT', auth: true, body: v });
      toast('资料已保存', 'ok');
      await refreshMeData();
      renderUserPage();
      hooks.refreshState?.();
    } catch (err) {
      toast(err.message, 'err');
    }
    return true;
  }
  if (name === 'me-room') {
    const v = collectForm(formEl);
    const m = App.me?.member || {};
    try {
      await api('/me', {
        method: 'PUT',
        auth: true,
        body: { ...m, roomTitle: v.roomTitle }, // 只改标题，其余字段原样回传
      });
      toast('直播间名字已保存', 'ok');
      await refreshMeData();
      renderUserPage();
      hooks.refreshState?.();
    } catch (err) {
      toast(err.message, 'err');
    }
    return true;
  }
  return false;
}

/* ------------------------------ 备份：还原收尾 ------------------------------ */
/** 还原前的二次确认文案（上传与列表里的「还原」共用）。 */
function restoreConfirmText(what) {
  return (
    `确认用 ${what} 还原？\n\n` +
    '当前全部数据（届次 / 赛程 / 成员 / 头像…）都会被这份备份覆盖。\n' +
    '服务端会先自动打一份「还原前」的安全备份，万一还原错了还能倒回来。\n\n' +
    '还原完成后所有会话失效，需要重新登录。'
  );
}

/** 还原后的收尾：凭据可能已变，清掉本地会话并重载（本机访问会自动免登录回来）。 */
function afterRestore() {
  toast('已还原，正在重新登录…', 'ok', 6000);
  App.token = '';
  localStorage.removeItem(TOKEN_KEY);
  App.me = null;
  App.server = null;
  App.serverTried = false;
  setTimeout(() => location.reload(), 800);
}

/** 上传一份备份并还原（请求体就是原始 zip 字节）。 */
export async function uploadBackupFile(input) {
  const file = input.files && input.files[0];
  input.value = ''; // 立刻清空，允许连续选同一个文件
  if (!file) return;
  if (!window.confirm(restoreConfirmText(`「${file.name}」（${backupSizeText(file.size)}）`))) return;
  try {
    const res = await fetch(`${API}/backups/upload?name=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/zip', 'X-NTE-Token': App.token },
      body: file,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || `上传失败 (HTTP ${res.status})`);
    log.warn('已从上传的备份还原', data);
    afterRestore();
  } catch (err) {
    toast(err.message, 'err', 8000);
  }
}

/** 拉取 /api/me 并写入 App.me（含成员视图）。 */
export async function refreshMeData() {
  if (!App.token) {
    App.me = null;
    return null;
  }
  try {
    App.me = await api('/me', { auth: true });
    log.debug('身份已加载', App.me?.permission);
  } catch (err) {
    log.warn('/api/me 加载失败', err);
    App.me = null;
  }
  return App.me;
}

export { PERMISSION_LABEL, showSecretModal };
