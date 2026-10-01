"""视频合成流水线：逐帧渲染 -> 管道进 ffmpeg -> 合并 -> 导出。"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from .. import config, i18n, storage
from ..models import Project, Step
from . import cards, clips, ffmpeg_util, second_subs, slide_reveal, subtitles as subs, tts
from .ffmpeg_util import CREATE_NO_WINDOW, FFmpegError
from .renderer import StepRenderer, Theme, draw_subtitle, render_outro_card, render_title_card, sub_bottom

Progress = Optional[Callable[[float, str], None]]


class _Aborted(Exception):
    """并行渲染时别的片段已经出错（或用户点了停止）：这一段也不用再画了。"""


# 整个程序同时占用显卡编码器的片段数。NVIDIA 家用显卡同时只能开几路编码，同时渲染几个项目时
# 超了上限就会直接失败、退回 CPU 重编，最后成片还得整段重编一遍（编码器不一致）；排队等一下反而更快
_HW_SLOTS = threading.BoundedSemaphore(4)


def effective_config(proj: Project, overrides: Optional[dict] = None) -> Dict[str, object]:
    """全局配置 < 项目自己的 settings < 本次调用的 overrides。"""
    cfg = dict(config.load())
    for src in (proj.settings or {}, overrides or {}):
        cfg.update({k: v for k, v in src.items() if v is not None})
    return cfg


# ---- 时长 ----------------------------------------------------------------

def speech_lead(step: Step) -> float:
    """配音从这一步的第几秒开始：幻灯片翻页后停一拍再开口（翻页淡入正好做完），其余步骤一上来就说。"""
    if step.kind == "slide" and step.audio and step.audio_duration > 0 and not step.duration_override:
        return slide_reveal.LEAD
    return 0.0


def step_duration(step: Step, cfg: dict) -> float:
    if step.duration_override and step.duration_override > 0:
        return float(step.duration_override)
    pad = float(cfg.get("step_padding", 0.7))
    base = float(cfg.get("min_step_duration", 2.5))
    if step.plays_video():
        length = clips.clip_length(step.clip)
        # 视频静音、改配解说：解说比视频长就停在最后一帧，等话说完
        if not step.plays_clip_audio() and step.audio_duration > 0:
            return max(length, step.audio_duration + pad)
        return length
    if step.audio_duration > 0:
        return max(base, speech_lead(step) + step.audio_duration + pad)
    # 没有配音时，按文字长度估算一个合理停留时间
    n = len(step.narration or step.caption or "")
    return max(base, min(9.0, 1.6 + n * 0.16))


def _worker_count(cfg: dict, n_clips: int) -> int:
    """同时渲染几段。0 / 不填 = 按 CPU 核数自动（留一半给编码和系统，最多 4）。"""
    want = int(cfg.get("render_workers", 0) or 0)
    if want <= 0:
        want = max(1, min(4, (os.cpu_count() or 4) // 2))
    return max(1, min(want, n_clips))


# ---- 编码 ----------------------------------------------------------------

def _encode_clip(make_frames: Callable[[], object], width: int, height: int, fps: int,
                 audio: Optional[Path], out_path: Path, log_path: Path,
                 on_frame: Optional[Callable[[], None]] = None,
                 duration: float = 0.0, encoder: str = "libx264", audio_delay: float = 0.0) -> str:
    """make_frames() 返回一个产出帧字节的迭代器，逐帧写进 ffmpeg 的 stdin。

    返回实际用到的编码器名字（显卡编码中途失败会退回 CPU，所以不一定是传进来的那个）。

    duration：这一段应有的时长（秒）。必须给，而且要写死到 ffmpeg 参数里：
    配音后面接的静音（apad）和无配音时的 anullsrc 都是无限长的音频，只靠 -shortest 截断
    并不可靠 —— ffmpeg 7.1 就会一直往后写静音（画面是从管道慢慢喂进去的，音频却能飞快地
    生成，等画面结束时音频已经跑出去很远）。结果就是 1 分钟的视频变成一个多小时。
    """
    frame_count = max(1, int(round(duration * fps))) if duration > 0 else 0
    limit = frame_count / fps if frame_count else 0.0
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def attempt(codec: str, tick: Optional[Callable[[], None]]) -> int:
        cmd = [
            ffmpeg_util.encoder_bin(codec), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", f"{width}x{height}", "-framerate", str(fps),
            "-i", "pipe:0",
        ]
        if audio and Path(audio).exists():
            pad = f"apad=whole_dur={limit:.3f}" if limit else "apad"
            if audio_delay > 0:          # 配音晚一点开口（翻页后停一拍）
                pad = f"adelay=delays={int(round(audio_delay * 1000))}:all=1," + pad
            cmd += ["-i", str(audio), "-filter_complex", f"[1:a]{pad}[a]",
                    "-map", "0:v", "-map", "[a]"]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-map", "0:v", "-map", "1:a"]
        if limit:
            cmd += ["-t", f"{limit:.3f}"]
        cmd += ["-shortest", "-c:v", codec] + ffmpeg_util.encoder_args(codec) + [
            "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            str(out_path),
        ]
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                    creationflags=CREATE_NO_WINDOW)
            frames = None
            try:
                frames = make_frames()
                for buf in frames:
                    try:
                        proc.stdin.write(buf)
                    except OSError:
                        # ffmpeg 已经退出（比如显卡编码会话数到了上限、驱动出错）。Windows 上这时报的
                        # 常常不是 BrokenPipeError 而是 OSError 22，都按「这次没编成」处理，
                        # 下面看返回码退回 CPU 重编。只包住写管道这一句：画面生成出的错照常往外抛
                        break
                    if tick:
                        tick()
            finally:
                close = getattr(frames, "close", None)
                if close:
                    close()                      # 生成器里开着的解码 ffmpeg（视频步骤）马上结束
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.wait()
        return proc.returncode

    code = attempt(encoder, on_frame)
    if (code != 0 or not out_path.exists()) and encoder != "libx264":
        # 显卡编码器有时能试跑、真干活却挂（驱动老、同时开的编码会话超上限……）：退回 CPU 重编这一段
        encoder = "libx264"
        code = attempt(encoder, None)
    if code != 0 or not out_path.exists():
        err = log_path.read_text(encoding="utf-8", errors="ignore")[-1200:]
        raise FFmpegError(i18n.t("编码片段失败：{name}", name=out_path.name) + f"\n{err}")
    return encoder


def _concat(parts: List[Path], out_path: Path, work: Path, reencode: bool = False) -> Path:
    if not parts:
        raise FFmpegError(i18n.t("没有可合并的片段。"))
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return out_path
    lst = work / "concat.txt"
    lines = []
    for c in parts:
        p = str(c.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{p}'")
    lst.write_text("\n".join(lines), encoding="utf-8")
    args = ["-f", "concat", "-safe", "0", "-i", str(lst)]
    if reencode:
        # 有片段是显卡编的、有片段退回了 CPU：编码参数不一致，直接拼会出问题，重编一遍视频
        args += ["-c:v", "libx264"] + ffmpeg_util.encoder_args("libx264") + ["-pix_fmt", "yuv420p", "-c:a", "copy"]
    else:
        args += ["-c", "copy"]
    ffmpeg_util.run(args + ["-movflags", "+faststart", str(out_path)])
    return out_path


# ---- 帧生成器 -------------------------------------------------------------

def _cue_text_at(cues: List[subs.Cue], t: float) -> str:
    for c in cues:
        if c.start <= t <= c.end:
            return c.text
    return ""


def _step_frames(rend: StepRenderer, duration: float, fps: int,
                 cues: List[subs.Cue], theme: Theme):
    """逐帧产出 rgb24 字节。画面静止时（幻灯片翻页淡入之后、逐条出现的两条之间）直接复用上一帧的字节，
    省掉重复的绘制 —— 一页只有字幕在变时，这一步几乎不花时间。"""
    n = max(1, int(round(duration * fps)))
    last_text, last_buf, last_key = None, None, None
    for i in range(n):
        t = i / fps
        text = _cue_text_at(cues, t) if cues else ""
        key = rend.frame_key(t)
        if last_buf is not None and key is not None and key == last_key and text == last_text:
            yield last_buf
            continue
        img = rend.frame(t)
        if cues:
            img = draw_subtitle(img, text, theme)
        last_text, last_buf, last_key = text, img.tobytes(), key
        yield last_buf


def _card_frames(theme: Theme, title: str, subtitle: str, duration: float, fps: int,
                 cues: List[subs.Cue], outro: bool = False, card: Optional[dict] = None):
    """片头 / 片尾的帧。card = {"background", "show_text", "fit"}（自定义背景）。

    文字只在开头 0.7 秒浮现、最后 0.4 秒淡出，中间画面不变，直接复用上一帧；
    只显示背景图、不叠字时整段都不变。"""
    card = card or {}
    n = max(1, int(round(duration * fps)))
    animated = card.get("show_text", True) or card.get("background") is None
    last_text, last_buf = None, None
    for i in range(n):
        t = i / fps
        text = _cue_text_at(cues, t) if cues else ""
        still = not animated or 0.7 <= t <= duration - 0.4
        if still and last_buf is not None and text == last_text:
            yield last_buf
            continue
        img = (render_outro_card(theme, title, t, duration, **card) if outro
               else render_title_card(theme, title, subtitle, t, duration, **card))
        if cues:
            img = draw_subtitle(img, text, theme)
        buf = img.tobytes()
        last_text, last_buf = (text, buf) if still else (None, None)
        yield buf


def _card_duration(style, audio: float, auto: float) -> float:
    """设了停留时间就按设的来，但不能比配音还短（话没说完就切走）。"""
    if style is not None and style.duration > 0:
        return max(float(style.duration), audio + 0.3 if audio else 0.8)
    return auto


def _card_style(proj: Project, kind: str) -> dict:
    """渲染片头 / 片尾要用的背景参数；没有自定义背景（或图片丢了）时是空的。"""
    style = proj.intro_card if kind == "intro" else proj.outro_card
    bg = cards.resolve(proj.id, style)
    if bg is None:
        return {}
    return {"background": bg, "show_text": style.show_text, "fit": style.fit}


# ---- 主流程 --------------------------------------------------------------

def render_project(proj: Project, progress: Progress = None,
                   overrides: Optional[dict] = None) -> Dict[str, object]:
    t_start = time.time()
    cfg = effective_config(proj, overrides)
    theme = Theme.from_config(cfg)
    W, H, fps = theme.width, theme.height, int(cfg.get("video_fps", 30))
    burn = bool(cfg.get("burn_subtitles", True))

    work = storage.work_dir(proj.id) / f"render_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _render(proj, cfg, theme, W, H, fps, burn, work, progress, t_start)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render(proj: Project, cfg: dict, theme: Theme, W: int, H: int, fps: int, burn: bool,
            work: Path, progress: Progress, t_start: float) -> Dict[str, object]:
    shots = storage.screenshots_dir(proj.id)
    auds = storage.audio_dir(proj.id)
    out_dir = storage.output_dir(proj.id)
    out_dir.mkdir(parents=True, exist_ok=True)

    steps = [s for s in proj.steps if s.include]
    if not steps:
        raise FFmpegError(i18n.t("没有启用的步骤，无法生成视频。"))

    # --- 1. 规划时间轴 ---
    plan: List[Dict] = []
    cursor_t = 0.0
    # 片头片尾只用「正是按当前文案合成」的配音，文案改过就不拿旧音频凑数
    intro_audio = tts.card_audio(auds, "intro", proj.intro)
    intro_card, outro_card = _card_style(proj, "intro"), _card_style(proj, "outro")
    if cfg.get("intro_enabled", True) and (proj.title or proj.intro or intro_card):
        d = ffmpeg_util.probe_duration(intro_audio) if intro_audio else 0.0
        dur = _card_duration(proj.intro_card, d, max(2.8, d + 1.0) if d else 3.0)
        plan.append({"kind": "intro", "duration": dur, "start": cursor_t,
                     "audio": intro_audio, "text": proj.intro, "audio_dur": d})
        cursor_t += dur
    bad_audio: List[Tuple[int, str]] = []        # 配音文件丢了 / 读不出来、按静音出的段落（最后提醒用户）
    for no, s in enumerate(steps, 1):
        dur = step_duration(s, cfg)
        s.duration = dur
        ap = auds / s.audio if s.audio else None
        if ap is not None and not ap.exists() and not s.plays_clip_audio():
            bad_audio.append((len(plan), i18n.t("步骤 {n}", n=no)))
        if s.plays_clip_audio():
            # 播视频原声：解说不念（渲染时再从视频里取声音），字幕只用「字幕」这一栏，
            # 按「讲话转字幕」时记下的逐词时间对齐（换算到截取起点）
            plan.append({"kind": "step", "step": s, "no": no, "duration": dur, "start": cursor_t, "audio": None,
                         "text": s.subtitle_text(), "audio_dur": dur, "bounds": clips.clip_words(s.clip)})
        else:
            plan.append({"kind": "step", "step": s, "no": no, "duration": dur, "start": cursor_t,
                         "audio": ap if (ap and ap.exists()) else None, "lead": speech_lead(s),
                         "text": s.subtitle_text(), "audio_dur": s.audio_duration, "bounds": s.boundaries})
        cursor_t += dur
    outro_audio = tts.card_audio(auds, "outro", proj.outro)
    if cfg.get("outro_enabled", True) and (proj.outro or outro_card):
        d = ffmpeg_util.probe_duration(outro_audio) if outro_audio else 0.0
        dur = _card_duration(proj.outro_card, d, max(2.4, d + 1.0) if d else 2.6)
        plan.append({"kind": "outro", "duration": dur, "start": cursor_t,
                     "audio": outro_audio, "text": proj.outro, "audio_dur": d})
        cursor_t += dur
    total_dur = cursor_t

    # --- 2. 字幕 ---
    global_cues: List[subs.Cue] = []
    local_cues: List[List[subs.Cue]] = []
    for item in plan:
        text = (item.get("text") or "").strip()
        if not text:
            local_cues.append([])
            continue
        seg = subs.Segment(
            start=item.get("lead", 0.0),             # 字幕跟着配音一起晚一拍
            duration=item["audio_dur"] or max(1.0, item["duration"] - 0.4),
            text=text,
            boundaries=item.get("bounds") or [],
        )
        cues = subs.segment_to_cues(seg)
        local_cues.append(cues)
        for c in cues:
            global_cues.append(subs.Cue(c.start + item["start"], c.end + item["start"], c.text))

    slug = _safe_name(proj.title or proj.name)
    srt_path = out_dir / f"{slug}.srt"
    subs.write_srt(global_cues, srt_path)
    subs.write_vtt(global_cues, out_dir / f"{slug}.vtt")
    # 主字幕的时间轴：以后生成第二语言字幕（外挂）不用重新渲染
    second_subs.save_primary(out_dir, f"{slug}.mp4", global_cues, proj.language, burned=burn, space=theme.sub_space,
                             bottom=sub_bottom(theme))

    # --- 3. 逐段渲染 ---
    total_frames = sum(max(1, int(round(p["duration"] * fps))) for p in plan)
    done = [0]

    tick_lock = threading.Lock()
    stop = threading.Event()

    def tick():
        if stop.is_set():
            raise _Aborted()
        with tick_lock:
            done[0] += 1
            n = done[0]
        if progress and n % 12 == 0:
            frac = n / max(1, total_frames)
            eta = (time.time() - t_start) / max(0.01, frac) * (1 - frac)
            progress(frac * 0.97, i18n.t("渲染中 {done}/{total} 帧，预计还需 {sec} 秒", done=n, total=total_frames, sec=int(eta)))

    # 光标要从上一步点过的位置飞过来，所以先把每一步的点击点串一遍
    # （只读截图文件头算坐标，不解码图片，很快）
    prev_point: Optional[Tuple[float, float]] = None
    for item in plan:
        if item["kind"] != "step":
            continue
        s: Step = item["step"]
        item["prev_point"] = prev_point
        prev_point = StepRenderer.click_point(
            s, shots / s.screenshot if s.screenshot else Path("_"), theme) or prev_point

    encoder = ffmpeg_util.pick_encoder(str(cfg.get("video_encoder", "auto") or "auto"))
    workers = _worker_count(cfg, len(plan))
    outro_text = proj.outro or ("" if outro_card else i18n.t("完成！", _lang=i18n.content_lang(proj.language)))
    intro_title = proj.title or ("" if intro_card else proj.name)

    def _prev_slide_frame(i: int):
        """这一步和上一步都是幻灯片时，返回上一页最后的画面（翻页时从它淡过来）；否则 None。"""
        item, prev = plan[i], plan[i - 1] if i > 0 else None
        if (item["kind"] != "step" or item["step"].kind != "slide" or prev is None or prev["kind"] != "step"
                or prev["step"].kind != "slide"):
            return None
        ps: Step = prev["step"]
        try:
            pr = StepRenderer(step=ps, screenshot_path=shots / ps.screenshot if ps.screenshot else Path("_"),
                              theme=theme, total_steps=len(steps), duration=prev["duration"], step_no=prev["no"],
                              speech_offset=prev.get("lead", 0.0))
            return pr.frame(max(0.36, prev["duration"] - 1.0 / fps))
        except Exception:
            return None

    def render_clip(idx_item) -> Tuple[Path, str]:
        i, item = idx_item
        if stop.is_set():
            raise _Aborted()
        clip = work / f"clip_{i:03d}.mp4"
        log = work / f"clip_{i:03d}.log"
        cues = local_cues[i] if burn else []
        audio = item["audio"]
        video_src = (clips.resolve(proj.id, item["step"].clip)
                     if item["kind"] == "step" and item["step"].plays_video() else None)
        vinfo = None
        if video_src is not None:
            try:
                vinfo = clips.probe(video_src)
            except clips.ClipError:
                video_src = None                  # 视频文件坏了：这一步退回只显示幻灯片，不让整段渲染失败
        if video_src is not None:
            s: Step = item["step"]
            if s.plays_clip_audio():
                audio = clips.extract_audio(video_src, s.clip, work / f"clip_{i:03d}_audio.wav")

            def make_frames(item=item, cues=cues, s=s, src=video_src, vinfo=vinfo):
                rend = StepRenderer(step=s, screenshot_path=shots / s.screenshot if s.screenshot else Path("_"),
                                    theme=theme, total_steps=len(steps), duration=item["duration"],
                                    step_no=item["no"])
                box, inset = clips.placement(s.clip, rend.draw_box, rend.shot is not None, W, H,
                                             vinfo["width"], vinfo["height"])
                base = rend.stage if inset else Image.new("RGB", (W, H), (0, 0, 0))
                n = max(1, int(round(item["duration"] * fps)))
                return clips.frames(src, s.clip, base, box, fps, n,
                                    lambda t: _cue_text_at(cues, t) if cues else "",
                                    lambda img, text: draw_subtitle(img, text, theme))
        elif item["kind"] == "step":
            def make_frames(item=item, cues=cues, i=i):
                s: Step = item["step"]
                rend = StepRenderer(
                    step=s,
                    screenshot_path=shots / s.screenshot if s.screenshot else Path("_"),
                    theme=theme, total_steps=len(steps),
                    prev_point=item.get("prev_point"), duration=item["duration"], step_no=item["no"],
                    speech_offset=item.get("lead", 0.0), prev_frame=_prev_slide_frame(i),
                )
                return _step_frames(rend, item["duration"], fps, cues, theme)
        elif item["kind"] == "intro":
            def make_frames(item=item, cues=cues):
                return _card_frames(theme, intro_title, proj.subtitle,
                                    item["duration"], fps, cues, card=intro_card)
        else:
            def make_frames(item=item, cues=cues):
                return _card_frames(theme, outro_text, "", item["duration"], fps, cues, outro=True,
                                    card=outro_card)
        hw = encoder != "libx264"
        if hw:
            while not _HW_SLOTS.acquire(timeout=0.3):
                # 等显卡空出来（别的项目也在渲染）；等的时候照样响应「停止」
                if stop.is_set():
                    raise _Aborted()
                if progress:
                    progress(min(1.0, done[0] / max(1, total_frames)) * 0.97, "")
        try:
            used = _encode_clip(make_frames, W, H, fps, audio, clip, log, tick,
                                duration=item["duration"], encoder=encoder, audio_delay=item.get("lead", 0.0))
        except FFmpegError:
            if audio is None or ffmpeg_util.audio_readable(audio):
                raise
            # 配音文件坏了（上传的录音格式不对、文件被截断或被杀毒软件动过……）：
            # 别让整个视频失败，这一段按静音出，最后提醒重新配音
            label = (i18n.t("步骤 {n}", n=item["no"]) if item["kind"] == "step"
                     else i18n.t("片头") if item["kind"] == "intro" else i18n.t("片尾"))
            with tick_lock:
                bad_audio.append((i, label))
            used = _encode_clip(make_frames, W, H, fps, None, clip, log, None,
                                duration=item["duration"], encoder=encoder)
        finally:
            if hw:
                _HW_SLOTS.release()
        return clip, used

    if progress:
        progress(0.0, i18n.t("渲染中 {done}/{total} 帧，预计还需 {sec} 秒",
                             done=0, total=total_frames, sec="…"))
    if workers > 1:
        # 画面是 Python 一帧一帧画出来的，这是整个流程最慢的地方。Pillow 画图时会放开 GIL，
        # 所以多线程能真正用上多核；每段片段还各自有一个 ffmpeg 在编码，一起并行。
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as ex:
            futs = [ex.submit(render_clip, x) for x in enumerate(plan)]
            wait(futs, return_when=FIRST_EXCEPTION)
            bad = next((f for f in futs if f.done() and not f.cancelled() and f.exception() is not None), None)
            if bad is not None:
                # 有一段出错或用户点了停止：还没开始的不再开始，正在画的尽快停下，马上报出来
                # （不然要等其余几十段全部画完才看到错误）
                stop.set()
                for f in futs:
                    f.cancel()
                raise bad.exception()
            made = [f.result() for f in futs]
    else:
        made = [render_clip(x) for x in enumerate(plan)]
    clip_paths: List[Path] = [c for c, _ in made]
    used = {u for _, u in made}
    encoder = used.pop() if len(used) == 1 else "mixed"

    # --- 4. 合并 ---
    if progress:
        progress(0.97, i18n.t("合并片段…"))
    out_name = f"{slug}.mp4"
    out_path = out_dir / out_name
    part = work / "final.mp4"
    _concat(clip_paths, part, work, reencode=(encoder == "mixed"))
    storage._replace_with_retry(part, out_path)   # 合成完再一次性替换，播放器不会读到半个文件

    proj.output = out_name
    elapsed = time.time() - t_start
    warning = ""
    if bad_audio:
        warning = i18n.t("有 {n} 段的配音文件读不出来（{parts}），已按静音生成。请重新配音后再渲染。",
                         n=len(bad_audio), parts=("、" if i18n.current() == "zh" else ", ").join(x for _, x in sorted(bad_audio)))
    if progress:
        done_msg = i18n.t("完成！时长 {duration} 秒，耗时 {elapsed} 秒", duration=f"{total_dur:.1f}", elapsed=f"{elapsed:.0f}")
        progress(1.0, done_msg + (" " + warning if warning else ""))
    return {
        "warning": warning,
        "file": out_name,
        "duration": round(total_dur, 2),
        "size": out_path.stat().st_size if out_path.exists() else 0,
        "srt": srt_path.name,
        "elapsed": round(elapsed, 1),
        "steps": len(steps),
        "encoder": encoder,          # 实际用的编码器：libx264 = CPU，h264_nvenc/qsv/amf = 显卡
        "workers": workers,
    }


# ---- 单步预览图 -----------------------------------------------------------

def _jpeg(img: Image.Image, theme: Theme, scale: float) -> bytes:
    if scale and scale != 1.0:
        img = img.resize((int(theme.width * scale), int(theme.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def render_step_preview(proj: Project, step: Step, t: float = 1.2,
                        with_subtitle: bool = True, scale: float = 0.5) -> bytes:
    theme = Theme.from_config(effective_config(proj))
    included = [s.id for s in proj.steps if s.include]
    rend = StepRenderer(
        step=step,
        screenshot_path=storage.screenshots_dir(proj.id) / step.screenshot if step.screenshot else Path("_"),
        theme=theme,
        total_steps=len(included),
        duration=max(2.5, step.duration or 3.0),
        step_no=included.index(step.id) + 1 if step.id in included else 0,
    )
    img = rend.final_frame() if rend.reveal is not None else rend.frame(t)
    src = clips.resolve(proj.id, step.clip) if step.plays_video() else None
    if src is not None:
        try:
            info = clips.probe(src)
            box, inset = clips.placement(step.clip, rend.draw_box, rend.shot is not None,
                                         theme.width, theme.height, info["width"], info["height"])
            img = rend.stage.copy() if inset else Image.new("RGB", (theme.width, theme.height), (0, 0, 0))
            frame = clips.grab_frame(src, clips.clip_range(step.clip)[0] + t, box[2], box[3])
            if frame is not None:
                img.paste(frame, (box[0], box[1]))
        except clips.ClipError:
            pass
    text = (step.subtitle_text() or "").strip().split("\n")[0]      # 预览只显示第一行（两人问答一句一行）
    if with_subtitle and text:
        img = draw_subtitle(img, text[:60], theme)
    return _jpeg(img, theme, scale)


def render_card_preview(proj: Project, kind: str = "intro", t: float = 1.2,
                        with_subtitle: bool = True, scale: float = 0.5) -> bytes:
    """片头 / 片尾卡的预览图。"""
    theme = Theme.from_config(effective_config(proj))
    card = _card_style(proj, kind)
    if kind == "outro":
        text = proj.outro or ("" if card else i18n.t("完成！", _lang=i18n.content_lang(proj.language)))
        img = render_outro_card(theme, text, t, max(2.6, t + 1.5), **card)
        sub = proj.outro
    else:
        img = render_title_card(theme, proj.title or ("" if card else proj.name), proj.subtitle,
                                t, max(3.0, t + 1.5), **card)
        sub = proj.intro
    if with_subtitle and sub:
        img = draw_subtitle(img, sub[:60], theme)
    return _jpeg(img, theme, scale)


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad or ord(c) < 32 else c for c in (name or "tutorial")).strip(" .")
    out = (out or "tutorial")[:60].strip(" .")
    if out.split(".")[0].upper() in _RESERVED:      # Windows 保留名当文件名会失败
        out = "_" + out
    return out or "tutorial"
