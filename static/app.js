/* StepCast 编辑器前端 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  project: null,
  stepId: null,
  settings: {},
  health: null,
  llmDrafts: {},         // 设置里各家 AI 服务商还没保存的修改
  llmModels: {},         // 「获取模型列表」拿到的结果
  voices: [],
  languages: [],
  job: null,
  mode: 'shot',          // shot | render
  saveTimer: null,
  audio: new Audio(),
};

/* ---------- API ---------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* */ }
    throw new Error(detail);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

function fileUrl(kind, name) {
  return `/api/projects/${S.project.id}/file/${kind}/${encodeURIComponent(name)}`;
}

function toast(msg, bad = false) {
  const box = $('#toast');
  box.textContent = msg;
  box.classList.toggle('bad', bad);
  box.classList.add('show');
  clearTimeout(box._h);
  box._h = setTimeout(() => box.classList.remove('show'), bad ? 5200 : 2600);
}

/* ---------- 启动 ---------- */

async function boot() {
  bindUI();
  await Promise.all([loadHealth(), loadSettings(), loadLanguages()]);
  loadVoices();                                  // 需要联网，不阻塞
  const pid = new URLSearchParams(location.search).get('p');
  const list = (await api('/api/projects')).projects;
  if (pid) await openProject(pid);
  else if (list.length) await openProject(list[0].id);
  else showProjects();
  pollRecording();
}

async function loadHealth() {
  try {
    const h = await api('/api/health');
    $('#badgeFF').textContent = h.ffmpeg.ok ? 'ffmpeg ✓' : 'ffmpeg ✗';
    $('#badgeFF').className = 'badge ' + (h.ffmpeg.ok ? 'ok' : 'bad');
    S.health = h;
    const ai = h.llm || {};
    $('#badgeAI').textContent = `${shortName(ai.name)} ${ai.configured ? '✓' : '✗'}`;
    $('#badgeAI').className = 'badge ' + (ai.configured ? 'ok' : 'bad');
    $('#badgeAI').title = ai.configured ? t('模型：{model}', { model: ai.model }) : t('还没配好 AI 模型：右上角 ⚙ 设置 → AI 与语音');
    $('#aboutInfo').textContent = t('数据目录：{dir}　ffmpeg：{ffmpeg}', { dir: h.data_dir, ffmpeg: h.ffmpeg.path || t('未找到') });
  } catch (e) { toast(t('无法连接本地服务：{error}', { error: e.message }), true); }
}

async function loadSettings() {
  S.settings = await api('/api/settings');
  fillSettingsForm();
}

/** 语言下拉框的选项：中文在最前，然后「欧洲语言」「其他语言」两组（顺序由后端定） */
function langOptions(withCode = false) {
  const opt = (l) => `<option value="${l.code}">${l.name}${withCode ? ` (${l.code})` : ''}</option>`;
  const group = (g) => S.languages.filter(l => (l.group || 'other') === g).map(opt).join('');
  const eu = group('europe'), other = group('other');
  return group('zh')
    + (eu ? `<optgroup label="${t('欧洲语言')}">${eu}</optgroup>` : '')
    + (other ? `<optgroup label="${t('其他语言')}">${other}</optgroup>` : '');
}

async function loadLanguages() {
  S.languages = (await api('/api/languages')).languages;
  for (const sel of [$('#sLang'), $('#tLang')]) sel.innerHTML = langOptions(true);
  syncLanguageFields();
}

/** 设置里的「解说语言 / 音色」按保存的值显示。
 *  设置、语言列表、音色列表是同时去取的，谁先回来不一定。以前语言列表先到时下拉框就停在第一项
 *  「简体中文」，而填设置的时候又不管这两项，于是每次打开都显示中文，这时点保存就真把中文存进去了。
 *  现在三个地方取回来都调这里，以最后能凑齐的为准。 */
function syncLanguageFields() {
  const lang = S.settings && S.settings.language;
  const sel = $('#sLang');
  if (!lang || !sel.options.length) return;
  if (![...sel.options].some(o => o.value === lang)) {       // 存的语言不在列表里也照样显示，不然会变成空白
    sel.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(lang)}">${escapeHtml(lang)}</option>`);
  }
  sel.value = lang;
  fillVoiceSelect($('#sVoice'), lang, S.settings.voice);
}

async function loadVoices() {
  try {
    const r = await api('/api/voices');
    S.voices = r.voices;
    S.voiceDefaults = r.defaults || {};
    S.voiceMale = r.male_defaults || {};
    syncLanguageFields();
    fillVoiceSelect($('#tVoice'), $('#tLang').value || 'en-US');
  } catch (e) {
    $('#sVoice').innerHTML = `<option value="">${t('获取语音列表失败（需联网）')}</option>`;
  }
}

function fillVoiceSelect(sel, locale, selected) {
  if (!S.voices.length) return;
  const lang = (locale || '').split('-')[0].toLowerCase();
  const loc = (locale || '').toLowerCase();
  // 同一种语言里，和所选地区完全一样的排前面（选 English (UK) 时先给英式口音，而不是澳洲的）
  const speaks = (v) => v.locale.toLowerCase().startsWith(lang) || (v.langs || []).includes(lang);
  const mine = S.voices.filter(speaks)
    .sort((a, b) => (b.locale.toLowerCase() === loc) - (a.locale.toLowerCase() === loc));
  const rest = S.voices.filter(v => !mine.includes(v));
  const opt = (v) => `<option value="${escapeHtml(v.name)}">${[
    escapeHtml(v.friendly || v.name),
    v.gender === 'Female' ? t('女') : v.gender === 'Male' ? t('男') : '',
    (v.langs || []).length > 1 ? t('多语言') : escapeHtml(v.locale),
  ].filter(Boolean).join(' · ')}</option>`;
  sel.innerHTML =
    (mine.length ? `<optgroup label="${t('当前语言')}">${mine.map(opt).join('')}</optgroup>` : '') +
    `<optgroup label="${t('其他语言')}">${rest.map(opt).join('')}</optgroup>`;
  // 重新填完选项后浏览器会自动选中第一项，所以要明确挑：传进来的 > 这个地区的推荐音色 > 同语言第一个
  const has = (name) => name && S.voices.some(v => v.name === name);
  // 正在用的音色不在列表里（比如那家配音服务的 Key 删了）：照样显示出来，别悄悄换成别的音色
  if (selected && !has(selected)) {
    sel.insertAdjacentHTML('afterbegin',
      `<option value="${escapeHtml(selected)}">${escapeHtml(selected)} · ${t('当前设置（列表里没有）')}</option>`);
    sel.value = selected;
    return;
  }
  const want = has(selected) ? selected
    : has(S.voiceDefaults && S.voiceDefaults[locale]) ? S.voiceDefaults[locale]
      : (mine[0] && mine[0].name);
  if (want) sel.value = want;
}

/* ---------- 项目 ---------- */

async function openProject(pid) {
  await flushSaves();
  try {
    S.project = await api('/api/projects/' + pid);
  } catch (e) { toast(t('打开项目失败：{error}', { error: e.message }), true); return; }
  history.replaceState(null, '', '?p=' + pid);
  $('#projName').value = S.project.name;
  $('#btnOpenVideo').disabled = !S.project.output;
  S.stepId = S.project.steps.length ? S.project.steps[0].id : null;
  syncDialogueUI();
  renderSteps();
  renderStage();
  renderInspector();
  $('#mProjects').classList.add('hidden');
  watchProjectJobs();
}

async function refreshProject(keepStep = true) {
  if (!S.project) return;
  await flushSaves();             // 先把没保存的修改提交，再拉最新数据，不然会被旧数据盖住
  const sid = S.stepId;
  S.project = await api('/api/projects/' + S.project.id);
  if (keepStep && (isCard(sid) || S.project.steps.some(s => s.id === sid))) S.stepId = sid;
  else S.stepId = S.project.steps[0]?.id || null;
  $('#btnOpenVideo').disabled = !S.project.output;
  syncDialogueUI();
  renderSteps(); renderStage(); renderInspector();
}

async function showProjects() {
  const { projects } = await api('/api/projects');
  $('#projList').innerHTML = projects.length ? projects.map(p => `
    <div class="projrow" data-id="${p.id}">
      <img src="${p.thumbnail ? `/api/projects/${encodeURIComponent(p.id)}/file/screenshots/${encodeURIComponent(p.thumbnail)}` : ''}" alt="">
      <div class="grow">
        <div><b>${escapeHtml(p.name)}</b> ${p.recording ? `<span class="badge bad">${t('录制中')}</span>` : ''}
          ${p.source === 'slides' ? '<span class="badge ppt">PPT</span>' : ''}</div>
        <div class="small muted">${t('{n} 步', { n: p.steps })} · ${new Date(p.updated_at * 1000).toLocaleString(i18nLang())} ${p.output ? '· ' + t('已导出') : ''}</div>
      </div>
      <button class="sm danger" data-del="${p.id}">${t('删除')}</button>
    </div>`).join('') : `<p class="muted small">${t('还没有项目。用 Chrome 扩展录一段，或新建空项目。')}</p>`;
  $('#mProjects').classList.remove('hidden');
  $$('#projList .projrow').forEach(row => {
    row.onclick = (e) => {
      if (e.target.dataset.del) return;
      openProject(row.dataset.id);
    };
  });
  $$('#projList [data-del]').forEach(b => {
    b.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(t('删除这个项目？截图、语音和视频都会被永久删除。'))) return;
      await api('/api/projects/' + b.dataset.del, { method: 'DELETE' });
      if (S.project && S.project.id === b.dataset.del) {
        PENDING.steps.clear(); PENDING.project = null; PENDING.pid = '';
        S.project = null; location.href = '/'; return;
      }
      showProjects();
    };
  });
}

/* ---------- 步骤列表 ---------- */

const KIND_LABEL = {
  click: t('点击'), input: t('输入'), key: t('按键'), navigate: t('打开页面'),
  manual: t('截图'), scroll: t('滚动'), slide: t('幻灯片'), video: t('视频'),
};

/** 视频实际播放多长（和后端 clips.clip_range 同一套规则）。 */
function clipLen(c) {
  const total = c.duration || 0;
  const end = c.end > 0 && (!total || c.end <= total) ? c.end : total;
  const start = Math.min(Math.max(0, c.start || 0), Math.max(0, end - 0.1));
  return Math.max(0.1, end - start);
}

/** 播视频原声的视频步骤：解说不念，「字幕」是视频里讲的话（和后端 Step.plays_clip_audio 同一规则）。 */
function playsClipAudio(s) {
  const c = s && s.clip;
  return !!(s && s.kind === 'video' && c && c.file && c.mode !== 'poster' && c.audio !== 'mute' && c.has_audio);
}

/** 改了解说词：字幕默认跟着一起改；播视频原声的步骤字幕是视频里的讲话，不能被解说覆盖。 */
function narrationPatch(s, text) {
  if (playsClipAudio(s)) return { narration: text };
  $('#fCaption').value = text;
  return { narration: text, caption: text };
}

function stepSummary(s) {
  const tg = s.target || {};
  const label = (tg.text || tg.name || '').trim();
  if (s.kind === 'input') return t('在「{label}」输入 {value}', { label: label || t('输入框'), value: s.value || '' });
  if (s.kind === 'navigate') return s.page_title || s.url;
  if (s.kind === 'key') return t('在「{label}」按下 {key}', { label: label || t('输入框'), key: s.value });
  if (s.kind === 'manual') return s.page_title || t('手动截图');
  if (s.kind === 'slide') return s.page_title || t('第 {n} 页', { n: s.index + 1 });
  if (s.kind === 'video') {
    const c = s.clip || {};
    if (!c.file) return c.missing === 'online' ? t('在线视频，需要上传视频文件') : t('还没有视频文件，需要上传');
    return t('{name} · {sec} 秒', { name: c.source || t('视频'), sec: clipLen(c).toFixed(1) });
  }
  return t('点击「{label}」', { label: label || tg.tag || t('元素') });
}

function renderSteps() {
  const list = $('#stepList');
  const p = S.project;
  if (!p) { list.innerHTML = ''; return; }
  $('#stepCount').textContent = p.steps.filter(s => s.include).length;

  const cardRow = (id, icon, label, sub, on) => `
    <div class="step-card card-step ${id === S.stepId ? 'active' : ''} ${on ? '' : 'excluded'}"
         data-id="${id}">
      <div class="card-ico ${id === '__outro__' ? 'end' : ''}">${icon}</div>
      <div class="meta">
        <div class="t">${label}</div>
        <div class="d">${escapeHtml(sub || t('未设置，不会出现在视频里'))}</div>
      </div>
    </div>`;

  const introBg = cardStyle('intro').image ? cardStyle('intro').source : '';
  const outroBg = cardStyle('outro').image ? cardStyle('outro').source : '';
  const introOn = introEnabled() && !!(p.title || p.intro || introBg);
  const outroOn = outroEnabled() && !!(p.outro || outroBg);
  const off = t('已关闭，不会出现在视频里');
  const bgOnly = (name) => t('背景：{name}', { name });
  const html = [
    cardRow('__intro__', '▶', t('片头'),
      introOn ? (p.title || (introBg ? bgOnly(introBg) : p.name))
        : (p.title || p.intro || introBg ? off : ''), introOn),
    p.steps.length
      ? p.steps.map((s, i) => `
    <div class="step-card ${s.id === S.stepId ? 'active' : ''} ${s.include ? '' : 'excluded'}"
         data-id="${s.id}" draggable="true">
      <span class="no">${i + 1}</span>
      <img src="${s.screenshot ? fileUrl('screenshots', s.screenshot)
        : (s.kind === 'video' && s.clip && s.clip.file
          ? `/api/projects/${p.id}/steps/${s.id}/preview?t=0.5&scale=0.2` : '')}" loading="lazy" alt="">
      <div class="meta">
        <div class="t"><span class="kind-dot k-${escapeHtml(s.kind)}"></span>${escapeHtml(s.title || KIND_LABEL[s.kind] || s.kind)}</div>
        <div class="d">${escapeHtml(s.narration || stepSummary(s))}</div>
      </div>
    </div>`).join('')
      : `<p class="muted small" style="padding:10px 6px">${t('还没有录制步骤')}</p>`,
    cardRow('__outro__', '✓', t('片尾'),
      outroOn ? (p.outro || bgOnly(outroBg)) : (p.outro || outroBg ? off : ''), outroOn),
  ].join('');
  list.innerHTML = html;

  $$('#stepList .step-card').forEach(card => {
    card.onclick = () => selectStep(card.dataset.id);
    if (card.classList.contains('card-step')) return;   // 片头片尾不参与排序
    card.ondragstart = (e) => { e.dataTransfer.setData('text/plain', card.dataset.id); card.style.opacity = .4; };
    card.ondragend = () => { card.style.opacity = 1; $$('.step-card').forEach(c => c.classList.remove('dragover')); };
    card.ondragover = (e) => { e.preventDefault(); card.classList.add('dragover'); };
    card.ondragleave = () => card.classList.remove('dragover');
    card.ondrop = async (e) => {
      e.preventDefault();
      const from = e.dataTransfer.getData('text/plain');
      const to = card.dataset.id;
      if (!from || from === to) return;
      const arr = S.project.steps;
      const fi = arr.findIndex(s => s.id === from);
      const ti = arr.findIndex(s => s.id === to);
      const [moved] = arr.splice(fi, 1);
      arr.splice(ti, 0, moved);
      renderSteps();
      await api(`/api/projects/${S.project.id}/steps`, {
        method: 'PUT', body: { steps: arr.map(s => ({ id: s.id })) },
      });
      toast(t('已调整顺序'));
    };
  });
}

function currentStep() {
  return S.project ? S.project.steps.find(s => s.id === S.stepId) : null;
}

function isCard(id = S.stepId) { return id === '__intro__' || id === '__outro__'; }
function cardKind() { return S.stepId === '__intro__' ? 'intro' : 'outro'; }
function cardStyle(kind) { return (S.project && S.project[kind + '_card']) || {}; }

/** 改片头 / 片尾的背景设置（换页、放置方式、叠字、停留时间、恢复默认），然后刷新预览。 */
async function cardRequest(method, suffix, body, done) {
  if (!S.project || !isCard()) return;
  await flushSaves();
  try {
    S.project = await api(`/api/projects/${S.project.id}/card/${cardKind()}${suffix}`,
      { method, body: body || undefined });
    if (done) toast(done);
  } catch (e) { toast(e.message, true); }
  renderSteps(); renderInspector(); renderStage();
}

