/* Markdown 编辑器（悬浮大窗）：赛事信息、服务器信息与通知共用这一套。
 *
 * 设计取舍：
 *
 * * **预览走后端同一个渲染器**（``POST /api/md/preview``）——编辑器里看到的就是
 *   发布后的样子，也不会出现「前端渲染器与后端不一致」这种最难查的问题；
 * * **工具栏按钮 = 往 textarea 里写标记**：不做富文本所见即所得。公告这种内容
 *   篇幅小、结构简单，源码模式反而更可控（复制粘贴 Markdown 也不会被打乱）；
 * * **图片上传**：本地选文件 → data:URL → ``POST /api/media`` → 插入
 *   ``![名字](/api/media/xxx.png =50%)``；点预览里的图片可以再调宽度；
 * * 移动端：编辑 / 预览切成上下两块，工具栏横向可滚。
 */

import { Modal, api, esc, log, toast } from './core.js';
import { icon, iconButton } from './icons.js';

const TABLE_TPL = '| 项目 | 说明 |\n| --- | --- |\n|  |  |\n';
const PREVIEW_DELAY = 260;

/** 工具栏：一屏放得下的常用功能（标题 H1~H4、强调、引用、列表、表格、图、链、码、线）。 */
function toolbarHtml() {
  const tools = [
    iconButton('h1', '一级标题', { act: 'h', data: { level: '1' } }),
    iconButton('h2', '二级标题', { act: 'h', data: { level: '2' } }),
    iconButton('h3', '三级标题', { act: 'h', data: { level: '3' } }),
    iconButton('h4', '四级标题', { act: 'h', data: { level: '4' } }),
    '<span class="mdx__sep"></span>',
    iconButton('bold', '加粗（Ctrl+B）', { act: 'bold' }),
    iconButton('italic', '斜体（Ctrl+I）', { act: 'italic' }),
    iconButton('strike', '删除线', { act: 'strike' }),
    iconButton('code', '行内代码', { act: 'code' }),
    '<span class="mdx__sep"></span>',
    iconButton('quote', '引用', { act: 'quote' }),
    iconButton('ul', '无序列表', { act: 'ul' }),
    iconButton('ol', '有序列表', { act: 'ol' }),
    iconButton('table', '插入表格', { act: 'table' }),
    iconButton('hr', '分隔线', { act: 'hr' }),
    '<span class="mdx__sep"></span>',
    iconButton('image', '插入图片', { act: 'image' }),
    iconButton('link', '插入链接（Ctrl+K）', { act: 'link' }),
  ];
  return `<div class="mdx__tools">${tools.join('')}</div>`;
}

/** 预览里点中图片后出现的宽度条（图片大小调整）。 */
function imageBarHtml() {
  const sizes = [
    ['25%', '25%'],
    ['50%', '50%'],
    ['75%', '75%'],
    ['100%', '100%'],
    ['', '原始'],
  ];
  return (
    `<div class="mdx__imgbar" data-imgbar hidden>` +
    `<span class="mdx__imgbar-label">${icon('image')}图片宽度</span>` +
    sizes
      .map(
        ([size, label]) =>
          `<button class="mdx__chip" type="button" data-mdx="img-size" data-size="${size}">${label}</button>`
      )
      .join('') +
    `<button class="mdx__btn mdx__btn--sm" type="button" data-mdx="img-close" ` +
    `title="完成">${icon('close')}</button></div>`
  );
}

/** 编辑器底部的附加勾选项（如「同时发到 QQ 群」）：用 ``data-extra`` 收集。 */
function extrasHtml(extras) {
  if (!extras.length) return '';
  return (
    `<div class="mdx__extras">` +
    extras
      .map(
        (item) =>
          `<label class="mdx__extra"${item.hint ? ` title="${esc(item.hint)}"` : ''}>` +
          `<input type="checkbox" data-extra="${esc(item.name)}"` +
          `${item.checked ? ' checked' : ''}> ` +
          `<span>${esc(item.label)}</span></label>`
      )
      .join('') +
    `</div>`
  );
}

