"""数据结构定义（录制步骤 / 项目）。"""
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
    """被操作元素的描述信息（来自 Chrome 扩展）。"""
    tag: str = ""
    role: str = ""
    text: str = ""
    name: str = ""            # aria-label / title / placeholder
    selector: str = ""
    input_type: str = ""
    rect: Optional[Rect] = None


class Redaction(BaseModel):
    """一块打码区域。坐标与 Target.rect 同一套：CSS 像素、视口坐标系。"""
    id: str = Field(default_factory=lambda: new_id("r_"))
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    mode: str = "blur"        # blur（马赛克+模糊）| pixelate（马赛克）| solid（纯色块）
    kind: str = "manual"      # manual | email | phone | id_card | bank | ip | name | keyword
    label: str = ""           # 命中的原文，只留在本机，方便你核对
    auto: bool = False        # 是否自动识别出来的


class TextNode(BaseModel):
    """页面上一段可见文字及其位置，用于事后按关键词补打码。"""
    t: str = ""
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0
    n: bool = False           # 录制时判定它落在「姓名/联系人」这类列里


class VideoClip(BaseModel):
    """「视频」步骤播放的视频：PPT 里嵌入的视频，或者自己插入的视频文件。"""
    file: str = ""            # 项目 media/ 目录下的视频文件；空 = 还没有（链接 / 在线视频要手动上传）
    source: str = ""          # 原文件名或链接地址（显示用）
    duration: float = 0.0     # 整段长度（秒）
    has_audio: bool = True    # 视频里有没有声音
    start: float = 0.0        # 截取起点（秒）
    end: float = 0.0          # 截取终点（秒）；0 = 到结尾
    mode: str = "inset"       # inset 在幻灯片原位置播放 | fullscreen 全屏 | poster 只显示封面
    rect: Optional[Rect] = None   # 在幻灯片上的位置，相对页面宽高的比例（0~1）；没有就只能全屏
    audio: str = "original"   # original 播视频原声 | mute 静音、改用这一步的解说配音
    missing: str = ""         # linked 链接到作者电脑上的文件 | online 在线视频 —— 都要手动上传
    # 「视频里的讲话转成字幕」的结果：识别出的原文，和每个词在视频里的时间（从视频开头算，
    # 所以之后再改截取起止点，字幕照样对得上）。和解说配音的 Step.boundaries 分开存，互不覆盖
    transcript: str = ""
    words: List[Dict[str, Any]] = Field(default_factory=list)


class RevealItem(BaseModel):
    """幻灯片上的一条内容（逐条出现用）：只含这一条像素的透明小图，和它在整页图上的位置。"""
    file: str = ""            # screenshots/ 下的 PNG（带透明度，已裁到最小范围）
    x: int = 0                # 在整页图上的位置（像素，整页图就是这一步的截图）
    y: int = 0
    text: str = ""            # 这一条的文字，用来找解说说到它的时间


class SlideReveal(BaseModel):
    """幻灯片「逐条出现 + 突出当前」的数据，导入时用 PowerPoint 生成。"""
    clean: str = ""           # 条目全部隐藏的整页图（screenshots/ 下）
    items: List[RevealItem] = Field(default_factory=list)   # 按阅读顺序
    enabled: bool = True      # 这一页用不用（项目设置里的总开关打开时才生效）
    # AI 对齐的结果：每一条在解说里开始讲的位置（字符偏移，-1 = 解说没讲到）。
    # align_key 是对齐时「解说 + 条目文字」的指纹，解说改了就对不上，渲染前会重新对齐
    align: List[int] = Field(default_factory=list)
    align_key: str = ""


class DialogueLine(BaseModel):
    """双人问答模式的一句台词。"""
    who: str = "host"         # host 主持人（提问、串场）| expert 讲师（讲解）
    text: str = ""


class Speaker(BaseModel):
    """双人问答模式的一位讲者。"""
    role: str = "host"        # host | expert
    name: str = ""            # 编辑器里区分是谁说的（不念出来，也不上字幕）
    voice: str = ""           # 配音音色


def join_lines(lines: List["DialogueLine"]) -> str:
    """台词拼成整段解说（一句一行）：字幕、导出、对齐都用它。"""
    return "\n".join(ln.text.strip() for ln in lines if (ln.text or "").strip())


