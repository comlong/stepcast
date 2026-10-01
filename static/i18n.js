/* Interface language. The Chinese source text is the key; translations are injected by the server as window.VT_I18N = { lang, dict }.
   Placeholders {name}; counts use ICU plurals {n, plural, one {# page} other {# pages}}.
   Kept consistent with backend/i18n.py and extension/i18n.js. */

(function (global) {
  const I18N = global.VT_I18N || { lang: 'zh', dict: {} };

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
    const text = (I18N.dict && I18N.dict[key]) || key;
    return params || text.indexOf('{') >= 0 ? formatMessage(text, params || {}, I18N.lang) : text;
  }

  global.t = t;
  global.N_ = (s) => s;                       // only marks text as translatable, does not translate on the spot
  global.i18nLang = () => I18N.lang;
  global.i18nFormat = formatMessage;          // for tests
})(typeof window !== 'undefined' ? window : globalThis);
