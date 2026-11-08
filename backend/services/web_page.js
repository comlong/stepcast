/* StepCast: injected into an HTML deck before the deck's own scripts run (DevTools Page.addScriptToEvaluateOnNewDocument),
   inside the headless browser StepCast started for the import / the recording. Nothing here touches the deck's files or the network.

   1. Virtual time. Date, performance.now, setTimeout / setInterval, requestAnimationFrame, requestIdleCallback, CSS animations and
      transitions, Web Animations and <video> follow a clock that only moves when StepCast moves it. Every recorded frame is then
      exactly 1/fps after the one before, however long a screenshot takes, and the same deck always gives the same video.
   2. Deck helpers: find the slides, their notes and the controls on them; tell which slide is showing; hide the presenter's chrome
      around the slide; a pointer and a click ring drawn into the page. */
(() => {
  'use strict';
  if (window.__sc) return;
  const W = window;
  // A frame inside the page. One of the deck's own frames follows the page's clock; another site's (a player) can't be reached and
  // keeps running by itself
  let host = null;
  if (W !== W.top) {
    try { host = W.parent.__sc; } catch (e) { return; }
    if (!host) return;
  }
  const CONFIG = W.__scConfig || {};
  try { delete W.__scConfig; } catch (e) { W.__scConfig = undefined; }

  if (CONFIG.freshStorage && !host) {   // every load starts like a new visitor: a deck that remembers the last slide can't jump away
    try { W.localStorage.clear(); } catch (e) { /* storage may be unavailable */ }
    try { W.sessionStorage.clear(); } catch (e) { /* idem */ }
  }
  // a dialog would stop the page until someone answers it, and nobody is there to
  W.alert = function alert() {};
  W.confirm = function confirm() { return true; };
  W.prompt = function prompt(message, value) { return value == null ? '' : String(value); };
  W.print = function print() {};
  // View transitions are run by the browser in real time, outside the virtual clock below: a deck that changes slides through one
  // (Marp …) would show the old slide for an unknown while. Without the feature, decks change slides at once, as they do in browsers
  // that don't have it (StepCast puts its own transition between slides)
  try { if (W.Document && 'startViewTransition' in W.Document.prototype) delete W.Document.prototype.startViewTransition; } catch (e) { /* ignore */ }

  // ------------------------------------------------------------------ virtual clock
  const RealDate = W.Date;
  const realStart = host ? host.realStart : RealDate.now();
  const perf = W.performance;
  const realPerfNow = perf.now.bind(perf);
  const perfStart = realPerfNow();
  const nativeRaf = W.requestAnimationFrame.bind(W);
  const nativeSetTimeout = W.setTimeout.bind(W);
  const nativeClearTimeout = W.clearTimeout.bind(W);
  const nativeFetch = W.fetch ? W.fetch.bind(W) : null;
  const MediaProto = W.HTMLMediaElement && W.HTMLMediaElement.prototype;
  const realPause = MediaProto && MediaProto.pause;
  const pausedDesc = MediaProto && Object.getOwnPropertyDescriptor(MediaProto, 'paused');

  let now = host ? host.now : 0;     // virtual milliseconds since this script was installed (a frame goes by its page's clock)
  let inCallback = false;
  const timers = new Map();          // id -> { due, fn, args, every }
  let timerSeq = 1;
  const rafs = new Map();
  let rafSeq = 1;
  const stats = { timers: 0, rafs: 0, mutations: 0, scrolls: 0, errors: [] };

  function report(e) {
    if (stats.errors.length < 20) stats.errors.push(String((e && (e.stack || e.message)) || e).slice(0, 300));
  }

  function addTimer(fn, ms, args, every) {
    if (typeof fn !== 'function') {
      const code = String(fn);
      fn = () => (0, eval)(code);
    }
    let d = Number(ms);
    if (!(d > 0)) d = 0;
    if (inCallback && d < 1) d = 1;   // a timer that keeps re-arming itself with 0 ms can't hold the clock still
    const id = timerSeq++;
    timers.set(id, { due: now + d, fn, args, every: every ? Math.max(1, d) : 0 });
    return id;
  }
  W.setTimeout = function setTimeout(fn, ms, ...args) { return addTimer(fn, ms, args, false); };
  W.setInterval = function setInterval(fn, ms, ...args) { return addTimer(fn, ms, args, true); };
  W.clearTimeout = function clearTimeout(id) { timers.delete(id); };
  W.clearInterval = function clearInterval(id) { timers.delete(id); };
  W.requestAnimationFrame = function requestAnimationFrame(cb) {
    const id = rafSeq++;
    rafs.set(id, cb);
    return id;
  };
  W.cancelAnimationFrame = function cancelAnimationFrame(id) { rafs.delete(id); };
  W.requestIdleCallback = function requestIdleCallback(cb) {
    return addTimer(() => cb({ didTimeout: false, timeRemaining: () => 40 }), 1, [], false);
  };
  W.cancelIdleCallback = function cancelIdleCallback(id) { timers.delete(id); };

  function VDate(...a) {
    if (!new.target) return new RealDate(realStart + now).toString();
    return Reflect.construct(RealDate, a.length ? a : [realStart + now], new.target);
  }
  VDate.prototype = RealDate.prototype;
  VDate.now = function now_() { return Math.floor(realStart + now); };
  VDate.parse = RealDate.parse;
  VDate.UTC = RealDate.UTC;
  W.Date = VDate;
  perf.now = function now_() { return perfStart + now; };

  // microtasks (promise callbacks) queued by one timer run before the next, as they would between two real timers
  const channel = new MessageChannel();
  const waiting = [];
  channel.port1.onmessage = () => { const w = waiting.shift(); if (w) w(); };
  function drain() { return new Promise((r) => { waiting.push(r); channel.port2.postMessage(0); }); }

  async function runTimers(to) {
    let guard = 0;
    for (;;) {
      let best = null;
      let bestId = 0;
      for (const [id, t] of timers) {
        if (t.due <= to && (!best || t.due < best.due || (t.due === best.due && id < bestId))) { best = t; bestId = id; }
      }
      if (!best || ++guard > 50000) break;
      now = Math.max(now, best.due);
      if (best.every) best.due += best.every; else timers.delete(bestId);
      inCallback = true;
      try { best.fn.apply(W, best.args); } catch (e) { report(e); }
      inCallback = false;
      stats.timers++;
      await drain();
    }
    now = Math.max(now, to);
  }

  async function runRafs() {
    if (!rafs.size) return 0;
    const list = [...rafs.values()];
    rafs.clear();
    const ts = perfStart + now;
    inCallback = true;
    for (const cb of list) { try { cb(ts); } catch (e) { report(e); } }
    inCallback = false;
    stats.rafs += list.length;
    await drain();
    return list.length;
  }

  // ---- CSS animations / transitions / Web Animations: paused, and seeked to the virtual time every frame
  const driven = new Map();          // Animation -> { start, page }  (page: paused by the deck itself, left alone while it stays so)
  const released = new WeakSet();    // finished or cancelled: never taken over again

  function scanAnimations(at) {
    let list;
    try { list = document.getAnimations(); } catch (e) { return 0; }
    let added = 0;
    for (const a of list) {
      if (driven.has(a) || released.has(a)) continue;
      if (a.timeline && a.timeline !== document.timeline) { released.add(a); continue; }   // scroll-driven: follows scrolling
      if (a.playState === 'finished') { released.add(a); continue; }
      if (a.playState === 'paused' && !a.pending) { driven.set(a, { start: at, page: true }); continue; }
      driven.set(a, { start: at, page: false });
      try { a.pause(); a.currentTime = 0; } catch (e) { /* ignore */ }
      added++;
    }
    return added;
  }

  function seekAnimations(at) {
    let moved = 0;
    for (const [a, st] of driven) {
      if (st.page) {
        if (a.playState !== 'running') continue;
        st.page = false;               // the deck started it again (e.g. animation-play-state changed): from here on it follows the clock
        st.start = at - (a.currentTime || 0) / (a.playbackRate || 1);
        try { a.pause(); } catch (e) { /* ignore */ }
      }
      if (a.playState === 'idle') { driven.delete(a); released.add(a); continue; }   // cancelled: the style changed back, the element went
      let timing = null;
      try { timing = a.effect ? a.effect.getComputedTiming() : null; } catch (e) { timing = null; }
      const end = timing && timing.endTime != null ? timing.endTime : Infinity;
      const rate = a.playbackRate || 1;
      const t = Math.max(0, (at - st.start) * Math.abs(rate));
      const before = a.currentTime;
      if (rate > 0 && Number.isFinite(end) && t >= end) {
        driven.delete(a);
        released.add(a);
        try { a.finish(); } catch (e) { try { a.currentTime = end; } catch (e2) { /* ignore */ } }   // ends as it would: animationend etc.
        moved++;
        continue;
      }
      try { a.currentTime = rate > 0 ? t : Math.max(0, (Number.isFinite(end) ? end : 0) - t); } catch (e) { /* ignore */ }
      const delay = timing ? timing.delay || 0 : 0;
      if (before !== a.currentTime && (t >= delay || (before || 0) >= delay)) moved++;
    }
    return moved;
  }

  // ---- <video> / <audio>: never really playing; their time follows the clock
  const media = new Map();           // element -> { start, from, playing }
  const seeks = [];
  function mediaPlaying(el) { const m = media.get(el); return !!(m && m.playing); }
  function fire(el, type) { try { el.dispatchEvent(new Event(type)); } catch (e) { /* ignore */ } }
  if (MediaProto) {
    MediaProto.play = function play() {
      if (!mediaPlaying(this)) {
        const from = this.currentTime || 0;
        media.set(this, { start: now, from, from0: from, playing: true });
        fire(this, 'play');
        fire(this, 'playing');
      }
      try { realPause.call(this); } catch (e) { /* ignore */ }
      return Promise.resolve();
    };
    MediaProto.pause = function pause() {
      const m = media.get(this);
      if (m && m.playing) { m.playing = false; fire(this, 'pause'); }
      return realPause.call(this);
    };
    if (pausedDesc && pausedDesc.get) {
      Object.defineProperty(MediaProto, 'paused', {
        configurable: true,
        get() { return media.has(this) ? !mediaPlaying(this) : pausedDesc.get.call(this); },
      });
    }
  }
  function syncMedia(at) {
    let playing = 0;
    try {
      document.querySelectorAll('video[autoplay], audio[autoplay]').forEach((el) => {
        if (!media.has(el)) media.set(el, { start: at, from: 0, from0: 0, playing: true });   // from the beginning, as when the page opens
      });
    } catch (e) { /* ignore */ }
    for (const [el, m] of media) {
      if (!m.playing || !el.isConnected) continue;
      playing++;
      try { if (!el.paused || pausedDesc) realPause.call(el); } catch (e) { /* ignore */ }
      const dur = el.duration;
      let pos = m.from + (at - m.start) / 1000 * (el.playbackRate || 1);
      if (Number.isFinite(dur) && dur > 0 && pos >= dur) {
        if (el.loop) pos %= dur;
        else { pos = dur; m.playing = false; fire(el, 'ended'); }
      }
      if (el.tagName === 'VIDEO' && Math.abs((el.currentTime || 0) - pos) > 0.0005) {
        seeks.push(new Promise((r) => {
          const done = () => { el.removeEventListener('seeked', done); r(); };
          el.addEventListener('seeked', done);
          setTimeoutReal(done, 1500);
          try { el.currentTime = pos; } catch (e) { done(); }
        }));
      } else {
        try { el.currentTime = pos; } catch (e) { /* ignore */ }
      }
    }
    return playing;
  }
  function setTimeoutReal(fn, ms) {     // a real wait (for a video frame to decode): the native timer from a fresh frame
    const start = realPerfNow();
    const tick = () => { if (realPerfNow() - start >= ms) fn(); else nativeRaf(tick); };
    nativeRaf(tick);
  }
  function within(promise, ms) {        // the promise's value, or 'late' after ms real milliseconds
    return new Promise((res) => {
      const t = nativeSetTimeout(() => res('late'), ms);
      Promise.resolve(promise).then((v) => { nativeClearTimeout(t); res(v); }, () => { nativeClearTimeout(t); res('late'); });
    });
  }

  // ---- SVG's own animations (<animate> …): each drawing's clock is paused and set to the virtual time
  const smil = new Map();            // outermost <svg> -> virtual time its clock started
  function seekSmil(at) {
    let nodes;
    try { nodes = document.querySelectorAll('animate, animateTransform, animateMotion, animateColor, set'); } catch (e) { return 0; }
    if (!nodes.length) return 0;
    const roots = new Set();
    nodes.forEach((a) => {
      let s = a.ownerSVGElement;
      while (s && s.ownerSVGElement) s = s.ownerSVGElement;
      if (s) roots.add(s);
    });
    let n = 0;
    for (const s of roots) {
      if (!smil.has(s)) {
        smil.set(s, at);
        try { s.pauseAnimations(); } catch (e) { /* ignore */ }
      }
      try { s.setCurrentTime(Math.max(0, (at - smil.get(s)) / 1000)); } catch (e) { continue; }
      if (inStage(s)) n++;
    }
    return n;
  }

  // ---- pictures that move by themselves (GIF, animated WebP / PNG). The browser plays them by its own clock; taken over here, the
  // picture is swapped frame by frame on the virtual one. One that can't be taken over plays on: every frame is captured then.
  const animated = new Map();        // <img> -> { urls, ends, total, loops, start, shown }
  let animatedFree = false;
  const MAX_ANIM_FRAMES = 600;
  const MAX_ANIM_PIXELS = 150e6;

  function sniffImage(b) {
    if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return 'image/gif';
    if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e) return 'image/png';
    if (b[0] === 0x52 && b[1] === 0x49 && b[8] === 0x57 && b[9] === 0x45) return 'image/webp';
    return '';
  }
  async function takeOverImage(img) {
    if (animated.has(img)) return;
    const u = img.currentSrc || img.src || '';
    if (!/^data:image\/(gif|webp|a?png)|\.(gif|webp|a?png)([?#]|$)/i.test(u)) return;
    const looksAnimated = /gif/i.test(u);
    if (!W.ImageDecoder || !nativeFetch) { if (looksAnimated) animatedFree = true; return; }
    let decoder = null;
    try {
      const buf = await (await nativeFetch(u)).arrayBuffer();
      const type = sniffImage(new Uint8Array(buf.slice(0, 12)));
      if (!type || !(await W.ImageDecoder.isTypeSupported(type))) { if (looksAnimated) animatedFree = true; return; }
      decoder = new W.ImageDecoder({ data: buf, type, preferAnimation: true });
      await decoder.completed;
      await decoder.tracks.ready;
      const track = decoder.tracks.selectedTrack;
      if (!track || !track.animated || track.frameCount < 2) return;
      const count = Math.min(track.frameCount, MAX_ANIM_FRAMES);
      const urls = [];
      const ends = [];
      let total = 0;
      let pixels = 0;
      for (let i = 0; i < count; i++) {
        const { image } = await decoder.decode({ frameIndex: i });
        pixels += image.displayWidth * image.displayHeight;
        if (pixels > MAX_ANIM_PIXELS) { image.close(); animatedFree = true; urls.forEach((x) => URL.revokeObjectURL(x)); return; }
        const c = new W.OffscreenCanvas(image.displayWidth, image.displayHeight);
        c.getContext('2d').drawImage(image, 0, 0);
        urls.push(URL.createObjectURL(await c.convertToBlob({ type: 'image/png' })));
        let d = (image.duration || 0) / 1000;
        if (d <= 10) d = 100;            // as browsers show a frame without a delay
        total += d;
        ends.push(total);
        image.close();
      }
      const loops = Number.isFinite(track.repetitionCount) ? track.repetitionCount + 1 : Infinity;
      if (img.parentElement && img.parentElement.tagName === 'PICTURE') {
        img.parentElement.querySelectorAll('source').forEach((s) => s.remove());
      }
      img.removeAttribute('srcset');
      animated.set(img, { urls, ends, total, loops, start: now, shown: -1 });
    } catch (e) {
      if (looksAnimated) animatedFree = true;
    } finally {
      try { if (decoder) decoder.close(); } catch (e) { /* ignore */ }
    }
  }
  function seekImages(at) {
    let moved = 0;
    for (const [img, st] of animated) {
      if (!img.isConnected) continue;
      let t = Math.max(0, at - st.start);
      t = st.loops !== Infinity && t >= st.total * st.loops ? st.total - 0.001 : t % st.total;
      let k = st.ends.findIndex((e) => t < e);
      if (k < 0) k = st.ends.length - 1;
      if (k === st.shown) continue;
      st.shown = k;
      img.src = st.urls[k];
      seeks.push(within(img.decode(), 800));
      moved++;
    }
    return moved;
  }
  function imgsOf(root) {
    const imgs = [...root.querySelectorAll('img')];
    imgs.forEach((i) => { if (i.loading === 'lazy') i.loading = 'eager'; });   // on the page at once, wherever it is
    return imgs;
  }
  function imagesLoaded(imgs, ms) {
    return within(Promise.all(imgs.filter((i) => !i.complete).map((i) => new Promise((r) => {
      i.addEventListener('load', r, { once: true });
      i.addEventListener('error', r, { once: true });
    }))), ms);
  }

  // ---- the page's frames (<iframe> of the deck's own files): their clocks move with this one
  const children = new Set();        // { frame, alive, tick, hold, restart, prepare }

  // ---- what changed in a frame (nothing changed: StepCast reuses the previous screenshot)
  let stage = null;                  // the element recorded; changes outside it don't count
  let ui = null;
  function inStage(node) {
    if (ui && (node === ui || ui.contains(node))) return false;
    if (!stage || !stage.isConnected) return true;
    const el = node.nodeType === 1 ? node : node.parentElement;
    return !el || stage.contains(el) || el.contains(stage);
  }
  const observer = new MutationObserver((recs) => {
    for (const r of recs) { if (inStage(r.target)) { stats.mutations++; return; } }
  });
  observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  W.addEventListener('scroll', (e) => { if (!e.target || e.target === document || inStage(e.target)) stats.scrolls++; }, true);

  function hasCanvas() {
    const root = stage && stage.isConnected ? stage : document;
    try { return !!root.querySelector('canvas'); } catch (e) { return false; }
  }

  // Until StepCast starts the clock, animations that appear are held at their first frame (the page loads in real time)
  let holding = true;
  function hold() {
    if (holding) { scanAnimations(now); seekSmil(now); }
    nativeRaf(hold);
  }
  nativeRaf(hold);
  document.addEventListener('transitionrun', () => { if (holding) scanAnimations(now); }, true);
  document.addEventListener('animationstart', () => { if (holding) scanAnimations(now); }, true);

  async function step(to) {
    await runTimers(to);
    now = Math.max(now, to);
    const r = await runRafs();
    const playing = syncMedia(now);
    scanAnimations(now);              // animations created by the callbacks start now
    const moved = seekAnimations(now);
    const svgs = seekSmil(now);
    const images = seekImages(now);
    let kids = 0;
    for (const c of children) {
      if (!c.alive()) { children.delete(c); continue; }
      const res = await within(c.tick(now), 1500);
      if (res === 'late') children.delete(c);
      else if (res) kids++;
    }
    return { raf: r, playing, moved, svgs, images, kids };
  }

  // Moves the clock to `to`; true if something on the page changed (else the last picture can be used again)
  async function moveTo(to) {
    const m0 = stats.mutations;
    const s0 = stats.scrolls;
    const t0 = stats.timers;
    const r = await step(Math.max(now, to));
    if (seeks.length) await Promise.all(seeks.splice(0));
    const canvas = (r.raf > 0 || stats.timers !== t0) && hasCanvas();
    return r.moved > 0 || stats.mutations !== m0 || stats.scrolls !== s0 || canvas || r.playing > 0 || r.svgs > 0 || r.images > 0
      || r.kids > 0 || animatedFree;
  }

  // ------------------------------------------------------------------ pointer and click ring
  let cursor = null;
  let ring = null;
  let spot = null;
  let uiKey = '';
  function ensureUi() {
    if (ui && ui.isConnected) return;
    ui = document.createElement('div');
    ui.id = '__sc_ui';
    ui.setAttribute('aria-hidden', 'true');
    ui.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;overflow:visible;pointer-events:none;z-index:2147483647;'
      + 'contain:layout style;';
    spot = document.createElement('div');
    spot.style.cssText = 'position:fixed;left:0;top:0;box-sizing:border-box;opacity:0;pointer-events:none;';
    ui.appendChild(spot);
    ring = document.createElement('div');
    ring.style.cssText = 'position:fixed;left:0;top:0;border-radius:50%;box-sizing:border-box;opacity:0;pointer-events:none;';
    cursor = document.createElement('div');
    cursor.style.cssText = 'position:fixed;left:0;top:0;opacity:0;pointer-events:none;filter:drop-shadow(0 2px 3px rgba(0,0,0,.35));';
    cursor.innerHTML = '<svg viewBox="0 0 24 34" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg">'
      + '<path d="M2.5 2.5 L2.5 27 L8.6 21.3 L12.9 31.2 L17.2 29.3 L12.9 19.6 L21 19.6 Z" fill="#fff" stroke="#111" '
      + 'stroke-width="1.7" stroke-linejoin="round"/></svg>';
    ui.appendChild(ring);
    ui.appendChild(cursor);
    (document.documentElement || document).appendChild(ui);
  }
  function applyUi(s) {
    const key = JSON.stringify(s || null);
    if (key === uiKey) return false;
    uiKey = key;
    if (!s) { if (ui) { cursor.style.opacity = '0'; ring.style.opacity = '0'; spot.style.opacity = '0'; } return true; }
    ensureUi();
    if (s.f) {                       // the highlight: the piece framed, the rest of the slide a little darker
      const f = s.f;
      spot.style.width = f.w + 'px';
      spot.style.height = f.h + 'px';
      spot.style.transform = `translate(${f.x}px, ${f.y}px)`;
      spot.style.borderRadius = f.r + 'px';
      spot.style.border = `${f.b}px solid ${f.c}`;
      spot.style.boxShadow = `0 0 0 9999px rgba(12,16,28,${f.d}), 0 0 ${f.g}px ${f.c}`;
      spot.style.opacity = String(f.o);
    } else {
      spot.style.opacity = '0';
    }
    if (s.x == null) { cursor.style.opacity = '0'; ring.style.opacity = '0'; return true; }
    const size = s.size || 24;
    cursor.style.width = (size * 24 / 34) + 'px';
    cursor.style.height = size + 'px';
    cursor.style.transform = `translate(${s.x - size * 0.07}px, ${s.y - size * 0.07}px)`;
    cursor.style.opacity = String(s.o == null ? 1 : s.o);
    if (s.ring) {
      const r = s.ring;
      const d = r.d * (0.25 + 0.75 * r.p);
      ring.style.width = ring.style.height = d + 'px';
      ring.style.border = `${Math.max(2, r.d * 0.07)}px solid ${r.color || '#ff5c39'}`;
      ring.style.background = (r.fill || 'rgba(255,92,57,.18)');
      ring.style.transform = `translate(${r.x - d / 2}px, ${r.y - d / 2}px)`;
      ring.style.opacity = String(Math.max(0, 1 - r.p));
    } else {
      ring.style.opacity = '0';
    }
    return true;
  }

  // ------------------------------------------------------------------ deck helpers
  const SKIP = /^(SCRIPT|STYLE|TEMPLATE|LINK|META|NOSCRIPT|BR|HR)$/;
  const NOTE_ATTRS = ['data-notes', 'data-note', 'data-speaker-notes', 'data-speaker-note', 'data-script', 'data-narration',
    'data-voiceover', 'data-voice-over', 'data-say', 'data-transcript'];
  const NOTE_SEL = 'aside.notes, .notes, .speaker-notes, .speaker-note, .speakernotes, .slide-notes, .presenter-notes, '
    + '[data-notes]:not(section), script[type="text/notes"], script[type="text/x-notes"], template.notes, .bespoke-marp-note';
  // notes named by a class of their own (remark-slide-notes, slide-note, deck-notes …): hidden, or named as the slide's / speaker's
  const NOTE_CLASS_RX = /(^|[-_])notes$|[-_]note$/i;
  const NOTE_NAMED_RX = /(speaker|presenter|slide)[-_]?notes?$/i;
  const CHROME_RX = /(^|[-_\s])(nav|navi|navigation|controls?|progress|pager|pagination|dots|bullets|counter|toolbar|topbar|hint|notes?|speaker|timer|menu|arrows?|prev|next|thumbs?|thumbnails?|sidebar|overview|help)([-_\s]|$)/i;
  const ACTIVE_RX = /(^|[-_])(active|current|present|visible|show|shown|on|is-active|is-current|selected|in|enter|entered)([-_]|$)/i;
  const START_RX = /^[▶►▷→\s]*(开始|开始演示|开始播放|开始讲解|点击开始|点此开始|进入|进入演示|start|start presentation|start now|begin|enter|play|launch|get started|commencer|démarrer|starten|beginnen|iniciar|empezar|comenzar|inizia|avvia|rozpocznij)[▶►▷→\s]*$/i;  // i18n: ignore

  function norm(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  function cssOf(el) { try { return getComputedStyle(el); } catch (e) { return null; } }

  function shown(el) {               // displayed at all (may be scrolled away)
    if (!el || !el.isConnected || el.nodeType !== 1) return false;
    const cs = cssOf(el);
    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const ps = cssOf(p);
      if (ps && Number(ps.opacity) < 0.02) return false;
    }
    return true;
  }

  function onScreen(el) {            // the part of it inside the window, in square CSS pixels (0 when hidden)
    if (!shown(el)) return 0;
    const r = el.getBoundingClientRect();
    const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0);
    const h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    return w > 0 && h > 0 ? w * h : 0;
  }

  function words(el) { return [...(el.classList || [])].concat(el.id ? [el.id] : []); }
  function slideish(el) {
    return words(el).some((t) => /slide|^page|-page|_page|scene|screen/i.test(t))
      || el.hasAttribute('data-slide') || el.hasAttribute('data-page') || el.hasAttribute('data-stepcast-slide');
  }

  function groups() {
    const out = [];
    const all = document.body ? document.body.querySelectorAll('*') : [];
    const parents = new Set();
    for (let i = 0; i < all.length && i < 20000; i++) if (all[i].childElementCount >= 2) parents.add(all[i]);
    if (document.body && document.body.childElementCount >= 2) parents.add(document.body);
    // names that say "slide", numbers ignored (slide-1, slide-2 are one kind of thing); a name only one child has (slide-intro next to
    // plain "slide") doesn't set it apart from the others
    const namesOf = (k) => [...new Set(words(k).filter((t) => /slide|page|scene|screen/i.test(t) && !CHROME_RX.test(t))
      .map((t) => t.replace(/\d+/g, '#')))];
    for (const p of parents) {
      const bySig = new Map();
      const kids = [...p.children].filter((k) => !SKIP.test(k.tagName) && !(k.id || '').startsWith('__sc'));
      const seen = new Map();
      for (const k of kids) for (const t of namesOf(k)) seen.set(t, (seen.get(t) || 0) + 1);
      for (const k of kids) {
        const sw = namesOf(k).filter((t) => seen.get(t) >= 2).sort().join('.');
        const marked = k.hasAttribute('data-stepcast-slide') || k.hasAttribute('data-slide') || k.hasAttribute('data-page');
        if (!sw && !marked && k.tagName !== 'SECTION') continue;
        const sig = k.tagName + '|' + sw + (marked ? '|m' : '');
        if (!bySig.has(sig)) bySig.set(sig, []);
        bySig.get(sig).push(k);
      }
      for (const [sig, members] of bySig) if (members.length >= 2) out.push({ parent: p, members, sig });
    }
    return out;
  }

  function stackedVertically(members) {
    let prev = -Infinity;
    for (const m of members) {
      const r = m.getBoundingClientRect();
      const top = r.top + scrollY;
      if (r.height < innerHeight * 0.4 || top < prev - 2) return false;
      prev = top + r.height * 0.5;
    }
    return true;
  }

  function judge(g) {
    const va = innerWidth * innerHeight;
    const vis = g.members.filter(shown);
    const on = g.members.map(onScreen);
    let s = Math.min(g.members.length, 60);
    if (g.sig.includes('|m') || /slide/i.test(g.sig)) s += 30;
    else if (/page|scene|screen/i.test(g.sig)) s += 14;
    else s += 6;                                         // plain <section>s
    const best = Math.max(...on);
    if (vis.length === 1 || (vis.length > 1 && on.filter((a) => a > va * 0.05).length === 1 && !stackedVertically(g.members))) {
      g.mode = 'one';
      s += best >= va * 0.15 ? 26 + 10 * best / va : -30;
    } else if (vis.length === g.members.length && stackedVertically(g.members)) {
      g.mode = 'scroll';
      s += 16;
    } else {
      g.mode = 'many';
      s -= 30;                                           // several side by side: a grid of cards, not slides
    }
    return s;
  }

  let deck = null;                   // { kind, members, mode, parent, adapter }

  function revealApi() {
    const R = W.Reveal;
    return R && typeof R.getTotalSlides === 'function' && typeof R.slide === 'function' ? R : null;
  }
  function showerApi() {
    const S = W.shower;
    try {
      return S && typeof S.goTo === 'function' && Array.isArray(S.slides) && S.slides.length && S.slides[0] && S.slides[0].element
        ? S : null;
    } catch (e) { return null; }
  }
  function showerSteps(el) {         // a Shower slide's inner steps (shown one per "next"), and how many are shown already
    const S = showerApi();
    const o = (S && S.options) || {};
    const steps = [...el.querySelectorAll(o.stepSelector || '.next')];
    const on = (s) => s.classList.contains(o.activeSlideClass || 'active') || s.classList.contains(o.visitedSlideClass || 'visited');
    return { steps, shown: steps.filter(on).length };
  }

  function findDeck() {
    const R = revealApi();
    if (R) {
      const slides = typeof R.getSlides === 'function' ? R.getSlides()
        : [...document.querySelectorAll('.reveal .slides section')].filter((s) => !s.querySelector('section'));
      if (slides.length) return { kind: 'reveal', members: slides, mode: 'one', parent: document.querySelector('.reveal') };
    }
    // (window.impress is also the element with id="impress" when impress.js itself didn't load)
    if (typeof W.impress === 'function' && document.querySelector('#impress .step')) {
      const steps = [...document.querySelectorAll('#impress .step')];
      return { kind: 'impress', members: steps, mode: 'one', parent: document.getElementById('impress') };
    }
    const S = showerApi();
    if (S) {                         // Shower starts as a list of all slides: one at a time is its full mode
      try { if (!S.isFullMode && typeof S.enterFullMode === 'function') S.enterFullMode(); } catch (e) { /* ignore */ }
      return { kind: 'shower', members: S.slides.map((s) => s.element), mode: 'one', parent: S.container || document.body };
    }
    const sw = document.querySelector('.swiper, .swiper-container');
    if (sw && sw.swiper && sw.swiper.slides && sw.swiper.slides.length > 1) {
      try { if (sw.swiper.autoplay && sw.swiper.autoplay.stop) sw.swiper.autoplay.stop(); } catch (e) { /* ignore */ }   // it would turn on by itself
      return { kind: 'swiper', members: [...sw.swiper.slides], mode: 'one', parent: sw };
    }
    let best = null;
    let bestScore = -Infinity;
    for (const g of groups()) {
      const s = judge(g);
      if (s > bestScore) { best = g; bestScore = s; }
    }
    if (best && bestScore >= 20) return { kind: 'generic', members: best.members, mode: best.mode, parent: best.parent };
    return null;
  }

  function activeIndex() {
    if (!deck) return -1;
    if (deck.kind === 'reveal') {
      const R = revealApi();
      const cur = R && R.getCurrentSlide && R.getCurrentSlide();
      const k = deck.members.indexOf(cur);
      if (k >= 0) return k;
    }
    if (deck.kind === 'swiper') {
      const sw = deck.parent.swiper;
      if (sw) return sw.activeIndex;
    }
    if (deck.kind === 'shower') {
      const S = showerApi();
      if (S && S.activeSlideIndex >= 0) return S.activeSlideIndex;
    }
    if (deck.kind === 'impress') {   // its neighbours are on the canvas too, and may look bigger
      const k = deck.members.findIndex((m) => m.classList.contains('active'));
      if (k >= 0) return k;
    }
    let best = -1;
    let area = 0;
    deck.members.forEach((m, k) => {
      const a = onScreen(m);
      if (a > area + 1) { best = k; area = a; }
    });
    return best;
  }

  function notesText(el) {
    for (const a of NOTE_ATTRS) if (el.hasAttribute && el.hasAttribute(a)) return el.getAttribute(a);
    if (!deck) {                     // the page is one slide: notes on the section holding it
      for (const a of NOTE_ATTRS) {
        const n = el.querySelector('[' + a + ']');
        if (n) return n.getAttribute(a);
      }
    }
    const R = revealApi();
    if (deck && deck.kind === 'reveal' && R && typeof R.getSlideNotes === 'function') {
      try { const n = R.getSlideNotes(el); if (n) return htmlText(n); } catch (e) { /* ignore */ }
    }
    if (deck && deck.kind === 'generic' && document.querySelector('.bespoke-marp-note')) {
      const k = deck.members.indexOf(el);
      const n = document.querySelector(`.bespoke-marp-note[data-index="${k}"]`);
      if (n) return n.innerText || n.textContent;
    }
    for (const n of el.querySelectorAll(NOTE_SEL)) {
      if (n.classList && n.classList.contains('note') && shown(n)) continue;   // a visible "note" box is content
      for (const a of NOTE_ATTRS) if (n.hasAttribute(a)) return n.getAttribute(a);   // data-notes on an element inside
      const t = n.tagName === 'TEMPLATE' ? n.content.textContent : (n.innerText || n.textContent);
      if (norm(t)) return t;
    }
    for (const n of el.querySelectorAll('[class*="note" i]')) {
      if (noteClass(n) && norm(n.textContent)) return n.innerText || n.textContent;
    }
    // notes kept as an HTML comment inside the slide (as Markdown slide tools write them): the last comment that reads like
    // sentences, not like a label or code
    const comments = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_COMMENT);
    for (let c = walker.nextNode(); c; c = walker.nextNode()) comments.push(norm(c.nodeValue));
    for (let k = comments.length - 1; k >= 0; k--) {
      const t = comments[k];
      const cjk = /[぀-ヿ㐀-鿿가-힯]/.test(t);
      if (t.length >= (cjk ? 12 : 30) && !/[<>{};=]|^(todo|fixme|eslint|prettier|#region|#endregion)/i.test(t)) return t;
    }
    return '';
  }
  function htmlText(html) {
    const d = document.createElement('div');
    d.innerHTML = String(html);
    return d.innerText || d.textContent || '';
  }

  function noteClass(n) {
    if (!n.classList) return false;
    for (const c of n.classList) {
      if (NOTE_CLASS_RX.test(c) && (NOTE_NAMED_RX.test(c) || !shown(n))) return true;
    }
    return false;
  }
  function isNoteNode(n) {
    if (n.nodeType !== 1) return false;
    try { return (n.matches(NOTE_SEL) && !(n.classList.contains('note') && shown(n))) || noteClass(n); } catch (e) { return false; }
  }
  function textOf(el) {              // the text as written: CSS may show a heading or a button in capitals (text-transform)
    const cs = cssOf(el);
    if (!cs || !cs.textTransform || cs.textTransform === 'none') return norm(el.innerText || el.textContent);
    let out = '';                    // innerText would give the capitals: the text nodes themselves, a line break as a space
    const walk = (n) => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeType !== 1) return;
      if (n.tagName === 'BR') { out += ' '; return; }
      if (SKIP.test(n.tagName)) return;
      for (const c of n.childNodes) walk(c);
    };
    walk(el);
    return norm(out);
  }
  function visibleText(el) {         // the slide's own text, without its notes
    const parts = [];
    const walk = (n) => {
      if (n.nodeType === 3) { parts.push(n.nodeValue); return; }
      if (n.nodeType !== 1 || SKIP.test(n.tagName) || isNoteNode(n)) return;
      const cs = cssOf(n);
      if (cs && (cs.display === 'none' || cs.visibility === 'hidden')) return;
      const block = cs && /^(block|flex|grid|list-item|table|table-row)$/.test(cs.display);
      if (block) parts.push('\n');
      for (const c of n.childNodes) walk(c);
      if (block) parts.push('\n');
    };
    walk(el);
    return parts.join('').replace(/[ \t ]+/g, ' ').replace(/ *\n[\s]*/g, '\n').trim();
  }
  function titleOf(el) {
    let hidden = '';                 // the first heading on screen; else the first one at all (a heading only screen readers get)
    for (const h of el.querySelectorAll('h1, h2, h3, [role=heading]')) {
      if (isNoteNode(h)) continue;
      const t = textOf(h);
      if (!t) continue;
      if (shown(h)) return t.slice(0, 120);
      if (!hidden) hidden = t;
    }
    return hidden.slice(0, 120);
  }

  // ---- controls: what a click can do on a slide
  const CTRL = 'button, [role=button], [role=tab], [role=switch], [role=checkbox], [role=radio], [role=menuitem], [role=option], '
    + 'a[href], input:not([type=hidden]), select, textarea, summary, label[for], [onclick], [tabindex]:not([tabindex="-1"]), [data-action]';
  function pathOf(el, root) {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let n = el; n && n !== root; n = n.parentElement) {
      let k = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) k++;
      parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + k + ')');
    }
    return ':scope > ' + parts.join(' > ');
  }
  function groupOf(el, root) {
    const p = el.parentElement;
    const cls = [...el.classList].filter((c) => !ACTIVE_RX.test(c) && !/^(sel|done|hover|focus|disabled)$/i.test(c)).sort().join('.');
    return (p && p !== root ? pathOf(p, root) : ':scope') + '>' + el.tagName.toLowerCase() + '.' + cls;
  }
  function controlsOf(root) {
    const cand = new Set();
    root.querySelectorAll(CTRL).forEach((el) => cand.add(el));
    root.querySelectorAll('*').forEach((el) => {
      const cs = cssOf(el);
      if (cs && cs.cursor === 'pointer') cand.add(el);
    });
    const rr = root.getBoundingClientRect();
    const out = [];
    for (const el of cand) {
      if (!shown(el) || isNoteNode(el) || el.closest('#__sc_ui')) continue;
      if (el.disabled) continue;
      let inner = false;
      for (let p = el.parentElement; p && p !== root; p = p.parentElement) if (cand.has(p)) { inner = true; break; }
      if (inner) continue;
      if (el.tagName === 'A') {
        const h = el.getAttribute('href') || '';
        if (!h.startsWith('#') || /^#(\/|\d|slide|page)/i.test(h)) continue;   // to another page / another slide: not a demo
      }
      const r = el.getBoundingClientRect();
      out.push({
        path: (root.ownerDocument !== document ? 'frame:' : '') + pathOf(el, root),
        text: (textOf(el) || norm(el.value || '')).slice(0, 160),
        name: norm(el.getAttribute('aria-label') || el.getAttribute('title') || (el.querySelector('img[alt]') || {}).alt || '').slice(0, 160),
        tag: el.tagName.toLowerCase(),
        cls: [...el.classList].filter((c) => !ACTIVE_RX.test(c)).slice(0, 5).join(' '),
        id: el.id || '',
        group: groupOf(el, root),
        x: rr.width ? (r.left - rr.left) / rr.width : 0,
        y: rr.height ? (r.top - rr.top) / rr.height : 0,
        w: rr.width ? r.width / rr.width : 0,
        h: rr.height ? r.height / rr.height : 0,
      });
    }
    out.sort((a, b) => (Math.abs(a.y - b.y) > 0.03 ? a.y - b.y : a.x - b.x));
    return out;
  }

  function mainFrame() {             // a page that only frames its content (a viewer around slide files): the big frame, if readable
    const va = innerWidth * innerHeight;
    for (const f of document.querySelectorAll('iframe')) {
      if (onScreen(f) < va * 0.4) continue;
      try { if (f.contentDocument && f.contentDocument.body) return f; } catch (e) { /* another site's */ }
    }
    return null;
  }
  function rootOf(k) {               // the element holding slide k's content
    const el = deck && k != null && k >= 0 ? deck.members[k] : null;
    if (el) return el;
    const f = deck ? null : mainFrame();
    return f ? f.contentDocument.body : document.body;
  }

  // ---- content: the pieces of a slide the narration can talk about (cards, list items, table rows, captioned pictures)
  function blockKey(el) {            // what a piece is called: its heading, else the first words of its text
    for (const h of el.querySelectorAll('h1,h2,h3,h4,h5,h6,strong,b,dt,th,[class*="title" i],[class*="head" i],[class*="name" i]')) {
      if (isNoteNode(h) || !shown(h)) continue;
      const t = textOf(h);
      if (t.length >= 2 && t.length <= 40) return t;
    }
    const line = (el.innerText || el.textContent || '').split('\n').map(norm).find((x) => x) || '';
    const cut = line.split(/[：:，,。;；！!？?（(]|\s[-–—]\s/)[0].trim();  // i18n: ignore
    return (cut.length >= 2 ? cut : line).slice(0, 30);
  }
  function blocksOf(root) {
    const rr = root.getBoundingClientRect();
    const area = Math.max(1, rr.width * rr.height);
    const prefix = root.ownerDocument !== document ? 'frame:' : '';
    const title = [...root.querySelectorAll('h1, h2, h3, [role=heading]')].find((h) => !isNoteNode(h) && shown(h) && textOf(h));
    const found = new Map();
    const add = (el, kind) => { if (!found.has(el)) found.set(el, kind); };
    root.querySelectorAll('li').forEach((el) => add(el, 'item'));
    root.querySelectorAll('tr').forEach((el) => { if (el.querySelector('td')) add(el, 'row'); });
    root.querySelectorAll('figure, blockquote').forEach((el) => add(el, el.tagName === 'FIGURE' ? 'figure' : 'quote'));
    // boxes of one kind side by side or under each other: cards, the rows of a list, the steps of a flow
    for (const p of [root, ...root.querySelectorAll('*')]) {
      if (p.childElementCount < 2) continue;
      const kinds = new Map();
      for (const k of p.children) {
        if (SKIP.test(k.tagName) || /^(LI|TR|TD|TH|THEAD|TBODY|CAPTION|COL|COLGROUP|OPTION)$/.test(k.tagName)) continue;
        const sig = k.tagName + '.' + [...k.classList].filter((c) => !ACTIVE_RX.test(c)).sort().join('.').replace(/\d+/g, '#');
        if (!kinds.has(sig)) kinds.set(sig, []);
        kinds.get(sig).push(k);
      }
      for (const group of kinds.values()) if (group.length >= 2 && group.length <= 16) group.forEach((k) => add(k, 'card'));
    }
    const out = [];
    for (const [el, kind] of found) {
      if (isNoteNode(el) || el.closest('#__sc_ui') || el.matches('button, input, select, textarea, a[href]') || !shown(el)) continue;
      if (title && (el === title || el.contains(title))) continue;    // the slide's title, and what holds it, is not a piece of it
      const cs = cssOf(el);
      if (cs && cs.display === 'inline') continue;
      const r = el.getBoundingClientRect();
      const a = r.width * r.height;
      if (a < area * 0.002 || a > area * 0.55 || r.height < rr.height * 0.02) continue;
      const text = norm(el.innerText || el.textContent || '');
      if (text.length < 2) continue;
      out.push({ path: prefix + pathOf(el, root), key: blockKey(el), text: text.slice(0, 200), kind,
        x: (r.left - rr.left) / rr.width, y: (r.top - rr.top) / rr.height, w: r.width / rr.width, h: r.height / rr.height });
    }
    // a wrapper around one piece reads the same as the piece: the outer one is kept (its frame sits better)
    out.sort((p, q) => q.w * q.h - p.w * p.h);
    const seen = new Set();
    const kept = out.filter((b) => { if (seen.has(b.text)) return false; seen.add(b.text); return true; });
    kept.sort((p, q) => (Math.abs(p.y - q.y) > 0.03 ? p.y - q.y : p.x - q.x));
    return kept.slice(0, 80);
  }

  function stageOf(k) {              // the element whose rectangle is recorded (k < 0: the page is one slide)
    if (!deck || k < 0) return pageStage() || document.documentElement;
    const m = deck.members[k];
    if (!m || deck.mode === 'scroll' || deck.kind === 'reveal' || deck.kind === 'impress') return document.documentElement;
    const scaled = m.classList.contains('remark-slide-container') ? m.querySelector('.remark-slide-scaler') : null;
    if (scaled && shown(scaled)) return scaled;      // remark: the 4:3 slide itself, not the window it is centred in
    const r = m.getBoundingClientRect();
    const p = m.parentElement;
    if (p && p !== document.body) {
      const pr = p.getBoundingClientRect();
      if (Math.abs(pr.left - r.left) < 3 && Math.abs(pr.top - r.top) < 3 && Math.abs(pr.width - r.width) < 3
          && Math.abs(pr.height - r.height) < 3) return p;
    }
    return m;
  }
  function rectOf(el) {
    if (!el || el === document.documentElement) return { x: 0, y: 0, w: innerWidth, h: innerHeight };
    const r = el.getBoundingClientRect();
    const x = Math.max(0, r.left);
    const y = Math.max(0, r.top);
    return { x, y, w: Math.min(r.right, innerWidth) - x, h: Math.min(r.bottom, innerHeight) - y };
  }

  function chromeLike(el, area) {
    if (/^(BUTTON|NAV|INPUT|SELECT|PROGRESS|DIALOG)$/.test(el.tagName)) return true;
    const label = (el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '');
    if (CHROME_RX.test(label)) return true;
    const r = el.getBoundingClientRect();
    const a = r.width * r.height;
    if (el.querySelector('button, a[href], input, select') && a < area * 0.5) return true;
    return a < area * 0.08 && norm(el.innerText).length < 40;
  }
  function isolate(el) {             // hide the presenter's chrome lying over the slide (arrows, dots, counters …)
    if (!el || el === document.documentElement) {
      if (deck) hideOverlays();      // a deck filling the window: its controls, progress bar, slide number
      return;
    }
    const sr = el.getBoundingClientRect();
    const area = sr.width * sr.height;
    for (let n = el; n && n.parentElement && n !== document.body; n = n.parentElement) {
      for (const sib of n.parentElement.children) {
        if (sib === n || SKIP.test(sib.tagName) || sib.id === '__sc_ui') continue;
        const r = sib.getBoundingClientRect();
        let over = r.right > sr.left + 1 && r.left < sr.right - 1 && r.bottom > sr.top + 1 && r.top < sr.bottom - 1;
        if (!over && (r.width < 2 || r.height < 2)) {
          // a progress bar may be a box 0 px wide that draws itself with a pseudo-element: on the slide's edge, named like chrome,
          // and laid out (not a box hidden for later, such as a dialog a click opens)
          const cs = cssOf(sib);
          const label = (sib.id || '') + ' ' + (typeof sib.className === 'string' ? sib.className : '');
          over = !!cs && cs.display !== 'none' && CHROME_RX.test(label)
            && r.right >= sr.left && r.left <= sr.right && r.bottom >= sr.top && r.top <= sr.bottom;
        }
        if (over && chromeLike(sib, area)) sib.setAttribute('data-sc-hidden', '');
      }
    }
    // square corners, no shadow — at once, not through the deck's own transitions (held still by the clock)
    const tr = [el.style.getPropertyValue('transition'), el.style.getPropertyPriority('transition')];
    el.style.setProperty('transition', 'none', 'important');
    el.style.setProperty('border-radius', '0', 'important');
    el.style.setProperty('box-shadow', 'none', 'important');
    void cssOf(el).borderRadius;
    if (tr[0]) el.style.setProperty('transition', tr[0], tr[1]); else el.style.removeProperty('transition');
    hideStyle();
  }
  function hideOverlays() {          // full-window decks: fixed controls outside the slides
    const area = innerWidth * innerHeight;
    document.querySelectorAll('body *').forEach((el) => {
      if (el.id === '__sc_ui' || (deck && deck.members.some((m) => m.contains(el) || el.contains(m)))) return;
      const cs = cssOf(el);
      if (!cs || (cs.position !== 'fixed' && cs.position !== 'absolute' && cs.position !== 'sticky')) return;
      if (chromeLike(el, area)) el.setAttribute('data-sc-hidden', '');
    });
    hideStyle();
  }
  function hideStyle() {
    if (document.getElementById('__sc_style')) return;
    const st = document.createElement('style');
    st.id = '__sc_style';
    // no transition: under "transition: all" the change of visibility would itself be a transition, held still by the clock
    st.textContent = '[data-sc-hidden]{visibility:hidden!important;transition:none!important;animation:none!important}';
    (document.head || document.documentElement).appendChild(st);
  }

  function fragmentsOf(root) {        // reveal.js builds: texts in the order they come in
    const byIndex = new Map();
    let auto = 1000;
    root.querySelectorAll('.fragment').forEach((f) => {
      const raw = f.getAttribute('data-fragment-index');
      const k = raw != null && raw !== '' ? Number(raw) : auto++;
      byIndex.set(k, ((byIndex.get(k) || '') + ' ' + norm(f.innerText || f.textContent)).trim());
    });
    return [...byIndex.keys()].sort((a, b) => a - b).map((k) => byIndex.get(k));
  }

  function slideInfo(k) {
    const root = rootOf(k);
    return {
      title: titleOf(root),
      text: visibleText(root).slice(0, 4000),
      notes: (notesText(root) || '').slice(0, 12000),
      controls: controlsOf(root),
      blocks: blocksOf(root),
      fragments: !deck ? [] : deck.kind === 'reveal' ? fragmentsOf(root)
        : deck.kind === 'shower' ? showerSteps(root).steps.map((s) => norm(s.innerText || s.textContent)) : [],
    };
  }

  // ---- notes kept in the deck's script: a list with one text per slide, or slide objects with a notes field
  const NOTE_NAME_RX = /note|script|narrat|speak|transcript|talk|voice|讲稿|备注|旁白|解说/i;  // i18n: ignore
  const NOTE_FIELDS = ['notes', 'note', 'speakerNotes', 'speaker_notes', 'speakerNote', 'speaker', 'presenterNotes', 'narration',
    'voiceover', 'voiceOver', 'script', 'transcript', 'talk', 'say', '讲稿', '备注', '旁白', '解说'];  // i18n: ignore

  function literalAt(src, at) {      // the array / object literal starting at `at` (strings, template strings and comments skipped over)
    let depth = 0;
    let quote = '';
    for (let i = at; i < src.length && i - at < 500000; i++) {
      const c = src[i];
      if (quote) {
        if (c === '\\') i++;
        else if (c === quote) quote = '';
        continue;
      }
      if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
      if (c === '/' && src[i + 1] === '/') { const e = src.indexOf('\n', i); if (e < 0) return ''; i = e; continue; }
      if (c === '/' && src[i + 1] === '*') { const e = src.indexOf('*/', i + 2); if (e < 0) return ''; i = e + 1; continue; }
      if (c === '[' || c === '{' || c === '(') depth++;
      else if (c === ']' || c === '}' || c === ')') { depth--; if (depth === 0) return src.slice(at, i + 1); }
    }
    return '';
  }
  function noteValue(v) {
    if (typeof v === 'string') return v;
    if (Array.isArray(v) && v.length && v.every((x) => typeof x === 'string')) return v.join('\n');
    return '';
  }
  function notesFromValue(name, v, count) {    // { texts, score } when this value holds one note per slide
    let list = null;
    const hinted = NOTE_NAME_RX.test(name);
    if (Array.isArray(v)) list = v;
    else if (v && typeof v === 'object') {
      const keys = Object.keys(v);
      if (Array.isArray(v.slides)) list = v.slides;
      else if (hinted && keys.length && keys.every((x) => /^\d+$/.test(x))) {    // { 1: "…", 2: "…" }
        const base = keys.some((x) => Number(x) === 0) ? 0 : 1;
        list = [];
        keys.forEach((x) => { list[Number(x) - base] = v[x]; });
      } else if (hinted && deck && deck.members.filter((m) => m.id && m.id in v).length >= count / 2) {   // { intro: "…", plan: "…" }
        list = deck.members.map((m) => v[m.id]);
      }
    }
    if (!list || Math.abs(list.length - count) > (deck ? 0 : 1)) return null;
    let texts = null;
    if (list.some((x) => x && typeof x === 'object' && !Array.isArray(x))) {     // slide objects: their notes field
      for (const f of NOTE_FIELDS) {
        const t = list.map((x) => (x && typeof x === 'object' ? noteValue(x[f]) : ''));
        if (t.filter((x) => norm(x)).length >= Math.max(1, count / 2)) { texts = t; break; }
      }
    } else if (hinted) {
      const t = list.map(noteValue);
      const full = t.filter((x) => norm(x));
      if (full.length >= Math.max(1, count / 2) && full.join('').length / full.length >= 6) texts = t;
    }
    if (!texts) return null;
    texts = Array.from({ length: count }, (_, k) => norm(htmlText(texts[k] || '')));
    return { texts, score: (list.length === count ? 1000000 : 0) + (hinted ? 100000 : 0) + texts.join('').length };
  }
  function scriptNotes(count) {
    const src = [...document.scripts].filter((s) => !s.src).map((s) => s.textContent).join('\n').slice(0, 3000000);
    const seen = new Set();
    let best = null;
    let tried = 0;
    for (const m of src.matchAll(/([\p{L}_$][\p{L}\p{N}_$]*)\s*[:=]\s*(?=[[{])/gu)) {
      if (tried >= 400) break;
      const at = m.index + m[0].length;
      if (seen.has(at)) continue;
      seen.add(at);
      const lit = literalAt(src, at);
      if (lit.length < 20 || !/["'`]/.test(lit)) continue;
      tried++;
      let v;
      try { v = (0, eval)('(' + lit + ')'); } catch (e) { continue; }   // a literal built from the deck's variables: not plain data
      const hit = notesFromValue(m[1], v, count);
      if (hit && (!best || hit.score > best.score)) best = hit;
    }
    return best ? best.texts : null;
  }

  function nextBuild() {             // show the next part of the slide through the deck's API; false: no API (press the key)
    if (!deck) return false;
    if (deck.kind === 'reveal') { const R = revealApi(); if (R) { R.nextFragment(); return true; } }
    if (deck.kind === 'shower') {
      const S = showerApi();
      const cur = S && deck.members[S.activeSlideIndex];
      // only while the slide has a step left: Shower's next() goes on to the next slide after the last one
      if (cur) { const st = showerSteps(cur); if (st.shown < st.steps.length) S.next(); return true; }
    }
    return false;
  }
  function allBuilds() {             // every part of the slide shown (its still picture); how many it took
    if (!deck) return 0;
    let n = 0;
    if (deck.kind === 'reveal') {
      const R = revealApi();
      const cur = deck.members[activeIndex()];
      const steps = cur ? Math.min(200, fragmentsOf(cur).length) : 0;
      for (; R && n < steps; n++) R.nextFragment();
    } else if (deck.kind === 'shower') {
      const S = showerApi();
      const cur = S && deck.members[S.activeSlideIndex];
      if (cur) {
        const st = showerSteps(cur);
        for (; n < st.steps.length - st.shown; n++) S.next();
      }
    }
    return n;
  }

  function pageStage() {             // a page with no slides in it (one file per slide): its slide-shaped box, if it has one
    const va = innerWidth * innerHeight;
    let best = null;
    let bestArea = 0;
    const cands = [document.body, ...document.querySelectorAll('body > *, body > * > *, [class*=slide], [id*=slide], [class*=page], [class*=stage], [class*=canvas]')];
    for (const el of cands) {
      if (!el || SKIP.test(el.tagName) || (el.id || '').startsWith('__sc') || !shown(el)) continue;
      const r = el.getBoundingClientRect();
      const ratio = r.width / Math.max(1, r.height);
      const area = r.width * r.height;
      if (ratio < 1.2 || ratio > 2.2 || area < va * 0.2 || r.left < -1 || r.top < -1 || r.right > innerWidth + 1) continue;
      if (area > bestArea + 1) { best = el; bestArea = area; }
    }
    return best && bestArea < va * 0.97 ? best : null;
  }

  function nextControl() {           // a "next slide" control outside the slides
    const rx = /(^|\b)(next|forward|weiter|suivant|siguiente|avanti|volgende|dalej)(\b|$)|下一|后一|向后|[›→»❯⟩>]\s*$/i;  // i18n: ignore
    for (const el of document.querySelectorAll('button, a, [role=button], [onclick], [aria-label], [title]')) {
      if (!shown(el) || (deck && deck.members.some((m) => m.contains(el)))) continue;
      const label = norm(el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || '');
      if (label && rx.test(label) && label.length <= 30) return pathOf(el, document.body);
    }
    return '';
  }

  function show(k, how) {            // show slide k without reloading (an API or the classes / layout of the slides)
    if (!deck) return false;
    const m = deck.members[k];
    if (!m) return false;
    if (how === 'api') {
      if (deck.kind === 'reveal') {
        const R = revealApi();
        const idx = R.getIndices ? R.getIndices(m) : { h: k, v: 0 };
        R.slide(idx.h, idx.v || 0, -1);   // its fragments hidden: they come in one by one ("next")
        return true;
      }
      if (deck.kind === 'impress') { W.impress().goto(m); return true; }
      if (deck.kind === 'swiper') { deck.parent.swiper.slideTo(k, 0); return true; }
      if (deck.kind === 'shower') { showerApi().goTo(k); return true; }
      if (W.stepcast && typeof W.stepcast.go === 'function') { W.stepcast.go(k); return true; }
      return false;
    }
    if (how === 'scroll') {
      m.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'instant' });
      return true;
    }
    if (how === 'classes') {         // the active slide has a class the others don't: move it
      const cur = deck.members[activeIndex()];
      if (!cur || cur === m) return cur === m;
      const marks = [...cur.classList].filter((c) => ACTIVE_RX.test(c) && !deck.members.some((o) => o !== cur && o.classList.contains(c)));
      if (!marks.length) return false;
      marks.forEach((c) => { cur.classList.remove(c); m.classList.add(c); });
      return true;
    }
    return false;
  }

  // ------------------------------------------------------------------ the API StepCast calls
  const api = {
    version: 1,
    get now() { return now; },
    get realStart() { return realStart; },
    // virtual time
    async frame(to, uiState) {
      holding = false;
      const moved = await moveTo(to);
      const uiChanged = applyUi(uiState);
      return { changed: moved || uiChanged, now };
    },
    async advance(ms, every) {       // fast-forward without recording
      holding = false;
      const end = now + Math.max(0, ms);
      const d = every || 16;
      while (now < end - 1e-6) await step(Math.min(end, now + d));
      return now;
    },
    // Every animation and playing video present starts again from its beginning at the current time. With slide k: only what
    // moves inside it; the way there (the last slide going, the slides moving along, slide k itself coming in) ends at once —
    // StepCast puts its own transition between the slides
    restart(k) {
      scanAnimations(now);
      const root = deck && k != null && k >= 0 ? deck.members[k] : null;
      for (const [a, st] of driven) {
        let target = null;
        try { target = a.effect && a.effect.target; } catch (e) { target = null; }
        if (root && !(target && target !== root && root.contains(target))) {
          let end = Infinity;
          try { end = a.effect.getComputedTiming().endTime; } catch (e) { end = Infinity; }
          if (Number.isFinite(end)) {
            driven.delete(a);
            released.add(a);
            try { a.finish(); } catch (e) { try { a.currentTime = end; } catch (e2) { /* ignore */ } }
            continue;
          }                          // endless (a moving background behind the slides): it goes on
        }
        st.start = now;
        try { if (!st.page) a.currentTime = 0; } catch (e) { /* ignore */ }
      }
      for (const [el, m] of media) {
        if (!m.playing) continue;
        m.start = now;
        m.from = m.from0 || 0;
        try { el.currentTime = m.from; } catch (e) { /* ignore */ }
      }
      const within_ = (el) => !root || root.contains(el);
      for (const s of smil.keys()) {
        if (!within_(s)) continue;
        smil.set(s, now);
        try { s.setCurrentTime(0); } catch (e) { /* ignore */ }
      }
      for (const [img, st] of animated) {
        if (within_(img)) { st.start = now; st.shown = -1; }
      }
      for (const c of children) {
        if (!within_(c.frame)) continue;
        try { c.restart(); } catch (e) { children.delete(c); }
      }
      return driven.size;
    },
    hold() {
      holding = true;
      scanAnimations(now);
      seekSmil(now);
      for (const c of children) { try { c.hold(); } catch (e) { children.delete(c); } }
      return true;
    },
    // Slide k (or the page) as it will be shown: its pictures in and decoded
    async ready(k) {
      const root = rootOf(k);
      const imgs = imgsOf(root);
      await imagesLoaded(imgs, 4000);
      await within(Promise.all(imgs.map((i) => (i.decode ? i.decode().catch(() => 0) : 0))), 1500);
      return true;
    },
    // Before recording slide k: what moves by itself outside the clock is taken over
    async prepareMedia(k) {
      const root = rootOf(k);
      const imgs = imgsOf(root);
      await imagesLoaded(imgs, 4000);
      for (const i of imgs) await takeOverImage(i);
      try {
        for (const el of root.querySelectorAll('*')) {
          const cs = cssOf(el);
          if (cs && /\.gif\b|image\/gif/i.test(cs.backgroundImage || '')) { animatedFree = true; break; }
        }
      } catch (e) { /* ignore */ }
      for (const c of children) {
        if (root.contains(c.frame)) { try { await within(c.prepare(), 60000); } catch (e) { /* ignore */ } }
      }
      return animated.size;
    },
    __adopt(child) { children.add(child); },
    busy() {                         // still moving by itself: finite animations, timers due soon
      let finite = 0;
      for (const [a, st] of driven) {
        if (st.page) continue;
        try { if (Number.isFinite(a.effect.getComputedTiming().endTime)) finite++; } catch (e) { /* ignore */ }
      }
      let soon = 0;
      for (const t of timers.values()) if (!t.every && t.due <= now + 1200) soon++;
      let playing = 0;
      for (const [el, m] of media) if (m.playing && el.isConnected && !el.loop) playing++;
      return { finite, soon, playing, mutations: stats.mutations };
    },
    errors() { return stats.errors.splice(0); },
    // deck
    detect() {
      deck = findDeck();
      if (!deck) return { found: false, count: 0, hash: location.hash };
      return {
        found: true, kind: deck.kind, mode: deck.mode, count: deck.members.length,
        active: activeIndex(), hash: location.hash, api: !!(W.stepcast && typeof W.stepcast.go === 'function'),
        fragments: deck.members.map((m) => (deck.kind === 'shower' ? showerSteps(m).steps : m.querySelectorAll('.fragment')).length),
      };
    },
    active: activeIndex,
    show,
    slide: slideInfo,
    nextBuild,
    allBuilds,
    scriptNotes,
    stageRect(k) {
      const el = stageOf(k);
      stage = el === document.documentElement ? null : el;
      return rectOf(el);
    },
    isolate(k) { isolate(stageOf(k)); return rectOf(stageOf(k)); },
    signature() {                    // what is on screen, for decks StepCast can't read: changes when a key shows something new
      const texts = [];
      const f = mainFrame();
      (f ? f.contentDocument : document).querySelectorAll('body *').forEach((el) => {
        if (el.childElementCount === 0 && onScreen(el) > 0) texts.push(norm(el.textContent).slice(0, 80));
      });
      return texts.join('|').slice(0, 20000);
    },
    panels() {                       // text of note panels outside the slides (a presenter view next to the slide)
      const out = {};
      document.querySelectorAll('[id], [class]').forEach((el) => {
        const label = (el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '');
        if (!/(note|script|speaker|讲稿|备注)/i.test(label) || el.closest('#__sc_ui')) return;   // i18n: ignore
        if (deck && deck.members.some((m) => m.contains(el))) return;
        const t = norm(el.textContent);
        if (t.length >= 8) out[pathOf(el, document.body)] = t.slice(0, 12000);
      });
      return out;
    },
    nextControl,
    rect(k, path) {                  // where a control is now (window coordinates), or null when it isn't there / visible
      let root = k >= 0 && deck ? deck.members[k] : document.body;
      let dx = 0;
      let dy = 0;
      if (path.startsWith('frame:')) {   // a control in the big frame of a page that only frames its content
        const f = mainFrame();
        if (!f) return null;
        const fr = f.getBoundingClientRect();
        root = f.contentDocument.body;
        dx = fr.left + f.clientLeft;
        dy = fr.top + f.clientTop;
        path = path.slice(6);
      }
      if (!root) return null;
      let el = null;
      try { el = path.startsWith('#') ? root.ownerDocument.querySelector(path) : root.querySelector(path); } catch (e) { el = null; }
      if (!el || !shown(el)) return null;
      el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
      const r = el.getBoundingClientRect();
      return { x: r.left + dx, y: r.top + dy, w: r.width, h: r.height };
    },
    // Where a piece of content is now; null when it isn't there, or something else stands at that place by now (another tab's content)
    blockRect(k, path, text) {
      const r = api.rect(k, path);
      if (!r) return null;
      let root = k >= 0 && deck ? deck.members[k] : document.body;
      let p = path;
      if (p.startsWith('frame:')) { const f = mainFrame(); if (!f) return null; root = f.contentDocument.body; p = p.slice(6); }
      let el = null;
      try { el = p.startsWith('#') ? root.ownerDocument.querySelector(p) : root.querySelector(p); } catch (e) { el = null; }
      const now = norm((el && (el.innerText || el.textContent)) || '');
      return now.startsWith(norm(text).slice(0, 8)) ? r : null;
    },
    // A cover over the whole page in front of the slides, with one "start" button
    startButton() {
      const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
      if (!el || (deck && deck.members.some((m) => m.contains(el)))) return '';
      let cover = null;
      for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
        const cs = cssOf(n);
        const r = n.getBoundingClientRect();
        if (cs && (cs.position === 'fixed' || cs.position === 'absolute') && r.width * r.height >= innerWidth * innerHeight * 0.8) cover = n;
      }
      if (!cover || (deck && deck.members.some((m) => cover.contains(m)))) return '';
      const hits = [...cover.querySelectorAll('button, a, [role=button], [onclick], input[type=button], input[type=submit]')]
        .filter((b) => shown(b) && START_RX.test(norm(b.innerText || b.value || b.getAttribute('aria-label') || '')));
      return hits.length === 1 ? pathOf(hits[0], document.body) : '';
    },
    centerIsControl() {              // what a click in the middle of the window would hit is a control of the page
      const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
      return !!(el && (el.closest(CTRL) || (cssOf(el) || {}).cursor === 'pointer'));
    },
    blur() { try { if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); } catch (e) { /* ignore */ } },
    configureReveal() {
      const R = revealApi();
      if (!R || typeof R.configure !== 'function') return false;
      R.configure({ controls: false, progress: false, slideNumber: false, autoSlide: 0, help: false, hash: false,
        history: false, overview: false, pause: false, showNotes: false, autoAnimate: true });
      return true;
    },
  };
  Object.defineProperty(W, '__sc', { value: api, enumerable: false, configurable: false });

  if (host) {                        // a frame of the deck's page: the page moves this clock
    const frameEl = W.frameElement;
    try {
      host.__adopt({
        frame: frameEl,
        alive: () => { try { return !!frameEl && frameEl.isConnected && frameEl.contentWindow === W; } catch (e) { return false; } },
        tick: (to) => { holding = false; return moveTo(to); },
        hold: () => { api.hold(); },
        restart: () => { api.restart(); },
        prepare: () => api.prepareMedia(-1),
      });
    } catch (e) { /* the page went on meanwhile */ }
  }
})();
