"""Data structures (recorded steps / projects)."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from . import i18n


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class Rect(BaseModel):
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0


class Target(BaseModel):
    """Description of the element the user acted on (from the Chrome extension)."""
    tag: str = ""
    role: str = ""
    text: str = ""
    name: str = ""            # aria-label / title / placeholder
    selector: str = ""
    input_type: str = ""
    rect: Optional[Rect] = None


class Redaction(BaseModel):
    """One redaction box. Same coordinate system as Target.rect: CSS pixels in viewport coordinates."""
    id: str = Field(default_factory=lambda: new_id("r_"))
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    mode: str = "blur"        # blur (mosaic + blur) | pixelate (mosaic) | solid (solid block)
    kind: str = "manual"      # manual | email | phone | id_card | bank | ip | name | keyword
    label: str = ""           # the matched text; stays on this computer so you can review it
    auto: bool = False        # detected automatically


class TextNode(BaseModel):
    """A piece of visible text on the page and its position, used to redact keywords afterwards."""
    t: str = ""
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    n: bool = False           # during recording it was found in a "name / contact" style column


class VideoClip(BaseModel):
    """The video played by a "video" step: a video embedded in the deck, or a video file inserted by the user."""
    file: str = ""            # video file in the project's media/ folder; empty = not available yet (linked / online videos must be uploaded)
    source: str = ""          # original file name or link (for display)
    duration: float = 0.0     # full length (seconds)
    has_audio: bool = True    # whether the video has sound
    start: float = 0.0        # trim start (seconds)
    end: float = 0.0          # trim end (seconds); 0 = to the end
    mode: str = "inset"       # inset = play at its place on the slide | fullscreen | poster = show the cover only
    rect: Optional[Rect] = None   # position on the slide as a fraction of the page size (0..1); without it, full screen only
    audio: str = "original"   # original = play the video's own sound | mute = silence it and voice the step's narration
    missing: str = ""         # linked = file on the author's computer | online = online video — both must be uploaded by hand
    # Result of "turn speech in the video into subtitles": the recognised text and each word's time in the video
    # (from the start of the video, so changing the trim later keeps them in sync). Kept apart from Step.boundaries of the narration
    transcript: str = ""
    words: List[Dict[str, Any]] = Field(default_factory=list)


class RevealItem(BaseModel):
    """One item on a slide (for reveal-one-by-one): a transparent image with just this item's pixels, and its position on the page image."""
    file: str = ""            # PNG under screenshots/ (with alpha, cropped to the smallest box)
    x: int = 0                # position on the page image (pixels; the page image is this step's screenshot)
    y: int = 0
    text: str = ""            # the item's text, used to find when the narration talks about it


class SlideReveal(BaseModel):
    """Data for "reveal points one by one + highlight the current one" on a slide; generated with PowerPoint on import."""
    clean: str = ""           # page image with all items hidden (under screenshots/)
    items: List[RevealItem] = Field(default_factory=list)   # in reading order
    enabled: bool = True      # whether this slide uses it (only when the project-wide switch is on)
    # AI alignment result: where each item starts being discussed in the narration (character offset, -1 = not mentioned).
    # align_key fingerprints "narration + item texts" at alignment time; once the narration changes it no longer matches and is re-aligned before rendering
    align: List[int] = Field(default_factory=list)
    align_key: str = ""


class DialogueLine(BaseModel):
    """One line of a two-person Q&A dialogue."""
    who: str = "host"         # host (asks, links topics) | expert (explains)
    text: str = ""


class Speaker(BaseModel):
    """One of the two speakers in Q&A mode."""
    role: str = "host"        # host | expert
    name: str = ""            # tells the speakers apart in the editor (never spoken or shown in subtitles)
    voice: str = ""           # voice-over voice


def join_lines(lines: List["DialogueLine"]) -> str:
    """Join the dialogue lines into one narration (one line per sentence); used by subtitles, export and alignment."""
    return "\n".join(ln.text.strip() for ln in lines if (ln.text or "").strip())


def drop_stale_lines(step: "Step") -> None:
    """When the narration is replaced as a whole (transcription, imported script, direct edit …) instead of editing the lines,
    the old lines no longer match: clear them so the step is read as one narration by the host. Otherwise the voice-over would read the old lines while the subtitles show the new narration."""
    if step.lines and join_lines(step.lines) != (step.narration or "").strip():
        step.lines = []


