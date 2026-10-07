/* 全站 SVG 图标集（内联，不请求任何外部资源）。
 *
 * 线条取自 **Feather Icons**（MIT，https://feathericons.com）——一套开放、统一、
 * 24×24 网格 / 2px 描边的图标集；标题图标（H1~H4）与品牌环在此基础上自绘。
 *
 * 三条约定：
 *
 * 1. 全部走 `stroke="currentColor"`：**自动跟随主色**；
 * 2. 尺寸由 CSS 的 `font-size` 决定（`.ic` 是 1em 方块），调用方不用传尺寸；
 * 3. 静态 HTML 写 `<span class="ic" data-icon="bell"></span>`，
 *    由 `hydrateIcons()` 在启动时补上 SVG；JS 模板直接调 `icon('bell')`。
 *
 * 不引图标字体 / 雪碧图的理由：本站没有构建步骤，几十个图标内联一共十几 KB，
 * 省掉一次请求、也没有字形加载闪烁（FOUT）。
 */

const S = (path, extra = '') =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}${extra}</svg>`;

/** 标题图标：井号 + 级别数字（数字用 text 画，避免再引入一套字体）。 */
const heading = (level) =>
  S(
    '<line x1="4" y1="9" x2="17" y2="9"/><line x1="4" y1="15" x2="17" y2="15"/>' +
      '<line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/>',
    `<text x="19" y="21" font-size="11" font-weight="700" fill="currentColor" stroke="none" ` +
      `font-family="system-ui, sans-serif">${level}</text>`
  );

const PATHS = {
  /* ---- 导航（底栏六个页签） ---- */
  home: S('<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>' +
    '<polyline points="9 22 9 12 15 12 15 22"/>'),
  chart: S('<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/>' +
    '<line x1="6" y1="20" x2="6" y2="14"/>'),
  list: S('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/>' +
    '<line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/>' +
    '<line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>'),
  users: S('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>' +
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'),
  video: S('<polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>'),
  shield: S('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>'),
  user: S('<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
  hexagon: S('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 ' +
    '1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'),
  /* 品牌环：面板标题认不出关键词时的兜底图标（与顶栏 logo 同一套几何） */
  ring: S('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/>' +
    '<circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/>'),

  /* ---- 赛事 ---- */
  award: S('<circle cx="12" cy="8" r="7"/>' +
    '<polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88"/>'),
  flag: S('<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/>' +
    '<line x1="4" y1="22" x2="4" y2="15"/>'),
  target: S('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/>' +
    '<circle cx="12" cy="12" r="2"/>'),
  branch: S('<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/>' +
    '<circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>'),
  shuffle: S('<polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/>' +
    '<polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/>' +
    '<line x1="4" y1="4" x2="9" y2="9"/>'),
  calendar: S('<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>' +
    '<line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>' +
    '<line x1="3" y1="10" x2="21" y2="10"/>'),
  clock: S('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
  zap: S('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),
  star: S('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 ' +
    '2 9.27 8.91 8.26 12 2"/>'),

  /* ---- 内容与配置 ---- */
  book: S('<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>' +
    '<path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>'),
  file: S('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
    '<polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>' +
    '<line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>'),
  sliders: S('<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>' +
    '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>' +
    '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>' +
    '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>' +
    '<line x1="17" y1="16" x2="23" y2="16"/>'),
  droplet: S('<path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/>'),
  globe: S('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>' +
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'),
  bell: S('<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>' +
    '<path d="M13.7 21a2 2 0 0 1-3.4 0"/>'),
  message: S('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  send: S('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>'),
  image: S('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.6"/>' +
    '<path d="M21 15.5 16 10.5 5 21"/>'),
  link: S('<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>' +
    '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'),
  external: S('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>' +
    '<polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'),
  code: S('<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>'),

  /* ---- 排序 / 状态 ---- */
  activity: S('<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'),
  wifi: S('<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/>' +
    '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>'),
  radio: S('<circle cx="12" cy="12" r="2"/>' +
    '<path d="M16.24 7.76a6 6 0 0 1 0 8.49m-8.48-.01a6 6 0 0 1 0-8.49m11.31-2.82a10 10 0 0 1 0 ' +
    '14.14m-14.14 0a10 10 0 0 1 0-14.14"/>'),
  check: S('<polyline points="20 6 9 17 4 12"/>'),
  checkCircle: S('<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>' +
    '<polyline points="22 4 12 14.01 9 11.01"/>'),
  xCircle: S('<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>' +
    '<line x1="9" y1="9" x2="15" y2="15"/>'),
  alert: S('<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 ' +
    '0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
  info: S('<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/>' +
    '<circle cx="12" cy="8" r="1" fill="currentColor" stroke="none"/>'),
  eye: S('<path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5 1.5 12 1.5 12z"/>' +
    '<circle cx="12" cy="12" r="3"/>'),
  eyeOff: S('<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 ' +
    '5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 ' +
    '3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'),

  /* ---- 权限与安全 ---- */
  key: S('<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 ' +
    '0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>'),
  lock: S('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>' +
    '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
  unlock: S('<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>' +
    '<path d="M7 11V7a5 5 0 0 1 9.9-1"/>'),
  login: S('<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>' +
    '<polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>'),
  ban: S('<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>'),

  /* ---- 数据与运维 ---- */
  database: S('<ellipse cx="12" cy="5" rx="9" ry="3"/>' +
    '<path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>'),
  archive: S('<polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/>' +
    '<line x1="10" y1="12" x2="14" y2="12"/>'),
  drive: S('<line x1="22" y1="12" x2="2" y2="12"/>' +
    '<path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 ' +
    '2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/>' +
    '<line x1="10" y1="16" x2="10.01" y2="16"/>'),
  tool: S('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 ' +
    '7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),
  refresh: S('<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>' +
    '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'),
  download: S('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>' +
    '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
  upload: S('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>' +
    '<polyline points="7 9 12 4 17 9"/><line x1="12" y1="4" x2="12" y2="16"/>'),
  search: S('<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>'),
  play: S('<polygon points="5 3 19 12 5 21 5 3"/>'),
  pause: S('<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>'),

  /* ---- 操作 ---- */
  edit: S('<path d="M11 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-6"/>' +
    '<path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z"/>'),
  trash: S('<path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>' +
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'),
  save: S('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>' +
    '<polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>'),
  plus: S('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
  plusCircle: S('<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/>' +
    '<line x1="8" y1="12" x2="16" y2="12"/>'),
  minus: S('<line x1="5" y1="12" x2="19" y2="12"/>'),
  close: S('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
  left: S('<polyline points="15 18 9 12 15 6"/>'),
  right: S('<polyline points="9 18 15 12 9 6"/>'),
  down: S('<polyline points="6 9 12 15 18 9"/>'),
  arrowRight: S('<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>'),
  hr: S('<line x1="4" y1="12" x2="20" y2="12"/>'),

  /* ---- Markdown 工具栏 ---- */
  h1: heading(1),
  h2: heading(2),
  h3: heading(3),
  h4: heading(4),
  bold: S('<path d="M6 4h8a4 4 0 0 1 0 8H6z"/><path d="M6 12h9a4 4 0 0 1 0 8H6z"/>'),
  italic: S('<line x1="19" y1="4" x2="10" y2="4"/><line x1="14" y1="20" x2="5" y2="20"/>' +
    '<line x1="15" y1="4" x2="9" y2="20"/>'),
  strike: S('<path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/>' +
    '<line x1="4" y1="12" x2="20" y2="12"/>'),
  quote: S('<path d="M7 7h4v5a4 4 0 0 1-4 4z"/><path d="M14 7h4v5a4 4 0 0 1-4 4z"/>'),
  ul: S('<line x1="9" y1="6" x2="20" y2="6"/><line x1="9" y1="12" x2="20" y2="12"/>' +
    '<line x1="9" y1="18" x2="20" y2="18"/><circle cx="4.5" cy="6" r="1.4" fill="currentColor" ' +
    'stroke="none"/><circle cx="4.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>' +
    '<circle cx="4.5" cy="18" r="1.4" fill="currentColor" stroke="none"/>'),
  ol: S('<line x1="10" y1="6" x2="20" y2="6"/><line x1="10" y1="12" x2="20" y2="12"/>' +
    '<line x1="10" y1="18" x2="20" y2="18"/><path d="M4 6h1.5v4M4 10h2.5"/>' +
    '<path d="M4 18h2a1.2 1.2 0 0 0 0-2.4H4.6a1.2 1.2 0 0 1 0-2.4H6"/>'),
  table: S('<rect x="3" y="4" width="18" height="16" rx="1.5"/><line x1="3" y1="10" x2="21" y2="10"/>' +
    '<line x1="9.5" y1="10" x2="9.5" y2="20"/><line x1="15.5" y1="10" x2="15.5" y2="20"/>'),
};

/**
 * 取一个图标。``name`` 不认识时返回空串（宁可少个图标，也不要破图）。
 * 尺寸走 CSS（``.ic`` 是 1em 方块），所以调用方只要设 font-size 或由父级决定。
 */
export function icon(name, { cls = '' } = {}) {
  const body = PATHS[name];
  if (!body) return '';
  return `<span class="ic${cls ? ` ${cls}` : ''}" aria-hidden="true">${body}</span>`;
}

/** 把静态 HTML 里 `<span class="ic" data-icon="…">` 补成真正的 SVG。 */
export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((host) => {
    const name = host.dataset.icon;
    if (!name || host.dataset.iconReady === name) return;
    const body = PATHS[name];
    if (!body) {
      host.removeAttribute('data-icon');
      return;
    }
    host.innerHTML = body;
    host.dataset.iconReady = name;
  });
}

/** 工具栏按钮（Markdown 编辑器专用：小方块、无文字）。 */
export function iconButton(name, label, { act = '', data = '', cls = '' } = {}) {
  const attrs = Object.entries(data)
    .map(([key, value]) => ` data-${key}="${String(value)}"`)
    .join('');
  return (
    `<button class="mdx__btn${cls ? ` ${cls}` : ''}" type="button" title="${label}" ` +
    `aria-label="${label}"${act ? ` data-mdx="${act}"` : ''}${attrs}>${icon(name)}</button>`
  );
}

/**
 * 普通按钮：图标 + 文字（走全站的 ``.btn`` 样式，不是编辑器那套小方块）。
 * ``act`` 为 ``data-act``（由 actions.js 分发），``data`` 是附带的 ``data-*``。
 */
export function iconBtn(name, label, { act = '', data = {}, cls = '', title = '' } = {}) {
  const attrs = Object.entries(data)
    .map(([key, value]) => ` data-${key}="${String(value)}"`)
    .join('');
  return (
    `<button class="btn${cls ? ` ${cls}` : ''}" type="button"${title ? ` title="${title}"` : ''}` +
    `${act ? ` data-act="${act}"` : ''}${attrs}>${icon(name)}<span>${label}</span></button>`
  );
}

/**
 * 面板标题 → 图标（按关键词匹配，先命中先用）。
 *
 * 用关键词而不是让每个调用点都传一次图标：面板头全站几十处，改一处就全站生效，
 * 而且以后新加面板只要名字里带上「通知 / 赛制 / 名单」这类词就自动有图标。
 * 认不出来的用品牌六边形兜底——比留空更像「有意为之」，也不至于每加一个面板
 * 都要回来补一行配置。
 */
const PANEL_ICONS = [
  [/通知|公告/, 'bell'],
  [/开发者|作者/, 'user'],
  [/组件|许可|版权|致谢/, 'book'],
  [/项目|关于/, 'info'],
  [/说明|信息|简介|介绍/, 'file'],
  [/规则|须知/, 'book'],
  [/赛制|设置|配置|参数/, 'sliders'],
  [/界面|主题|外观|颜色/, 'droplet'],
  [/直播|推流|机位|流/, 'video'],
  [/名单|成员|选手|参赛|车手|作者|档案/, 'users'],
  [/组队|分组|抽签|队伍/, 'shuffle'],
  [/对阵|淘汰|晋级|树/, 'branch'],
  [/赛程|日程|轮次|时间/, 'calendar'],
  [/积分|排名|排行|榜|统计|数据/, 'chart'],
  [/冠军|奖|名次/, 'award'],
  [/备份|归档|快照/, 'archive'],
  [/机器人|推送|消息|群/, 'message'],
  [/登录|权限|密钥|KEY|鉴权|访问/, 'key'],
  [/封禁|限制|安全|风控/, 'shield'],
  [/日志|操作|动态|活动/, 'activity'],
  [/状态|诊断|健康|信号/, 'wifi'],
  [/地址|链接|网址/, 'link'],
  [/站点|服务器|全站|届/, 'globe'],
  [/图片|图|头像/, 'image'],
  [/存储|数据库|磁盘/, 'database'],
  [/频道|会话/, 'radio'],
];

export function panelIcon(title) {
  const text = String(title || '');
  for (const [pattern, name] of PANEL_ICONS) {
    if (pattern.test(text)) return name;
  }
  return 'ring';
}
