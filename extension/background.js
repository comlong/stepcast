/* StepCast 录制器 — 后台服务工作线程
 *
 * 负责：维护录制状态、截图（带限流）、把步骤 POST 给本地 Python 服务。
 */

importScripts('i18n.js');
const i18nReady = VTI18N.init();

const DEFAULTS = {
  serverUrl: 'http://127.0.0.1:8756',
  recording: false,
  projectId: '',
  projectName: '',
  stepCount: 0,
  captureNavigation: true,
  captureScroll: false,
  imageFormat: 'png',       // png | jpeg
  jpegQuality: 92,
  autoRedact: true,         // 录制时自动识别邮箱/手机号/身份证/银行卡/IP 并打码
  indexText: true,          // 记录页面文字位置，便于事后按关键词补打码
  redactKeywords: '',       // 额外要打码的词，逗号分隔
  magicMic: false,          // 边录边讲：录制时同时录麦克风
  micDeviceId: '',
  micMode: 'ai',            // ai = 识别成文字交给 AI 配音 | own = 保留原声
  micLanguage: 'en-US',
  micActive: false,
  lastMicJob: '',
  lastError: ''
};

async function getState() {
  const s = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...s };
}

async function setState(patch) {
  await chrome.storage.local.set(patch);
  const s = await getState();
  updateBadge(s);
  return s;
}

function redactCfg(s) {
  return { autoRedact: s.autoRedact, indexText: s.indexText, redactKeywords: s.redactKeywords };
}

function updateBadge(s) {
  if (s.recording) {
    chrome.action.setBadgeBackgroundColor({ color: '#E5484D' });
    chrome.action.setBadgeText({ text: String(s.stepCount || 0) });
    chrome.action.setTitle({ title: t('录制中 · 已捕获 {n} 步', { n: s.stepCount }) });
  } else {
    chrome.action.setBadgeText({ text: '' });
    chrome.action.setTitle({ title: t('StepCast 录制器') });
  }
}

/* ---------- 截图（Chrome 限制约每秒 2 次，这里串行 + 限流） ---------- */

let captureChain = Promise.resolve();
let lastCaptureAt = 0;
const MIN_GAP = 620;

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function tellTab(tabId, msg) {
  try { await chrome.tabs.sendMessage(tabId, msg); } catch (e) { /* 页面没有 content script */ }
}

async function rawCapture(windowId, format, quality) {
  const opts = format === 'jpeg' ? { format: 'jpeg', quality } : { format: 'png' };
  return await chrome.tabs.captureVisibleTab(windowId, opts);
}

/** 串行排队截图，并在截图前隐藏页面上的录制浮层。 */
function captureTab(tab, { hideOverlay = true } = {}) {
  const run = async () => {
    const s = await getState();
    const gap = Date.now() - lastCaptureAt;
    if (gap < MIN_GAP) await sleep(MIN_GAP - gap);
    if (hideOverlay && tab.id != null) {
      await tellTab(tab.id, { type: 'vt_overlay', show: false });
      await sleep(35);
    }
    let dataUrl = '';
    try {
      dataUrl = await rawCapture(tab.windowId, s.imageFormat, s.jpegQuality);
    } catch (e) {
      // 限流或权限问题，等一下重试一次
      await sleep(700);
      try {
        dataUrl = await rawCapture(tab.windowId, s.imageFormat, s.jpegQuality);
      } catch (e2) {
        await setState({ lastError: t('截图失败：{error}', { error: e2.message || e2 }) });
      }
    }
    lastCaptureAt = Date.now();
    if (hideOverlay && tab.id != null) tellTab(tab.id, { type: 'vt_overlay', show: true });
    return dataUrl;
  };
  captureChain = captureChain.then(run, run);
  return captureChain;
}

/* ---------- 边录边讲 ---------- */

async function ensureOffscreen() {
  const ctx = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
  if (ctx.length) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: 'Record the narration spoken while the user records browser actions'
  });
}

function toOffscreen(type, payload = {}) {
  return chrome.runtime.sendMessage({ target: 'offscreen', type, ...payload });
}

