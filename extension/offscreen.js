/* 边录边讲的录音端。
 * 由 background 创建为 offscreen document，接收 mic_start / mic_stop 指令。
 * 停止时把整段录音连同「开始录音的时刻」一起上传，服务端据此把话分配到每个操作步骤。
 */
let recorder = null;
let stream = null;
let chunks = [];
let startedAt = 0;

async function start(deviceId) {
  if (recorder && recorder.state === 'recording') return { ok: true, startedAt };
  const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (deviceId) audio.deviceId = { exact: deviceId };
  stream = await navigator.mediaDevices.getUserMedia({ audio });
  chunks = [];
  const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
  recorder = new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 64000 });
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  recorder.start(1000);
  startedAt = Date.now();
  return { ok: true, startedAt };
}

async function stop(msg) {
  if (!recorder) {
    await VTI18N.init();
    return { ok: false, error: t('没有正在进行的录音') };
  }
  const blob = await new Promise((resolve) => {
    recorder.onstop = () => resolve(new Blob(chunks, { type: 'audio/webm' }));
    recorder.stop();
  });
  stream.getTracks().forEach((track) => track.stop());
  recorder = null;
  stream = null;
  const seconds = (Date.now() - startedAt) / 1000;
  if (!msg.upload || !msg.projectId) return { ok: true, size: blob.size, seconds };

  const fd = new FormData();
  fd.append('file', blob, 'session.webm');
  fd.append('rec_start', String(startedAt));
  fd.append('mode', msg.mode || 'ai');
  fd.append('language', msg.language || '');
  const url = `${msg.serverUrl.replace(/\/$/, '')}/api/projects/${msg.projectId}/magic-mic`;
  const res = await fetch(url, { method: 'POST', body: fd });
  let body = {};
  try { body = await res.json(); } catch (e) { /* ignore */ }
  return {
    ok: res.ok, size: blob.size, seconds, job: body.id || '',
    error: res.ok ? '' : (body.detail || `HTTP ${res.status}`),
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.target !== 'offscreen') return false;
  (async () => {
    try {
      if (msg.type === 'mic_start') sendResponse(await start(msg.deviceId));
      else if (msg.type === 'mic_stop') sendResponse(await stop(msg));
      else if (msg.type === 'mic_state') {
        sendResponse({ recording: !!recorder, startedAt });
      } else sendResponse({ ok: false, error: 'unknown' });
    } catch (e) {
      sendResponse({ ok: false, error: `${e.name || 'Error'}: ${e.message || e}` });
    }
  })();
  return true;
});