class Step(BaseModel):
    id: str = Field(default_factory=lambda: new_id("s_"))
    index: int = 0
    kind: str = "click"       # click | input | key | navigate | scroll | manual | slide | video
    url: str = ""
    page_title: str = ""
    ts: float = Field(default_factory=time.time)

    screenshot: str = ""      # file name relative to screenshots/
    img_w: int = 0
    img_h: int = 0
    viewport_w: int = 0
    viewport_h: int = 0

    target: Optional[Target] = None
    point: Optional[Dict[str, float]] = None   # click position (CSS pixels, viewport coordinates)
    value: str = ""           # typed text / key name

    # --- slides (kind == "slide") ---
    slide_text: str = ""      # the slide's body text
    slide_notes: str = ""     # speaker notes

    reveal: Optional[SlideReveal] = None      # reveal one by one (None = the whole slide appears at once)
    sequence: str = ""        # how a slide with videos is presented: "" = narration first, then its videos | "video_first" (see slide_sequence)

    # --- video (kind == "video") ---
    clip: Optional[VideoClip] = None

    # --- privacy ---
    redactions: List[Redaction] = Field(default_factory=list)
    text_nodes: List[TextNode] = Field(default_factory=list)  # local only, can be cleared with one click

    # --- script (written by the LLM or edited by hand) ---
    title: str = ""           # small title at the top of the frame
    narration: str = ""       # narration (spoken)
    caption: str = ""         # subtitle (defaults to the narration)
    note: str = ""            # the user's own note for the LLM
    lines: List[DialogueLine] = Field(default_factory=list)   # Q&A dialogue lines; narration is them joined together

    # --- generated ---
    audio: str = ""           # file name relative to audio/
    voice_source: str = "tts"  # tts = AI voice | own = the user's recording (editing the text doesn't discard it)
    audio_duration: float = 0.0
    boundaries: List[Dict[str, Any]] = Field(default_factory=list)  # word boundaries (subtitle timing)
    line_times: List[List[float]] = Field(default_factory=list)     # [start, end] of each dialogue line in the voice-over (seconds)
    duration: float = 0.0     # actual segment length (computed when rendering / can be overridden)
    duration_override: float = 0.0

    include: bool = True
    zoom: bool = True
    highlight: bool = True

    def plays_video(self) -> bool:
        """A video step with a video file that isn't "poster only": the video really plays when rendering."""
        c = self.clip
        return self.kind == "video" and c is not None and bool(c.file) and c.mode != "poster"

    def plays_clip_audio(self) -> bool:
        """Plays the video's own sound: no voice-over for the narration, and the AI doesn't write one (subtitles can still be written)."""
        return self.plays_video() and self.clip.audio == "original" and self.clip.has_audio

    def subtitle_text(self) -> str:
        """The subtitle shown on screen. With the video's own sound only the "subtitle" field is used (the narration isn't spoken);
        if the video is muted and narrated, a subtitle still transcribed from the original sound is replaced by the narration so they match."""
        if self.plays_clip_audio():
            return self.caption
        c = self.clip
        if self.kind == "video" and c is not None and c.transcript and self.caption == c.transcript:
            return self.narration
        return self.caption or self.narration

    def caption_follows_narration(self) -> bool:
        """Whether editing the narration also updates the subtitle. For steps playing the video's own sound the subtitle is the speech in the video and must not be overwritten."""
        return not self.plays_clip_audio()


class SubtitleTrack(BaseModel):
    """One second-language subtitle track (external subtitle file next to the video)."""
    lang: str = ""            # language code, e.g. en-US
    name: str = ""            # display name, e.g. English (US)
    video: str = ""           # the video it was made for (outdated after renaming / re-rendering)
    key: str = ""             # fingerprint of the main subtitle timeline at generation time
    vtt: str = ""             # file name under output/
    srt: str = ""
    created_at: float = 0.0


class CardStyle(BaseModel):
    """Custom intro / outro background: an image, or one page of a PPT / PDF."""
    image: str = ""           # rendered background under the project's cards/ folder (relative path); empty = default gradient
    source: str = ""          # uploaded file name, for display only
    page: int = 1             # which page of the PPT / PDF
    pages: int = 0            # number of pages in the original file; 0 for an image
    show_text: bool = True    # overlay the title (intro) / outro text on the background
    fit: str = "contain"      # contain = show it whole, fill the edges with a blurred copy | cover = fill the frame, crop the rest
    duration: float = 0.0     # seconds on screen; 0 = follow the voice-over length