async function startMic(s) {
  try {
    await ensureOffscreen();
    const r = await toOffscreen('mic_start', { deviceId: s.micDeviceId });
    if (!r || !r.ok) throw new Error((r && r.error) || t('无法开始录音'));
    await setState({ micActive: true });
    return true;
  } catch (e) {
    const denied = /NotAllowed|Permission/i.test(String(e.message));
    await setState({
      micActive: false,
      lastError: denied
        ? t('麦克风未授权：点扩展弹窗里的「麦克风设置」授权一次（本次录制继续，只是没有录音）')
        : t('录音启动失败：{error}', { error: e.message || e })
    });
    if (denied) chrome.tabs.create({ url: chrome.runtime.getURL('mic.html') });
    return false;
  }
}

/** 停止录音并上传。不等上传结束就返回，编辑器会轮询识别进度。 */
async function stopMic(s, projectId) {
  if (!s.micActive) return;
  await setState({ micActive: false });
  toOffscreen('mic_stop', {
    upload: true, projectId, serverUrl: s.serverUrl, mode: s.micMode, language: s.micLanguage
  }).then(async (r) => {
    if (r && r.ok) await setState({ lastMicJob: r.job || '' });
    else await setState({ lastError: t('录音上传失败：{error}', { error: (r && r.error) || t('未知错误') }) });
    // 上传期间如果又开始了新的录制，录音页正在用，不能关
    if (!(await getState()).micActive) {
      try { await chrome.offscreen.closeDocument(); } catch (e) { /* 已关闭 */ }
    }
  }).catch(async (e) => {
    await setState({ lastError: t('录音上传失败：{error}', { error: e.message || e }) });
  });
}

/* ---------- 与本地服务通信 ---------- */

async function api(path, body, method = 'POST') {
  const s = await getState();
  const res = await fetch(s.serverUrl.replace(/\/$/, '') + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`${res.status} ${body.slice(0, 200)}`);
  }
  return await res.json();
}

