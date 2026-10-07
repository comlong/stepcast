"""Video pipeline: render frames -> pipe them into ffmpeg -> concatenate -> export."""
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
from . import cards, clips, ffmpeg_util, second_subs, slide_reveal, slide_sequence, subtitles as subs, tts
from .ffmpeg_util import CREATE_NO_WINDOW, FFmpegError
from .renderer import StepRenderer, Theme, draw_subtitle, ease_out_cubic, render_outro_card, render_title_card, sub_bottom

Progress = Optional[Callable[[float, str], None]]


class _Aborted(Exception):
    """Another segment of a parallel render already failed (or the user clicked Stop): no need to draw this one either."""


# Number of segments using the GPU encoder at once across the whole program. Consumer NVIDIA cards allow only a few concurrent encodes; when several
# projects render at once, exceeding the limit fails, falls back to CPU, and the final video must be re-encoded entirely (mixed encoders); queueing is faster
_HW_SLOTS = threading.BoundedSemaphore(4)


def effective_config(proj: Project, overrides: Optional[dict] = None) -> Dict[str, object]:
    """Global settings < the project's own settings < overrides of this call."""
    cfg = dict(config.load())
    for src in (proj.settings or {}, overrides or {}):
        cfg.update({k: v for k, v in src.items() if v is not None})
    return cfg


# ---- durations -------------------------------------------------------------

def speech_lead(step: Step) -> float:
    """Second at which the voice-over starts in this step: slides pause briefly after the slide change (the fade-in just finishes); other steps start right away."""
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
        # video muted and narrated: if the narration is longer than the video, hold the last frame until it ends
        if not step.plays_clip_audio() and step.audio_duration > 0:
            return max(length, step.audio_duration + pad)
        return length
    if step.audio_duration > 0:
        return max(base, speech_lead(step) + step.audio_duration + pad)
    # without voice-over, estimate a reasonable display time from the text length
    n = len(step.narration or step.caption or "")
    return max(base, min(9.0, 1.6 + n * 0.16))


