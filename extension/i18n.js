/* 扩展的界面语言：跟编辑器用同一个设置（服务端的 ui_language）。
 *
 * 译文随扩展打包在 locales/{lang}.json（由 tools/i18n_extract.py --sync-extension 从
 * static/locales 里挑出扩展用到的部分），所以服务没开时也能按上次的语言显示。
 * 中文原文就是 key；格式化规则和 static/i18n.js、backend/i18n.py 保持一致。
 */
(function (global) {
  const LANGS = ['zh', 'en', 'de', 'fr', 'pl', 'it', 'es', 'nl'];
  const HAN = /[一-鿿]/;
  const state = { lang: 'en', dict: {} };

  function normalize(code) {
    const c = String(code || '').trim().toLowerCase().split(/[-_]/)[0];
    return LANGS.includes(c) ? c : '';
  }

  function pluralCategory(lang, n) {
    const x = Number(n);
    if (!isFinite(x) || lang === 'zh') return 'other';
    const integer = Number.isInteger(x);
    const i = Math.floor(Math.abs(x));
    if (lang === 'fr') return i === 0 || i === 1 ? 'one' : 'other';
    if (lang === 'pl') {
      if (!integer) return 'other';
      if (i === 1) return 'one';
      if (i % 10 >= 2 && i % 10 <= 4 && !(i % 100 >= 12 && i % 100 <= 14)) return 'few';
      return 'many';
    }
    return integer && i === 1 ? 'one' : 'other';
  }

  function matchBrace(s, start) {
    let depth = 0;
    for (let j = start; j < s.length; j++) {
      if (s[j] === '{') depth++;
      else if (s[j] === '}' && --depth === 0) return j;
    }
    return -1;
  }

  function branches(spec) {
    const out = {};
    const re = /\s*(=\d+|zero|one|two|few|many|other)\s*\{/y;
    let i = 0;
    while (i < spec.length) {
      re.lastIndex = i;
      const m = re.exec(spec);
      if (!m) break;
      const open = re.lastIndex - 1;
      const close = matchBrace(spec, open);
      if (close < 0) break;
      out[m[1]] = spec.slice(open + 1, close);
      i = close + 1;
    }
    return out;
  }

  function formatMessage(text, params, lang) {
    let out = '';
    let i = 0;
    while (i < text.length) {
      const ch = text[i];
      if (ch !== '{') { out += ch; i++; continue; }
      const j = matchBrace(text, i);
      if (j < 0) { out += text.slice(i); break; }
      const inner = text.slice(i + 1, j);
      const comma = inner.indexOf(',');
      const name = (comma < 0 ? inner : inner.slice(0, comma)).trim();
      const rest = comma < 0 ? '' : inner.slice(comma + 1);
      const comma2 = rest.indexOf(',');
      const kind = (comma2 < 0 ? rest : rest.slice(0, comma2)).trim();
      if (kind === 'plural' && params && name in params) {
        const n = params[name];
        const br = branches(rest.slice(comma2 + 1));
        let chosen = Number.isInteger(Number(n)) ? br['=' + Number(n)] : undefined;
        if (chosen === undefined) chosen = br[pluralCategory(lang, n)] ?? br.other ?? '';
        out += formatMessage(chosen.split('#').join(String(n)), params, lang);
      } else if (!rest && params && name in params) {
        out += String(params[name]);
      } else {
        out += text.slice(i, j + 1);
      }
      i = j + 1;
    }
    return out;
  }

  function t(key, params) {
    const text = state.dict[key] || key;
    return params || text.indexOf('{') >= 0 ? formatMessage(text, params || {}, state.lang) : text;
  }

  async function loadDict(lang) {
    if (lang === 'zh') return {};
    try {
      const res = await fetch(chrome.runtime.getURL(`locales/${lang}.json`));
      return res.ok ? await res.json() : {};
    } catch (e) {
      return {};
    }
  }

  /** 读上次记住的语言（连上本地服务后会跟编辑器的设置同步）；第一次用默认英语。 */
  async function init() {
    let saved = '';
    try { saved = (await chrome.storage.local.get('vt_ui_lang')).vt_ui_lang; } catch (e) { /* */ }
    const lang = normalize(saved) || 'en';
    state.lang = lang;
    state.dict = await loadDict(lang);
    return lang;
  }

  /** 服务端的界面语言变了就跟着换。换了返回 true。 */
  async function setLang(code) {
    const lang = normalize(code);
    if (!lang || lang === state.lang) return false;
    state.lang = lang;
    state.dict = await loadDict(lang);
    try { await chrome.storage.local.set({ vt_ui_lang: lang }); } catch (e) { /* */ }
    return true;
  }

  function use(lang, dict) {
    state.lang = normalize(lang) || state.lang;
    state.dict = dict || {};
  }

  const norm = (s) => s.replace(/\s+/g, ' ').trim();

  /** 翻译页面里的静态文字（和服务端 translate_html 同一套规则）。可重复调用：原文记在节点上。 */
  function translateDom(root) {
    root = root || document.body;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const parent = n.parentElement;
      if (!parent || parent.closest('script,style,[translate="no"]')) continue;
      if (n.vtKey === undefined) {
        const key = norm(n.nodeValue);
        n.vtKey = HAN.test(key) ? key : null;
        const raw = n.nodeValue;
        n.vtLead = raw.slice(0, raw.length - raw.trimStart().length);
        n.vtTail = raw.slice(raw.trimEnd().length);
      }
      if (n.vtKey) n.nodeValue = n.vtLead + t(n.vtKey) + n.vtTail;
    }
    for (const attr of ['placeholder', 'title']) {
      root.querySelectorAll(`[${attr}]`).forEach((el) => {
        const store = 'vtKey' + attr;
        if (el[store] === undefined) {
          const key = norm(el.getAttribute(attr));
          el[store] = HAN.test(key) ? key : null;
        }
        if (el[store]) el.setAttribute(attr, t(el[store]));
      });
    }
    if (document.documentElement) document.documentElement.lang = state.lang;
  }

  global.VTI18N = { init, setLang, use, t, translateDom, normalize, lang: () => state.lang, dict: () => state.dict };
  global.t = t;
})(typeof self !== 'undefined' ? self : this);
