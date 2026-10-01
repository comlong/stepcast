/* StepCast recorder — content script
 * Listens for clicks / input / Enter on the page, extracts element information and hands it to the background for screenshot and report.
 */
(() => {
  if (window.__vtInjected) return;
  window.__vtInjected = true;

  const INTERACTIVE = [
    'a', 'button', 'input', 'select', 'textarea', 'label', 'summary',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]',
    '[role=checkbox]', '[role=radio]', '[role=option]', '[role=switch]',
    '[contenteditable=true]', '[onclick]', '[tabindex]'
  ].join(',');

  let recording = false;
  let stepCount = 0;
  let lastSentAt = 0;
  let lastSig = '';
  const committed = new WeakMap();     // input -> value already recorded, so one input isn't recorded twice
  let overlay = null;
  let overlayHidden = false;
  let cfg = { autoRedact: true, indexText: true, keywords: [] };

  function applyCfg(s) {
    if (!s) return;
    cfg.autoRedact = s.autoRedact !== false;
    cfg.indexText = s.indexText !== false;
    cfg.keywords = String(s.redactKeywords || '')
      .split(/[,，\n]/).map(x => x.trim()).filter(x => x.length > 1);
  }

  /* ---------- element description ---------- */

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return '#' + el.id;
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth++) {
      let part = node.tagName.toLowerCase();
      if (node.id && /^[A-Za-z][\w-]*$/.test(node.id)) {
        parts.unshift('#' + node.id);
        break;
      }
      const cls = (node.className && typeof node.className === 'string')
        ? node.className.trim().split(/\s+/).filter(c => c && c.length < 24).slice(0, 2)
        : [];
      if (cls.length) part += '.' + cls.join('.');
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  function labelFor(el) {
    if (!el) return '';
    const attr = (n) => (el.getAttribute && el.getAttribute(n)) || '';
    const tag = el.tagName ? el.tagName.toLowerCase() : '';

    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      let lbl = attr('aria-label') || attr('placeholder') || attr('title') || attr('name');
      if (!lbl && el.id) {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) lbl = (l.innerText || '').trim();
      }
      if (!lbl) {
        const p = el.closest('label');
        if (p) lbl = (p.innerText || '').trim();
      }
      if (!lbl && tag === 'input' && (el.type === 'submit' || el.type === 'button')) lbl = el.value || '';
      return (lbl || '').replace(/\s+/g, ' ').trim().slice(0, 100);
    }
    let txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!txt) txt = attr('aria-label') || attr('title') || attr('alt');
    if (!txt) {
      const img = el.querySelector && el.querySelector('img[alt],svg title');
      if (img) txt = img.getAttribute('alt') || img.textContent || '';
    }
    return (txt || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  }

  function roleOf(el) {
    const r = el.getAttribute && el.getAttribute('role');
    if (r) return r;
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const map = { a: 'link', button: 'button', select: 'combobox', textarea: 'textbox', summary: 'disclosure' };
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['checkbox', 'radio', 'range'].includes(t)) return t;
      if (['submit', 'button', 'reset'].includes(t)) return 'button';
      return 'textbox';
    }
    return map[tag] || '';
  }

  function rectOf(el) {
    try {
      const r = el.getBoundingClientRect();
      if (!r || (!r.width && !r.height)) return null;
      return {
        x: Math.max(0, r.left),
        y: Math.max(0, r.top),
        w: Math.min(r.width, window.innerWidth),
        h: Math.min(r.height, window.innerHeight)
      };
    } catch (e) { return null; }
  }

  function pickTarget(node) {
    if (!node || node.nodeType !== 1) return node;
    const hit = node.closest ? node.closest(INTERACTIVE) : null;
    if (!hit) return node;
    const r = hit.getBoundingClientRect();
    const area = r.width * r.height;
    const vp = window.innerWidth * window.innerHeight;
    // if the hit is a huge container, the original element is more accurate
    if (area > vp * 0.55) return node;
    return hit;
  }

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    return {
      tag: (el.tagName || '').toLowerCase(),
      role: roleOf(el),
      text: labelFor(el),
      name: (el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('name') || '')) || '',
      selector: cssPath(el),
      input_type: (el.tagName === 'INPUT' ? (el.type || 'text') : ''),
      rect: rectOf(el)
    };
  }

  function maskValue(el) {
    const t = (el.type || '').toLowerCase();
    const name = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('autocomplete') || '')).toLowerCase();
    if (t === 'password' || /pass|pwd|secret|token|cvv|card/.test(name)) return '••••••••';
    let v = el.value != null ? String(el.value) : (el.innerText || '');
    return v.replace(/\s+/g, ' ').trim().slice(0, 120);
  }

  /* ---------- sensitive data detection ---------- */

  const RX = [
    ['email', /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g],
    ['id_card', /\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b/g],
    ['phone', /(?<!\d)1[3-9]\d{9}(?!\d)/g],
    ['bank', /(?<!\d)\d{16,19}(?!\d)/g],
    ['ip', /(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)/g],
  ];
  const LABELED_NAME = /(?:姓\s*名|名\s*字|联系人|负责人|申请人|操作人|创建人|员工|学员|讲师|用户名|Name|Owner|Contact|Employee)\s*[:：]\s*([一-龥]{2,4}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})/g;
  const NAME_COL = /姓\s*名|联系人|负责人|操作人|创建人|申请人|用户名|学员|讲师|员工|\bName\b|\bOwner\b|\bContact\b|\bEmployee\b/i;
  const CJK_NAME = /^[一-龥]{2,4}$/;
  const NOT_BANK = /^(?:19|20)\d{2}[01]\d[0-3]\d/;

  const MAX_NODES = 400;
  const MAX_LEN = 160;

  /** Whether this text node lies in a "name" column — judged by the table header, far more accurate than guessing surnames */
  function inNameColumn(el) {
    const td = el && el.closest && el.closest('td');
    if (!td) return false;
    const tr = td.closest('tr');
    if (!tr) return false;
    const idx = Array.prototype.indexOf.call(tr.children, td);
    if (idx < 0) return false;
    const scope = td.closest('.ant-table, .el-table, table') || document;
    const head = scope.querySelector('thead tr:last-child') || scope.querySelector('tr');
    const th = head && head.children[idx];
    return !!th && NAME_COL.test((th.innerText || '').trim());
  }

  function rectsFor(node, start, end) {
    try {
      const r = document.createRange();
      r.setStart(node, start);
      r.setEnd(node, end);
      return Array.from(r.getClientRects());   // text spanning lines returns several rectangles
    } catch (e) { return []; }
  }

  function pushHits(out, node, text, nameCol) {
    const seen = [];
    const add = (kind, s, e, raw) => {
      if (seen.some(([a, b]) => s < b && e > a)) return;
      seen.push([s, e]);
      for (const rc of rectsFor(node, s, e)) {
        if (rc.width < 2 || rc.height < 2) continue;
        out.push({
          x: Math.max(0, rc.left - 3), y: Math.max(0, rc.top - 2),
          w: rc.width + 6, h: rc.height + 4,
          mode: 'blur', kind, label: raw.slice(0, 60), auto: true,
        });
      }
    };
    for (const kw of cfg.keywords) {
      if (kw.length < 2) continue;
      let i = -1;
      const low = text.toLowerCase(), k = kw.toLowerCase();
      while ((i = low.indexOf(k, i + 1)) >= 0) add('keyword', i, i + kw.length, text.substr(i, kw.length));
    }
    for (const [kind, rx] of RX) {
      rx.lastIndex = 0;
      let m;
      while ((m = rx.exec(text))) {
        if (kind === 'bank' && NOT_BANK.test(m[0])) continue;
        add(kind, m.index, m.index + m[0].length, m[0]);
      }
    }
    LABELED_NAME.lastIndex = 0;
    let m;
    while ((m = LABELED_NAME.exec(text))) {
      const g = m[1];
      const s = m.index + m[0].lastIndexOf(g);
      add('name', s, s + g.length, g);
    }
    if (nameCol && CJK_NAME.test(text.trim())) {
      const s = text.indexOf(text.trim());
      add('name', s, s + text.trim().length, text.trim());
    }
  }

  /** Walk the visible text; returns { nodes: text index, hits: automatic redaction boxes } */
  function scanPage() {
    const nodes = [];
    const hits = [];
    if (!cfg.autoRedact && !cfg.indexText) return { nodes, hits };
    const vw = window.innerWidth, vh = window.innerHeight;
    let walker;
    try {
      walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
          const t = n.nodeValue;
          if (!t || !t.trim() || t.length > 400) return NodeFilter.FILTER_REJECT;
          const p = n.parentElement;
          if (!p) return NodeFilter.FILTER_REJECT;
          const tag = p.tagName;
          if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TITLE')
            return NodeFilter.FILTER_REJECT;
          if (p.closest('#vt-overlay')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
    } catch (e) { return { nodes, hits }; }

    let n, scanned = 0;
    while ((n = walker.nextNode()) && scanned < 1500 && nodes.length < MAX_NODES) {
      scanned++;
      const range = document.createRange();
      range.selectNodeContents(n);
      const r = range.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
      const text = n.nodeValue;
      const nameCol = inNameColumn(n.parentElement);
      if (cfg.indexText) {
        nodes.push({
          t: text.trim().slice(0, MAX_LEN),
          x: Math.max(0, r.left), y: Math.max(0, r.top), w: r.width, h: r.height,
          n: nameCol,
        });
      }
      if (cfg.autoRedact) pushHits(hits, n, text, nameCol);
    }

    // values of inputs / selects aren't text nodes; go through them separately
    if (cfg.autoRedact) {
      for (const el of document.querySelectorAll('input,textarea,select')) {
        const v = el.tagName === 'SELECT'
          ? (el.options[el.selectedIndex] || {}).text || ''
          : (el.value || '');
        if (!v || v.length > 200) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || r.bottom <= 0 || r.top >= vh) continue;
        let hit = cfg.keywords.some(k => k.length > 1 && v.toLowerCase().includes(k.toLowerCase()));
        if (!hit) {
          for (const [kind, rx] of RX) {
            rx.lastIndex = 0;
            if (rx.test(v) && !(kind === 'bank' && NOT_BANK.test(v))) { hit = kind; break; }
          }
        }
        if (hit) {
          hits.push({
            x: Math.max(0, r.left - 2), y: Math.max(0, r.top - 2),
            w: r.width + 4, h: r.height + 4,
            mode: 'blur', kind: typeof hit === 'string' ? hit : 'keyword',
            label: v.slice(0, 60), auto: true,
          });
        }
      }
    }
    return { nodes, hits };
  }

  /* ---------- reporting ---------- */

  function send(kind, el, point, value) {
    if (!recording) return;
    const now = Date.now();                   // moment of the click; narrate-while-recording aligns the speech by it
    const sig = kind + '|' + (el ? cssPath(el) : '') + '|' + (value || '');
    if (sig === lastSig && now - lastSentAt < 400) return;   // only drop same-element repeats such as double clicks
    lastSig = sig;
    lastSentAt = now;
    // scan synchronously here: the page may navigate away right after the click
    let scan = { nodes: [], hits: [] };
    try { scan = scanPage(); } catch (e) { /* a broken page structure must not block recording */ }
    const evt = {
      kind,
      url: location.href,
      title: document.title,
      target: el ? describe(el) : null,
      point: point || null,
      value: value || '',
      viewportW: window.innerWidth,
      viewportH: window.innerHeight,
      dpr: window.devicePixelRatio || 1,
      client_ts: now,
      redactions: scan.hits,
      text_nodes: scan.nodes
    };
    try {
      chrome.runtime.sendMessage({ type: 'vt_event', event: evt }, () => void chrome.runtime.lastError);
    } catch (e) { /* extension was just reloaded */ }
  }

  function isOurs(node) {
    return !!(node && node.closest && node.closest('#vt-overlay'));
  }

  const NON_TEXT = ['checkbox', 'radio', 'submit', 'button', 'reset', 'file', 'image',
                    'range', 'color', 'hidden'];

  function isTextField(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea' || tag === 'select') return true;
    if (tag === 'input') return !NON_TEXT.includes((el.type || 'text').toLowerCase());
    return !!el.isContentEditable;
  }

  function fieldValue(el) {
    if (el.isContentEditable) return el.innerText || '';
    if (el.tagName === 'SELECT') return (el.options[el.selectedIndex] || {}).text || '';
    return el.value || '';
  }

  /** If an input's content differs from the last recorded value, record an "input" step. */
  function commitInput(el) {
    if (!recording || !isTextField(el) || isOurs(el)) return false;
    const v = fieldValue(el);
    if (committed.get(el) === v) return false;
    committed.set(el, v);
    if (!String(v).trim()) return false;
    const r = rectOf(el);
    send('input', el, r ? { x: r.x + r.w / 2, y: r.y + r.h / 2 } : null, maskValue(el));
    return true;
  }

  /* ---------- event listeners ---------- */

  document.addEventListener('focusin', (e) => {
    const el = e.target;
    if (recording && isTextField(el) && !committed.has(el)) committed.set(el, fieldValue(el));
  }, true);

  document.addEventListener('mousedown', (e) => {
    if (!recording || e.button !== 0 || isOurs(e.target)) return;
    const el = pickTarget(e.target);
    const active = document.activeElement;
    if (active && active !== el && !(el && el.contains && el.contains(active)) && isTextField(active)) {
      commitInput(active);
    }
    send('click', el, { x: e.clientX, y: e.clientY });
  }, true);

  document.addEventListener('change', (e) => {
    if (!recording || isOurs(e.target)) return;
    const t = (e.target.type || '').toLowerCase();
    if (t === 'checkbox' || t === 'radio') return;          // these are recorded via click
    commitInput(e.target);
  }, true);

  document.addEventListener('keydown', (e) => {
    if (!recording || isOurs(e.target)) return;
    if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;   // Enter that confirms an IME candidate doesn't count
    const el = e.target;
    if (!isTextField(el) || el.tagName === 'SELECT') return;
    // in a multi-line text box a plain Enter is a line break; only Ctrl/⌘+Enter submits
    if ((el.tagName === 'TEXTAREA' || el.isContentEditable) && !(e.ctrlKey || e.metaKey)) return;
    commitInput(el);
    const r = rectOf(el);
    send('key', el, r ? { x: r.x + r.w / 2, y: r.y + r.h / 2 } : null, 'Enter');
  }, true);

  /* ---------- recording overlay (hidden automatically for screenshots) ---------- */

  function ensureOverlay() {
    if (overlay && document.body.contains(overlay)) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'vt-overlay';
    overlay.innerHTML = `
      <span class="vt-dot"></span>
      <span class="vt-label"></span>
      <span class="vt-count">0</span>
      <button class="vt-btn" id="vt-snap">＋</button>
      <button class="vt-btn vt-stop" id="vt-stop"></button>
    `;
    labelOverlay(overlay);
    // translations come from the background (content scripts can't read the extension's files); re-apply the text once received
    chrome.runtime.sendMessage({ type: 'vt_i18n' }, (r) => {
      if (chrome.runtime.lastError || !r || !r.lang) return;
      VTI18N.use(r.lang, r.dict);
      if (overlay) labelOverlay(overlay);
    });
    (document.body || document.documentElement).appendChild(overlay);
    overlay.querySelector('#vt-stop').addEventListener('click', (ev) => {
      ev.stopPropagation();
      chrome.runtime.sendMessage({ type: 'vt_stop' }, () => void chrome.runtime.lastError);
    });
    overlay.querySelector('#vt-snap').addEventListener('click', (ev) => {
      ev.stopPropagation();
      chrome.runtime.sendMessage({
        type: 'vt_manual',
        event: { url: location.href, title: document.title, viewportW: innerWidth, viewportH: innerHeight, dpr: devicePixelRatio }
      }, () => void chrome.runtime.lastError);
    });
    return overlay;
  }

  function labelOverlay(o) {
    o.querySelector('.vt-label').textContent = VTI18N.t('录制中');
    o.querySelector('#vt-snap').title = VTI18N.t('手动补一张截图（Alt+Shift+S）');
    o.querySelector('#vt-stop').title = VTI18N.t('停止录制');
    o.querySelector('#vt-stop').textContent = VTI18N.t('停止');
  }

  function renderOverlay() {
    if (!recording) {
      if (overlay) overlay.remove();
      overlay = null;
      return;
    }
    const o = ensureOverlay();
    o.querySelector('.vt-count').textContent = String(stepCount);
    o.classList.toggle('vt-invisible', overlayHidden);
  }

  function flash() {
    if (!overlay) return;
    overlay.classList.add('vt-flash');
    setTimeout(() => overlay && overlay.classList.remove('vt-flash'), 450);
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === 'vt_scan') {
      applyCfg(msg.cfg);
      let scan = { nodes: [], hits: [] };
      try { scan = scanPage(); } catch (e) { /* ignore */ }
      sendResponse(scan);
    } else if (msg.type === 'vt_overlay') {
      overlayHidden = !msg.show;
      renderOverlay();
      sendResponse({ ok: true });
    } else if (msg.type === 'vt_recording') {
      recording = !!msg.recording;
      if (typeof msg.stepCount === 'number') stepCount = msg.stepCount;
      applyCfg(msg.cfg);
      renderOverlay();
      if (msg.flash) flash();
      sendResponse({ ok: true });
    }
    return true;
  });

  // sync the state once when the page opens
  try {
    chrome.runtime.sendMessage({ type: 'vt_state' }, (s) => {
      if (chrome.runtime.lastError || !s) return;
      recording = !!s.recording;
      stepCount = s.stepCount || 0;
      applyCfg(s);
      renderOverlay();
    });
  } catch (e) { /* ignore */ }
})();