function renderCardBackground(kind) {
  const c = cardStyle(kind);
  const has = !!c.image;
  const badge = $('#pjBgBadge');
  badge.textContent = has ? (c.pages ? 'PPT / PDF' : t('图片')) : t('默认');
  badge.classList.toggle('own', has);
  $('#pjBgInfo').textContent = !has ? t('渐变背景，配色跟随视频风格')
    : c.pages > 1 ? t('{name} · 第 {page} 页，共 {pages} 页', { name: c.source, page: c.page, pages: c.pages })
      : c.source;
  $('#pjBgRemove').disabled = !has;
  $('#pjBgOpts').classList.toggle('hidden', !has);
  $('#pjBgPageWrap').classList.toggle('hidden', !(c.pages > 1));
  $('#pjBgPageLabel').textContent = t('使用第几页（共 {n} 页）', { n: c.pages || 1 });
  $('#pjBgPage').max = c.pages || 1;
  setField('#pjBgPage', String(c.page || 1));
  $('#pjBgFit').value = c.fit || 'contain';
  $('#pjBgText').checked = !!c.show_text;
  $('#pjBgTextLabel').textContent = kind === 'intro' ? t('在背景上显示标题文字') : t('在背景上显示片尾文字');
  setField('#pjBgDur', String(c.duration || 0));
}

function introEnabled() {
  const ps = (S.project && S.project.settings) || {};
  if (ps.intro_enabled !== undefined) return !!ps.intro_enabled;
  return S.settings.intro_enabled !== false;
}

function outroEnabled() {
  const ps = (S.project && S.project.settings) || {};
  if (ps.outro_enabled !== undefined) return !!ps.outro_enabled;
  return S.settings.outro_enabled !== false;
}

/* ---------- 保存：同一对象的多次修改合并成一次提交，任何字段都不会被后来的修改冲掉 ---------- */

const PENDING = { pid: '', steps: new Map(), project: null, timer: 0 };

function queueSave() {
  clearTimeout(PENDING.timer);
  PENDING.timer = setTimeout(flushSaves, 500);
}

async function flushSaves() {
  clearTimeout(PENDING.timer);
  const pid = PENDING.pid;
  if (!pid) return;
  const steps = [...PENDING.steps.entries()];
  const proj = PENDING.project;
  PENDING.steps.clear();
  PENDING.project = null;
  PENDING.pid = '';
  try {
    await Promise.all([
      ...steps.map(([sid, patch]) =>
        api(`/api/projects/${pid}/steps/${sid}`, { method: 'PATCH', body: patch })),
      proj ? api('/api/projects/' + pid, { method: 'PATCH', body: proj }) : null,
    ]);
    if (steps.length || proj) {
      renderSteps();
      if (proj && isCard()) renderStage();
    }
  } catch (e) {
    toast(t('保存失败：{error}', { error: e.message }), true);
  }
}

window.addEventListener('beforeunload', (e) => {
  if (PENDING.steps.size || PENDING.project) {
    flushSaves();
    e.preventDefault();
    e.returnValue = '';
  }
});

function saveProject(patch) {
  if (!S.project) return;
  if (patch.settings) {
    S.project.settings = { ...(S.project.settings || {}), ...patch.settings };
  }
  const rest = { ...patch };
  delete rest.settings;
  Object.assign(S.project, rest);
  PENDING.pid = S.project.id;
  const cur = PENDING.project || {};
  PENDING.project = {
    ...cur, ...rest,
    ...(patch.settings ? { settings: { ...(cur.settings || {}), ...patch.settings } } : {}),
  };
  queueSave();
}

function selectStep(id) {
  flushSaves();
  S.stepId = id;
  renderSteps(); renderStage(); renderInspector();
}

/* ---------- 中间预览 ---------- */

function viewportOf(s) {
  const ratio = (s.viewport_w && s.img_w) ? s.img_w / s.viewport_w : 1;
  return { w: s.viewport_w || s.img_w || 1, h: (s.img_h || 1) / ratio };
}

function renderStage() {
  const card = isCard();
  const s = currentStep();
  const empty = !s && !card;
  const mode = card ? 'render' : S.mode;
  $('#emptyState').classList.toggle('hidden', !empty);
  const video = $('#stageVideo');
  const showVideo = !empty && !card && mode === 'shot' && s.kind === 'video' && !!(s.clip && s.clip.file);
  $('#shotWrap').classList.toggle('hidden', empty || mode !== 'shot' || showVideo);
  $('#previewImg').classList.toggle('hidden', empty || mode !== 'render');
  video.classList.toggle('hidden', !showVideo);
  if (!showVideo && !video.paused) video.pause();
  $('#tabShot').classList.toggle('primary', mode === 'shot');
  $('#tabRender').classList.toggle('primary', mode === 'render');
  $('#tabShot').disabled = card;
  $('#btnDeleteStep').disabled = card || empty;
  if (empty) return;

  if (card) {
    const kind = S.stepId === '__intro__' ? 'intro' : 'outro';
    $('#previewImg').src =
      `/api/projects/${S.project.id}/card/${kind}/preview?t=1.4&scale=0.55&_=${Date.now()}`;
    $('#stageHint').textContent = kind === 'intro' ? t('片头画面预览') : t('片尾画面预览');
    $('#btnPlayAudio').disabled = false;
    return;
  }

  if (showVideo) {
    const url = fileUrl('media', s.clip.file);
    if (video.dataset.src !== url) {
      video.dataset.src = url;
      video.src = url;
      video.onloadedmetadata = () => { video.currentTime = s.clip.start || 0; };
    }
    $('#stageHint').textContent = t('拖动进度条找到想要的起止点，再点右边的「当前位置」');
    return;
  }

  if (S.mode === 'shot') {
    $('#shot').src = s.screenshot ? fileUrl('screenshots', s.screenshot) : '';
    $('#shot').onload = () => { drawHighlight(); renderRedactions(); };
    drawHighlight();
    renderRedactions();
    if (!S.redactMode) {
      $('#stageHint').textContent = s.target && s.target.rect ? t('拖动橙色框可调整高亮区域') : t('这一步没有高亮区域');
    }
  } else {
    renderRedactions();
    $('#previewImg').src = `/api/projects/${S.project.id}/steps/${s.id}/preview?t=1.3&scale=0.55&_=${Date.now()}`;
    $('#stageHint').textContent = t('视频里的实际画面（含高亮、字幕、序号）');
  }
}

function drawHighlight() {
  const s = currentStep();
  const hl = $('#hl');
  if (!s || !s.target || !s.target.rect || !s.highlight || S.redactMode) {
    hl.classList.add('hidden');
    return;
  }
  const vp = viewportOf(s);
  const r = s.target.rect;
  hl.classList.remove('hidden');
  hl.style.left = (r.x / vp.w * 100) + '%';
  hl.style.top = (r.y / vp.h * 100) + '%';
  hl.style.width = (r.w / vp.w * 100) + '%';
  hl.style.height = (r.h / vp.h * 100) + '%';
  hl.querySelector('.tip').textContent = (s.target.text || s.target.tag || '').slice(0, 24);
}

function initHighlightDrag() {
  const hl = $('#hl');
  let mode = null, sx = 0, sy = 0, orig = null;

  const start = (e, m) => {
    const s = currentStep();
    if (!s || !s.target || !s.target.rect) return;
    mode = m; sx = e.clientX; sy = e.clientY; orig = { ...s.target.rect };
    e.preventDefault(); e.stopPropagation();
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', end);
  };
  const move = (e) => {
    const s = currentStep();
    if (!s || !mode) return;
    const img = $('#shot');
    const vp = viewportOf(s);
    const kx = vp.w / img.clientWidth;
    const ky = vp.h / img.clientHeight;
    const dx = (e.clientX - sx) * kx;
    const dy = (e.clientY - sy) * ky;
    const r = s.target.rect;
    if (mode === 'move') { r.x = Math.max(0, orig.x + dx); r.y = Math.max(0, orig.y + dy); }
    else { r.w = Math.max(10, orig.w + dx); r.h = Math.max(10, orig.h + dy); }
    drawHighlight();
  };
  const end = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', end);
    if (mode) saveStep({ target_rect: currentStep().target.rect });
    mode = null;
  };
  hl.addEventListener('mousedown', (e) => start(e, 'move'));
  hl.querySelector('.handle').addEventListener('mousedown', (e) => start(e, 'resize'));
}

/* ---------- 打码框 ---------- */

const RD_LABEL = {
  email: t('邮箱'), phone: t('手机号'), id_card: t('身份证'), bank: t('银行卡'),
  ip: 'IP', name: t('人名'), keyword: t('关键词'), manual: t('手动'),
};

function toVp(s, clientX, clientY) {
  const r = $('#shot').getBoundingClientRect();
  const vp = viewportOf(s);
  return {
    x: (clientX - r.left) * vp.w / Math.max(1, r.width),
    y: (clientY - r.top) * vp.h / Math.max(1, r.height),
  };
}

function renderRedactions() {
  const layer = $('#redactLayer');
  const s = currentStep();
  layer.classList.toggle('on', !!S.redactMode);
  if (!s || S.mode !== 'shot') { layer.innerHTML = ''; return; }
  const vp = viewportOf(s);
  layer.innerHTML = (s.redactions || []).map(r => `
    <div class="rbox ${r.auto ? 'auto' : ''} ${r.id === S.rdSel ? 'sel' : ''}" data-id="${r.id}"
         style="left:${r.x / vp.w * 100}%;top:${r.y / vp.h * 100}%;
                width:${r.w / vp.w * 100}%;height:${r.h / vp.h * 100}%">
      <span class="rlabel">${escapeHtml(r.label
        ? t('{kind}：{label}', { kind: RD_LABEL[r.kind] || r.kind, label: r.label.slice(0, 14) })
        : (RD_LABEL[r.kind] || r.kind))}</span>
      <span class="rdel" data-del="${r.id}">×</span>
      <span class="rgrip"></span>
    </div>`).join('');
}

function saveRedactions(rerender = true) {
  const s = currentStep();
  if (!s) return;
  clearTimeout(S.rdTimer);
  S.rdTimer = setTimeout(async () => {
    try {
      await api(`/api/projects/${S.project.id}/steps/${s.id}/redactions`, {
        method: 'PUT', body: { redactions: s.redactions },
      });
      if (rerender) renderRedactList();
    } catch (e) { toast(t('打码保存失败：{error}', { error: e.message }), true); }
  }, 350);
}

function renderRedactList() {
  const s = currentStep();
  if (!s) return;
  const list = s.redactions || [];
  $('#rdCount').textContent = list.length ? t('（{n}）', { n: list.length }) : '';
  $('#rdList').innerHTML = list.length ? list.map(r => `
    <div class="rd-item" data-id="${r.id}">
      <span class="k ${escapeHtml(r.kind)}">${escapeHtml(RD_LABEL[r.kind] || r.kind)}</span>
      <span class="lb muted">${escapeHtml(r.label || t('手动框选'))}</span>
      <button class="sm" data-mode="${r.id}" title="${t('切换打码方式')}">${
        { blur: t('模糊'), pixelate: t('像素'), solid: t('色块') }[r.mode] || r.mode}</button>
      <button class="sm danger" data-rm="${r.id}">×</button>
    </div>`).join('') : `<span class="muted">${t('这一步没有打码区域')}</span>`;

  $$('#rdList [data-rm]').forEach(b => b.onclick = () => {
    s.redactions = s.redactions.filter(r => r.id !== b.dataset.rm);
    renderRedactions(); renderRedactList(); saveRedactions(false);
  });
  $$('#rdList [data-mode]').forEach(b => b.onclick = () => {
    const r = s.redactions.find(x => x.id === b.dataset.mode);
    if (!r) return;
    r.mode = { blur: 'pixelate', pixelate: 'solid', solid: 'blur' }[r.mode] || 'blur';
    renderRedactList(); saveRedactions(false);
  });
  $$('#rdList .rd-item').forEach(el => el.onmouseenter = () => {
    S.rdSel = el.dataset.id; renderRedactions();
  });
}

function initRedactDrag() {
  const layer = $('#redactLayer');
  let mode = null, start = null, orig = null, target = null;

  layer.addEventListener('mousedown', (e) => {
    if (!S.redactMode) return;
    const s = currentStep();
    if (!s) return;
    e.preventDefault();
    const del = e.target.dataset && e.target.dataset.del;
    if (del) {
      s.redactions = s.redactions.filter(r => r.id !== del);
      renderRedactions(); renderRedactList(); saveRedactions(false);
      return;
    }
    const boxEl = e.target.closest && e.target.closest('.rbox');
    start = toVp(s, e.clientX, e.clientY);
    if (boxEl) {
      target = s.redactions.find(r => r.id === boxEl.dataset.id);
      if (!target) return;
      orig = { ...target };
      mode = e.target.classList.contains('rgrip') ? 'resize' : 'move';
      S.rdSel = target.id;
    } else {
      target = {
        id: 'r_' + Math.random().toString(36).slice(2, 12),
        x: start.x, y: start.y, w: 0, h: 0,
        mode: S.rdDefaultMode || 'blur', kind: 'manual', label: '', auto: false,
      };
      s.redactions = s.redactions || [];
      s.redactions.push(target);
      orig = { ...target };
      mode = 'create';
      S.rdSel = target.id;
    }
    renderRedactions();
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', end);
  });

  const move = (e) => {
    const s = currentStep();
    if (!s || !mode || !target) return;
    const p = toVp(s, e.clientX, e.clientY);
    if (mode === 'create') {
      target.x = Math.min(orig.x, p.x); target.y = Math.min(orig.y, p.y);
      target.w = Math.abs(p.x - orig.x); target.h = Math.abs(p.y - orig.y);
    } else if (mode === 'move') {
      target.x = Math.max(0, orig.x + (p.x - start.x));
      target.y = Math.max(0, orig.y + (p.y - start.y));
    } else {
      target.w = Math.max(6, orig.w + (p.x - start.x));
      target.h = Math.max(6, orig.h + (p.y - start.y));
    }
    renderRedactions();
  };

  const end = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', end);
    const s = currentStep();
    if (s && target && (target.w < 5 || target.h < 5)) {
      s.redactions = s.redactions.filter(r => r !== target);
      renderRedactions();
    }
    if (mode) { renderRedactList(); saveRedactions(false); }
    mode = null; target = null;
  };
}

function setRedactMode(on) {
  S.redactMode = on;
  $('#btnRedactMode').classList.toggle('primary', on);
  if (on && S.mode !== 'shot') { S.mode = 'shot'; renderStage(); }
  $('#hl').classList.toggle('hidden', on || !currentStep());
  $('#stageHint').textContent = on
    ? t('在截图上拖动框选要打码的区域；拖框可移动，右下角可缩放，× 删除')
    : t('拖动橙色框可调整高亮区域');
  renderRedactions();
  if (!on) drawHighlight();
}

/* ---------- 右侧属性 ---------- */

function renderInspector() {
  const card = isCard();
  const s = currentStep();
  $('#inspEmpty').classList.toggle('hidden', !!s || card);
  $('#inspProject').classList.toggle('hidden', !card);
  $('#inspBody').classList.toggle('hidden', !s || card);

  if (card) {
    const p = S.project;
    const isIntro = S.stepId === '__intro__';
    $('#pjHead').textContent = isIntro ? t('片头') : t('片尾');
    $('#pjIntroFields').classList.toggle('hidden', !isIntro);
    $('#pjOutroFields').classList.toggle('hidden', isIntro);
    setField('#pjTitle', p.title || '');
    setField('#pjSubtitle', p.subtitle || '');
    setField('#pjIntro', p.intro || '');
    setField('#pjOutro', p.outro || '');
    $('#pjIntroOn').checked = introEnabled();
    $('#pjOutroOn').checked = outroEnabled();
    const txt = isIntro ? p.intro : p.outro;
    const on = isIntro ? introEnabled() : outroEnabled();
    const hasBg = !!cardStyle(isIntro ? 'intro' : 'outro').image;
    $('#pjTTS').disabled = !on || !(txt || '').trim();
    $('#pjAudioInfo').textContent = !on
      ? (isIntro ? t('片头已关闭：不会出现在视频里，也不会合成配音。文案会保留，勾回来就恢复。')
                 : t('片尾已关闭：不会出现在视频里，也不会合成配音。文案会保留，勾回来就恢复。'))
      : (txt || '').trim()
        ? t('点「② 合成语音」时会自动补上这段配音；改过文案后旧配音会自动作废。')
        : isIntro ? t('没有旁白文案，这一段将静音显示。')
          : hasBg ? t('没有片尾文案：视频结尾只显示背景画面，没有配音。')
            : t('片尾文案为空，视频里不会有片尾。');
    renderCardBackground(isIntro ? 'intro' : 'outro');
    return;
  }
  if (!s) return;
  const tg = s.target || {};
  const chips = [
    `<span class="chip">${escapeHtml(KIND_LABEL[s.kind] || s.kind)}</span>`,
    tg.text ? `<span class="chip" title="${escapeHtml(tg.text)}">「${escapeHtml(tg.text.slice(0, 22))}」</span>` : '',
    tg.role ? `<span class="chip">${escapeHtml(tg.role)}</span>` : '',
    s.value ? `<span class="chip">${escapeHtml(t('值：{value}', { value: s.value.slice(0, 18) }))}</span>` : '',
    s.page_title ? `<span class="chip" title="${escapeHtml(s.url)}">${escapeHtml(s.page_title.slice(0, 24))}</span>` : '',
  ].join('');
  $('#stepFacts').innerHTML = chips;
  setField('#fTitle', s.title || '');
  setField('#fNarration', s.narration || '');
  setField('#fCaption', s.caption || '');
  const dlg = isDialogue() && s.kind !== 'video';
  $('#fNarrationWrap').classList.toggle('hidden', dlg);
  $('#dlgWrap').classList.toggle('hidden', !dlg);
  $('#btnUseNotes').classList.toggle('hidden', dlg);
  if (dlg) renderDialogue(s);
  setField('#fNote', s.note || '');
  $('#fInclude').checked = !!s.include;
  $('#fZoom').checked = !!s.zoom;
  $('#fHighlight').checked = !!s.highlight;
  $('#fDuration').value = s.duration_override || 0;
  $('#btnPlayAudio').disabled = !s.audio;
  renderVoiceCard(s);
  renderSlideBox(s);
  renderVideoBox(s);
  renderRedactList();
}

