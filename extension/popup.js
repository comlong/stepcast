/* 弹窗界面逻辑 */
const $ = (id) => document.getElementById(id);

function msg(type, payload = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, ...payload }, (r) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(r || {});
    });
  });
}

let state = {};
let formFilled = false;

function fillForm() {
  // 只在弹窗打开时填一次，否则每 2 秒的刷新会冲掉正在输入的内容
  $('serverUrl').value = state.serverUrl || 'http://127.0.0.1:8756';
  $('captureNavigation').checked = !!state.captureNavigation;
  $('jpeg').checked = state.imageFormat === 'jpeg';
  $('autoRedact').checked = state.autoRedact !== false;
  $('indexText').checked = state.indexText !== false;
  $('redactKeywords').value = state.redactKeywords || '';
  $('magicMic').checked = !!state.magicMic;
  $('micOpts').hidden = !state.magicMic;
  $('micMode').value = state.micMode || 'ai';
  $('micLanguage').value = state.micLanguage || 'en-US';
  refreshMicPerm();
  formFilled = true;
}

async function refresh() {
  state = await msg('vt_state');
  if (!formFilled) fillForm();
  $('recMic').hidden = !state.micActive;
  $('count').textContent = state.stepCount || 0;

  $('idleView').hidden = !!state.recording;
  $('recView').hidden = !state.recording;
  $('err').textContent = state.lastError || '';

  const ping = await msg('vt_ping');
  const dot = $('dot');
  dot.className = 'dot';
  if (state.recording) {
    dot.classList.add('rec');
    $('statusText').textContent = t('录制中 · {name}', { name: state.projectName || '' });
  } else if (ping.ok) {
    dot.classList.add('ok');
    const d = ping.data || {};
    fillMicLanguages(d.languages);
    // 界面语言跟编辑器走：那边改了，这里下次刷新就换过来
    if (d.ui_language && await VTI18N.setLang(d.ui_language)) {
      VTI18N.translateDom();
      refreshMicPerm();                 // 这句是脚本生成的，换语言后要重写一次
    }
    const bits = [];
    if (d.ffmpeg && !d.ffmpeg.ok) bits.push(t('缺 ffmpeg'));
    if (!(d.llm && d.llm.configured)) bits.push(t('未配 AI 模型'));
    $('statusText').textContent = bits.length
      ? t('服务已连接 · {problems}', { problems: bits.join(t('、')) }) : t('服务已连接');
  } else {
    dot.classList.add('bad');
    $('statusText').textContent = t('未连接：请先运行 python app.py');
  }
}

/** 「边录边讲」的语言：服务连上后换成和编辑器一样的完整列表（中文、欧洲语言在前）；连不上时用 popup.html 里写的几种 */
let micLangsFilled = false;
function fillMicLanguages(list) {
  if (micLangsFilled || !Array.isArray(list) || !list.length) return;
  const sel = $('micLanguage');
  const keep = sel.value || state.micLanguage || 'en-US';
  sel.textContent = '';
  for (const l of list) {
    const o = document.createElement('option');
    o.value = l.code;
    o.textContent = l.name;
    o.setAttribute('translate', 'no');
    sel.appendChild(o);
  }
  sel.value = list.some(l => l.code === keep) ? keep : 'en-US';
  micLangsFilled = true;
}

async function refreshMicPerm() {
  if (!$('magicMic').checked) return;
  try {
    const st = await navigator.permissions.query({ name: 'microphone' });
    $('micPerm').innerHTML = st.state === 'granted'
      ? `<span style="color:#30c463">${t('✓ 麦克风已授权')}</span>`
      : `<span style="color:#f0b03c">${t('需要先授权麦克风')}</span>`;
  } catch (e) { $('micPerm').textContent = ''; }
}

const saveMic = () => msg('vt_set_state', {
  patch: { magicMic: $('magicMic').checked, micMode: $('micMode').value, micLanguage: $('micLanguage').value }
});

$('magicMic').addEventListener('change', async () => {
  $('micOpts').hidden = !$('magicMic').checked;
  await saveMic();
  refreshMicPerm();
});
$('micMode').addEventListener('change', saveMic);
$('micLanguage').addEventListener('change', saveMic);
$('micSetup').addEventListener('click', () => chrome.tabs.create({ url: chrome.runtime.getURL('mic.html') }));

$('start').addEventListener('click', async () => {
  $('start').disabled = true;
  const r = await msg('vt_start', { name: $('name').value.trim() });
  $('start').disabled = false;
  if (!r.ok) { $('err').textContent = r.error || t('启动失败'); return; }
  await refresh();
  window.close();
});

$('stop').addEventListener('click', async () => {
  const r = await msg('vt_stop');
  const pid = (r.data && r.data.project_id) || state.projectId;
  const base = (state.serverUrl || 'http://127.0.0.1:8756').replace(/\/$/, '');
  if (pid) chrome.tabs.create({ url: `${base}/?p=${pid}` });
  window.close();
});

$('snap').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  await msg('vt_manual', { event: { url: tab.url, title: tab.title } });
  setTimeout(refresh, 900);
});

$('openEditor').addEventListener('click', () => {
  const base = ($('serverUrl').value || 'http://127.0.0.1:8756').replace(/\/$/, '');
  chrome.tabs.create({ url: base + '/' });
});

$('save').addEventListener('click', async () => {
  await msg('vt_set_state', {
    patch: {
      serverUrl: $('serverUrl').value.trim() || 'http://127.0.0.1:8756',
      captureNavigation: $('captureNavigation').checked,
      imageFormat: $('jpeg').checked ? 'jpeg' : 'png',
      autoRedact: $('autoRedact').checked,
      indexText: $('indexText').checked,
      redactKeywords: $('redactKeywords').value.trim()
    }
  });
  $('err').textContent = t('已保存');
  setTimeout(() => ($('err').textContent = ''), 1500);
  refresh();
});

(async () => {
  await VTI18N.init();
  VTI18N.translateDom();
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 2000);
})();