def _worker_count(cfg: dict, n_clips: int) -> int:
    """How many segments to render at once. 0 / unset = automatic by CPU count (half left for encoding and the system, at most 4)."""
    want = int(cfg.get("render_workers", 0) or 0)
    if want <= 0:
        want = max(1, min(4, (os.cpu_count() or 4) // 2))
    return max(1, min(want, n_clips))


# ---- encoding --------------------------------------------------------------

def _encode_clip(make_frames: Callable[[], object], width: int, height: int, fps: int,
                 audio: Optional[Path], out_path: Path, log_path: Path,
                 on_frame: Optional[Callable[[], None]] = None,
                 duration: float = 0.0, encoder: str = "libx264", audio_delay: float = 0.0) -> str:
    """make_frames() returns an iterator of frame bytes, written frame by frame into ffmpeg's stdin.

    Returns the encoder actually used (a GPU encode that fails halfway falls back to CPU, so it may differ from the one passed in).

    duration: the length this segment must have (seconds). It is required and is written into the ffmpeg arguments:
    the silence appended after the voice-over (apad) and anullsrc without voice-over are infinite audio, and cutting them only with -shortest
    isn't reliable — ffmpeg 7.1 keeps writing silence (frames are fed slowly through the pipe while audio is generated very fast,
    so by the time the frames end the audio is far ahead). The result was a one-minute video turning into more than an hour.
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
            if audio_delay > 0:          # the voice-over starts a little later (short pause after a slide change)
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
                        # ffmpeg has already exited (e.g. the GPU's encode session limit was reached, a driver error). On Windows this
                        # is often OSError 22 rather than BrokenPipeError; both count as "this encode failed",
                        # and the return code below triggers a CPU re-encode. Only the pipe write is wrapped: errors from frame generation propagate as usual
                        break
                    if tick:
                        tick()
            finally:
                close = getattr(frames, "close", None)
                if close:
                    close()                      # the decoding ffmpeg opened by the generator (video steps) ends right away
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.wait()
        return proc.returncode

    code = attempt(encoder, on_frame)
    if (code != 0 or not out_path.exists()) and encoder != "libx264":
        # GPU encoders sometimes pass the test run but fail on real work (old driver, too many concurrent sessions …): re-encode this segment on the CPU
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
        # some segments were GPU-encoded and some fell back to CPU: the encoding parameters differ and simple concatenation breaks, so re-encode the video
        args += ["-c:v", "libx264"] + ffmpeg_util.encoder_args("libx264") + ["-pix_fmt", "yuv420p", "-c:a", "copy"]
    else:
        args += ["-c", "copy"]
    ffmpeg_util.run(args + ["-movflags", "+faststart", str(out_path)])
    return out_path


# ---- frame generators ------------------------------------------------------

def _cue_text_at(cues: List[subs.Cue], t: float) -> str:
    for c in cues:
        if c.start <= t <= c.end:
            return c.text
    return ""


def _step_frames(rend: StepRenderer, duration: float, fps: int,
                 cues: List[subs.Cue], theme: Theme):
    """Yield rgb24 frame bytes. While the frame is static (after a slide fade-in, between two reveal items) the previous frame's bytes are reused,
    skipping redundant drawing — when only the subtitle changes on a slide, this step costs almost nothing."""
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
    """Frames of the intro / outro card. card = {"background", "show_text", "fit"} (custom background).

    Text only fades in during the first 0.7 s and out during the last 0.4 s; in between the frame is unchanged and the previous one is reused;
    with only a background image and no text, the whole card is static."""
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
    """Use the configured display time, but never shorter than the voice-over (don't cut away mid-sentence)."""
    if style is not None and style.duration > 0:
        return max(float(style.duration), audio + 0.3 if audio else 0.8)
    return auto


def _card_style(proj: Project, kind: str) -> dict:
    """Background parameters for rendering the intro / outro; empty without a custom background (or if the image is missing)."""
    style = proj.intro_card if kind == "intro" else proj.outro_card
    bg = cards.resolve(proj.id, style)
    if bg is None:
        return {}
    return {"background": bg, "show_text": style.show_text, "fit": style.fit}


# ---- video-first slides ----------------------------------------------------

def _video_first_args(proj: Project, slide: Step, theme: Theme) -> Dict[str, object]:
    """Extra StepRenderer arguments for a slide whose videos play first (see slide_sequence); {} for other steps."""
    if not slide_sequence.is_video_first(slide):
        return {}
    videos = slide_sequence.slide_videos(proj.steps, slide)
    if not videos:
        return {}
    shots = storage.screenshots_dir(proj.id)
    box, has_slide = StepRenderer.layout_box(slide, shots / slide.screenshot if slide.screenshot else Path("_"), theme)
    last = slide_sequence.last_frames(proj.id, videos, box, has_slide, theme.width, theme.height)
    # when the last video played in place the picture simply continues (no fade); after a full-screen one the slide fades in as usual
    return {"under": [f for f in last if f is not None], "over": slide_sequence.over_video(slide, videos, shots),
            "fade_in": last[-1] is None}


def _video_first_base(proj: Project, slide: Step, theme: Theme, args: Dict[str, object]) -> Optional[Image.Image]:
    """Background while a video-first slide's videos play: the slide with the text over the videos hidden.
    args = _video_first_args of the slide; None without reveal data (the video step's own background is used then)."""
    if not args or not slide.screenshot:
        return None
    try:
        rend = StepRenderer(step=slide, screenshot_path=storage.screenshots_dir(proj.id) / slide.screenshot,
                            theme=theme, **args)
    except Exception:
        return None
    return rend.reveal.clean if rend.reveal is not None else None


def _fade_in(frames, prev: Optional[Image.Image], bg: Tuple[int, int, int], fps: int, W: int, H: int):
    """Fade the first 0.35 s of a segment in the way a slide change does (see StepRenderer.frame):
    from prev (the previous slide's last frame), otherwise from the background color."""
    start = prev if prev is not None and prev.size == (W, H) else Image.new("RGB", (W, H), bg)
    for k, buf in enumerate(frames):
        t = k / fps
        if t < 0.35:
            img = Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1)
            buf = Image.blend(start, img, ease_out_cubic(t / 0.35)).tobytes()
        yield buf


# ---- main flow -------------------------------------------------------------

def preview_size(cfg: dict, max_width: int = 1280, max_fps: int = 24) -> Dict[str, int]:
    """Size and frame rate of a playback preview: the project's picture shape at (at most) 1280 px wide and 24 fps."""
    W, H = int(cfg.get("video_width", 1920)), int(cfg.get("video_height", 1080))
    k = min(1.0, max_width / max(1, W))
    return {"video_width": max(2, int(round(W * k / 2)) * 2), "video_height": max(2, int(round(H * k / 2)) * 2),
            "video_fps": min(max_fps, int(cfg.get("video_fps", 30)))}


def render_project(proj: Project, progress: Progress = None, overrides: Optional[dict] = None,
                   preview_out: Optional[Path] = None) -> Dict[str, object]:
    """Render the project's included steps. With preview_out the video goes to that file instead (a playback preview of a few pages):
    the real video and its subtitle files are left alone, and intro / outro follow the overrides only."""
    t_start = time.time()
    cfg = effective_config(proj, overrides)
    theme = Theme.from_config(cfg)
    W, H, fps = theme.width, theme.height, int(cfg.get("video_fps", 30))
    burn = bool(cfg.get("burn_subtitles", True))

    work = storage.work_dir(proj.id) / f"render_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _render(proj, cfg, theme, W, H, fps, burn, work, progress, t_start, preview_out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render(proj: Project, cfg: dict, theme: Theme, W: int, H: int, fps: int, burn: bool,
            work: Path, progress: Progress, t_start: float, preview_out: Optional[Path] = None) -> Dict[str, object]:
    shots = storage.screenshots_dir(proj.id)
    auds = storage.audio_dir(proj.id)
    out_dir = storage.output_dir(proj.id)
    if preview_out is None:
        out_dir.mkdir(parents=True, exist_ok=True)

    # video-first slides: their videos are rendered in front of them
    order, video_owner = slide_sequence.render_order(proj.steps)
    steps = [s for s in order if s.include]
    if not steps and preview_out is None:
        raise FFmpegError(i18n.t("没有启用的步骤，无法生成视频。"))
    # their renderer arguments (the videos' last frames …) and the background while their videos play, prepared once
    vf_args = {s.id: _video_first_args(proj, s, theme) for s in steps if slide_sequence.is_video_first(s)}
    vf_base = {o.id: _video_first_base(proj, o, theme, vf_args.get(o.id, {})) for o in video_owner.values()}

    # --- 1. plan the timeline ---
    plan: List[Dict] = []
    cursor_t = 0.0
    # intro and outro only use voice-over synthesised from the current text; after a text change the old audio isn't used
    intro_audio = tts.card_audio(auds, "intro", proj.intro)
    intro_card, outro_card = _card_style(proj, "intro"), _card_style(proj, "outro")
    if cfg.get("intro_enabled", True) and (proj.title or proj.intro or intro_card):
        d = ffmpeg_util.probe_duration(intro_audio) if intro_audio else 0.0
        dur = _card_duration(proj.intro_card, d, max(2.8, d + 1.0) if d else 3.0)
        plan.append({"kind": "intro", "duration": dur, "start": cursor_t,
                     "audio": intro_audio, "text": proj.intro, "audio_dur": d})
        cursor_t += dur
    bad_audio: List[Tuple[int, str]] = []        # voice-over files that are missing / unreadable, rendered as silence (the user is told at the end)
    for no, s in enumerate(steps, 1):
        dur = step_duration(s, cfg)
        s.duration = dur
        ap = auds / s.audio if s.audio else None
        if ap is not None and not ap.exists() and not s.plays_clip_audio():
            bad_audio.append((len(plan), i18n.t("步骤 {n}", n=no)))
        if s.plays_clip_audio():
            # video with its own sound: the narration isn't spoken (the sound is taken from the video when rendering), only the "subtitle" field is shown,
            # aligned with the word times recorded by "speech to subtitles" (shifted to the trim start)
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
    if not plan:
        raise FFmpegError(i18n.t("没有启用的步骤，无法生成视频。"))

    # --- 2. subtitles ---
    global_cues: List[subs.Cue] = []
    local_cues: List[List[subs.Cue]] = []
    for item in plan:
        text = (item.get("text") or "").strip()
        if not text:
            local_cues.append([])
            continue
        seg = subs.Segment(
            start=item.get("lead", 0.0),             # subtitles start a beat later together with the voice-over
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
    if preview_out is None:                  # (a preview must not touch the subtitle files of the real video)
        subs.write_srt(global_cues, srt_path)
        subs.write_vtt(global_cues, out_dir / f"{slug}.vtt")
        # main subtitle timeline: second-language subtitles (external) can be generated later without re-rendering
        second_subs.save_primary(out_dir, f"{slug}.mp4", global_cues, proj.language, burned=burn, space=theme.sub_space,
                                 bottom=sub_bottom(theme))

    # --- 3. render the segments ---
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

    # the cursor flies in from where the previous step clicked, so chain the click points of all steps first
    # (coordinates come from the screenshot file headers without decoding the images, so this is fast)
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
        """If this step is a slide (or the first video of a video-first slide) and the previous one is a slide,
        return the previous slide's last frame (cross-fade from it); otherwise None."""
        item, prev = plan[i], plan[i - 1] if i > 0 else None
        if item["kind"] != "step" or prev is None or prev["kind"] != "step" or prev["step"].kind != "slide":
            return None
        if item["step"].kind != "slide" and item["step"].id not in video_owner:
            return None
        ps: Step = prev["step"]
        try:
            pr = StepRenderer(step=ps, screenshot_path=shots / ps.screenshot if ps.screenshot else Path("_"),
                              theme=theme, total_steps=len(steps), duration=prev["duration"], step_no=prev["no"],
                              speech_offset=prev.get("lead", 0.0), **vf_args.get(ps.id, {}))
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
                video_src = None                  # broken video file: this step falls back to showing just the slide instead of failing the whole render
        if video_src is not None:
            s: Step = item["step"]
            if s.plays_clip_audio():
                audio = clips.extract_audio(video_src, s.clip, work / f"clip_{i:03d}_audio.wav")

            def make_frames(item=item, cues=cues, s=s, src=video_src, vinfo=vinfo, i=i):
                rend = StepRenderer(step=s, screenshot_path=shots / s.screenshot if s.screenshot else Path("_"),
                                    theme=theme, total_steps=len(steps), duration=item["duration"],
                                    step_no=item["no"])
                box, inset = clips.placement(s.clip, rend.draw_box, rend.shot is not None, W, H,
                                             vinfo["width"], vinfo["height"])
                base = rend.stage if inset else Image.new("RGB", (W, H), (0, 0, 0))
                owner = video_owner.get(s.id)
                if owner is not None and inset and vf_base.get(owner.id) is not None:
                    base = vf_base[owner.id]                # the text over the video stays hidden until after it
                n = max(1, int(round(item["duration"] * fps)))
                frames = clips.frames(src, s.clip, base, box, fps, n,
                                      lambda t: _cue_text_at(cues, t) if cues else "",
                                      lambda img, text: draw_subtitle(img, text, theme))
                prev = plan[i - 1] if i > 0 else None
                if owner is None or (prev is not None and prev["kind"] == "step"
                                     and video_owner.get(prev["step"].id) is owner):
                    return frames
                # the first video of a video-first slide opens that slide: it comes in like a slide change
                return _fade_in(frames, _prev_slide_frame(i), theme.bg, fps, W, H)
        elif item["kind"] == "step":
            def make_frames(item=item, cues=cues, i=i):
                s: Step = item["step"]
                rend = StepRenderer(
                    step=s,
                    screenshot_path=shots / s.screenshot if s.screenshot else Path("_"),
                    theme=theme, total_steps=len(steps),
                    prev_point=item.get("prev_point"), duration=item["duration"], step_no=item["no"],
                    speech_offset=item.get("lead", 0.0), prev_frame=_prev_slide_frame(i),
                    **vf_args.get(s.id, {}),
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
                # wait for the GPU to be free (other projects are rendering too); keep responding to Stop while waiting
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
            # broken voice-over file (wrong format of an uploaded recording, truncated, touched by antivirus …):
            # don't fail the whole video — render this segment silent and remind the user to redo the voice-over
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
        # Frames are drawn one by one in Python, the slowest part of the pipeline. Pillow releases the GIL while drawing,
        # so threads really use several cores; each segment also has its own ffmpeg encoding in parallel.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as ex:
            futs = [ex.submit(render_clip, x) for x in enumerate(plan)]
            wait(futs, return_when=FIRST_EXCEPTION)
            bad = next((f for f in futs if f.done() and not f.cancelled() and f.exception() is not None), None)
            if bad is not None:
                # a segment failed or the user clicked Stop: don't start pending segments, stop the running ones as soon as possible and report right away
                # (otherwise the error would only show after dozens of remaining segments are drawn)
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

    # --- 4. concatenate ---
    if progress:
        progress(0.97, i18n.t("合并片段…"))
    out_name = f"{slug}.mp4"
    out_path = preview_out or (out_dir / out_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = work / "final.mp4"
    _concat(clip_paths, part, work, reencode=(encoder == "mixed"))
    storage._replace_with_retry(part, out_path)   # replace the file in one go after writing, so a player never reads half a file

    if preview_out is None:
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
        "file": out_path.name,
        "duration": round(total_dur, 2),
        "size": out_path.stat().st_size if out_path.exists() else 0,
        "srt": srt_path.name if preview_out is None else "",
        # where each step lies in the video (the editor follows the playback in the step list)
        "segments": [{"id": (it["step"].id if it["kind"] == "step" else "__%s__" % it["kind"]),
                      "start": round(it["start"], 2), "end": round(it["start"] + it["duration"], 2)} for it in plan],
        "elapsed": round(elapsed, 1),
        "steps": len(steps),
        "encoder": encoder,          # encoder actually used: libx264 = CPU, h264_nvenc/qsv/amf = GPU
        "workers": workers,
    }


# ---- single-step preview ---------------------------------------------------

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
        **_video_first_args(proj, step, theme),
    )
    img = rend.final_frame() if rend.reveal is not None else rend.frame(t)
    src = clips.resolve(proj.id, step.clip) if step.plays_video() else None
    if src is not None:
        try:
            info = clips.probe(src)
            box, inset = clips.placement(step.clip, rend.draw_box, rend.shot is not None,
                                         theme.width, theme.height, info["width"], info["height"])
            img = rend.stage.copy() if inset else Image.new("RGB", (theme.width, theme.height), (0, 0, 0))
            owner = slide_sequence.render_order(proj.steps)[1].get(step.id)
            under = _video_first_base(proj, owner, theme, _video_first_args(proj, owner, theme)) \
                if owner is not None and inset else None
            if under is not None:
                img = under.copy()                          # the text over the video stays hidden while it plays
            frame = clips.grab_frame(src, clips.clip_range(step.clip)[0] + t, box[2], box[3])
            if frame is not None:
                img.paste(frame, (box[0], box[1]))
        except clips.ClipError:
            pass
    text = (step.subtitle_text() or "").strip().split("\n")[0]      # the preview only shows the first line (Q&A has one sentence per line)
    if with_subtitle and text:
        img = draw_subtitle(img, text[:60], theme)
    return _jpeg(img, theme, scale)


def render_card_preview(proj: Project, kind: str = "intro", t: float = 1.2,
                        with_subtitle: bool = True, scale: float = 0.5) -> bytes:
    """Preview image of the intro / outro card."""
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
    if out.split(".")[0].upper() in _RESERVED:      # Windows reserved names fail as file names
        out = "_" + out
    return out or "tutorial"