function saveStep(patch) {
  const s = currentStep();
  if (!s) return;
  if (!patch.target_rect) {
    // 服务端规则：AI 配音的步骤改了解说词，旧配音作废；原声步骤改文字只是修字幕，录音保留
    if ('narration' in patch && patch.narration !== s.narration && s.voice_source !== 'own') {
      s.audio = ''; s.audio_duration = 0; s.boundaries = [];
      renderVoiceCard(s);
    }
    Object.assign(s, patch);
  }
  PENDING.pid = S.project.id;
  PENDING.steps.set(s.id, { ...(PENDING.steps.get(s.id) || {}), ...patch });
  queueSave();
}

/** 输入框正被编辑时不回填，避免光标跳到末尾、吞掉刚打的字 */
function setField(sel, value) {
  const el = $(sel);
  if (document.activeElement === el) return;
  if (el.value !== value) el.value = value;
}

/* ---------- 任务进度 ---------- */

function setProgress(frac, text) {
  $('#progressBar').firstElementChild.style.width = Math.round(frac * 100) + '%';
  $('#progressText').textContent = text || '';
}

function busy(on) {
  ['btnAuto', 'btnScript', 'btnTTS', 'btnRender', 'btnTranslate'].forEach(id => {
    $('#' + id).disabled = on;
  });
}

async function runJob(path, body, label) {
  if (!S.project) return;
  await flushSaves();             // 任务要读到你刚改的文字
  busy(true);
  setProgress(0.02, label + '…');
  let job;
  try {
    job = await api(path, { method: 'POST', body });
  } catch (e) { busy(false); setProgress(0, e.message); toast(e.message, true); return; }
  return pollJob(job.id, label);
}

/** 轮询一个后台任务直到结束。onTick 可用来同步弹窗里的进度文字。 */
const POLLING = new Map();
const JOB_LABELS = new Map();

function pollJob(jobId, label, { refresh = true, onTick = null } = {}) {
  if (POLLING.has(jobId)) return POLLING.get(jobId);
  const p = pollJobInner(jobId, label, refresh, onTick).finally(() => {
    POLLING.delete(jobId);
    JOB_LABELS.delete(jobId);
    if (!POLLING.size) { busy(false); S.jobActive = false; }
    updateStopButton();
  });
  POLLING.set(jobId, p);
  JOB_LABELS.set(jobId, label);
  updateStopButton();
  return p;
}

/* ---------- 停止任务 ---------- */

function updateStopButton() {
  const btn = $('#btnStopJob');
  const running = POLLING.size > 0;
  btn.classList.toggle('hidden', !running);
  if (!running) {
    btn.disabled = false;
    btn.textContent = t('■ 停止');
  }
}

async function stopJobs() {
  const ids = [...POLLING.keys()];
  if (!ids.length) return;
  const labels = ids.map(id => JOB_LABELS.get(id));
  if (labels.includes(JOB_LABEL.magic_mic)
      && !confirm(t('现在停止会丢弃这段讲解录音（还没识别完）。确定停止吗？'))) return;
  const btn = $('#btnStopJob');
  btn.disabled = true;
  btn.textContent = t('正在停止…');
  await Promise.all(ids.map(id =>
    api(`/api/jobs/${id}/cancel`, { method: 'POST' }).catch(() => null)));
}

function pollJobInner(jobId, label, refresh, onTick) {
  S.jobActive = true;
  busy(true);
  return new Promise((resolve) => {
    const tick = async () => {
      let j;
      try { j = await api('/api/jobs/' + jobId); } catch (e) { setTimeout(tick, 1500); return; }
      setProgress(j.progress, j.message);
      if (onTick) onTick(j);
      if (j.status === 'running' || j.status === 'pending') { setTimeout(tick, 900); return; }
      if (j.status === 'cancelled') {
        setProgress(0, t('{label}已停止', { label }));
        toast(t('已停止。已经做完的部分保留着，改完再点一次就会接着做'));
        if (refresh && S.project) await refreshProject();
        resolve(null);
        return;
      }
      if (j.status === 'error') {
        toast(j.error, true);
        setProgress(0, t('失败：{error}', { error: j.error.slice(0, 60) }));
        resolve(null);
        return;
      }
      // 做完了但有需要注意的地方（比如某几段配音文件坏了、按静音生成）：用醒目的提示代替「完成」
      if (j.result && j.result.warning) toast(j.result.warning, true);
      else toast(t('{label}完成', { label }));
      if (refresh && S.project) await refreshProject();
      resolve(j.result);
    };
    tick();
  });
}

