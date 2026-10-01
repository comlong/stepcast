"""测试用的假素材：一个「网页录制」项目，页面是用 PIL 画的假后台，配音是 ffmpeg 生成的音调。

不含任何真实网站、真实截图或公司资料，可以放心提交。调用前要先设好 VT_DATA_DIR（和 config），
项目直接写在 data_dir 下，不经过 storage，所以导入顺序无所谓。
"""
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PID = "p_c0ffee000001"
IMG_W, IMG_H = 1920, 1080          # 截图像素
VIEW_W, VIEW_H = 1536, 864         # 视口（CSS 像素），相当于 125% 缩放的屏幕
DPR = IMG_W / VIEW_W

MENU = ["平台管理", "学习资源", "培训", "培训申请", "运营", "综合查询"]
SUB = ["机构管理", "用户管理", "角色权限"]

# (动作, 目标元素矩形（CSS 像素）, 点击点, 输入值, 解说) —— 8 步，每步配音 6~7 秒
STEPS = [
    ("click", (0, 64, 220, 40), (76, 84), "", "首先打开后台管理页面，在左侧菜单里点击「平台管理」。"),
    ("click", (60, 108, 160, 40), (104, 128), "", "展开以后，在下面找到「机构管理」，点击进入。"),
    ("click", (260, 150, 180, 36), (330, 168), "", "这里列出了所有机构，我们先点开第一行的详情看看。"),
    ("click", (1330, 96, 90, 32), (1375, 112), "", "看完以后，点击右上角的「新增」按钮，开始创建一个新的机构。"),
    ("click", (560, 250, 700, 34), (760, 267), "", "在弹出的表单里，先点一下「机构编码」这一栏。"),
    ("input", (560, 250, 700, 34), (760, 267), "ORG-2031", "输入机构编码，编码在整个平台里不能重复。"),
    ("click", (560, 330, 700, 34), (760, 347), "", "接着点击「机构名称」这一栏，准备填写名称。"),
    ("click", (1050, 620, 120, 36), (1110, 638), "", "最后检查一遍，确认无误后点击「保存」，新机构就建好了。"),
]
INTRO = "这段演示带你在示例后台里新建一个机构，只需要几步就能完成。"
OUTRO = "好了，新的机构已经创建完成，感谢观看。"
VOICE = "zh-CN-XiaoxiaoNeural"


def _font(size):
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _page(step_no: int) -> Image.Image:
    """画一张假后台页面（在 CSS 像素坐标上画，最后放大到截图像素）。"""
    im = Image.new("RGB", (VIEW_W, VIEW_H), (245, 247, 250))
    d = ImageDraw.Draw(im)
    f, fb, fs = _font(15), _font(20), _font(13)
    d.rectangle((0, 0, VIEW_W, 56), fill=(24, 44, 84))
    d.text((24, 16), "示例后台管理", font=fb, fill="white")
    for i, t in enumerate(("待办", "admin", "简体中文")):
        d.text((VIEW_W - 260 + i * 80, 20), t, font=f, fill=(210, 220, 240))
    d.rectangle((0, 56, 220, VIEW_H), fill=(255, 255, 255))
    y = 64
    for i, t in enumerate(MENU):
        if i == 0 and step_no >= 1:
            d.rectangle((0, y, 220, y + 40), fill=(230, 240, 255))
        d.text((40, y + 10), t, font=f, fill=(40, 40, 40))
        y += 44
        if i == 0 and step_no >= 1:
            for j, s in enumerate(SUB):
                if j == 0 and step_no >= 2:
                    d.rectangle((60, y, 220, y + 40), fill=(210, 228, 255))
                d.text((76, y + 10), s, font=f, fill=(60, 60, 60))
                y += 44
    title = "首页" if step_no < 2 else "机构管理"
    d.text((260, 80), title, font=fb, fill=(30, 30, 30))
    if step_no < 2:
        for i, (k, v) in enumerate((("机构总数", "792"), ("用户总数", "3568"), ("讲师", "27"))):
            x = 260 + i * 400
            d.rounded_rectangle((x, 140, x + 360, 280), 10, fill="white", outline=(225, 228, 235))
            d.text((x + 20, 160), k, font=f, fill=(120, 120, 120))
            d.text((x + 20, 200), v, font=_font(40), fill=(24, 44, 84))
    else:
        d.rounded_rectangle((1330, 96, 1420, 128), 6, fill=(22, 119, 255))
        d.text((1356, 102), "新增", font=f, fill="white")
        for r in range(8):
            yy = 150 + r * 44
            d.rectangle((260, yy, 1500, yy + 36), fill="white" if r % 2 else (250, 251, 253))
            d.text((300, yy + 9), f"示例机构 {r + 1:02d}", font=f, fill=(50, 50, 50))
            d.text((700, yy + 9), f"ORG-{2000 + r}", font=f, fill=(90, 90, 90))
            d.text((1000, yy + 9), "启用", font=f, fill=(0, 150, 80))
    if step_no >= 4:                                    # 新增表单（弹窗）
        d.rounded_rectangle((460, 180, 1300, 700), 12, fill="white", outline=(200, 205, 215), width=2)
        d.text((490, 200), "新增机构", font=fb, fill=(30, 30, 30))
        for k, (label, yy) in enumerate((("机构编码", 250), ("机构名称", 330), ("所属区域", 410), ("备注", 490))):
            d.text((490, yy - 22), label, font=fs, fill=(100, 100, 100))
            d.rounded_rectangle((560, yy, 1260, yy + 34), 4, fill="white", outline=(190, 195, 205))
            if k == 0 and step_no >= 6:
                d.text((572, yy + 8), "ORG-2031", font=f, fill=(20, 20, 20))
        d.rounded_rectangle((1050, 620, 1170, 656), 6, fill=(22, 119, 255))
        d.text((1090, 628), "保存", font=f, fill="white")
    return im.resize((IMG_W, IMG_H), Image.LANCZOS)