async function pingServer() {
  try {
    const s = await getState();
    const res = await fetch(s.serverUrl.replace(/\/$/, '') + '/api/health', { cache: 'no-store' });
    if (!res.ok) return { ok: false, error: 'HTTP ' + res.status };
    const data = await res.json();
    if (data.ui_language) await VTI18N.setLang(data.ui_language);   // 跟编辑器的界面语言走
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

/* ---------- 录制控制 ---------- */

async function startRecording(name) {
  const s0 = await getState();
  const r = await api('/api/capture/start', { name: name || '', language: s0.magicMic ? s0.micLanguage : '' });
  await setState({
    recording: true,
    projectId: r.project_id,
    projectName: r.name,
    stepCount: 0,
    lastError: ''
  });
  if (s0.magicMic) await startMic(await getState());
  broadcastOverlay(true);
  return r;
}

async function stopRecording() {
  const s = await getState();
  let r = { ok: false };
  if (s.projectId) {
    try { r = await api('/api/capture/stop', { project_id: s.projectId }); } catch (e) { /* ignore */ }
  }
  await stopMic(s, s.projectId);
  await setState({ recording: false });
  broadcastOverlay(false);
  return { ...r, project_id: s.projectId, steps: s.stepCount, mic: s.micActive };
}

async function broadcastOverlay(on) {
  const tabs = await chrome.tabs.query({});
  const s = await getState();
  for (const t of tabs) {
    if (!t.id || !/^https?:/.test(t.url || '')) continue;
    tellTab(t.id, { type: 'vt_recording', recording: on, stepCount: s.stepCount, cfg: redactCfg(s) });
  }
}

/** 核心：收到一个操作事件 -> 截图 -> 上报 */
async function recordEvent(evt, tab) {
  const s = await getState();
  if (!s.recording || !s.projectId) return { ok: false, reason: 'not-recording' };

  let scan = { nodes: [], hits: [] };
  if (!evt.redactions && tab.id != null && (s.autoRedact || s.indexText)) {
    try {
      const r = await chrome.tabs.sendMessage(tab.id, { type: 'vt_scan', cfg: redactCfg(s) });
      if (r) scan = r;
    } catch (e) { /* 页面没有 content script */ }
  }

  const shot = await captureTab(tab);
  const payload = {
    project_id: s.projectId,
    kind: evt.kind || 'click',
    url: evt.url || tab.url || '',
    page_title: evt.title || tab.title || '',
    target: evt.target || null,
    point: evt.point || null,
    value: evt.value || '',
    viewport_w: evt.viewportW || 0,
    viewport_h: evt.viewportH || 0,
    device_pixel_ratio: evt.dpr || 1,
    client_ts: evt.client_ts || Date.now(),
    screenshot_b64: shot || '',
    redactions: evt.redactions || scan.hits || [],
    text_nodes: evt.text_nodes || scan.nodes || []
  };
  try {
    const r = await api('/api/capture/step', payload);
    const st = await setState({ stepCount: r.steps || (s.stepCount + 1) });
    if (tab.id != null) {
      tellTab(tab.id, {
        type: 'vt_recording', recording: true, stepCount: st.stepCount,
        flash: true, cfg: redactCfg(st)
      });
    }
    return r;
  } catch (e) {
    await setState({ lastError: t('上报失败：{error}', { error: e.message || e }) });
    return { ok: false, error: String(e) };
  }
}

/* ---------- 事件入口 ---------- */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.target === 'offscreen') return false;   // 发给录音页的，不归这里管
  (async () => {
    try {
      switch (msg.type) {
        case 'vt_event': {
          const tab = sender.tab || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
          sendResponse(await recordEvent(msg.event, tab));
          break;
        }
        case 'vt_state':
          sendResponse(await getState());
          break;
        case 'vt_set_state':
          sendResponse(await setState(msg.patch || {}));
          break;
        case 'vt_start':
          sendResponse({ ok: true, data: await startRecording(msg.name) });
          break;
        case 'vt_stop':
          sendResponse({ ok: true, data: await stopRecording() });
          break;
        case 'vt_ping':
          sendResponse(await pingServer());
          break;
        case 'vt_i18n':
          await i18nReady;
          sendResponse({ lang: VTI18N.lang(), dict: VTI18N.dict() });
          break;
        case 'vt_manual': {
          const tab = sender.tab || (await chrome.tabs.query({ active: true, currentWindow: true }))[0];
          sendResponse(await recordEvent({ kind: 'manual', client_ts: Date.now(), ...(msg.event || {}) }, tab));
          break;
        }
        default:
          sendResponse({ ok: false, error: 'unknown message' });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true;   // 异步响应
});

// 弹窗或录音页换了语言，后台也跟着换
chrome.storage.onChanged.addListener((changes) => {
  if (changes.vt_ui_lang) VTI18N.setLang(changes.vt_ui_lang.newValue);
});

/* 页面加载完成后补一张“结果”截图 */
const navTimers = new Map();
chrome.webNavigation.onCompleted.addListener(async (details) => {
  if (details.frameId !== 0) return;
  const s = await getState();
  if (!s.recording || !s.captureNavigation) return;
  if (!/^https?:/.test(details.url || '')) return;

  clearTimeout(navTimers.get(details.tabId));
  navTimers.set(details.tabId, setTimeout(async () => {
    try {
      const tab = await chrome.tabs.get(details.tabId);
      if (!tab.active) return;
      await recordEvent({
        kind: 'navigate',
        client_ts: details.timeStamp,
        url: details.url,
        title: tab.title,
        target: null,
        point: null
      }, tab);
    } catch (e) { /* tab 已关闭 */ }
  }, 1100));
});

chrome.commands.onCommand.addListener(async (command) => {
  const s = await getState();
  if (command === 'toggle-recording') {
    if (s.recording) await stopRecording();
    else await startRecording('');
  } else if (command === 'capture-step') {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) await recordEvent({ kind: 'manual', client_ts: Date.now(), url: tab.url, title: tab.title }, tab);
  }
});

chrome.runtime.onInstalled.addListener(async () => {
  const s = await getState();
  await setState({ recording: false });
  updateBadge({ ...s, recording: false });
});

chrome.runtime.onStartup.addListener(async () => {
  await setState({ recording: false, micActive: false });
});