async function postForm(path, form) {
  const res = await fetch(path, { method: 'POST', body: form });
  let body = {};
  try { body = await res.json(); } catch (e) { /* */ }
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

const JOB_LABEL = {
  magic_mic: t('识别讲解录音'), voice: t('处理录音'), dictate: t('口述识别'), tts: t('合成语音'),
  render: t('渲染视频'), script: t('生成解说'), auto: t('一键生成'), translate: t('翻译'),
  slides_create: t('生成幻灯片视频'), asr_prepare: t('准备语音识别模型'),
};

/** 打开项目时，如果后台已经有它的任务在跑（比如刚录完的讲解在识别），接上进度。 */
async function watchProjectJobs() {
  if (!S.project || S.jobActive) return;
  try {
    const { jobs } = await api('/api/jobs?project_id=' + S.project.id);
    const run = jobs.find(j => (j.status === 'running' || j.status === 'pending') && !POLLING.has(j.id));
    if (run) await pollJob(run.id, JOB_LABEL[run.kind] || t('后台任务'));
  } catch (e) { /* */ }
}

/* ---------- 录制状态轮询 ---------- */

async function pollRecording() {
  try {
    const st = await api('/api/capture/status');
    const b = $('#badgeRec');
    if (st.recording) {
      b.textContent = t('● 录制中 {n} 步', { n: st.steps });
      b.className = 'badge bad';
      if (S.project && S.project.id === st.project_id && st.steps !== S.project.steps.length) {
        await refreshProject();
      }
    } else { b.textContent = ''; b.className = 'badge'; }
    watchProjectJobs();
  } catch (e) { /* 服务没开 */ }
  setTimeout(pollRecording, 2500);
}

/* ---------- 设置表单 ---------- */

function shortName(name) { return (name || t('AI 模型')).replace(/\s*[（(].*?[）)]/g, ''); }

function aiName() {
  const ai = (S.health && S.health.llm) || (S.settings && S.settings.llm) || {};
  return shortName(ai.name);
}

/* ---------- AI 服务商设置 ---------- */

function llmPreset(pid) {
  return ((S.settings.llm || {}).providers || []).find(p => p.id === pid);
}

/* 商用配音服务（豆包、MiniMax、通义）的 Key */
function ttsDraft(id) {
  S.ttsDrafts = S.ttsDrafts || {};
  return S.ttsDrafts[id] || (S.ttsDrafts[id] = {});
}

function renderTtsFields() {
  const id = $('#sTtsSvc').value;
  const x = (S.settings.tts_services || []).find(v => v.id === id);
  if (!x) return;
  const d = ttsDraft(id);
  $('#sTtsHint').innerHTML = escapeHtml(x.hint)
    + ` <a href="${escapeHtml(x.key_url)}" target="_blank" rel="noopener">${t('申请 API Key ↗')}</a>`;
  $('#sTtsKey').value = d.api_key || '';
  let st;
  if (d.clear_key) st = t('保存后会清除已存的 Key');
  else if (x.key_from.startsWith('env:')) st = t('正在使用环境变量 {env} 里的 Key（优先于这里填的）', { env: x.key_from.slice(4) });
  else if (x.key_from === 'llm') st = t('用的是「AI 模型」里同一家的 Key：{key}', { key: x.key_masked });
  else if (x.key_masked) st = t('已保存：{key}', { key: x.key_masked });
  else st = t('未设置');
  $('#sTtsState').textContent = st;
  $('#btnTtsClear').classList.toggle('hidden', x.key_from !== 'config' || !!d.clear_key);
  $('#sTtsVoicesWrap').classList.toggle('hidden', id !== 'doubao');
  $('#sTtsVoices').value = d.voices !== undefined ? d.voices : (x.voices || '');
}

function ttsSettingsPatch() {
  const out = {};
  for (const [id, d] of Object.entries(S.ttsDrafts || {})) {
    const e = {};
    if ((d.api_key || '').trim()) e.api_key = d.api_key.trim();
    if (d.clear_key) e.clear_key = true;
    if (d.voices !== undefined) e.voices = d.voices.trim();
    if (Object.keys(e).length) out[id] = e;
  }
  return out;
}

function llmDraft(pid) {
  return S.llmDrafts[pid] || (S.llmDrafts[pid] = {});
}

function renderProviderFields() {
  const pid = $('#sProvider').value;
  const p = llmPreset(pid);
  if (!p) return;
  const d = llmDraft(pid);
  const esc = escapeHtml;
  $('#sProviderHint').innerHTML = esc(p.hint)
    + (p.key_url ? ` <a href="${esc(p.key_url)}" target="_blank" rel="noopener">${p.needs_key ? t('申请 API Key ↗') : t('下载 ↗')}</a>` : '');

  const showBase = p.base_url_required || pid === 'ollama' || !!p.saved_base_url || d.base_url !== undefined;
  $('#sBaseField').classList.toggle('hidden', !showBase);
  $('#sBaseUrl').placeholder = p.base_url_hint || p.base_url || '';
  $('#sBaseUrl').value = d.base_url !== undefined ? d.base_url : (p.saved_base_url || '');

  $('#sKeyLabel').textContent = p.needs_key ? 'API Key' : t('API Key（可选）');
  $('#sKey').value = d.api_key || '';
  let keyState;
  if (d.clear_key) keyState = t('保存后会清除已存的 Key');
  else if (p.key_env) keyState = t('正在使用环境变量 {env} 里的 Key（优先于这里填的）', { env: p.key_env });
  else if (p.key_masked) keyState = t('已保存：{key}', { key: p.key_masked });
  else keyState = p.needs_key ? t('未设置') : t('不需要 Key');
  $('#sKeyState').textContent = keyState;
  $('#btnClearKey').classList.toggle('hidden', !p.key_masked || !!p.key_env || !!d.clear_key);

  $('#sModel').value = d.model !== undefined ? d.model : (p.saved_model || '');
  $('#sModel').placeholder = pid === 'azure' ? t('你的部署名，例如 gpt-5-mini') : (p.model || t('模型名'));
  const models = [...new Set([...(p.models || []), ...(S.llmModels[pid] || [])])];
  $('#sModelList').innerHTML = models.map(m => `<option value="${esc(m)}">`).join('');
  $('#sTestState').textContent = '';
}

function llmForm() {
  return { provider: $('#sProvider').value, api_key: $('#sKey').value.trim(),
           base_url: $('#sBaseUrl').value.trim(), model: $('#sModel').value.trim() };
}

function fillSettingsForm() {
  const c = S.settings;
  const ai = c.llm || { providers: [] };
  S.llmDrafts = {};
  $('#sProvider').innerHTML = ai.providers.map(p =>
    `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}${p.configured ? ' ✓' : ''}</option>`).join('');
  $('#sProvider').value = ai.provider;
  renderProviderFields();
  $('#sRate').value = c.tts_rate || '+0%';
  S.ttsDrafts = {};
  const svcs = c.tts_services || [];
  const keep = $('#sTtsSvc').value;
  $('#sTtsSvc').innerHTML = svcs.map(x =>
    `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)}${x.configured ? ' ✓' : ''}</option>`).join('');
  if (keep && svcs.some(x => x.id === keep)) $('#sTtsSvc').value = keep;
  renderTtsFields();
  $('#sRes').value = `${c.video_width}x${c.video_height}`;
  $('#sFps').value = String(c.video_fps || 30);
  $('#sAccent').value = c.accent_color || '#FF5C39';
  $('#sBg').value = c.background_color || '#0E1116';
  $('#sZoomEnabled').checked = !!c.zoom_enabled;
  $('#sDim').checked = !!c.dim_background;
  $('#sCursor').checked = c.show_cursor !== false;
  $('#sBadge').checked = c.show_step_badge !== false;
  $('#sFrame').checked = c.browser_frame !== false;
  $('#sSubs').checked = !!c.burn_subtitles;
  $('#sSubSpace').checked = c.second_sub_space !== false;
  $('#sIntro').checked = !!c.intro_enabled;
  $('#sOutro').checked = c.outro_enabled !== false;
  $('#sMinDur').value = c.min_step_duration ?? 2.5;
  $('#sZoomF').value = c.zoom_factor ?? 1.35;
  $('#sAsrModel').value = c.asr_model || 'small';
  $('#sHfEndpoint').value = c.hf_endpoint || '';
  $('#sFillers').checked = c.asr_remove_fillers !== false;
  syncLanguageFields();
}

function llmSettingsPatch() {
  const out = {};
  for (const [pid, d] of Object.entries(S.llmDrafts)) {
    const e = {};
    if (d.model !== undefined) e.model = d.model.trim();
    if (d.base_url !== undefined) e.base_url = d.base_url.trim();
    if ((d.api_key || '').trim()) e.api_key = d.api_key.trim();
    if (d.clear_key) e.clear_key = true;
    if (Object.keys(e).length) out[pid] = e;
  }
  // 当前选中的这家：模型框里显示的就是要保存的（哪怕是预设的默认值）
  const cur = $('#sProvider').value;
  out[cur] = { ...(out[cur] || {}), model: $('#sModel').value.trim() };
  return out;
}

async function saveSettings() {
  const prevLang = S.settings.language;
  const prevVoice = S.settings.voice;
  const [w, h] = $('#sRes').value.split('x').map(Number);
  const patch = {
    llm_provider: $('#sProvider').value,
    llm_providers: llmSettingsPatch(),
    language: $('#sLang').value,
    voice: $('#sVoice').value,
    tts_rate: $('#sRate').value,
    tts_services: ttsSettingsPatch(),
    video_width: w, video_height: h, video_fps: Number($('#sFps').value),
    accent_color: $('#sAccent').value, background_color: $('#sBg').value,
    zoom_enabled: $('#sZoomEnabled').checked,
    dim_background: $('#sDim').checked,
    show_cursor: $('#sCursor').checked,
    show_step_badge: $('#sBadge').checked,
    browser_frame: $('#sFrame').checked,
    burn_subtitles: $('#sSubs').checked,
    second_sub_space: $('#sSubSpace').checked,
    intro_enabled: $('#sIntro').checked,
    outro_enabled: $('#sOutro').checked,
    min_step_duration: Number($('#sMinDur').value),
    zoom_factor: Number($('#sZoomF').value),
    asr_model: $('#sAsrModel').value,
    hf_endpoint: $('#sHfEndpoint').value.trim(),
    asr_remove_fillers: $('#sFillers').checked,
  };
  // 列表还没加载出来（比如音色要联网取）时下拉框是空的：空值不提交，保持原来的设置
  if (!patch.language) delete patch.language;
  if (!patch.voice) delete patch.voice;
  S.settings = await api('/api/settings', { method: 'POST', body: patch });
  $('#sKey').value = '';
  fillSettingsForm();
  if (Object.keys(patch.tts_services || {}).length) loadVoices();     // 填了配音服务的 Key：它的音色要加进下拉框
  await loadHealth();
  // 这里是全局默认值。只有你在这次保存里真的改了语言 / 音色，才同步到当前打开的项目，
  // 否则一个已翻译成英文的项目会被悄悄改回默认的中文音色。
  const langChanged = !!patch.language && patch.language !== prevLang;
  const voiceChanged = !!patch.voice && patch.voice !== prevVoice;
  if (S.project && (langChanged || voiceChanged)) {
    const body = {};
    if (langChanged) body.language = patch.language;
    if (voiceChanged) body.voice = patch.voice;
    await api('/api/projects/' + S.project.id, { method: 'PATCH', body });
    await refreshProject();
  }
  toast(t('设置已保存'));
  $('#mSettings').classList.add('hidden');
}

/* ---------- 事件绑定 ---------- */

/* ---------- 界面语言 ---------- */

function bindLanguageSwitch() {
  const sel = $('#uiLang');
  const langs = (window.VT_I18N && window.VT_I18N.langs) || { zh: '中文' };   // i18n: ignore
  sel.innerHTML = Object.entries(langs)
    .map(([code, name]) => `<option value="${code}">${escapeHtml(name)}</option>`).join('');
  sel.value = i18nLang();
  sel.onchange = async () => {
    sel.disabled = true;
    try {
      await flushSaves();
      await api('/api/settings', { method: 'POST', body: { ui_language: sel.value } });
      location.reload();
    } catch (e) {
      sel.disabled = false;
      sel.value = i18nLang();
      toast(e.message, true);
    }
  };
}

function bindUI() {
  bindLanguageSwitch();
  initHighlightDrag();
  initRedactDrag();
  bindVoiceAndImport();

  // 打码
  $('#btnRedactMode').onclick = () => setRedactMode(!S.redactMode);
  $('#btnRedactHere').onclick = () => setRedactMode(true);
  $('#btnRedactScan').onclick = async () => {
    $('#mRedact').classList.remove('hidden');
    try {
      const st = await api(`/api/projects/${S.project.id}/redact/stats`);
      $('#rdStats').textContent =
        t('当前共 {regions} 个打码区域（其中自动识别 {auto} 个），本机记录了 {indexed} 段页面文字可供扫描。',
          { regions: st.regions, auto: st.auto, indexed: st.indexed });
    } catch (e) { $('#rdStats').textContent = ''; }
  };
  $('#btnCloseRedact').onclick = () => $('#mRedact').classList.add('hidden');
  $('#rdMode').onchange = () => { S.rdDefaultMode = $('#rdMode').value; };
  $('#btnRunRedact').onclick = async () => {
    const body = {
      builtin: $('#rdBuiltin').checked,
      names: $('#rdNames').checked,
      names_guess: $('#rdNamesGuess').checked,
      mode: $('#rdMode').value,
      keywords: $('#rdKeywords').value.split(/[,，\n]/).map(x => x.trim()).filter(x => x.length > 1),
    };
    $('#btnRunRedact').disabled = true;
    try {
      const r = await api(`/api/projects/${S.project.id}/redact/scan`, { method: 'POST', body });
      toast(t('新增 {regions} 个打码区域，抹掉 {masked} 处文字', { regions: r.regions, masked: r.masked }));
      $('#mRedact').classList.add('hidden');
      await refreshProject();
    } catch (e) { toast(e.message, true); }
    $('#btnRunRedact').disabled = false;
  };
  $('#btnRdClearAuto').onclick = async () => {
    if (!confirm(t('删除所有自动识别的打码框？手动画的会保留。'))) return;
    const r = await api(`/api/projects/${S.project.id}/redact/clear-auto`, { method: 'POST', body: {} });
    toast(t('已删除 {n} 个', { n: r.removed }));
    $('#mRedact').classList.add('hidden');
    await refreshProject();
  };
  $('#btnRdClearIndex').onclick = async () => {
    if (!confirm(t('清空本机保存的页面文字索引？\n已经打好的码不受影响，但之后无法再按关键词补打码。'))) return;
    const r = await api(`/api/projects/${S.project.id}/redact/clear-index`, { method: 'POST' });
    toast(t('已清空 {n} 段文字', { n: r.removed }));
    $('#mRedact').classList.add('hidden');
  };

  $('#btnProjects').onclick = showProjects;
  $('#btnOpenProjects').onclick = showProjects;
  $('#btnCloseProjects').onclick = () => $('#mProjects').classList.add('hidden');
  $('#btnNewProject').onclick = async () => {
    const p = await api('/api/projects', { method: 'POST', body: { name: t('空项目') } });
    openProject(p.id);
  };
  $('#btnRefresh').onclick = () => refreshProject();

  $('#projName').onchange = async () => {
    if (!S.project) return;
    await api('/api/projects/' + S.project.id, { method: 'PATCH', body: { name: $('#projName').value } });
    toast(t('已重命名'));
  };

  $('#tabShot').onclick = () => { S.mode = 'shot'; renderStage(); };
  $('#tabRender').onclick = () => { S.mode = 'render'; renderStage(); };

  $('#btnDeleteStep').onclick = async () => {
    const s = currentStep();
    if (!s || !confirm(t('删除这一步？'))) return;
    await api(`/api/projects/${S.project.id}/steps/${s.id}`, { method: 'DELETE' });
    S.stepId = null;
    await refreshProject(false);
  };

  S.audio.onerror = () => toast(t('这一段还没有配音，先点「合成语音」'), true);

  $('#btnPlayAudio').onclick = () => {
    if (isCard()) {
      S.audio.src = fileUrl('audio', `${S.stepId}.mp3`) + '?t=' + Date.now();
      S.audio.play().catch(() => {});
      return;
    }
    const s = currentStep();
    if (!s || !s.audio) return;
    S.audio.src = fileUrl('audio', s.audio) + '?t=' + Date.now();
    S.audio.play().catch(() => {});
  };

  // 片头 / 片尾
  $('#pjTitle').oninput = () => saveProject({ title: $('#pjTitle').value });
  $('#pjSubtitle').oninput = () => saveProject({ subtitle: $('#pjSubtitle').value });
  $('#pjIntro').oninput = () => saveProject({ intro: $('#pjIntro').value });
  $('#pjOutro').oninput = () => saveProject({ outro: $('#pjOutro').value });
  $('#pjIntroOn').onchange = () => {
    saveProject({ settings: { intro_enabled: $('#pjIntroOn').checked } });
    renderSteps(); renderInspector();
  };
  $('#pjOutroOn').onchange = () => {
    saveProject({ settings: { outro_enabled: $('#pjOutroOn').checked } });
    renderSteps(); renderInspector();
  };
  $('#pjPlay').onclick = () => $('#btnPlayAudio').click();
  $('#pjTTS').onclick = async () => {
    const kind = S.stepId === '__intro__' ? 'intro' : 'outro';
    $('#pjTTS').disabled = true;
    try {
      const r = await api(`/api/projects/${S.project.id}/card/${kind}/tts`,
        { method: 'POST', body: {} });
      $('#pjAudioInfo').textContent = t('已生成配音 {sec} 秒', { sec: r.duration.toFixed(1) });
      S.audio.src = fileUrl('audio', r.audio) + '?t=' + Date.now();
      S.audio.play().catch(() => {});
    } catch (e) { toast(e.message, true); }
    $('#pjTTS').disabled = false;
  };

  // 片头 / 片尾背景
  $('#pjBgUpload').onclick = () => $('#pjBgFile').click();
  $('#pjBgFile').onchange = async () => {
    const f = $('#pjBgFile').files[0];
    $('#pjBgFile').value = '';
    if (!f || !S.project || !isCard()) return;
    const form = new FormData();
    form.append('file', f, f.name);
    try {
      const job = await postForm(`/api/projects/${S.project.id}/card/${cardKind()}/background`, form);
      await pollJob(job.id, t('处理片头片尾背景'));
    } catch (e) { toast(e.message, true); }
  };
  $('#pjBgRemove').onclick = () => cardRequest('DELETE', '/background', null, t('已恢复默认背景'));
  $('#pjBgPage').onchange = () => cardRequest('PATCH', '', { page: Number($('#pjBgPage').value) || 1 });
  $('#pjBgFit').onchange = () => cardRequest('PATCH', '', { fit: $('#pjBgFit').value });
  $('#pjBgText').onchange = () => cardRequest('PATCH', '', { show_text: $('#pjBgText').checked });
  $('#pjBgDur').onchange = () => cardRequest('PATCH', '', { duration: Number($('#pjBgDur').value) || 0 });

  // 视频步骤
  $('#vbMode').onchange = () => videoPatch({ mode: $('#vbMode').value });
  $('#vbAudio').onchange = () => videoPatch({ audio: $('#vbAudio').value });
  $('#vbStart').onchange = () => videoPatch({ start: Number($('#vbStart').value) || 0 });
  $('#vbEnd').onchange = () => videoPatch({ end: Number($('#vbEnd').value) || 0 });
  $('#vbSetStart').onclick = () => videoAtCurrent('start');
  $('#vbSetEnd').onclick = () => videoAtCurrent('end');
  $('#vbUpload').onclick = () => $('#vbFile').click();
  $('#vbFile').onchange = async () => {
    const f = $('#vbFile').files[0];
    $('#vbFile').value = '';
    const s = currentStep();
    if (f && s) await uploadVideo(`/api/projects/${S.project.id}/steps/${s.id}/video`, f, t('处理视频'));
  };
  $('#vbTranscribe').onclick = async () => {
    const s = currentStep();
    if (!s) return;
    await flushSaves();
    try {
      const job = await api(`/api/projects/${S.project.id}/steps/${s.id}/video/transcribe`, { method: 'POST' });
      await pollJob(job.id, t('识别视频里的讲话'));
    } catch (e) { toast(e.message, true); }
  };
  $('#btnInsertVideo').onclick = () => {
    if (!S.project) { toast(t('先打开一个项目'), true); return; }
    $('#insertVideoFile').click();
  };
  $('#insertVideoFile').onchange = async () => {
    const f = $('#insertVideoFile').files[0];
    $('#insertVideoFile').value = '';
    if (!f || !S.project) return;
    const after = S.stepId && !isCard() ? S.stepId : '';
    const r = await uploadVideo(`/api/projects/${S.project.id}/steps/video`, f, t('插入视频'), { after });
    if (r && r.step) selectStep(r.step);
  };

  // 属性表单
  $('#fTitle').oninput = () => saveStep({ title: $('#fTitle').value });
  $('#fNarration').oninput = () => saveStep(narrationPatch(currentStep(), $('#fNarration').value));
  $('#fCaption').oninput = () => saveStep({ caption: $('#fCaption').value });
  $('#fNote').oninput = () => saveStep({ note: $('#fNote').value });
  $('#fInclude').onchange = () => saveStep({ include: $('#fInclude').checked });
  $('#fZoom').onchange = () => saveStep({ zoom: $('#fZoom').checked });
  $('#fHighlight').onchange = () => { saveStep({ highlight: $('#fHighlight').checked }); drawHighlight(); };
  $('#fDuration').onchange = () => saveStep({ duration_override: Number($('#fDuration').value) });

  $('#btnRewrite').onclick = async () => {
    const s = currentStep();
    if (!s) return;
    $('#btnRewrite').disabled = true;
    try {
      const r = await api(`/api/projects/${S.project.id}/steps/${s.id}/rewrite`, {
        method: 'POST', body: { instruction: $('#fRewrite').value || t('更简洁自然一些'), apply: true },
      });
      if (!playsClipAudio(s)) { s.caption = r.text; $('#fCaption').value = r.text; }
      if (r.lines) s.lines = r.lines;             // 两人问答：改写的是整段台词
      s.narration = r.text; s.audio = ''; s.audio_duration = 0;
      s.voice_source = 'tts';
      $('#fNarration').value = r.text;
      renderInspector(); renderSteps();
      toast(t('已改写'));
    } catch (e) { toast(e.message, true); }
    $('#btnRewrite').disabled = false;
  };

  $('#btnStepTTS').onclick = async () => {
    const s = currentStep();
    if (!s) return;
    $('#btnStepTTS').disabled = true;
    try {
      const r = await api(`/api/projects/${S.project.id}/steps/${s.id}/tts`, { method: 'POST', body: {} });
      s.audio = r.audio; s.audio_duration = r.duration; s.voice_source = 'tts';
      renderInspector();
      S.audio.src = fileUrl('audio', r.audio) + '?t=' + Date.now(); S.audio.play().catch(() => {});
    } catch (e) { toast(e.message, true); }
    $('#btnStepTTS').disabled = false;
  };

  // 底部动作
  $('#btnScript').onclick = openScriptModal;
  $('#gNotes').onchange = () => {
    $('#gOverwrite').checked = $('#gNotes').value === 'verbatim';
    syncScriptModal();
  };
  $('#gMissing').onchange = syncScriptModal;
  $('#btnCloseScript').onclick = () => $('#mScript').classList.add('hidden');
  $('#btnRunScript').onclick = async () => {
    $('#mScript').classList.add('hidden');
    const body = {
      style: $('#gStyle').value, audience: $('#gAudience').value,
      extra: $('#gExtra').value, overwrite: $('#gOverwrite').checked,
    };
    if (S.project.source === 'slides') {
      body.notes_mode = $('#gNotes').value;
      body.missing = $('#gMissing').value;
      body.detail = $('#gDetail').value;
    }
    await runJob(`/api/projects/${S.project.id}/script`, body, t('生成解说'));
  };

  $('#btnTTS').onclick = () => runJob(`/api/projects/${S.project.id}/tts`,
    { voice: S.project.voice || '', only_missing: true }, t('合成语音'));

  $('#btnRender').onclick = async () => {
    const r = await runJob(`/api/projects/${S.project.id}/render`, {}, t('渲染视频'));
    if (r) openVideo();
  };

  $('#btnAuto').onclick = async () => {
    const r = await runJob(`/api/projects/${S.project.id}/auto`, { style: $('#gStyle').value }, t('一键生成'));
    if (r) openVideo();
  };

  $('#btnTranslate').onclick = () => {
    fillVoiceSelect($('#tVoice'), $('#tLang').value);
    // 幻灯片项目不翻译现有解说：按 PPT 原文直接用目标语言重写（后端 switch_language）
    const slides = S.project.source === 'slides';
    $('#tHint').textContent = slides
      ? t('幻灯片项目不翻译现在的解说，而是按 PPT 原文（页面文字和演讲者备注）直接用目标语言重写；插入的视频步骤照常翻译。会替换当前脚本并清空已生成的语音。')
      : t('翻译会替换当前脚本并清空已生成的语音。建议先「导出脚本」备份，或复制一份项目。');
    if (isDialogue()) {
      $('#tHint').textContent += ' ' + t('两位讲者的音色会换成目标语言的音色（这里选的是主持人的），之后可以在「🎭 讲者」里改。');
    }
    $('#btnRunTranslate').textContent = slides ? t('按 PPT 原文重写并切换') : t('翻译并切换');
    $('#mTranslate').classList.remove('hidden');
  };
  $('#tLang').onchange = () => fillVoiceSelect($('#tVoice'), $('#tLang').value);
  $('#btnCloseTranslate').onclick = () => $('#mTranslate').classList.add('hidden');
  $('#btnRunTranslate').onclick = async () => {
    $('#mTranslate').classList.add('hidden');
    await runJob(`/api/projects/${S.project.id}/translate`, {
      target_language: $('#tLang').value, voice: $('#tVoice').value, apply: true,
    }, S.project.source === 'slides' ? t('重写解说') : t('翻译'));
  };

  $('#btnExport').onclick = (e) => showMenu(e.currentTarget, [
    [t('图文文档（Markdown）'), () => window.open(`/api/projects/${S.project.id}/export/markdown`, '_blank')],
    [t('脚本 JSON'), () => window.open(`/api/projects/${S.project.id}/export/script`, '_blank')],
    [t('字幕 SRT'), () => {
      const n = (S.project.output || '').replace(/\.mp4$/, '.srt');
      if (!n) return toast(t('先渲染一次视频'), true);
      window.open(fileUrl('output', n) + '?download=true', '_blank');
    }],
    [t('第二语言字幕 / 网页播放包…'), () => {
      if (!S.project.output) return toast(t('先渲染一次视频'), true);
      openVideo();
      setTimeout(() => $('#vSub2Box').scrollIntoView({ block: 'start', behavior: 'smooth' }), 150);
    }],
  ]);

  $('#btnOpenVideo').onclick = openVideo;
  $('#btnStopJob').onclick = stopJobs;
  bindSub2();
  $('#btnCloseVideo').onclick = closeVideoModal;

  // 设置弹窗
  $('#btnSettings').onclick = () => {
    S.llmDrafts = {};                // 丢掉上次没保存就关掉的 AI 设置草稿
    $('#sProvider').value = (S.settings.llm || {}).provider;
    renderProviderFields();
    $('#mSettings').classList.remove('hidden');
  };
  $('#btnCloseSettings').onclick = () => $('#mSettings').classList.add('hidden');
  $('#btnSaveSettings').onclick = saveSettings;
  $('#sLang').onchange = () => fillVoiceSelect($('#sVoice'), $('#sLang').value);
  $$('.tabs button').forEach(b => {
    b.onclick = () => {
      $$('.tabs button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      $$('[data-pane]').forEach(p => p.classList.toggle('hidden', p.dataset.pane !== b.dataset.tab));
    };
  });
  $('#sProvider').onchange = renderProviderFields;
  $('#sTtsSvc').onchange = renderTtsFields;
  $('#sTtsKey').oninput = () => { ttsDraft($('#sTtsSvc').value).api_key = $('#sTtsKey').value; };
  $('#sTtsVoices').oninput = () => { ttsDraft($('#sTtsSvc').value).voices = $('#sTtsVoices').value; };
  $('#btnTtsClear').onclick = () => { ttsDraft($('#sTtsSvc').value).clear_key = true; renderTtsFields(); };
  $('#btnTtsTest').onclick = async () => {
    $('#btnTtsTest').disabled = true;
    $('#sTtsState').textContent = t('正在合成测试音频…');
    try {
      const r = await api('/api/settings/test-tts', {
        method: 'POST', body: { service: $('#sTtsSvc').value, api_key: $('#sTtsKey').value.trim() },
      });
      $('#sTtsState').textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
    } catch (e) { $('#sTtsState').textContent = '✗ ' + e.message; }
    $('#btnTtsTest').disabled = false;
  };
  $('#sBaseUrl').oninput = () => { llmDraft($('#sProvider').value).base_url = $('#sBaseUrl').value; };
  $('#sKey').oninput = () => { llmDraft($('#sProvider').value).api_key = $('#sKey').value; };
  $('#sModel').oninput = () => { llmDraft($('#sProvider').value).model = $('#sModel').value; };
  $('#btnClearKey').onclick = () => {
    llmDraft($('#sProvider').value).clear_key = true;
    renderProviderFields();
  };
  $('#btnTestKey').onclick = async () => {
    $('#btnTestKey').disabled = true;
    $('#sTestState').textContent = t('正在连接…');
    try {
      const r = await api('/api/settings/test-key', { method: 'POST', body: llmForm() });
      $('#sTestState').textContent = r.ok ? `✓ ${r.message} (${r.model})` : '✗ ' + r.message.slice(0, 160);
    } catch (e) { $('#sTestState').textContent = '✗ ' + e.message; }
    $('#btnTestKey').disabled = false;
  };
  $('#btnListModels').onclick = async () => {
    const pid = $('#sProvider').value;
    $('#btnListModels').disabled = true;
    $('#sTestState').textContent = t('正在获取模型列表…');
    try {
      const r = await api('/api/settings/llm-models', { method: 'POST', body: llmForm() });
      if (r.ok) {
        S.llmModels[pid] = r.models;
        $('#sModelList').innerHTML = [...new Set([...(llmPreset(pid).models || []), ...r.models])]
          .map(m => `<option value="${escapeHtml(m)}">`).join('');
        $('#sTestState').textContent = r.models.length
          ? t('找到 {n} 个模型：清空模型框就能从下拉里选', { n: r.models.length })
          : t('这个服务没有返回模型列表，直接手填模型名');
      } else {
        $('#sTestState').textContent = '✗ ' + r.message.slice(0, 160);
      }
    } catch (e) { $('#sTestState').textContent = '✗ ' + e.message; }
    $('#btnListModels').disabled = false;
  };
  $('#btnTryVoice').onclick = async () => {
    const voice = $('#sVoice').value;
    const s = currentStep();
    const text = (s && s.narration) || '';
    toast(t('正在合成试听…'));
    try {
      const res = await fetch('/api/tts/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice, text: text.slice(0, 120) }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      S.audio.src = URL.createObjectURL(await res.blob());
      S.audio.play().catch(() => {});
    } catch (e) { toast(e.message, true); }
  };

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input,textarea')) return;
    const steps = S.project ? S.project.steps : [];
    const i = steps.findIndex(s => s.id === S.stepId);
    if (e.key === 'ArrowDown' && i < steps.length - 1) { selectStep(steps[i + 1].id); e.preventDefault(); }
    if (e.key === 'ArrowUp' && i > 0) { selectStep(steps[i - 1].id); e.preventDefault(); }
    if (e.key === 'Escape') {
      if (!$('#mVideo').classList.contains('hidden')) closeVideoModal();
      $$('.modal').forEach(m => m.classList.add('hidden'));
    }
  });

  $$('.modal').forEach(m => {
    m.onclick = (e) => {
      if (e.target !== m) return;
      if (m.id === 'mVideo') closeVideoModal(); else m.classList.add('hidden');
    };
  });
}

/* ---------- 生成解说弹窗（PPT 项目可以改用备注原文） ---------- */

function slideNoteStats() {
  const steps = S.project.steps.filter(s => s.include);
  const withNotes = steps.filter(s => (s.slide_notes || '').trim()).length;
  return { total: steps.length, withNotes, missing: steps.length - withNotes };
}

function openScriptModal() {
  if (!S.project) return;
  const slides = S.project.source === 'slides';
  $('#gSlides').classList.toggle('hidden', !slides);
  $('#gCapture').classList.toggle('hidden', slides);
  if (slides) {
    const ps = S.project.settings || {};
    const st = slideNoteStats();
    $('#gNotes').value = st.withNotes ? (ps.slides_notes_mode || 'verbatim') : 'ignore';
    $('#gNotes').disabled = !st.withNotes;
    $('#gNotes').querySelector('option[value="verbatim"]').hidden = isDialogue();
    if (isDialogue() && $('#gNotes').value === 'verbatim') $('#gNotes').value = 'reference';
    $('#gMissing').value = ps.slides_missing || 'ai';
    $('#gDetail').value = ps.slides_detail || 'standard';
    const nl = notesLang(S.project.steps.filter(x => x.include).map(x => x.slide_notes || '').filter(Boolean));
    const target = (S.project.language || '').split('-')[0];
    const targetName = langLabel(S.project.language || target);
    $('#gSlidesInfo').textContent = (st.withNotes
      ? t('这个项目 {total} 页里有 {notes} 页写了演讲者备注。', { total: st.total, notes: st.withNotes })
      : t('这个项目的页面都没有演讲者备注（PDF 导入的也没有），解说只能由 AI 根据页面内容写。可以先在右栏「演讲者备注」里补上。'))
      + (nl && !langRelated(nl, target) && st.withNotes
        ? ' ' + t('备注是{notes}，解说要用{target}：备注不能原样念，会由 AI 参考备注用{target}来写。',
            { notes: langLabel(nl), target: targetName })
        : '');
    $('#gOverwrite').checked = $('#gNotes').value === 'verbatim';
    syncScriptModal();
  }
  $('#mScript').classList.remove('hidden');
}

function syncScriptModal() {
  const st = slideNoteStats();
  const mode = $('#gNotes').value;
  $('#gMissingField').classList.toggle('hidden', isDialogue() || !(mode === 'verbatim' && st.missing > 0 && st.withNotes > 0));
  const aiNeeded = mode !== 'verbatim' || ($('#gMissing').value === 'ai' && st.missing > 0);
  $('#gDetailField').classList.toggle('hidden', !aiNeeded);
}

function openVideo() {
  if (!S.project || !S.project.output) return toast(t('还没有导出视频'), true);
  const url = fileUrl('output', S.project.output);
  $('#vTitle').textContent = S.project.title || S.project.name;
  $('#vPlayer').src = url + '?t=' + Date.now();
  $('#vDownload').href = url + '?download=true';
  $('#vSrt').href = fileUrl('output', S.project.output.replace(/\.mp4$/, '.srt')) + '?download=true';
  $('#vInfo').textContent = S.project.output;
  $('#vPack').href = `/api/projects/${S.project.id}/export/player`;
  SUB2.cues = {};
  SUB2.cur = [];
  showSub2();
  buildSub2Track();
  $('#vSub2Panel').classList.add('hidden');
  $('#btnSub2Open').classList.remove('hidden');
  $('#mVideo').classList.remove('hidden');
  loadSub2();
}

function showMenu(anchor, items) {
  document.querySelector('.ctxmenu')?.remove();
  const box = document.createElement('div');
  box.className = 'ctxmenu';
  Object.assign(box.style, {
    position: 'fixed', zIndex: 80, background: 'var(--panel2)', border: '1px solid var(--line)',
    borderRadius: '9px', padding: '5px', boxShadow: '0 10px 30px rgba(0,0,0,.5)', minWidth: '170px',
  });
  items.forEach(([label, fn]) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.className = 'sm';
    Object.assign(b.style, { display: 'block', width: '100%', textAlign: 'left', background: 'transparent' });
    b.onclick = () => { box.remove(); fn(); };
    box.appendChild(b);
  });
  document.body.appendChild(box);
  const r = anchor.getBoundingClientRect();
  box.style.left = r.left + 'px';
  box.style.top = (r.top - box.offsetHeight - 6) + 'px';
  setTimeout(() => document.addEventListener('click', () => box.remove(), { once: true }), 0);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* =====================================================================
 * 语音输入：录我的声音 / 上传音频 / 口述转文字 / 改用 AI 配音
 * ===================================================================== */

function renderVoiceCard(s) {
  const own = s.voice_source === 'own';
  $('#voiceBadge').textContent = own ? t('🎙 原声') : t('AI 配音');
  $('#voiceBadge').className = 'vbadge' + (own ? ' own' : '');
  $('#audioInfo').textContent = s.audio
    ? t('{sec} 秒', { sec: s.audio_duration.toFixed(1) })
    : (s.narration ? t('还没有生成语音') : t('没有解说词，这一步静音停留'));
  $('#btnPlayStep').disabled = !s.audio;
  $('#btnVoiceToAI').classList.toggle('hidden', !own);
  $('#btnStepTTS').classList.toggle('hidden', own);
  $('#btnVoiceDelete').disabled = !s.audio;
}

function renderSlideBox(s) {
  const slide = s.kind === 'slide';
  $('#slideBox').classList.toggle('hidden', !slide);
  $('#fZoomWrap').classList.toggle('hidden', slide);
  $('#fHighlightWrap').classList.toggle('hidden', slide);
  $('#fNoteWrap').classList.toggle('hidden', slide);
  if (!slide) return;
  $('#slideText').textContent = s.slide_text || t('（这一页没有可提取的文字）');
  setField('#fSlideNotes', s.slide_notes || '');
  $('#btnUseNotes').disabled = !(s.slide_notes || '').trim();
  renderRevealBox(s);
}

/** 逐条出现：整个项目的开关 + 这一页的开关。数据是导入 PPT 时用 PowerPoint 生成的。 */
function hasReveal(s) { return !!(s && s.reveal && (s.reveal.items || []).length >= 2); }

function renderRevealBox(s) {
  const any = S.project.steps.some(hasReveal);
  const on = !!(S.project.settings || {}).slides_reveal;
  const here = hasReveal(s);
  $('#fRevealAll').checked = on && any;
  $('#fRevealAll').disabled = !any;
  $('#fRevealPage').checked = here && s.reveal.enabled !== false;
  $('#fRevealPage').disabled = !here || !on;
  $('#revealHint').textContent = !any
    ? t('这个项目没有逐条出现的数据：导入 PPT 时勾选「逐条出现」才会生成（需要本机 PowerPoint）。')
    : !here ? t('这一页内容不多或排版太复杂，整页一起出现。')
      : t('这一页分成 {n} 条：解说说到哪条，哪条就出现。', { n: s.reveal.items.length });
}

function renderVideoBox(s) {
  const isVideo = s.kind === 'video';
  $('#videoBox').classList.toggle('hidden', !isVideo);
  $('#rdSection').classList.toggle('hidden', isVideo);         // 视频画面上不能打码
  if (!isVideo) return;
  ['#fZoomWrap', '#fHighlightWrap', '#fNoteWrap'].forEach(id => $(id).classList.add('hidden'));
  const c = s.clip || {};
  const has = !!c.file;
  const badge = $('#vbBadge');
  badge.textContent = has ? t('{sec} 秒', { sec: clipLen(c).toFixed(1) }) : t('缺视频文件');
  badge.classList.toggle('own', has);
  $('#vbInfo').textContent = has
    ? [c.source, t('全长 {sec} 秒', { sec: (c.duration || 0).toFixed(1) }), c.has_audio ? '' : t('没有声音')]
      .filter(Boolean).join(' · ')
    : '';
  const miss = has ? ''
    : c.missing === 'online'
      ? t('这是在线视频（{url}），没法自动下载。把视频下载到本机后点「上传视频文件」，上传后会自动放进成片。', { url: c.source })
      : c.missing === 'broken'
        ? t('PPT 里的这个视频读不出来（可能是不支持的格式）。请上传一个 mp4 文件替换。')
        : t('这个视频没有嵌在 PPT 里，而是链接到作者电脑上的文件（{path}）。请上传这个视频文件，上传后会自动放进成片。',
          { path: c.source || '?' });
  $('#vbMissing').textContent = miss;
  $('#vbMissing').classList.toggle('hidden', !miss);
  $('#vbUpload').textContent = has ? t('⬆ 换一个视频文件') : t('⬆ 上传视频文件');
  $('#vbTranscribe').disabled = !has || !c.has_audio;
  $('#vbMode').querySelector('option[value="inset"]').disabled = !c.rect;
  $('#vbMode').value = c.mode || (c.rect ? 'inset' : 'fullscreen');
  $('#vbMode').disabled = !has;
  setField('#vbStart', String(c.start || 0));
  setField('#vbEnd', String(c.end || 0));
  $('#vbAudio').value = c.audio || 'original';
  ['#vbStart', '#vbEnd', '#vbSetStart', '#vbSetEnd'].forEach(id => { $(id).disabled = !has || c.mode === 'poster'; });
  $('#vbAudio').disabled = !has || !c.has_audio || c.mode === 'poster';
  $('#vbHint').textContent = !has ? ''
    : c.mode === 'poster' ? t('只显示这一页的画面（视频的封面），配上面的解说词。')
      : (c.audio === 'original' && c.has_audio)
        ? t('播放视频原声：上面的解说词不会被念出来。想在画面上显示字幕，写在「字幕」里，或者点「视频里的讲话转成字幕」。')
        : t('视频静音，播放时念上面的解说词；解说比视频长的话，画面停在最后一帧等解说说完。');
}

async function videoPatch(body) {
  const s = currentStep();
  if (!s || s.kind !== 'video') return;
  await flushSaves();
  try {
    Object.assign(s, await api(`/api/projects/${S.project.id}/steps/${s.id}/video`, { method: 'PATCH', body }));
  } catch (e) { toast(e.message, true); }
  renderSteps(); renderInspector(); renderStage();
}

function videoAtCurrent(field) {
  const v = $('#stageVideo');
  if (v.classList.contains('hidden') || !v.src) {
    toast(t('先切到「原始截图」，在播放器里找到想要的位置'), true);
    return;
  }
  videoPatch({ [field]: Math.round(v.currentTime * 10) / 10 });
}

async function uploadVideo(path, file, label, extra = {}) {
  const form = new FormData();
  form.append('file', file, file.name);
  for (const [k, v] of Object.entries(extra)) form.append(k, v);
  try {
    const job = await postForm(path, form);
    return await pollJob(job.id, label);
  } catch (e) { toast(e.message, true); }
  return null;
}

const VR = { mode: 'voice', stream: null, rec: null, chunks: [], blob: null,
             t0: 0, timer: 0, raf: 0, ctx: null, max: 60 };

function vrStopPreview() {
  cancelAnimationFrame(VR.raf);
  clearInterval(VR.timer);
  if (VR.rec && VR.rec.state === 'recording') { try { VR.rec.stop(); } catch (e) { /* */ } }
  if (VR.stream) VR.stream.getTracks().forEach(track => track.stop());
  if (VR.ctx) VR.ctx.close().catch(() => {});
  VR.stream = null; VR.ctx = null;
  $('#vrLevel').style.width = '0';
}

async function vrPreview(deviceId) {
  vrStopPreview();
  VR.stream = await navigator.mediaDevices.getUserMedia({
    audio: { deviceId: deviceId ? { exact: deviceId } : undefined,
             echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  VR.ctx = new AudioContext();
  const an = VR.ctx.createAnalyser();
  an.fftSize = 512;
  VR.ctx.createMediaStreamSource(VR.stream).connect(an);
  const buf = new Uint8Array(an.fftSize);
  const loop = () => {
    an.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
    $('#vrLevel').style.width = Math.min(100, peak / 128 * 170) + '%';
    VR.raf = requestAnimationFrame(loop);
  };
  loop();
}

async function openRecorder(mode) {
  if (!S.project) return;
  if (mode === 'voice' && !currentStep()) return;
  VR.mode = mode;
  VR.max = mode === 'voice' ? 60 : 120;
  VR.blob = null;
  $('#vrTitle').textContent = mode === 'voice' ? t('🎙 录我的声音') : t('🎤 口述转文字');
  $('#vrOptsOwn').classList.toggle('hidden', mode !== 'voice');
  $('#vrConfirm').textContent = mode === 'voice' ? t('使用这段录音') : t('转成文字');
  $('#vrConfirm').disabled = true;
  $('#vrRetake').classList.add('hidden');
  $('#vrPlayer').classList.add('hidden');
  $('#vrRec').classList.remove('on');
  $('#vrRec').disabled = false;
  $('#vrTime').textContent = '00:00';
  $('#vrHint').textContent = t('点红色按钮开始，最长 {sec} 秒', { sec: VR.max });
  $('#vrLang').innerHTML = langOptions();
  $('#vrLang').value = S.project.language || 'en-US';
  $('#mVoice').classList.remove('hidden');
  try {
    await vrPreview(S.vrDevice || '');
    const devs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput');
    $('#vrDevice').innerHTML = devs.map((d, i) =>
      `<option value="${d.deviceId}">${escapeHtml(d.label || t('麦克风 {n}', { n: i + 1 }))}</option>`).join('');
    if (S.vrDevice) $('#vrDevice').value = S.vrDevice;
  } catch (e) {
    $('#vrHint').textContent = t('拿不到麦克风：{error}（检查浏览器地址栏左侧的权限设置）', { error: e.message || e.name });
    $('#vrRec').disabled = true;
  }
}

function closeRecorder() {
  vrStopPreview();
  $('#vrPlayer').pause();
  $('#mVoice').classList.add('hidden');
}

function vrToggle() {
  if (VR.rec && VR.rec.state === 'recording') { VR.rec.stop(); return; }
  if (!VR.stream) return;
  VR.chunks = [];
  const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
  VR.rec = new MediaRecorder(VR.stream, { mimeType: mime });
  VR.rec.ondataavailable = e => { if (e.data.size) VR.chunks.push(e.data); };
  VR.rec.onstop = () => {
    clearInterval(VR.timer);
    $('#vrRec').classList.remove('on');
    VR.blob = new Blob(VR.chunks, { type: 'audio/webm' });
    $('#vrPlayer').src = URL.createObjectURL(VR.blob);
    $('#vrPlayer').classList.remove('hidden');
    $('#vrConfirm').disabled = VR.blob.size < 2000;
    $('#vrRetake').classList.remove('hidden');
    $('#vrHint').textContent = t('听一下没问题就确认；不满意点「重录」');
  };
  VR.rec.start(250);
  VR.t0 = Date.now();
  $('#vrRec').classList.add('on');
  $('#vrPlayer').classList.add('hidden');
  $('#vrConfirm').disabled = true;
  $('#vrRetake').classList.add('hidden');
  $('#vrHint').textContent = VR.mode === 'voice' ? t('录音中…再点一次停止') : t('说吧，说完再点一次');
  VR.timer = setInterval(() => {
    const sec = (Date.now() - VR.t0) / 1000;
    $('#vrTime').textContent = `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
    if (sec >= VR.max) VR.rec.stop();
  }, 200);
}

async function vrConfirm() {
  if (!VR.blob) return;
  const blob = VR.blob;
  const lang = $('#vrLang').value;
  if (VR.mode === 'voice') {
    const s = currentStep();
    const form = new FormData();
    form.append('file', blob, 'voice.webm');
    form.append('transcribe', $('#vrTranscribe').checked ? 'true' : 'false');
    form.append('replace_text', $('#vrReplace').checked ? 'true' : 'false');
    form.append('language', lang);
    closeRecorder();
    await uploadStepVoice(s, form);
  } else {
    const form = new FormData();
    form.append('file', blob, 'dictate.webm');
    form.append('language', lang);
    closeRecorder();
    try {
      const job = await postForm('/api/transcribe', form);
      const r = await pollJob(job.id, t('口述识别'), { refresh: false });
      if (r && r.text) insertNarration(r.text);
      else if (r) toast(t('没有识别到说话内容'), true);
    } catch (e) { toast(e.message, true); }
  }
}

function insertNarration(text) {
  const ta = $('#fNarration');
  const cur = ta.value.trim();
  // 中日文接着写不加空格、用全角逗号；其他语言用半角逗号加空格
  const cjk = /^(zh|ja)/.test(S.project.language || '');
  const joiner = /[。！？.!?]$/.test(cur) ? (cjk ? '' : ' ') : (cjk ? '，' : ', ');
  ta.value = cur ? cur + joiner + text : text;
  saveStep(narrationPatch(currentStep(), ta.value));
  toast(t('已填入解说词'));
}

async function uploadStepVoice(s, form) {
  try {
    const job = await postForm(`/api/projects/${S.project.id}/steps/${s.id}/voice`, form);
    const r = await pollJob(job.id, t('处理录音'));
    if (r) {
      S.stepId = s.id;
      renderInspector();
      S.audio.src = fileUrl('audio', r.audio) + '?t=' + Date.now();
      S.audio.play().catch(() => {});
    }
  } catch (e) { toast(e.message, true); }
}

/* =====================================================================
 * PPT / PDF 转视频
 * ===================================================================== */

const IM = { manifest: null, selected: new Set(), notesPicked: false, autoRef: false };

// 粗判文字语言：和后端 backend/services/langdetect.py 同一套规则，改的时候两边一起改（tests/test_languages.py 会对比）
const LANG_STOP = Object.fromEntries(Object.entries({
  en: 'the and is to of you this that with for it are on we our can',
  de: 'der die und das ist nicht mit sie ein eine zu auf für wir den',
  fr: 'le la les et est des une un pour que dans vous pas nous du',
  es: 'el la los las y es que para una un con por del se lo',
  it: 'il che è di per una un non sono con della gli le si',
  nl: 'de het een en van is dat niet met voor je op we zijn',
  pl: 'i w nie na się jest z do to że jak dla oraz są',
  pt: 'o os as e que para uma um com não você do da em ao pelo pela também muito quando já',
  sv: 'och att det som en på är av för med till den har inte om ett vi kan',
  da: 'og at det som en på er af for med til den har ikke om et vi kan hvad efter meget nogle',
  nb: 'og at det som en på er av for med til den har ikke om et vi kan hva etter mye noen',
  fi: 'ja on ei se että oli mutta kun tai ovat voi myös sekä tämä joka jos kuin',
  is: 'og að er í á sem um við ekki til var með en það þetta eru fyrir',
  et: 'ja on ei et see oli ka kui aga või mis ning siis nad oma veel kes seda',
  lv: 'un ir ar par no uz kas bet lai arī vai tas to ka nav kā var tā',
  lt: 'ir yra kad su į iš bet kaip tai ar jo o nuo už per taip buvo dar',
  cs: 'a je se na v že to s z do jsou pro ale jak také by k tak jsme',
  sk: 'a je sa na v že to s z do sú pre ale ako aj by k tak sme',
  hu: 'a az és hogy nem is egy van meg de ez csak már mint vagy ha el kell',
  ro: 'și şi si în de la cu este nu pe o un care din să se pentru mai sunt sau',
  hr: 'i je u na da se su za od koji ali kao što sa iz ili nije biti to',
  sl: 'in je v na da se so za od ki ali kot pa z iz tudi ni bi to',
  sq: 'dhe në të që është një për me nga i e janë por si nuk ka do',
  ca: 'el la els les i que de per amb una un no és del als més són aquest',
  gl: 'unha non con en un máis ou polo pola polos polas tamén moi cando xa',
  ga: 'agus an na is tá ar le sa go ag i don seo sin atá níl bhí mar',
  cy: 'a y yr ac yn mae i o ar ei eu wedi hwn hon gyda am ond roedd fel',
  mt: 'il l u ta li fil tal biex huwa hija għal minn ma din dan jew kif wkoll',
  tr: 've bir bu da de için ile olarak çok daha ne gibi var ama en olan kadar mı',
}).map(([k, v]) => [k, new Set(v.split(' '))]));
// 靠虚词分不太清的近亲语言：判断成其中一种时，对同组的其他语言也算「同一种」
const LANG_FAMILIES = [['da', 'nb', 'sv'], ['cs', 'sk'], ['hr', 'bs', 'sr', 'sl'], ['gl', 'pt'], ['sr', 'mk']];
const CYRILLIC = ['ru', 'uk', 'bg', 'sr', 'mk'];
const SR_WORDS = new Set('је шта овај ова који која ће сам'.split(' '));
const MK_WORDS = new Set('е што овој оваа кој која ќе сум'.split(' '));

/** 两个基础语言代码算不算同一种（相同、近亲，或者「分不清的西里尔文」对上用西里尔字母的语言） */
function langRelated(a, b) {
  if (a === b) return true;
  if (a === 'cyrl' || b === 'cyrl') return CYRILLIC.includes(a === 'cyrl' ? b : a);
  return LANG_FAMILIES.some(f => f.includes(a) && f.includes(b));
}

function cyrillicLang(text) {
  const t = text.toLowerCase();
  const has = (chars) => [...chars].some(c => t.includes(c));
  if (has('іїєґ')) return 'uk';
  if (has('ќѓѕ')) return 'mk';
  if (has('ђћ')) return 'sr';
  if (has('јљњџ')) {
    const words = t.match(/\p{L}+/gu) || [];
    return words.filter(w => MK_WORDS.has(w)).length > words.filter(w => SR_WORDS.has(w)).length ? 'mk' : 'sr';
  }
  if (has('ыэё')) return 'ru';
  return t.includes('ъ') ? 'bg' : 'cyrl';
}

/** 一段文字是什么语言（zh、en……）；西里尔字母分不清是哪种时 'cyrl'；判断不出来返回 '' */
function guessLang(text) {
  text = text || '';
  const letters = (text.match(/\p{L}/gu) || []).length;
  if (letters < 12) return '';
  const n = (re) => (text.match(re) || []).length;
  const enough = (k) => k >= Math.max(4, letters * 0.2);
  const kana = /[\u3040-\u30ff]/g;
  if (enough(n(kana))) return 'ja';
  if (enough(n(/[\uac00-\ud7af]/g))) return 'ko';
  if (enough(n(/[\u4e00-\u9fff]/g))) return n(kana) ? 'ja' : 'zh';
  if (enough(n(/[\u0370-\u03ff\u1f00-\u1fff]/g))) return 'el';
  if (enough(n(/[\u0400-\u04ff]/g))) return cyrillicLang(text);
  if (enough(n(/[\u0e00-\u0e7f]/g))) return 'th';
  if (enough(n(/[\u0600-\u06ff]/g))) return 'ar';
  if (enough(n(/[\u0900-\u097f]/g))) return 'hi';
  const words = text.toLowerCase().match(/\p{L}+/gu) || [];
  if (words.length < 5) return '';
  const scores = Object.entries(LANG_STOP)
    .map(([code, set]) => [words.filter(w => set.has(w)).length / words.length, code])
    .sort((a, b) => (b[0] - a[0]) || (a[1] < b[1] ? -1 : 1));
  const [best, code] = scores[0];
  // 和「不是近亲的第二名」比：丹麦语 / 挪威语这种分不太清的，只要确定是这一组就行
  const rival = (scores.slice(1).find(([, c]) => !langRelated(c, code)) || [0])[0];
  return best >= 0.06 && best >= rival * 1.3 ? code : '';
}

/** 一批备注大多是什么语言 */
function notesLang(notes) {
  const c = {};
  notes.forEach(x => { const g = guessLang(x); if (g) c[g] = (c[g] || 0) + 1; });
  return Object.entries(c).sort((a, b) => b[1] - a[1])[0]?.[0] || '';
}

/** 语言名：完整代码（en-US）给出列表里的名字；只有基础代码（en，粗判出来的备注语言）时去掉地区（English） */
function langLabel(code) {
  if (code === 'cyrl') code = 'ru';          // 分不清是哪种西里尔文：多半是俄文
  const exact = (S.languages || []).find(x => x.code === code);
  if (exact) return exact.name;
  const l = (S.languages || []).find(x => x.code.split('-')[0] === code);
  return l ? l.name.replace(/\s*\(.*\)$/, '') : code;
}

function openImport() {
  IM.manifest = null;
  IM.selected = new Set();
  $('#imStage1').classList.remove('hidden');
  $('#imStage2').classList.add('hidden');
  $('#imCreate').classList.add('hidden');
  $('#imBack').classList.add('hidden');
  $('#imFile').value = '';
  $('#imStatus').textContent = '';
  $('#imDrop').style.pointerEvents = '';
  $('#imLang').innerHTML = langOptions();
  $('#imLang').value = S.settings.language || 'en-US';
  fillVoiceSelect($('#imVoice'), $('#imLang').value, S.settings.voice);
  fillPairVoices($('#imLang').value);
  api('/api/health').then(h => {
    if (!$('#imStatus').textContent) {
      $('#imStatus').textContent = h.powerpoint
        ? t('✓ 检测到本机 PowerPoint，幻灯片会原样导出')
        : t('未检测到 PowerPoint：PDF 可以正常导入；PPTX 会用简版文字页代替画面（建议先在 PowerPoint / WPS 里另存为 PDF）');
    }
  }).catch(() => {});
  $('#mProjects').classList.add('hidden');
  $('#mImport').classList.remove('hidden');
}

async function imUpload(file) {
  if (!file) return;
  if (!/\.(pptx|ppt|pdf)$/i.test(file.name)) { toast(t('只支持 .pptx / .ppt / .pdf'), true); return; }
  $('#imDrop').style.pointerEvents = 'none';
  $('#imStatus').textContent = t('上传 {name}（{mb} MB）…', { name: file.name, mb: (file.size / 1048576).toFixed(1) });
  try {
    const form = new FormData();
    form.append('file', file, file.name);
    const job = await postForm('/api/import/slides', form);
    const man = await pollJob(job.id, t('解析幻灯片'), {
      refresh: false, onTick: j => { $('#imStatus').textContent = j.message; },
    });
    if (!man) { $('#imDrop').style.pointerEvents = ''; return; }
    IM.manifest = man;
    IM.selected = new Set(man.slides.filter(x => !x.hidden).map(x => x.i));
    $('#imName').value = file.name.replace(/\.(pptx|ppt|pdf)$/i, '');
    $('#imMode').value = 'single';
    IM.notesPicked = false;
    IM.autoRef = false;
    $('#imNotes').value = man.with_notes ? 'verbatim' : 'ignore';
    $('#imMissing').value = 'ai';
    renderImport();
    $('#imStage1').classList.add('hidden');
    $('#imStage2').classList.remove('hidden');
    $('#imCreate').classList.remove('hidden');
    $('#imBack').classList.remove('hidden');
  } catch (e) {
    $('#imStatus').textContent = t('失败：{error}', { error: e.message });
    $('#imDrop').style.pointerEvents = '';
  }
}

function renderImport() {
  const m = IM.manifest;
  if (!m) return;
  $('#imFileName').textContent = m.filename;
  const chosen = m.slides.filter(x => IM.selected.has(x.i));
  const noteCount = chosen.filter(x => x.notes).length;
  const missCount = chosen.length - noteCount;
  const anyNotes = m.with_notes > 0;
  // 两人问答：没有「备注原文就是解说」这一项，音色分主持人、讲师两个
  const dlg = $('#imMode').value === 'dialogue';
  $('#imNotes').querySelector('option[value="verbatim"]').hidden = dlg;
  $('#imVoiceWrap').classList.toggle('hidden', dlg);
  $('#imHostWrap').classList.toggle('hidden', !dlg);
  $('#imExpertWrap').classList.toggle('hidden', !dlg);
  const nl = notesLang(chosen.filter(x => x.notes).map(x => x.notes));
  const target = ($('#imLang').value || '').split('-')[0];
  const otherLang = !!nl && !langRelated(nl, target);
  // 「备注原文」不合适时（两人问答、备注和解说不是同一种语言）自动改成「AI 参考备注」；
  // 条件不在了（比如解说语言又换成和备注一样）就改回来。自己选过的不动
  const noVerbatim = dlg || (otherLang && !IM.notesPicked);
  if (noVerbatim && $('#imNotes').value === 'verbatim') {
    $('#imNotes').value = 'reference';
    IM.autoRef = true;
  } else if (!noVerbatim && IM.autoRef && $('#imNotes').value === 'reference') {
    $('#imNotes').value = 'verbatim';
    IM.autoRef = false;
  }
  const mode = anyNotes ? $('#imNotes').value : 'ignore';
  const missing = $('#imMissing').value;
  $('#imSummary').textContent =
    t('已选 {chosen} / {total} 页 · {notes} 页有备注 · 渲染：{engine}',
      { chosen: chosen.length, total: m.count, notes: noteCount, engine: m.engine });

  // PDF 没有备注；PPT 没写备注。两种情况都直说，不让下拉框莫名变灰
  const hint = m.ext === '.pdf'
    ? t('PDF 里没有演讲者备注（PowerPoint 另存为 PDF 时不会把备注带上），解说只能由 AI 根据页面内容来写。想用备注作解说，请直接导入 .pptx 文件。')
    : (!anyNotes ? t('这份 PPT 没有写演讲者备注，解说将由 AI 根据页面内容来写。') : '');
  const langHint = otherLang && anyNotes && mode !== 'ignore'
    ? t('备注是{notes}，解说要用{target}：备注不能原样念，会由 AI 参考备注用{target}来写。',
        { notes: langLabel(nl), target: langLabel($('#imLang').value || target) })
    : '';
  const hints = [hint, langHint].filter(Boolean).join(' ');
  $('#imNotesHint').textContent = hints;
  $('#imNotesHint').classList.toggle('hidden', !hints);
  $('#imNotesField').classList.toggle('hidden', !anyNotes);
  $('#imMissingField').classList.toggle('hidden', !(mode === 'verbatim' && missCount > 0));
  const aiPages = mode === 'verbatim' ? (missing === 'ai' ? missCount : 0) : chosen.length;
  // 「备注原文」模式下，备注不是解说语言的页也要交给 AI（连同备注）用解说语言讲 —— 隐私提示里要算上
  const otherPages = mode === 'verbatim'
    ? chosen.filter(x => { const g = x.notes ? guessLang(x.notes) : ''; return g && !langRelated(g, target); }).length
    : 0;
  $('#imDetail').disabled = aiPages === 0;
  $('#imGrid').innerHTML = m.slides.map(x => `
    <div class="im-card ${IM.selected.has(x.i) ? 'on' : ''}" data-i="${x.i}"
         title="${escapeHtml((x.title || '') + (x.notes ? '\n\n' + t('备注：{notes}', { notes: x.notes.slice(0, 200) }) : ''))}">
      <span class="tick">${IM.selected.has(x.i) ? '✓' : ''}</span>
      <span class="tags">${(x.videos || []).length ? `<span class="v">${t('视频')}</span>` : ''}${x.notes ? `<span class="n">${t('备注')}</span>`
        : (mode === 'verbatim' && anyNotes ? `<span class="miss">${t('无备注')}</span>` : '')}${x.hidden ? `<span>${t('隐藏页')}</span>` : ''}</span>
      <img src="/api/import/slides/${m.id}/thumb/${x.i}" loading="lazy" alt="">
      <div class="cap">${x.i}. ${escapeHtml(x.title || t('（无标题）'))}</div>
    </div>`).join('');
  $$('#imGrid .im-card').forEach(c => c.onclick = () => {
    const i = Number(c.dataset.i);
    IM.selected.has(i) ? IM.selected.delete(i) : IM.selected.add(i);
    renderImport();
  });
  $('#imCreate').disabled = !chosen.length;

  // 幻灯片里的视频：会变成单独的「视频」步骤；没嵌在 PPT 里的要事后上传
  const vids = chosen.flatMap(x => x.videos || []);
  const missingVids = vids.filter(v => v.missing).length;
  $('#imVideosWrap').classList.toggle('hidden', !(m.slides.some(x => (x.videos || []).length)));
  // 逐条出现要用 PowerPoint 再导出每一页的内容：只有页面本身是 PowerPoint 导出的 PPT 才行
  $('#imRevealWrap').classList.toggle('hidden', !(m.ext !== '.pdf' && m.engine === 'PowerPoint'));
  let vh = '';
  if (vids.length && $('#imVideos').checked) {
    vh = t('选中的页里有 {n} 个视频：会在那一页后面加一个「视频」步骤，在原位置播放、带原声。', { n: vids.length });
    if (missingVids) {
      vh += ' ' + t('其中 {n} 个没有嵌在 PPT 里（链接到本机文件或在线视频），导入后需要手动上传视频文件。', { n: missingVids });
    }
  }
  $('#imVideosHint').textContent = vh;
  $('#imVideosHint').classList.toggle('hidden', !vh);

  // 告诉用户每一页的解说从哪来、会不会联网
  const ok = '<span style="color:var(--ok)">●</span> ';
  const warn = '<span style="color:var(--warn)">●</span> ';
  let msg = '';
  if (!chosen.length) msg = '';
  else if (mode === 'verbatim' && aiPages === 0 && otherPages === 0) {
    msg = ok + (missCount
      ? t('{notes} 页直接用备注原文作解说，{missing} 页没有备注先留空（导入后在右栏补上） —— 不调用 AI 模型，全程离线（配音除外）。',
          { notes: noteCount, missing: missCount })
      : t('{notes} 页直接用备注原文作解说 —— 不调用 AI 模型，全程离线（配音除外）。', { notes: noteCount }));
  } else if (mode === 'verbatim') {
    const parts = [];
    if (aiPages) {
      parts.push(t('{notes} 页直接用备注原文；<b>{missing} 页没有备注</b>，会把{missing, plural, =1 {这一页} other {这些页}}的标题和页面文字发给 {ai} 补写。幻灯片图片不会上传。',
        { notes: noteCount - otherPages, missing: missCount, ai: escapeHtml(aiName()) }));
    }
    if (otherPages) {
      parts.push(t('{n} 页的备注不是解说语言，会把{n, plural, =1 {这一页} other {这些页}}的<b>标题、页面文字和备注</b>发给 {ai}，用解说语言讲出来。',
        { n: otherPages, ai: escapeHtml(aiName()) }) + (aiPages ? '' : ' ' + t('幻灯片图片不会上传。')));
    }
    msg = warn + parts.join(' ');
  } else if (mode === 'reference') {
    msg = warn + t('会把选中 {n} 页的<b>标题、页面文字、演讲者备注</b>发给 {ai} 改写成口语。幻灯片图片不会上传。',
      { n: chosen.length, ai: escapeHtml(aiName()) });
  } else {
    msg = warn + t('会把选中 {n} 页的<b>标题和页面文字</b>发给 {ai} 写解说。幻灯片图片不会上传。',
      { n: chosen.length, ai: escapeHtml(aiName()) });
  }
  $('#imPrivacy').innerHTML = msg;
}

async function imCreate() {
  const m = IM.manifest;
  if (!m || !IM.selected.size) return;
  const body = {
    name: $('#imName').value.trim(),
    selected: [...IM.selected].sort((a, b) => a - b),
    notes_mode: m.with_notes ? $('#imNotes').value : 'ignore',
    missing: $('#imMissing').value,
    detail: $('#imDetail').value,
    language: $('#imLang').value,
    voice: $('#imMode').value === 'dialogue' ? $('#imHostVoice').value : $('#imVoice').value,
    dialogue: $('#imMode').value === 'dialogue',
    host_voice: $('#imHostVoice').value,
    expert_voice: $('#imExpertVoice').value,
    auto_voice: $('#imAutoVoice').checked,
    videos: $('#imVideos').checked,
    reveal: !$('#imRevealWrap').classList.contains('hidden') && $('#imReveal').checked,
  };
  $('#imCreate').disabled = true;
  try {
    const job = await api(`/api/import/slides/${m.id}/create`, { method: 'POST', body });
    $('#mImport').classList.add('hidden');
    const r = await pollJob(job.id, t('生成幻灯片视频'), { refresh: false });
    if (r && r.project_id) {
      await openProject(r.project_id);
      if (!r.warning) toast(t('幻灯片项目已生成，检查一下解说后点「③ 渲染视频」'));   // 有提醒时 pollJob 已经弹过了
    }
  } catch (e) { toast(e.message, true); }
  $('#imCreate').disabled = false;
}

/* ---------- 绑定 ---------- */

function bindVoiceAndImport() {
  // 录音 / 口述
  $('#btnRecordVoice').onclick = () => openRecorder('voice');
  $('#btnDictate').onclick = () => openRecorder('dictate');
  $('#vrRec').onclick = vrToggle;
  $('#vrCancel').onclick = closeRecorder;
  $('#vrConfirm').onclick = vrConfirm;
  $('#vrRetake').onclick = () => {
    VR.blob = null;
    $('#vrPlayer').classList.add('hidden');
    $('#vrRetake').classList.add('hidden');
    $('#vrConfirm').disabled = true;
    $('#vrTime').textContent = '00:00';
    $('#vrHint').textContent = t('点红色按钮重新开始');
  };
  $('#vrDevice').onchange = async () => {
    S.vrDevice = $('#vrDevice').value;
    try { await vrPreview(S.vrDevice); } catch (e) { toast(e.message, true); }
  };

  // 上传音频
  $('#btnUploadVoice').onclick = () => $('#voiceFile').click();
  $('#voiceFile').onchange = async () => {
    const f = $('#voiceFile').files[0];
    const s = currentStep();
    $('#voiceFile').value = '';
    if (!f || !s) return;
    if (f.size > 15 * 1048576) { toast(t('音频不能超过 15 MB'), true); return; }
    const form = new FormData();
    form.append('file', f, f.name);
    form.append('transcribe', 'true');
    form.append('replace_text', (s.narration || '').trim() ? 'false' : 'true');
    form.append('language', S.project.language || '');
    await uploadStepVoice(s, form);
  };

  $('#btnPlayStep').onclick = () => $('#btnPlayAudio').click();

  $('#btnVoiceToAI').onclick = async () => {
    const s = currentStep();
    if (!s) return;
    await api(`/api/projects/${S.project.id}/steps/${s.id}/voice/ai`, { method: 'POST' });
    s.voice_source = 'tts'; s.audio = ''; s.audio_duration = 0;
    renderInspector();
    if ((s.narration || '').trim()) $('#btnStepTTS').click();   // 顺手用 AI 重新念一遍
  };

  $('#btnVoiceDelete').onclick = async () => {
    const s = currentStep();
    if (!s || !s.audio || !confirm(t('删除这一步的配音？文字保留。'))) return;
    await api(`/api/projects/${S.project.id}/steps/${s.id}/voice`, { method: 'DELETE' });
    await refreshProject();
  };

  $('#btnVoiceMenu').onclick = (e) => showMenu(e.currentTarget, [
    [t('全部改用 AI 配音'), async () => {
      const r = await api(`/api/projects/${S.project.id}/voice/all-ai`, { method: 'POST' });
      toast(t('已切换 {n} 步，点「② 合成语音」生成 AI 配音', { n: r.switched }));
      await refreshProject();
    }],
    [t('删除所有配音（保留文字）'), async () => {
      if (!confirm(t('删除这个项目里所有步骤的配音（包括你录的原声）？文字会保留。'))) return;
      await api(`/api/projects/${S.project.id}/voice/remove-all`, { method: 'POST' });
      await refreshProject();
    }],
    [t('语音识别设置…'), () => {
      $('#mSettings').classList.remove('hidden');
      refreshAsrState();
    }],
  ]);

  // 幻灯片备注
  $('#fSlideNotes').oninput = () => saveStep({ slide_notes: $('#fSlideNotes').value });
  $('#fRevealAll').onchange = () => {
    saveProject({ settings: { slides_reveal: $('#fRevealAll').checked } });
    renderInspector(); renderStage();
  };
  $('#fRevealPage').onchange = () => {
    const s = currentStep();
    if (!hasReveal(s)) return;
    s.reveal.enabled = $('#fRevealPage').checked;
    saveStep({ reveal_enabled: s.reveal.enabled });
    renderInspector(); renderStage();
  };
  $('#btnUseNotes').onclick = () => {
    const notes = $('#fSlideNotes').value.trim();
    if (!notes) return;
    $('#fNarration').value = notes;
    $('#fCaption').value = notes;
    saveStep({ narration: notes, caption: notes });
    renderInspector();
  };

  // 语音识别设置
  $('#btnSettings').addEventListener('click', refreshAsrState);
  $('#btnAsrPrepare').onclick = async () => {
    await api('/api/settings', { method: 'POST', body: {
      asr_model: $('#sAsrModel').value, hf_endpoint: $('#sHfEndpoint').value.trim() } });
    const job = await api('/api/asr/prepare', { method: 'POST' });
    $('#btnAsrPrepare').disabled = true;
    await pollJob(job.id, t('准备语音识别模型'), {
      refresh: false, onTick: j => { $('#sAsrState').textContent = j.message; } });
    $('#btnAsrPrepare').disabled = false;
    refreshAsrState();
  };

  // PPT 导入
  $('#btnImportSlides').onclick = openImport;
  $('#btnEmptyImport').onclick = openImport;
  $('#imCancel').onclick = () => $('#mImport').classList.add('hidden');
  $('#imBack').onclick = openImport;
  $('#imFile').onchange = () => imUpload($('#imFile').files[0]);
  const drop = $('#imDrop');
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    imUpload(e.dataTransfer.files[0]);
  };
  $('#imAll').onclick = () => { IM.selected = new Set(IM.manifest.slides.map(x => x.i)); renderImport(); };
  $('#imNone').onclick = () => { IM.selected = new Set(); renderImport(); };
  $('#imNotes').onchange = () => { IM.notesPicked = true; IM.autoRef = false; renderImport(); };
  $('#imMissing').onchange = renderImport;
  $('#imVideos').onchange = renderImport;
  $('#imLang').onchange = () => {
    fillVoiceSelect($('#imVoice'), $('#imLang').value);
    fillPairVoices($('#imLang').value);
    renderImport();
  };
  $('#imMode').onchange = renderImport;
  $('#imCreate').onclick = imCreate;
  bindDialogue();
}

async function refreshAsrState() {
  try {
    const a = await api('/api/asr/status');
    $('#sAsrState').textContent = !a.installed
      ? t('未安装语音识别组件：pip install faster-whisper')
      : a.cached
        ? (a.loaded ? t('✓ 模型 {model} 已下载并加载（{dir}）', { model: a.model, dir: a.models_dir })
                    : t('✓ 模型 {model} 已下载（{dir}）', { model: a.model, dir: a.models_dir }))
        : t('模型 {model} 还没下载（约 {mb} MB）。第一次识别时会自动下载（连不上官方源时自动改用国内镜像），也可以现在点「下载 / 加载」。完全不能联网的话，把模型文件夹放到 {dir}',
            { model: a.model, mb: a.size_mb, dir: a.offline_dir });
  } catch (e) { /* */ }
}


/* ---------- 两人问答 ---------- */

function isDialogue() {
  return !!(S.project && (S.project.settings || {}).dialogue);
}


function speakerOf(role) {
  const sp = ((S.project && S.project.speakers) || []).find(x => x.role === role);
  return sp || { role, name: role === 'host' ? t('主持人') : t('讲师'), voice: '' };
}

/** 某种语言里挑一个男声：先用推荐的，没有就用这种语言的第一个男声 */
function maleVoice(locale) {
  const want = (S.voiceMale || {})[locale];
  if (want && S.voices.some(v => v.name === want)) return want;
  const lang = (locale || '').split('-')[0].toLowerCase();
  const m = S.voices.find(v => v.gender === 'Male' && v.locale.toLowerCase() === (locale || '').toLowerCase())
    || S.voices.find(v => v.gender === 'Male' && v.locale.toLowerCase().startsWith(lang));
  return m ? m.name : '';
}

function fillPairVoices(locale) {
  fillVoiceSelect($('#imHostVoice'), locale, (S.voiceDefaults || {})[locale]);
  fillVoiceSelect($('#imExpertVoice'), locale, maleVoice(locale));
}

function syncDialogueUI() {
  $('#btnSpeakers').classList.toggle('hidden', !isDialogue());
}

/** 这一步的台词。老数据只有整段解说时，当作主持人的一句（和配音的规则一样） */
function stepLines(s) {
  if (s.lines && s.lines.length) return s.lines;
  return (s.narration || '').trim() ? [{ who: 'host', text: s.narration }] : [];
}

function renderDialogue(s) {
  const box = $('#dlgLines');
  $('#dlgAddHost').textContent = '＋ ' + speakerOf('host').name;
  $('#dlgAddExpert').textContent = '＋ ' + speakerOf('expert').name;
  // 正在这一步的台词里打字：不重画（光标会跳）。换了一步一定重画，免得把上一步的台词当成这一步的来改
  if (box.dataset.sid === s.id && box.contains(document.activeElement)
      && document.activeElement.tagName === 'TEXTAREA') return;
  box.dataset.sid = s.id;
  const lines = stepLines(s);
  box.innerHTML = lines.length ? lines.map((ln, k) => `
    <div class="dlg-row" data-k="${k}">
      <button class="dlg-who ${ln.who === 'expert' ? 'expert' : 'host'}" data-act="who"
              title="${escapeHtml(t('点一下换成另一个人说'))}">${escapeHtml(speakerOf(ln.who).name)}</button>
      <textarea rows="2">${escapeHtml(ln.text)}</textarea>
      <div class="dlg-ops">
        <button class="xs" data-act="play" title="${escapeHtml(t('试听这一句'))}">▶</button>
        <button class="xs" data-act="up" title="${escapeHtml(t('上移'))}" ${k ? '' : 'disabled'}>↑</button>
        <button class="xs" data-act="del" title="${escapeHtml(t('删除这一句'))}">✕</button>
      </div>
    </div>`).join('')
    : `<p class="small muted">${t('还没有台词：点下面的按钮加一句，或者点「① 生成解说」让 AI 写。')}</p>`;
}

/** 改了台词：解说 = 台词全文，字幕没单独改过就跟着；AI 配音的旧配音作废 */
function saveLines(s, lines) {
  s.lines = lines.map(x => ({ who: x.who, text: x.text }));
  const text = s.lines.map(x => x.text.trim()).filter(Boolean).join('\n');
  if (text !== (s.narration || '') && s.voice_source !== 'own') {
    s.audio = ''; s.audio_duration = 0; s.boundaries = []; s.line_times = [];
    renderVoiceCard(s);
  }
  if (!playsClipAudio(s) && (!s.caption || s.caption === s.narration)) {
    s.caption = text;
    $('#fCaption').value = text;
  }
  s.narration = text;
  PENDING.pid = S.project.id;
  PENDING.steps.set(s.id, { ...(PENDING.steps.get(s.id) || {}), lines: s.lines });
  queueSave();
}

/** 试听：text 为空时服务端用这个音色语言的示例句 */
async function playText(text, voice) {
  toast(t('正在合成试听…'));
  try {
    const res = await fetch('/api/tts/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice, text: (text || '').slice(0, 200) }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    S.audio.src = URL.createObjectURL(await res.blob());
    S.audio.play().catch(() => {});
  } catch (e) { toast(e.message, true); }
}

/* 讲者弹窗 */

function spkCard(role) { return $(`#mSpeakers .spk-card[data-role="${role}"]`); }

function openSpeakers() {
  if (!isDialogue()) return;
  for (const role of ['host', 'expert']) {
    const sp = speakerOf(role);
    const card = spkCard(role);
    card.querySelector('.spk-name').value = sp.name || '';
    fillVoiceSelect(card.querySelector('.spk-voice'), S.project.language, sp.voice);
  }
  $('#mSpeakers').classList.remove('hidden');
}

async function saveSpeakers() {
  const speakers = ['host', 'expert'].map(role => ({
    role, name: spkCard(role).querySelector('.spk-name').value.trim(),
    voice: spkCard(role).querySelector('.spk-voice').value,
  }));
  try {
    await flushSaves();
    const r = await api(`/api/projects/${S.project.id}/speakers`, { method: 'PUT', body: { speakers } });
    $('#mSpeakers').classList.add('hidden');
    await refreshProject();
    toast(r.changed && r.changed.length
      ? t('已保存。换了音色的讲者说过的台词需要重新配音：点「② 合成语音」。')
      : t('已保存'));
  } catch (e) { toast(e.message, true); }
}

function bindDialogue() {
  $('#btnSpeakers').onclick = openSpeakers;
  $('#btnSpeakers2').onclick = openSpeakers;
  $('#btnCloseSpeakers').onclick = () => $('#mSpeakers').classList.add('hidden');
  $('#btnSaveSpeakers').onclick = saveSpeakers;
  $$('#mSpeakers .spk-card').forEach(card => {
    const role = card.dataset.role;
    card.querySelector('[data-act="try"]').onclick = () => {
      const s = currentStep();
      const mine = s ? stepLines(s).find(x => x.who === role) : null;
      playText(mine ? mine.text : '', card.querySelector('.spk-voice').value);
    };
  });

  // 台词编辑
  const box = $('#dlgLines');
  box.oninput = (e) => {
    const row = e.target.closest('.dlg-row');
    const s = currentStep();
    if (!row || !s || e.target.tagName !== 'TEXTAREA' || box.dataset.sid !== s.id) return;
    const lines = stepLines(s).map(x => ({ ...x }));
    const k = Number(row.dataset.k);
    if (!lines[k]) return;
    lines[k].text = e.target.value;
    saveLines(s, lines);
  };
  box.onclick = (e) => {
    const btn = e.target.closest('button');
    const row = e.target.closest('.dlg-row');
    const s = currentStep();
    if (!btn || !row || !s) return;
    const k = Number(row.dataset.k);
    const lines = stepLines(s).map(x => ({ ...x }));
    const act = btn.dataset.act;
    if (act === 'play') return lines[k].text.trim() && playText(lines[k].text, speakerOf(lines[k].who).voice);
    if (act === 'who') lines[k].who = lines[k].who === 'host' ? 'expert' : 'host';
    else if (act === 'up' && k > 0) [lines[k - 1], lines[k]] = [lines[k], lines[k - 1]];
    else if (act === 'del') lines.splice(k, 1);
    else return;
    saveLines(s, lines);
    renderDialogue(s);
  };
  const add = (who) => {
    const s = currentStep();
    if (!s) return;
    const lines = [...stepLines(s).map(x => ({ ...x })), { who, text: '' }];
    s.lines = lines;                       // 空句先只放在界面上，写了字才保存
    renderDialogue(s);
    const tas = $$('#dlgLines textarea');
    if (tas.length) tas[tas.length - 1].focus();
  };
  $('#dlgAddHost').onclick = () => add('host');
  $('#dlgAddExpert').onclick = () => add('expert');
}


/* ---------- 第二语言字幕（外挂） ---------- */

const SUB2 = { state: null, cues: {}, cur: [] };

async function loadSub2() {
  if (!S.project) return;
  try {
    SUB2.state = await api(`/api/projects/${S.project.id}/subtitles2`);
  } catch (e) { SUB2.state = null; }
  renderSub2();
}

function renderSub2() {
  const st = SUB2.state;
  if (!st) return;
  const esc = escapeHtml;
  const hint = !st.has_primary ? t('这个视频没有字幕，生成不了第二语言字幕。')
    : !st.burned ? t('主字幕没有烧进视频（设置 → 视频风格 →「把字幕烧进画面」关着）。第二字幕照样能生成，只是画面上只有它一种字幕。')
      : !st.space ? t('这个视频渲染时没有给第二语言字幕留位置，两种字幕可能叠在一起：建议重新渲染一次（设置 → 视频风格 →「给第二语言字幕留位置」默认打开）。')
        : '';
  $('#vSub2Hint').textContent = hint;
  $('#vSub2Hint').classList.toggle('hidden', !hint);
  const file = (f) => fileUrl('output', f) + '?download=true';
  $('#vSub2List').innerHTML = st.tracks.length ? st.tracks.map(x => `
    <div class="sub2-row small" data-lang="${esc(x.lang)}">
      <span class="name">${esc(x.name)}</span>
      ${x.stale
        ? `<span class="warn-text">${t('视频重新渲染过，需要重新生成')}</span>`
        : `<a class="btn sm" href="${file(x.vtt)}" download>VTT</a><a class="btn sm" href="${file(x.srt)}" download>SRT</a>`}
      <button class="sm" data-act="regen">${t('重新生成')}</button>
      <button class="sm danger" data-act="del">${t('删除')}</button>
    </div>`).join('')
    : `<p class="small muted">${t('还没有第二语言字幕。')}</p>`;
  const ok = st.tracks.filter(x => !x.stale);
  const keep = $('#vSub2Preview').value;
  $('#vSub2Preview').innerHTML = `<option value="">${t('不显示')}</option>`
    + ok.map(x => `<option value="${esc(x.lang)}">${esc(x.name)}</option>`).join('');
  $('#vSub2Preview').value = ok.some(x => x.lang === keep) ? keep : '';
  const have = new Set(ok.map(x => x.lang));
  // 勾选框按「中文 → 欧洲语言 → 其他语言」分组，组名占一整行
  const box = (l) => `
    <label class="switch small"><input type="checkbox" value="${esc(l.code)}"> ${esc(l.name)}${have.has(l.code) ? ' ✓' : ''}</label>`;
  const section = (g, title) => {
    const xs = S.languages.filter(l => (l.group || 'other') === g && l.code !== S.project.language);
    return xs.length ? (title ? `<div class="sub2-group">${title}</div>` : '') + xs.map(box).join('') : '';
  };
  $('#vSub2Langs').innerHTML = section('zh', '') + section('europe', t('欧洲语言')) + section('other', t('其他语言'));
  $('#btnSub2Gen').disabled = !st.has_primary;
  $('#btnSub2Open').disabled = !st.has_primary;
  $('#btnSub2Open').textContent = st.tracks.length ? t('🌐 再生成一种第二语言字幕') : t('🌐 生成第二语言字幕');
  $('#vPack').classList.toggle('disabled', !st.has_primary);
}

async function generateSub2(langs) {
  if (!langs.length) return toast(t('先勾选要生成的语言'), true);
  $('#btnSub2Gen').disabled = true;
  try {
    const job = await api(`/api/projects/${S.project.id}/subtitles2`, { method: 'POST', body: { languages: langs } });
    const r = await pollJob(job.id, t('第二语言字幕'), {
      refresh: false, onTick: j => { $('#vSub2State').textContent = j.message || ''; },
    });
    if (r) {
      SUB2.state = r;
      langs.forEach(l => delete SUB2.cues[l]);
      $$('#vSub2Langs input').forEach(i => { i.checked = false; });
      $('#vSub2State').textContent = '';
      $('#vSub2Panel').classList.add('hidden');
      $('#btnSub2Open').classList.remove('hidden');
      renderSub2();
      if (langs.includes($('#vSub2Preview').value)) setSub2Preview($('#vSub2Preview').value);
    }
  } catch (e) { toast(e.message, true); }
  $('#btnSub2Gen').disabled = false;
}

const SUB2_GAP = 0;                                       // 第二字幕的底框紧挨着主字幕的底框（和播放包一致）
const SUB2_UPRIGHT = ['ar'];  // 第二字幕默认斜体，阿拉伯文除外（和播放包一致）

async function setSub2Preview(lang) {
  const upright = SUB2_UPRIGHT.includes((lang || '').split('-')[0]);
  $('#vSub2Text').classList.toggle('upright', upright);
  $('#vPlayer').classList.toggle('upright', upright);
  SUB2.cur = [];
  if (lang) {
    try {
      if (!SUB2.cues[lang]) SUB2.cues[lang] = (await api(`/api/projects/${S.project.id}/subtitles2/${encodeURIComponent(lang)}`)).cues;
      SUB2.cur = SUB2.cues[lang] || [];
    } catch (e) { toast(e.message, true); }
  }
  layoutSub2();
  showSub2();
  buildSub2Track();
}

/* 用浏览器自己的全屏（播放器右下角的全屏按钮、双击画面）时，页面上的字幕层显示不出来：
 * 这时把第二字幕交给浏览器自己显示（同样放在主字幕下面） */
const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;
const vNativeFs = () => fsEl() === $('#vPlayer') || !!$('#vPlayer').webkitDisplayingFullscreen;

function syncSub2Track() {
  if (SUB2.track) SUB2.track.mode = SUB2.cur.length && vNativeFs() ? 'showing' : 'hidden';
  $('#vSub2Text').style.visibility = vNativeFs() ? 'hidden' : '';
  setTimeout(layoutSub2, 50);
}

function buildSub2Track() {
  const v = $('#vPlayer');
  if (!v.addTextTrack || typeof VTTCue === 'undefined') return;
  if (!SUB2.track) SUB2.track = v.addTextTrack('subtitles', 'second', '');
  const tr = SUB2.track;
  tr.mode = 'hidden';
  while (tr.cues && tr.cues.length) tr.removeCue(tr.cues[0]);
  const bottom = (SUB2.state && SUB2.state.bottom) || 0.935;
  for (const [a, b, text] of SUB2.cur) {
    const c = new VTTCue(a, b, text);
    c.snapToLines = false; c.line = (bottom + SUB2_GAP) * 100; c.position = 50; c.size = 92; c.align = 'center';
    tr.addCue(c);
  }
  syncSub2Track();
}

/** 请求全屏：成功、失败都有交代。有的内嵌浏览器既不答应也不拒绝，1.5 秒还没全屏就当失败 */
function requestFs(el) {
  const f = el.requestFullscreen || el.webkitRequestFullscreen;
  if (!f || !(document.fullscreenEnabled || document.webkitFullscreenEnabled)) return Promise.reject(new Error('no'));
  return new Promise((ok, bad) => {
    const timer = setTimeout(() => { if (!fsEl()) bad(new Error('timeout')); }, 1500);
    Promise.resolve(f.call(el)).then(() => { clearTimeout(timer); ok(); }, (e) => { clearTimeout(timer); bad(e); });
  });
}

/** ⛶：整块（视频 + 第二字幕层）全屏；浏览器不让时退一步用视频自己的全屏 */
function toggleVideoFullscreen() {
  if (fsEl()) return (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  requestFs($('#vStage')).catch(() => requestFs($('#vPlayer')))
    .catch(() => toast(t('浏览器不允许全屏'), true));
}

/** 关闭视频窗口：退出全屏、暂停（不然窗口关了声音还在放） */
function closeVideoModal() {
  if (fsEl()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  $('#vPlayer').pause();
  $('#mVideo').classList.add('hidden');
}

/** 第二字幕紧贴在主字幕下面（渲染时记下了主字幕底边的位置），字号约为画面高度的 2.8%；
 *  偶尔两行放不下时往上挪一点，不超出画面。和网页播放包里的播放页是同一套规则 */
function layoutSub2() {
  const v = $('#vPlayer'), box = $('#vSub2Text');
  const W = v.clientWidth, H = v.clientHeight, vw = v.videoWidth || 16, vh = v.videoHeight || 9;
  const k = Math.min(W / vw, H / vh), h = vh * k;
  const top = v.offsetTop + (H - h) / 2;
  const bottom = (SUB2.state && SUB2.state.bottom) || 0.935;
  box.style.fontSize = Math.max(11, h * 0.028) + 'px';
  const want = top + h * (bottom + SUB2_GAP);
  const maxTop = top + h * 0.994 - box.firstElementChild.offsetHeight;
  box.style.top = Math.min(want, maxTop) + 'px';
}

function showSub2() {
  const t0 = $('#vPlayer').currentTime, cues = SUB2.cur;
  let lo = 0, hi = cues.length - 1, text = '';
  while (lo <= hi) {
    const m = (lo + hi) >> 1, c = cues[m];
    if (t0 < c[0]) hi = m - 1; else if (t0 >= c[1]) lo = m + 1; else { text = c[2]; break; }
  }
  const span = $('#vSub2Text span');
  if (span.textContent !== text) { span.textContent = text; layoutSub2(); }
}

function bindSub2() {
  const v = $('#vPlayer');
  const loop = () => { showSub2(); if (!v.paused) requestAnimationFrame(loop); };
  v.addEventListener('play', loop);
  v.addEventListener('seeked', showSub2);
  v.addEventListener('timeupdate', showSub2);
  v.addEventListener('loadedmetadata', layoutSub2);
  window.addEventListener('resize', layoutSub2);
  for (const ev of ['fullscreenchange', 'webkitfullscreenchange']) document.addEventListener(ev, syncSub2Track);
  $('#btnVFull').onclick = toggleVideoFullscreen;
  $('#vSub2Preview').onchange = () => setSub2Preview($('#vSub2Preview').value);
  $('#btnSub2Open').onclick = () => {
    $('#vSub2Panel').classList.remove('hidden');
    $('#btnSub2Open').classList.add('hidden');
    $('#vSub2Panel').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };
  $('#btnSub2Cancel').onclick = () => {
    $('#vSub2Panel').classList.add('hidden');
    $('#btnSub2Open').classList.remove('hidden');
  };
  $('#btnSub2Gen').onclick = () => generateSub2($$('#vSub2Langs input:checked').map(i => i.value));
  $('#vSub2List').onclick = async (e) => {
    const btn = e.target.closest('button');
    const row = e.target.closest('.sub2-row');
    if (!btn || !row) return;
    const lang = row.dataset.lang;
    if (btn.dataset.act === 'regen') return generateSub2([lang]);
    if (btn.dataset.act === 'del') {
      try {
        SUB2.state = await api(`/api/projects/${S.project.id}/subtitles2/${encodeURIComponent(lang)}`, { method: 'DELETE' });
        delete SUB2.cues[lang];
        if ($('#vSub2Preview').value === lang) { $('#vSub2Preview').value = ''; setSub2Preview(''); }
        renderSub2();
      } catch (err) { toast(err.message, true); }
    }
  };
  $('#vPack').onclick = (e) => {
    if ($('#vPack').classList.contains('disabled')) { e.preventDefault(); return; }
    toast(t('正在打包（视频比较大时要等一会儿）…'));
  };
}


boot();