def _tone(path: Path, seconds: float, freq: int) -> None:
    ff = shutil.which("ffmpeg")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds:.3f}:sample_rate=24000",
                    "-af", "volume=0.2", "-c:a", "libmp3lame", "-b:a", "48k", str(path)], check=True)


def _duration(path: Path) -> float:
    out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout
    return round(float(out.strip() or 0), 3)


def make_capture_project(data_dir: Path, pid: str = PID) -> str:
    """在 data_dir 下造一个 8 步的网页录制项目（截图 + 配音都齐），返回项目 id。视频总长约 70 秒。"""
    from backend.models import Project, Rect, Step, Target

    root = Path(data_dir) / pid
    shutil.rmtree(root, ignore_errors=True)
    shots, audio = root / "screenshots", root / "audio"
    shots.mkdir(parents=True)
    audio.mkdir()
    steps = []
    for i, (kind, (x, y, w, h), (px, py), value, text) in enumerate(STEPS):
        sid = f"s_{i:012x}"
        _page(i).save(shots / f"{sid}.png")
        mp3 = audio / f"{sid}.mp3"
        _tone(mp3, 6.0 + (i % 3) * 0.5, 330 + i * 40)
        dur = _duration(mp3)
        steps.append(Step(
            id=sid, index=i, kind=kind, url="https://example.test/admin/", page_title="示例后台管理", ts=1.7e9 + i * 5,
            screenshot=f"{sid}.png", img_w=IMG_W, img_h=IMG_H, viewport_w=VIEW_W, viewport_h=VIEW_H,
            target=Target(tag="input" if kind == "input" else "li", text=text[:6],
                          input_type="text" if kind == "input" else "", rect=Rect(x=x, y=y, w=w, h=h)),
            point={"x": float(px), "y": float(py)}, value=value,
            title=f"第 {i + 1} 步", narration=text, caption=text,
            audio=mp3.name, voice_source="tts", audio_duration=dur,
            boundaries=[{"t": 0.1, "d": round(dur - 0.2, 3), "text": text}]))
    for kind, text, secs in (("intro", INTRO, 6.5), ("outro", OUTRO, 4.5)):
        _tone(audio / f"__{kind}__.mp3", secs, 262)
        (audio / f"__{kind}__.txt").write_text(json.dumps({"text": text, "voice": VOICE}, ensure_ascii=False),
                                              encoding="utf-8")
    proj = Project(id=pid, name="测试：网页录制", language="zh-CN", voice=VOICE,
                   title="在示例后台新建机构", subtitle="几步完成机构创建", intro=INTRO, outro=OUTRO,
                   summary="打开平台管理，进入机构管理，点击新增，填写编码和名称后保存。",
                   source="capture", steps=steps)
    (root / "project.json").write_text(proj.model_dump_json(indent=1), encoding="utf-8")
    return pid


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "_work" / "fixture_preview")
    out.mkdir(parents=True, exist_ok=True)
    print(make_capture_project(out), "->", out)