def drop_stale_lines(step: "Step") -> None:
    """解说被整段改掉（录音转写替换了文字、导入脚本、直接改解说……）而不是改台词时，旧台词就对不上了：
    清掉台词，之后这一步按整段解说由主持人念。不清的话配音念的是旧台词，字幕却是新解说。"""
    if step.lines and join_lines(step.lines) != (step.narration or "").strip():
        step.lines = []


class Step(BaseModel):
    id: str = Field(default_factory=lambda: new_id("s_"))
    index: int = 0
    kind: str = "click"       # click | input | key | navigate | scroll | manual | slide | video
    url: str = ""
    page_title: str = ""
    ts: float = Field(default_factory=time.time)

    screenshot: str = ""      # 相对 screenshots/ 的文件名
    img_w: int = 0
    img_h: int = 0
    viewport_w: int = 0
    viewport_h: int = 0

    target: Optional[Target] = None
    point: Optional[Dict[str, float]] = None   # 点击位置（CSS 像素，视口坐标）
    value: str = ""           # 输入内容 / 按键名

    # --- 幻灯片（kind == "slide"）---
    slide_text: str = ""      # 这一页上的正文
    slide_notes: str = ""     # 演讲者备注

    reveal: Optional[SlideReveal] = None      # 逐条出现（没有 = 整页一起出现）

    # --- 视频（kind == "video"）---
    clip: Optional[VideoClip] = None

    # --- 隐私 ---
    redactions: List[Redaction] = Field(default_factory=list)
    text_nodes: List[TextNode] = Field(default_factory=list)  # 仅本机，可一键清空

    # --- 脚本层（可由大模型生成，也可人工编辑） ---
    title: str = ""           # 画面上方的小标题
    narration: str = ""       # 解说词（转语音）
    caption: str = ""         # 字幕（默认等于 narration）
    note: str = ""            # 用户自己的备注，供 LLM 参考
    lines: List[DialogueLine] = Field(default_factory=list)   # 双人问答的台词；narration 是它拼起来的全文

    # --- 生成物 ---
    audio: str = ""           # 相对 audio/ 的文件名
    voice_source: str = "tts"  # tts = AI 配音 | own = 自己的录音（改文案不会作废它）
    audio_duration: float = 0.0
    boundaries: List[Dict[str, Any]] = Field(default_factory=list)  # 词边界（字幕对齐）
    line_times: List[List[float]] = Field(default_factory=list)     # 每句台词在配音里的 [开始, 结束]（秒）
    duration: float = 0.0     # 实际片段时长（渲染时计算/可手动覆盖）
    duration_override: float = 0.0

    include: bool = True
    zoom: bool = True
    highlight: bool = True

    def plays_video(self) -> bool:
        """视频步骤、有视频文件、不是「只显示封面」：渲染时真的播放视频。"""
        c = self.clip
        return self.kind == "video" and c is not None and bool(c.file) and c.mode != "poster"

    def plays_clip_audio(self) -> bool:
        """播的是视频原声：这一步的解说不配音、AI 也不写解说（字幕照样可以写）。"""
        return self.plays_video() and self.clip.audio == "original" and self.clip.has_audio

    def subtitle_text(self) -> str:
        """画面上显示的字幕。播视频原声时只用「字幕」这一栏（解说不念）；
        视频改成静音配解说后，如果字幕还是当初从原声转出来的那段，就换成解说词，免得字幕和配音对不上。"""
        if self.plays_clip_audio():
            return self.caption
        c = self.clip
        if self.kind == "video" and c is not None and c.transcript and self.caption == c.transcript:
            return self.narration
        return self.caption or self.narration

    def caption_follows_narration(self) -> bool:
        """改解说时字幕是否跟着改。播视频原声的步骤，字幕是视频里的讲话，不能被解说覆盖。"""
        return not self.plays_clip_audio()


class SubtitleTrack(BaseModel):
    """一种第二语言字幕（外挂字幕文件，放在成片旁边）。"""
    lang: str = ""            # 语言代码，比如 en-US
    name: str = ""            # 显示名，比如 English (US)
    video: str = ""           # 给哪个成片生成的（成片改名 / 重新渲染后就过期了）
    key: str = ""             # 生成时主字幕时间轴的指纹
    vtt: str = ""             # output/ 下的文件名
    srt: str = ""
    created_at: float = 0.0