class Project(BaseModel):
    id: str = Field(default_factory=lambda: new_id("p_"))
    name: str = Field(default_factory=lambda: i18n.t("未命名教程"))
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    language: str = "en-US"
    voice: str = "en-US-AriaNeural"

    title: str = ""           # video title (intro)
    subtitle: str = ""
    intro: str = ""           # intro narration
    outro: str = ""           # outro narration
    summary: str = ""
    intro_card: CardStyle = Field(default_factory=CardStyle)   # intro background (custom image / a slide)
    outro_card: CardStyle = Field(default_factory=CardStyle)

    source: str = "capture"   # capture = browser recording | slides = PPT/PDF import
    steps: List[Step] = Field(default_factory=list)
    settings: Dict[str, Any] = Field(default_factory=dict)  # overrides of the global settings
    recording: bool = False
    output: str = ""          # file name of the most recently rendered video
    translations: Dict[str, Any] = Field(default_factory=dict)
    speakers: List[Speaker] = Field(default_factory=list)   # the two speakers in Q&A mode (used when settings["dialogue"] is on)
    subtitle_tracks: List[SubtitleTrack] = Field(default_factory=list)   # second-language subtitles (external)

    def is_dialogue(self) -> bool:
        return bool((self.settings or {}).get("dialogue"))


# ---- request bodies ------------------------------------------------------

class StartCaptureReq(BaseModel):
    name: str = ""
    language: str = ""


class CaptureStepReq(BaseModel):
    project_id: str
    kind: str = "click"
    url: str = ""
    page_title: str = ""
    target: Optional[Target] = None
    point: Optional[Dict[str, float]] = None
    value: str = ""
    viewport_w: int = 0
    viewport_h: int = 0
    device_pixel_ratio: float = 1.0
    screenshot_b64: str = ""   # data:image/png;base64,...
    redactions: List[Redaction] = Field(default_factory=list)
    text_nodes: List[TextNode] = Field(default_factory=list)
    client_ts: float = 0       # when the event happened in the browser (ms); used to align with the narrate-while-recording audio


class SlidesCreateReq(BaseModel):
    name: str = ""
    selected: List[int] = Field(default_factory=list)   # 1-based slide numbers; empty = all non-hidden slides
    notes_mode: str = "verbatim"    # verbatim = notes as narration | reference = AI rewrites the notes | ignore = don't use notes
    missing: str = "ai"             # in verbatim mode, slides without notes: ai = AI writes them | empty = leave empty
    detail: str = "standard"        # brief | standard | comprehensive
    language: str = ""
    voice: str = ""
    auto_voice: bool = True         # generate the voice-over right after the narration
    videos: bool = True             # videos in slides: insert a "video" step after their slide
    reveal: bool = True             # reveal points one by one + highlight the current one (needs PowerPoint)
    dialogue: bool = False          # two-person Q&A: the host asks, the expert explains
    host_voice: str = ""
    expert_voice: str = ""


class RedactScanReq(BaseModel):
    keywords: List[str] = Field(default_factory=list)
    builtin: bool = True          # e-mail / phone / ID number / bank card / IP
    names: bool = True            # labelled names such as "Name: John Smith"
    names_guess: bool = False     # bare short Chinese names (false positives, off by default)
    mode: str = "blur"
    steps: List[str] = Field(default_factory=list)   # empty = all steps


class RedactionsReq(BaseModel):
    redactions: List[Redaction] = Field(default_factory=list)


class ScriptReq(BaseModel):
    style: str = "friendly"     # friendly | concise | formal
    audience: str = ""
    extra: str = ""
    overwrite: bool = False     # overwrite narration that was edited by hand
    # The following only apply to slide projects; empty = keep the choice made at import
    notes_mode: str = ""
    missing: str = ""
    detail: str = ""


class TranslateReq(BaseModel):
    target_language: str = "en-US"
    voice: str = ""
    apply: bool = True          # write the result into the steps (otherwise only store it in translations)


class TTSReq(BaseModel):
    voice: str = ""
    rate: str = ""
    volume: str = ""
    only_missing: bool = True


class RenderReq(BaseModel):
    burn_subtitles: Optional[bool] = None
    intro_enabled: Optional[bool] = None
    outro_enabled: Optional[bool] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
