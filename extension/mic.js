/* Microphone permission page.
 * The offscreen document can't show a permission prompt itself, so permission is granted once on this visible page; recording then works directly.
 */
const $ = (id) => document.getElementById(id);
let stream = null;
let raf = 0;

function msg(type, payload = {}) {
  return new Promise((r) => chrome.runtime.sendMessage({ type, ...payload }, (x) => r(x || {})));
}

async function listDevices(selected) {
  const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput');
  $('device').innerHTML = devs.map((d, i) =>
    `<option value="${d.deviceId}">${d.label || t('麦克风 {n}', { n: i + 1 })}</option>`).join('');
  if (selected && devs.some((d) => d.deviceId === selected)) $('device').value = selected;
}

async function meter(deviceId) {
  cancelAnimationFrame(raf);
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = await navigator.mediaDevices.getUserMedia({
    audio: deviceId ? { deviceId: { exact: deviceId } } : true,
  });
  const ctx = new AudioContext();
  const an = ctx.createAnalyser();
  an.fftSize = 512;
  ctx.createMediaStreamSource(stream).connect(an);
  const buf = new Uint8Array(an.fftSize);
  const tick = () => {
    an.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
    $('level').style.width = Math.min(100, (peak / 128) * 160) + '%';
    raf = requestAnimationFrame(tick);
  };
  tick();
}

async function granted() {
  $('step1').hidden = true;
  $('step2').hidden = false;
  const s = await msg('vt_state');
  await listDevices(s.micDeviceId);
  await meter($('device').value);
}

$('grant').onclick = async () => {
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((track) => track.stop());
    await granted();
  } catch (e) {
    const err = String(e.message || e.name).replace(/[<>&]/g, '');
    $('msg').innerHTML = `<span class="bad">${t('没有拿到麦克风权限：{error}', { error: err })}</span><br>`
      + t('如果之前点了「阻止」，请点地址栏左侧的图标把麦克风改为「允许」后刷新本页。');
  }
};

$('device').onchange = () => meter($('device').value);

$('save').onclick = async () => {
  await msg('vt_set_state', { patch: { micDeviceId: $('device').value, magicMic: true } });
  window.close();
};
$('close').onclick = () => window.close();

(async () => {
  await VTI18N.init();
  VTI18N.translateDom();
  document.title = t('边录边讲 · 麦克风设置');
  try {
    const st = await navigator.permissions.query({ name: 'microphone' });
    if (st.state === 'granted') await granted();
  } catch (e) { /* older Chrome can't query the permission; let the user click */ }
})();