class CardStyle(BaseModel):
    """片头 / 片尾的自定义背景：一张图片，或 PPT / PDF 里的某一页。"""
    image: str = ""           # 项目 cards/ 目录下渲染好的背景图（相对路径）；空 = 默认的渐变背景
    source: str = ""          # 上传的原文件名，只用来显示
    page: int = 1             # PPT / PDF 用第几页
    pages: int = 0            # 原文件一共几页；上传的是图片时为 0
    show_text: bool = True    # 在背景上叠加标题（片头）/ 片尾文字
    fit: str = "contain"      # contain = 完整显示，边缘用模糊背景补齐 | cover = 铺满画面，多出的裁掉
    duration: float = 0.0     # 停留秒数；0 = 按配音长度自动


class Project(BaseModel):
    id: str = Field(default_factory=lambda: new_id("p_"))
    name: str = Field(default_factory=lambda: i18n.t("未命名教程"))
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    language: str = "en-US"
    voice: str = "en-US-AriaNeural"

    title: str = ""           # 视频标题（片头）
    subtitle: str = ""
    intro: str = ""           # 片头解说
    outro: str = ""           # 片尾解说
    summary: str = ""
    intro_card: CardStyle = Field(default_factory=CardStyle)   # 片头背景（自定义图片 / PPT 某一页）
    outro_card: CardStyle = Field(default_factory=CardStyle)

    source: str = "capture"   # capture = 浏览器录制 | slides = PPT/PDF 导入
    steps: List[Step] = Field(default_factory=list)
    settings: Dict[str, Any] = Field(default_factory=dict)  # 覆盖全局配置
    recording: bool = False
    output: str = ""          # 最近一次导出的视频文件名
    translations: Dict[str, Any] = Field(default_factory=dict)
    speakers: List[Speaker] = Field(default_factory=list)   # 双人问答的两位讲者（settings["dialogue"] 打开时用）
    subtitle_tracks: List[SubtitleTrack] = Field(default_factory=list)   # 第二语言字幕（外挂）

    def is_dialogue(self) -> bool:
        return bool((self.settings or {}).get("dialogue"))


# ---- 请求体 --------------------------------------------------------------

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
    client_ts: float = 0       # 事件在浏览器里发生的时刻（毫秒），用于和边录边讲的音频对齐


class SlidesCreateReq(BaseModel):
    name: str = ""
    selected: List[int] = Field(default_factory=list)   # 1 开始的页码；空 = 全部未隐藏页
    notes_mode: str = "verbatim"    # verbatim 备注原文作解说 | reference AI 参考备注改写 | ignore 不用备注
    missing: str = "ai"             # 备注原文模式下，没有备注的页：ai AI 补写 | empty 留空
    detail: str = "standard"        # brief | standard | comprehensive
    language: str = ""
    voice: str = ""
    auto_voice: bool = True         # 生成完解说后顺手合成语音
    videos: bool = True             # 幻灯片里的视频：在那一页后面插入「视频」步骤
    reveal: bool = True             # 逐条出现 + 突出当前讲的内容（需要本机 PowerPoint）
    dialogue: bool = False          # 双人问答：主持人提问、讲师讲解
    host_voice: str = ""
    expert_voice: str = ""


class RedactScanReq(BaseModel):
    keywords: List[str] = Field(default_factory=list)
    builtin: bool = True          # 邮箱 / 手机号 / 身份证 / 银行卡 / IP
    names: bool = True            # 「姓名：张三」这类带标签的人名
    names_guess: bool = False     # 裸的中文短名（会误报，默认关）
    mode: str = "blur"
    steps: List[str] = Field(default_factory=list)   # 留空 = 全部步骤


class RedactionsReq(BaseModel):
    redactions: List[Redaction] = Field(default_factory=list)


class ScriptReq(BaseModel):
    style: str = "friendly"     # friendly | concise | formal
    audience: str = ""
    extra: str = ""
    overwrite: bool = False     # 是否覆盖已手动编辑的解说
    # 以下只对 PPT 项目有效；留空 = 沿用导入时的选择
    notes_mode: str = ""
    missing: str = ""
    detail: str = ""


class TranslateReq(BaseModel):
    target_language: str = "en-US"
    voice: str = ""
    apply: bool = True          # 直接写回步骤（否则只存到 translations）


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
