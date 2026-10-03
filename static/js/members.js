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
  panelHtml,
  PUSH_TIP_LINE,
} from './ui.js';
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
    const [members, config, guard] = await Promise.all([
      api('/members', { auth: true }),
      api('/server/config', { auth: true }),
      api('/server/login-guard', { auth: true }),
    ]);
    App.server = {
      members: members.members || [],
      duplicates: members.duplicates || {},
      config,
      guard,
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
  `<div class="field"><label for="adminKey">登录密钥</label>` +
  `<input id="adminKey" type="password" autocomplete="current-password" placeholder="请输入服务器管理员密钥"></div>` +
  `<button class="btn btn--primary btn--block" type="button" data-act="admin-login">登录</button>` +
  `<p class="gate__hint">密钥在服务启动日志里（首次启动自动生成）；` +
  `服务器管理员有且只有一个，忘记可在成员管理里轮换。` +
  `<br>在本机（localhost / 127.0.0.1）直接访问时无需登录。</p>` +
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
  if (m.hasKey) tags.push('<span class="badge badge--pending" title="已配置登录密钥">密钥</span>');
  if (m.hasBearer) tags.push('<span class="badge badge--pending" title="已配置 Bearer 令牌">令牌</span>');

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

/** 只重绘成员网格（搜索时避免整页重建，保住输入焦点）。 */
export function renderMemberGrid() {
  const host = qs('#memberGrid');
  if (!host) return;
  const data = App.server?.members || [];
  const shown = data.filter((m) => memberSearchMatch(m) && memberFilterMatch(m));
  host.innerHTML = shown.length
    ? `<div class="roster-grid">${shown.map(memberCardHtml).join('')}</div>`
    : `<div class="empty"><b>没有匹配的成员</b>调整搜索或筛选条件</div>`;
  const count = qs('#memberCount');
  if (count) count.textContent = `${shown.length} / ${data.length} 人`;
}

function membersPanelHtml() {
  const dups = Object.entries(App.server?.duplicates || {});
  const dupNotice = dups.length
    ? `<div class="notice notice--warn" style="margin-bottom:10px">有 <b>${dups.length}</b> 个推流 ID 重复：` +
      `${esc(dups.slice(0, 3).map(([k]) => k).join('、'))}${dups.length > 3 ? ' 等' : ''}。` +
      `重复会串流，请改掉其中一个。</div>`
    : '';
  const body =
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
    (e.current
      ? `<span class="panel__hint">主赛事</span>`
      : `<button class="btn btn--sm btn--primary" type="button" data-act="event-switch" data-id="${esc(e.id)}">设为主赛事</button>`) +
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
    `<article class="evt-card evt-card--act${e.current ? ' evt-card--on' : ''}">` +
    `<div class="evt-card__head">` +
    `<button class="evt-card__name" type="button" data-act="event-view" data-id="${esc(e.id)}" data-page="overview">` +
    `${esc(e.name || e.id)}</button>` +
    statusBadge(e.status) +
    (e.hidden ? '<span class="badge badge--pending">已隐藏</span>' : '') +
    `<span class="panel__hint">${esc(e.ownerUid ? `归属 ${e.ownerUid.slice(0, 8)}` : '归属 服务器')}</span>` +
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
    `<span class="panel__hint">历届（含进行中与未来）：可关闭 / 隐藏 / 切换 / 重命名 / 删除</span>` +
    `</div>` +
    (events.length
      ? `<div class="evt-grid">${events.map(eventAdminCard).join('')}</div>`
      : `<div class="empty"><b>暂无赛事</b>点「新建一届」开始</div>`);
  return panelHtml('届次管理', '全部届次 · 服务器统一管理', body);
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

function serverKeyPanelHtml() {
  const body =
    `<form class="form form--2" data-form="admin-key">` +
    fieldText('key', '新的服务器主管理 KEY', '', {
      type: 'password',
      hint: '至少 6 位。保存后所有管理会话立即失效，需用新 KEY 重新登录。',
    }) +
    fieldSwitch('storeHash', '只保存 sha256 哈希（推荐，文件里不留明文）', true) +
    `<div class="form-actions" style="grid-column:1/-1">` +
    `<button class="btn btn--primary" type="submit">更新主管理 KEY</button></div></form>`;
  return panelHtml('管理 KEY', '服务器主密钥（与成员密钥独立）', body);
}

function serverStatusPanelHtml() {
  const cfg = App.server?.config;
  if (!cfg) return '';
  const kv = [
    ['服务器管理员', esc(App.me?.name || '—')],
    ['成员总数', cfg.members ?? 0],
    ['赛事管理员', cfg.admins ?? 0],
    ['直播封禁', cfg.bans ?? 0],
    ['当前主赛事', `${esc(cfg.eventName || '—')}（${esc(cfg.eventId || '—')}）`],
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
    membersPanelHtml() +
    eventsPanelHtml() +
    loginGuardPanelHtml() +
    serverConfigPanelHtml() +
    serverKeyPanelHtml();
  renderMemberGrid();
}

/* ------------------------------ /user 页 ------------------------------ */
const userGateHtml = () =>
  `<div class="panel"><div class="gate">` +
  `<div class="gate__title">我的</div>` +
  `<p class="gate__desc">用你的<b>成员密钥</b>登录后，可修改头像 / 名字 / 游戏 UUID / 推流 ID / 直播间名字，` +
  `并轮换自己的密钥与 Bearer 令牌。</p>` +
  `<div class="field"><label for="adminKey">成员密钥</label>` +
  `<input id="adminKey" type="password" autocomplete="current-password" placeholder="请输入成员密钥"></div>` +
  `<button class="btn btn--primary btn--block" type="button" data-act="admin-login">登录</button>` +
  `<p class="gate__hint">没有密钥？请联系服务器管理员在成员管理里为您创建。` +
  `<br>服务器主管理 KEY 见服务启动日志（默认 <b>NTE-ADMIN</b>）。` +
  `<br>在本机（localhost / 127.0.0.1）直接访问时无需登录。</p>` +
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
      hint: m.streamId
        ? `OBS 推流地址：<WebRTC 根地址>/${esc(m.streamId)}/whip；令牌填到 OBS 的「Bearer 令牌」字段。${PUSH_TIP_LINE}`
        : `设置后即可用「推流 ID + Bearer 令牌」开播；${PUSH_TIP_LINE}`,
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

function meRoomPanel(m) {
  const play = m.play || {};
  const body =
    `<form class="form form--2" data-form="me-room">` +
    fieldText('roomTitle', '直播间名字', m.roomTitle, {
      hint: '开播后展示在频道里的标题（可随时改）',
    }) +
    `<div style="grid-column:1/-1" class="notice">` +
    (m.streamId
      ? `OBS → 推流：服务选 <b>WHIP</b>，服务器填 ` +
        `<code>&lt;WebRTC 根地址&gt;/${esc(m.streamId)}/whip</code>，` +
        `Bearer 令牌填到 OBS 的「Bearer 令牌」字段。` +
        (play.webrtc ? `<br>观看（WebRTC）：<code>${esc(play.webrtc)}</code>` : '') +
        (play.hls ? `<br>观看（HLS）：<code>${esc(play.hls)}</code>` : '')
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
    `<div class="notice" style="margin-top:10px">退出后需重新输入密钥才能编辑资料；` +
    `本站其余页面（总览 / 赛程 / 直播…）本来就是公开只读的。</div>`;
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
    // 正常不会走到这里：本机免登录与主 KEY 登录都会绑到唯一的服务器管理员成员上
    host.innerHTML =
      panelHtml(
        '我的',
        App.me?.name || '服务器管理员',
        `<div class="notice">当前会话没有绑定成员资料（key 级会话）。` +
          `本机免登录 / 主管理 KEY 登录都会绑定到唯一的<b>服务器管理员成员</b>；` +
          `若看到这条提示，说明还没有管理员成员，请到「管理 → 成员管理」创建一个。</div>`
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
          : '成员用「推流 ID + Bearer 令牌」推流；留空则不能推流',
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
