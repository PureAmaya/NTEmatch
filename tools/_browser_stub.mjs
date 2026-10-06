/* 浏览器最小桩（只给 tools/ 下的自检脚本用，不是站点代码）。
 *
 * 站点的前端模块是原生 ESM，直接 import 就会去碰 ``document`` / ``localStorage`` /
 * ``location``。这里把这些补上，好让纯 Node 的自检脚本能把**真实模块**跑起来
 * （而不是复制一份逻辑来测——那样测的是副本，改坏了真代码照样过）。
 *
 * 用法：
 *   const { el, makeEl, setEl } = installBrowserStub();
 *   ...
 *   const { App } = await import('../static/js/core.js');
 */

/** 造一个「长得像元素」的对象：够模块读属性、绑事件、拼 HTML 就行。 */
export function makeEl(id = '') {
  const el = {
    id,
    hidden: false,
    paused: true,
    muted: true,
    volume: 1,
    src: '',
    srcObject: null,
    innerHTML: '',
    textContent: '',
    className: '',
    dataset: {},
    children: [],
    isConnected: true,
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    play() {
      el.paused = false;
      return Promise.resolve();
    },
    load() {},
    removeAttribute(name) {
      if (name === 'src') el.src = '';
    },
    setAttribute() {},
    appendChild(child) {
      el.children.push(child);
      el.textContent += child?.textContent || '';
    },
    closest() {
      return null;
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
    removeEventListener() {},
    remove() {},
  };
  return el;
}

/**
 * 装好全局桩；返回 ``{ el, setEl, makeEl }``——
 * 用 ``setEl('#xxx', …)`` 预置元素，用 ``el('#xxx')`` 取回来。
 */
export function installBrowserStub() {
  const els = new Map();
  const el = (sel) => els.get(sel) || null;
  const setEl = (sel, node = makeEl(sel.replace('#', ''))) => {
    els.set(sel, node);
    return node;
  };

  globalThis.window = { Hls: undefined, matchMedia: () => ({ matches: false, addEventListener() {} }) };
  globalThis.location = { search: '', pathname: '/', href: 'http://localhost/' };
  globalThis.history = { replaceState() {} };
  globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  globalThis.localStorage = {
    store: {},
    getItem(k) {
      return k in this.store ? this.store[k] : null;
    },
    setItem(k, v) {
      this.store[k] = String(v);
    },
    removeItem(k) {
      delete this.store[k];
    },
  };
  globalThis.document = {
    querySelector: (sel) => els.get(sel) || null,
    querySelectorAll: () => [],
    createElement: () => makeEl(''),
    documentElement: { style: {}, dataset: {} },
    body: makeEl('body'),
    head: { appendChild() {} },
    addEventListener() {},
  };
  return { el, setEl, makeEl, els };
}
