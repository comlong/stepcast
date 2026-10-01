/* The extension's interface language: same setting as the editor (the server's ui_language).
 *
 * Translations ship with the extension in locales/{lang}.json (picked from static/locales by
 * tools/i18n_extract.py --sync-extension), so the last language is still shown when the service isn't running.
 * The Chinese source text is the key; formatting rules match static/i18n.js and backend/i18n.py.
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

  /** Read the last remembered language (synced with the editor's setting once connected to the local service); English the first time. */
  async function init() {
    let saved = '';
    try { saved = (await chrome.storage.local.get('vt_ui_lang')).vt_ui_lang; } catch (e) { /* */ }
    const lang = normalize(saved) || 'en';
    state.lang = lang;
    state.dict = await loadDict(lang);
    return lang;
  }

  /** Follow the server's interface language when it changes. Returns true if it changed. */
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

  /** Translate the static text on the page (same rules as the server's translate_html). Can be called repeatedly: the source text is kept on the node. */
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
