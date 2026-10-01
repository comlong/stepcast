"""自检脚本：不依赖 Chrome 扩展，用合成的假网页截图跑通整条流水线。

    python tools/selftest.py            # 合成语音 + 渲染视频
    python tools/selftest.py --no-tts   # 跳过语音（不需要联网）
    python tools/selftest.py --llm      # 额外测试大模型生成解说（用设置里选的那家）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw

from backend import config, storage
from backend.models import Rect, Step, Target
from backend.services import renderer, tts, video
from backend.services.ffmpeg_util import available


def fake_page(title: str, rows: list[str], highlight_row: int = -1,
              size=(1440, 860)) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """画一张假的后台管理页面截图，返回图片和高亮元素的矩形。"""
    W, H = size
    img = Image.new("RGB", (W, H), (247, 248, 251))
    d = ImageDraw.Draw(img)
    f_h1 = renderer.load_font(30, bold=True)
    f = renderer.load_font(19)
    f_s = renderer.load_font(15)

    d.rectangle((0, 0, 232, H), fill=(24, 28, 38))
    d.text((26, 30), "AcmeCloud", font=f_h1, fill=(255, 255, 255))
    for i, item in enumerate(["总览", "项目", "成员", "计费", "设置"]):
        y = 100 + i * 46
        if i == 1:
            d.rounded_rectangle((14, y - 8, 218, y + 30), radius=8, fill=(255, 92, 57))
        d.text((30, y), item, font=f, fill=(255, 255, 255) if i == 1 else (150, 158, 175))

    d.rectangle((232, 0, W, 74), fill=(255, 255, 255))
    d.line((232, 74, W, 74), fill=(228, 232, 240), width=1)
    d.text((268, 24), title, font=f_h1, fill=(20, 24, 34))

    rect = (0, 0, 0, 0)
    for i, row in enumerate(rows):
        y = 120 + i * 78
        d.rounded_rectangle((268, y, W - 40, y + 62), radius=10, fill=(255, 255, 255),
                            outline=(230, 234, 242), width=1)
        d.text((296, y + 12), row, font=f, fill=(28, 33, 46))
        d.text((296, y + 36), "更新于 2 小时前 · 3 位成员", font=f_s, fill=(140, 148, 166))
        bx = (W - 190, y + 15, W - 70, y + 47)
        d.rounded_rectangle(bx, radius=8, fill=(255, 92, 57))
        d.text(((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2), "打开", font=f_s,
               fill=(255, 255, 255), anchor="mm")
        if i == highlight_row:
            rect = (bx[0], bx[1], bx[2] - bx[0], bx[3] - bx[1])
    return img, rect


def build_project(name: str = "自检演示") -> str:
    proj = storage.create(name)
    proj.title = "3 分钟上手 AcmeCloud"
    proj.subtitle = "从登录到创建第一个项目"
    proj.intro = "欢迎使用 AcmeCloud，接下来我带你快速创建第一个项目。"
    proj.outro = "就这么简单，现在轮到你试一试了。"
    storage.save(proj)

    specs = [
        ("项目列表", ["示例项目", "营销官网", "移动端 App"], 0, "click", "打开",
         "首先在项目列表里找到你要进入的项目，点击右侧的打开按钮。"),
        ("新建项目", ["项目名称", "所属团队", "可见范围"], 1, "input", "所属团队",
         "在这里填写项目的基本信息，团队选错了后面不好改，注意确认一下。"),
        ("项目总览", ["构建流水线", "环境变量", "访问日志"], 2, "click", "打开",
         "最后进入访问日志，就能看到刚才这些操作的记录了。"),
    ]
    for i, (page, rows, hi, kind, label, narration) in enumerate(specs):
        img, rect = fake_page(page, rows, hi)
        step = Step(
            kind=kind, url=f"https://app.acmecloud.dev/projects/{i}",
            page_title=page,
            target=Target(tag="button", role="button", text=label,
                          rect=Rect(x=rect[0], y=rect[1], w=rect[2], h=rect[3])),
            point={"x": rect[0] + rect[2] / 2, "y": rect[1] + rect[3] / 2},
            value="平台组" if kind == "input" else "",
            viewport_w=img.size[0], viewport_h=img.size[1],
            img_w=img.size[0], img_h=img.size[1],
            title=page, narration=narration, caption=narration,
        )
        fname = f"{step.id}.png"
        img.save(storage.screenshots_dir(proj.id) / fname)
        step.screenshot = fname
        storage.add_step(proj.id, step)
    return proj.id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args()

    ff = available()
    print(f"ffmpeg: {'✓' if ff['ok'] else '✗'} {ff.get('path', '')}")
    if not ff["ok"] and not args.no_video:
        print("  没有 ffmpeg，无法渲染视频")
        return 1

    print("→ 生成演示项目…")
    pid = build_project()
    proj = storage.load(pid)
    print(f"  项目 {pid}，{len(proj.steps)} 步")

    if args.llm:
        from backend.services import script_gen
        print("→ 调用大模型生成解说…")
        r = script_gen.generate_script(proj, overwrite=True,
                                       progress=lambda f, m: print(f"   {int(f*100):3d}% {m}"))
        storage.save(proj)
        print("  ", r)

    if not args.no_tts:
        print("→ 合成语音（edge-tts，需要联网）…")
        try:
            r = tts.synth_project(proj, progress=lambda f, m: print(f"   {int(f*100):3d}% {m}"))
            storage.save(proj)
            print("  ", {k: v for k, v in r.items() if k != "errors"})
            if r["errors"]:
                print("   失败：", r["errors"][:2])
        except Exception as e:
            print("  语音失败（不影响后面流程）：", e)

    if not args.no_video:
        print("→ 渲染视频…")
        res = video.render_project(proj, progress=lambda f, m: print(f"   {int(f*100):3d}% {m}"))
        storage.save(proj)
        out = storage.output_dir(pid) / str(res["file"])
        print(f"✓ 完成：{out}")
        print(f"  时长 {res['duration']} 秒，{res['size'] / 1e6:.1f} MB，耗时 {res['elapsed']} 秒")
    print(f"\n在编辑器里打开：http://127.0.0.1:{config.get('server_port')}/?p={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