function bodyHtml({
  value,
  hint,
  placeholder,
  titleLabel,
  titleValue,
  titlePlaceholder,
  extras = [],
}) {
  return (
    `<div class="mdx" data-mdx>` +
    // 通知需要标题，信息类不需要 —— 同一套编辑器，多一个字段而已。
    // 标题放在编辑器里（而不是另开一个弹窗要），少一次弹窗来回。
    (titleLabel
      ? `<div class="mdx__title"><label for="mdx-title">${esc(titleLabel)}</label>` +
        `<input id="mdx-title" maxlength="60" value="${esc(titleValue || '')}" ` +
        `placeholder="${esc(titlePlaceholder || '')}"></div>`
      : '') +
    `<div class="mdx__bar">${toolbarHtml()}` +
    `<button class="mdx__btn mdx__btn--toggle" type="button" data-mdx="toggle">` +
    `${icon('eye')}<span>预览</span></button></div>` +
    imageBarHtml() +
    `<div class="mdx__panes" data-pane="edit">` +
    `<textarea class="mdx__input" spellcheck="false" placeholder="${esc(
      placeholder || '支持 Markdown：# 标题、**加粗**、- 列表、| 表格 |、![图片](…)'
    )}">${esc(value || '')}</textarea>` +
    `<div class="mdx__preview md" data-preview>` +
    `<div class="mdx__empty">预览会显示在这里（与发布后的效果一致）</div></div>` +
    `</div>` +
    `<div class="mdx__foot">${extrasHtml(extras)}` +
    `<span class="mdx__hint">${hint ? esc(hint) : ''}</span>` +
    `<span class="mdx__count" data-count></span></div>` +
    `</div>`
  );
}

/** 在光标处插入文本；有选中内容时用它替换。 */
function surround(ta, before, after, placeholder = '') {
  const { selectionStart: start, selectionEnd: end, value } = ta;
  const picked = value.slice(start, end) || placeholder;
  const next = value.slice(0, start) + before + picked + after + value.slice(end);
  ta.value = next;
  const caret = start + before.length;
  ta.setSelectionRange(caret, caret + picked.length);
  ta.focus();
}

/** 给选中的每一行加 / 去前缀（标题、引用、列表都用它）。 */
function prefixLines(ta, prefix) {
  const { selectionStart: start, selectionEnd: end, value } = ta;
  const lineStart = value.lastIndexOf('\n', start - 1) + 1;
  const lineEnd = value.indexOf('\n', end) === -1 ? value.length : value.indexOf('\n', end);
  const lines = value.slice(lineStart, lineEnd).split('\n');
  const allPrefixed = lines.every((line) => line.startsWith(prefix));
  const next = lines
    .map((line) => {
      if (allPrefixed) return line.slice(prefix.length);
      // 换标题级别 / 换列表类型：先清掉已有前缀，避免「## # 标题」这种叠罗汉
      return prefix + line.replace(/^(#{1,6}\s+|>\s+|[-*+]\s+|\d+[.)]\s+)/, '');
    })
    .join('\n');
  ta.value = value.slice(0, lineStart) + next + value.slice(lineEnd);
  ta.setSelectionRange(lineStart, lineStart + next.length);
  ta.focus();
}

/** 在光标处另起一段插入（表格、分隔线这种「块」）。 */
function insertBlock(ta, text) {
  const { selectionStart: start, selectionEnd: end, value } = ta;
  const needLf = start > 0 && !value.slice(0, start).endsWith('\n') ? '\n' : '';
  const body = needLf + text;
  ta.value = value.slice(0, start) + body + value.slice(end);
  const caret = start + body.length;
  ta.setSelectionRange(caret, caret);
  ta.focus();
}

/** 改图片宽度：把 ``![alt](src =50%)`` 里那段尺寸写掉（没有就补上）。 */
function setImageSize(text, src, size) {
  const escaped = src.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`(!\\[[^\\]]*\\]\\(${escaped})(?:\\s+=[^)\\s]*)?(\\))`);
  if (!re.test(text)) return text;
  return text.replace(re, size ? `$1 =${size}$2` : '$1$2');
}

/** 读本地文件成 data:URL（不依赖后端，选中就能预览）。 */
function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

function pickFile() {
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg,image/webp,image/gif';
    input.onchange = () => resolve(input.files && input.files[0] ? input.files[0] : null);
    // 用户直接关掉选择框时不会触发 change —— 不阻塞编辑器，回调里判空即可
    input.click();
  });
}

