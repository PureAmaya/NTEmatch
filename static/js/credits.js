/* 许可信息：页脚「开源组件」清单弹窗 + 开发者页（/developer）。
 *
 * 署名与许可本身写在 index.html 的静态 HTML 里——它不该依赖接口，更不该因为一次
 * 请求失败就消失。这里只负责把 /api/credits 的内容摊开给人看。
 *
 * 清单的唯一数据源是后端 app/credits.py：前端不抄第二份，
 * 否则换依赖时一定有一边忘了改。
 */

import { Modal, api, esc, log, qs, toast } from './core.js';
import { icon } from './icons.js';
// 开发者页的面板样式与别处一致（标题 / 图标 / 提示都复用同一套）
import { panelHtml } from './ui.js';

function groupHtml(group) {
  const rows = (group.items || [])
    .map(
      (item) =>
        `<div class="credits__row">` +
        `<span class="credits__name"><a href="${esc(item.url)}" target="_blank" ` +
        `rel="noopener noreferrer">${esc(item.name)}</a></span>` +
        `<span class="credits__lic">${esc(item.license)}</span>` +
        (item.note ? `<span class="credits__note">${esc(item.note)}</span>` : '') +
        `</div>`
    )
    .join('');
  return (
    `<div class="credits__group"><div class="credits__head">` +
    `<b>${esc(group.title || '')}</b><span>${esc(group.note || '')}</span></div>` +
    `${rows}</div>`
  );
}

function selfHtml(data) {
  const license = data.license || {};
  const author = data.author || {};
  return (
    `<div class="credits__self">` +
    `本站（${esc(data.source || '')}）以 <b>${esc(license.id || 'AGPL-3.0')}</b> 许可开源：` +
    `可自由使用、修改与分发，但<b>改动后对外提供服务也必须公开源码</b>。` +
    `版权归 <b>${esc(author.name || '')}</b> 所有。<br>` +
    `${esc(data.frontendNote || '')}` +
    `</div>`
  );
}

export async function openCreditsModal() {
  let data;
  try {
    data = await loadCredits();
  } catch (err) {
    // 清单取不到不该让人以为「这站没署名」：页脚的署名是静态的，这里只是详情
    log.warn('开源组件清单读取失败', err);
    toast('开源组件清单读取失败，请稍后再试', 'err');
    return;
  }
  Modal.open({
    title: '开源组件与许可',
    body: (data.groups || []).map(groupHtml).join('') + selfHtml(data),
    footer: `<button class="btn btn--sm btn--primary" type="button" data-close>关闭</button>`,
  });
}

/* ------------------------- 开发者页（/developer） ------------------------- */

/** 缓存一份：这一页与「开源组件」弹窗用的是同一份数据，翻来翻去不该反复请求。 */
let creditsData = null;

async function loadCredits() {
  if (!creditsData) creditsData = await api('/credits');
  return creditsData;
}

/** 站外链接做成按钮样式的胶囊（和页脚那几个同一套观感）。 */
const chip = (name, label, href, title) =>
  `<a class="btn btn--sm" href="${esc(href || '#')}" target="_blank" rel="noopener noreferrer" ` +
  `title="${esc(title || label)}">${icon(name)}<span>${esc(label)}</span></a>`;

const row = (label, value) =>
  `<div class="dev__row"><dt>${esc(label)}</dt><dd>${value}</dd></div>`;

const rowsHtml = (items) => `<dl class="dev__rows">${items.join('')}</dl>`;

function pageHtml(data) {
  const a = data.author || {};
  const lic = data.license || {};
  return (
    `<div class="dev">` +
    // ---- 名片 ----
    `<section class="panel dev__hero">` +
    `<img class="dev__ava" src="${esc(a.qqAvatar || '')}" alt="${esc(a.name || '开发者')}的头像" ` +
    `width="84" height="84" loading="lazy" referrerpolicy="no-referrer">` +
    `<h1 class="dev__name">${esc(a.name || '开发者')}</h1>` +
    `<p class="dev__role">DEVELOPER · 本站作者</p>` +
    `<div class="dev__links">` +
    chip('video', 'B 站', a.bilibili, '开发者的 B 站个人空间（新窗口打开）') +
    chip('code', '源码仓库', data.source, '本站源码，AGPL-3.0（新窗口打开）') +
    `<button class="btn btn--sm" type="button" data-act="credits" ` +
    `title="本站用到的开源组件与各自许可">${icon('book')}<span>开源组件</span></button>` +
    `</div></section>` +

    // ---- 开发者信息（不展示 QQ：署名只给名字与主页） ----
    panelHtml(
      '开发者信息',
      '怎么找到我',
      rowsHtml([
        row('昵称', esc(a.name || '—')),
        row('B 站', a.bilibili ? linked(a.bilibili, 'space.bilibili.com/11393965') : '—'),
        row('源码', data.source ? linked(data.source, 'github.com/PureAmaya/NTEmatch') : '—'),
      ])
    ) +

    // ---- 关于这个项目 ----
    panelHtml(
      '关于这个项目',
      '开源 · 自托管',
      rowsHtml([
        row('许可证', linked(lic.url, `${lic.id || 'AGPL-3.0'}（${lic.name || ''}）`)),
        row(
          '如何使用',
          esc('自托管：克隆源码仓库，按 README 装好依赖（uv sync）后启动即可；也可以用仓库里的 Dockerfile / compose。')
        ),
        row(
          '允许',
          esc('自由使用、修改、分发，包含私有部署与商用；改动之后只要仍以 AGPL-3.0 开源即可。')
        ),
        row(
          '不允许',
          esc('移除或替换版权与许可声明；把改动闭源后对外提供网络服务（AGPL 第 13 条）；用作者名义为衍生作品背书。')
        ),
      ])
    ) +

    // ---- 开源组件 ----
    panelHtml(
      '开源组件',
      '各组件的许可与用途',
      (data.groups || []).map(groupHtml).join('') + selfHtml(data)
    ) +
    `</div>`
  );
}

const linked = (href, text) =>
  `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>`;

/**
 * 渲染开发者页（`/developer`）。
 *
 * 数据源与「开源组件」弹窗完全相同（后端 `app/credits.py`），所以这里不需要
 * 第二份开发者信息：换名字 / 换仓库地址只改后端那一处。
 */
export async function renderDeveloperPage() {
  const host = qs('#developerBody');
  if (!host) return;
  if (!host.innerHTML) host.innerHTML = `<div class="panel dev__loading">正在读取开发者信息…</div>`;
  let data;
  try {
    data = await loadCredits();
  } catch (err) {
    log.warn('开发者信息读取失败', err);
    host.innerHTML =
      `<div class="panel dev__loading">开发者信息读取失败：` +
      `${esc(err.message || '请稍后再试')}</div>`;
    return;
  }
  host.innerHTML = pageHtml(data);
  // 头像取不到（离线 / 被墙）时换成一个占位字母，别留破图
  const img = host.querySelector('.dev__ava');
  if (img) {
    img.addEventListener('error', () => {
      const placeholder = document.createElement('div');
      placeholder.className = 'dev__ava dev__ava--ph';
      placeholder.textContent = String((data.author?.name || '开').slice(0, 1));
      img.replaceWith(placeholder);
    });
  }
}