/**
 * 打开编辑器。
 *
 * @param {object} opts
 * @param {string} opts.title    弹窗标题
 * @param {string} opts.value    初始 Markdown
 * @param {string} [opts.hint]   左下角提示（长度限制等）
 * @param {boolean} [opts.readOnly] 只读（如已结束的届的赛事信息）
 * @param {(text: string) => Promise<any>} opts.onSave 保存回调；resolve 后关闭
 */
export function openMarkdownEditor({
  title,
  value = '',
  hint = '',
  placeholder = '',
  titleLabel = '',
  titleValue = '',
  titlePlaceholder = '',
  readOnly = false,
  extras = [],
  onSave,
} = {}) {
  Modal.open({
    title,
    className: 'modal__box--mdx',
    body: bodyHtml({
      value,
      hint,
      placeholder,
      titleLabel,
      titleValue,
      titlePlaceholder,
      // 只读时勾选项没有意义（保存按钮都没了），干脆不渲染
      extras: readOnly ? [] : extras,
    }),
    footer: readOnly
      ? `<button class="btn btn--sm btn--ghost" type="button" data-close>关闭</button>`
      : `<button class="btn btn--sm btn--ghost" type="button" data-close>取消</button>` +
        `<button class="btn btn--sm btn--primary" type="button" data-save>${icon('save')} 保存</button>`,
    onMount(bodyEl, footEl) {
      const ta = bodyEl.querySelector('.mdx__input');
      const preview = bodyEl.querySelector('[data-preview]');
      const panes = bodyEl.querySelector('.mdx__panes');
      const bar = bodyEl.querySelector('[data-imgbar]');
      const countEl = bodyEl.querySelector('[data-count]');
      const labelEl = bodyEl.querySelector('.mdx__btn--toggle span');
      const initial = ta.value;
      let timer = 0;
      let selectedSrc = '';

      if (readOnly) ta.readOnly = true;

      const paintCount = () => {
        const n = ta.value.length;
        countEl.textContent = `${n} 字`;
      };

      const render = async () => {
        if (!ta.value.trim()) {
          preview.innerHTML = '<div class="mdx__empty">预览会显示在这里（与发布后的效果一致）</div>';
          return;
        }
        try {
          const res = await api('/md/preview', { method: 'POST', auth: true, body: { text: ta.value } });
          preview.innerHTML = res.html || '';
        } catch (err) {
          // 预览失败不该打断写作：提示一下，继续让人编辑
          log.warn('预览失败', err);
          preview.innerHTML = '<div class="mdx__empty">预览暂时不可用（保存不受影响）</div>';
        }
      };

      const schedule = () => {
        paintCount();
        window.clearTimeout(timer);
        timer = window.setTimeout(render, PREVIEW_DELAY);
      };

      ta.addEventListener('input', schedule);
      ta.addEventListener('keydown', (e) => {
        if (readOnly) return;
        const meta = e.ctrlKey || e.metaKey;
        if (meta && e.key.toLowerCase() === 'b') {
          e.preventDefault();
          surround(ta, '**', '**', '加粗文字');
          schedule();
        } else if (meta && e.key.toLowerCase() === 'i') {
          e.preventDefault();
          surround(ta, '*', '*', '斜体文字');
          schedule();
        } else if (meta && e.key.toLowerCase() === 'k') {
          e.preventDefault();
          surround(ta, '[', '](https://)', '链接文字');
          schedule();
        } else if (meta && e.key.toLowerCase() === 's') {
          e.preventDefault();
          save();
        } else if (e.key === 'Tab') {
          e.preventDefault();
          surround(ta, '  ', '');
        }
      });

      /** 工具栏动作。 */
      const act = async (name, dataset) => {
        switch (name) {
          case 'h':
            prefixLines(ta, '#'.repeat(Number(dataset.level) || 1) + ' ');
            break;
          case 'bold':
            surround(ta, '**', '**', '加粗文字');
            break;
          case 'italic':
            surround(ta, '*', '*', '斜体文字');
            break;
          case 'strike':
            surround(ta, '~~', '~~', '删除线');
            break;
          case 'code':
            surround(ta, '`', '`', '代码');
            break;
          case 'quote':
            prefixLines(ta, '> ');
            break;
          case 'ul':
            prefixLines(ta, '- ');
            break;
          case 'ol':
            prefixLines(ta, '1. ');
            break;
          case 'table':
            insertBlock(ta, TABLE_TPL);
            break;
          case 'hr':
            insertBlock(ta, '\n---\n');
            break;
          case 'link':
            surround(ta, '[', '](https://)', '链接文字');
            break;
          case 'image':
            await uploadImage();
            break;
          case 'img-size':
            ta.value = setImageSize(ta.value, selectedSrc, dataset.size || '');
            bar.hidden = true;
            break;
          case 'img-close':
            bar.hidden = true;
            break;
          case 'toggle': {
            const next = panes.dataset.pane === 'edit' ? 'preview' : 'edit';
            panes.dataset.pane = next;
            if (labelEl) labelEl.textContent = next === 'edit' ? '预览' : '编辑';
            break;
          }
          default:
            return;
        }
        schedule();
      };

      async function uploadImage() {
        const file = await pickFile();
        if (!file) return;
        try {
          const dataUrl = await readFileAsDataUrl(file);
          const res = await api('/media', { method: 'POST', auth: true, body: { data_url: dataUrl } });
          const alt = String(file.name || '图片').replace(/\.[^.]+$/, '');
          insertBlock(ta, `![${alt}](${res.url} =100%)`);
          toast('图片已上传', 'ok');
        } catch (err) {
          toast(err.message || '图片上传失败', 'err');
        }
        schedule();
      }

      bodyEl.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-mdx]');
        if (btn) {
          act(btn.dataset.mdx, btn.dataset);
          return;
        }
        // 点预览里的图片 → 调宽度
        const img = e.target.closest('.mdx__preview img');
        if (img) {
          selectedSrc = img.getAttribute('src') || '';
          bar.hidden = !selectedSrc;
        }
      });

      paintCount();
      render();

      // 关弹窗前拦一道：有未保存改动时，第一次点「取消」只提示，不丢内容。
      // 这里的事件在**按钮**上，先于 Modal 挂在 #modal 上的全局监听触发，
      // 所以 stopImmediatePropagation 足以拦住关闭（第二次点就放行）。
      let warned = false;
      const closeBtn = footEl.querySelector('[data-close]');
      if (closeBtn && !readOnly) {
        closeBtn.addEventListener('click', (e) => {
          if (ta.value !== initial && !warned) {
            e.preventDefault();
            e.stopImmediatePropagation();
            warned = true;
            closeBtn.textContent = '再点一次丢弃改动';
            closeBtn.classList.add('btn--danger');
          }
        });
      }

      async function save() {
        if (readOnly) return;
        const titleInput = bodyEl.querySelector('#mdx-title');
        const heading = titleInput ? titleInput.value.trim() : '';
        if (titleInput && !heading) {
          toast('先给这条通知起个标题', 'err');
          titleInput.focus();
          return;
        }
        const saveBtn = footEl.querySelector('[data-save]');
        if (saveBtn) saveBtn.disabled = true;
        // 附加勾选项（如「同时发到 QQ 群」）：按 data-extra 收成一个对象交给调用方，
        // 编辑器本身不认识任何具体业务
        const extraState = {};
        bodyEl.querySelectorAll('[data-extra]').forEach((el) => {
          extraState[el.dataset.extra] = el.checked;
        });
        try {
          await onSave({ title: heading, text: ta.value, extras: extraState });
          Modal.close();
        } catch (err) {
          toast(err.message || '保存失败', 'err');
        } finally {
          if (saveBtn) saveBtn.disabled = false;
        }
      }

      const saveBtn = footEl.querySelector('[data-save]');
      if (saveBtn) saveBtn.onclick = save;
    },
  });
}
