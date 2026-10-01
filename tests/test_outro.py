"""The outro switch: like the intro it can be turned off; then it isn't in the video, isn't voiced and isn't added back by the AI."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "outro_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, main, storage  # noqa: E402
from backend.services import ffmpeg_util, script_gen, tts, video  # noqa: E402
import selftest  # noqa: E402

# global settings go to a temporary file, never your real config.json
config.CONFIG_PATH = DATA / "config.json"
config._cache = None

c = TestClient(main.app, base_url="http://127.0.0.1:8756", headers={"Origin": "http://127.0.0.1:8756"})
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


def wait(j):
    assert "id" in j, j
    while True:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.2)


# offline fake voice-over: a real 1-second silent mp3
SILENT = SP / "outro_silent.mp3"
subprocess.run([ffmpeg_util.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                "anullsrc=r=24000:cl=mono", "-t", "1", "-q:a", "9", str(SILENT)], check=True)
synth_texts = []


def fake_synth(text, voice, out, *a, **k):
    synth_texts.append(text)
    shutil.copy(SILENT, out)
    return 1.0, []


tts.synth = fake_synth


class FakeLLM:
    def __init__(self, *a, **k):
        pass

    def chat_json(self, messages, **k):
        import re
        idx = [int(x) for x in re.findall(r'"i": (\d+)', messages[-1]["content"])]
        return {"title": "AI 标题", "intro": "AI 写的片头。", "outro": "AI 写的片尾。",
                "steps": [{"i": i, "title": f"步骤{i}", "narration": f"AI 写的第 {i} 步。"} for i in idx]}


script_gen.get_client = FakeLLM

cards = []
_real_outro, _real_title = video.render_outro_card, video.render_title_card
video.render_outro_card = lambda *a, **k: (cards.append("outro"), _real_outro(*a, **k))[1]
video.render_title_card = lambda *a, **k: (cards.append("intro"), _real_title(*a, **k))[1]
SMALL = {"width": 640, "height": 360, "fps": 8}


def render(pid, **over):
    cards.clear()
    r = wait(c.post(f"/api/projects/{pid}/render", json={**SMALL, **over}).json())
    assert r["status"] == "done", r
    p = storage.load(pid)
    dur = ffmpeg_util.probe_duration(storage.output_dir(pid) / p.output)
    return set(cards), dur


print("\n== 默认：片头片尾都在 ==")
pid = selftest.build_project("片尾开关")
synth_texts.clear()
r = wait(c.post(f"/api/projects/{pid}/tts", json={}).json())
check("配音任务成功", r["status"] == "done", r.get("error", ""))
check("片尾配了音", "就这么简单，现在轮到你试一试了。" in synth_texts)
got, dur_full = render(pid)
check("视频里有片头和片尾", got == {"intro", "outro"}, got)

print("\n== 关掉片尾（项目设置）==")
p = c.patch(f"/api/projects/{pid}", json={"settings": {"outro_enabled": False}}).json()
check("设置已保存", p["settings"].get("outro_enabled") is False)
check("片尾文案保留着", p["outro"] == "就这么简单，现在轮到你试一试了。")
got, dur_no = render(pid)
check("视频里没有片尾、片头还在", got == {"intro"}, got)
check("视频变短了", dur_no < dur_full - 1.5, f"{dur_full:.1f}s -> {dur_no:.1f}s")
md = c.get(f"/api/projects/{pid}/export/markdown").text
check("图文文档里也没有片尾", "轮到你试一试" not in md)

print("\n== 关掉后再点「生成解说」「一键生成」，片尾不会回来 ==")
c.patch(f"/api/projects/{pid}", json={"outro": "我改过的片尾。"})
r = wait(c.post(f"/api/projects/{pid}/script", json={"overwrite": True}).json())
check("生成解说成功", r["status"] == "done", r.get("error", ""))
p = storage.load(pid)
check("开关仍是关", p.settings.get("outro_enabled") is False)
synth_texts.clear()
r = wait(c.post(f"/api/projects/{pid}/auto", json=SMALL).json())
check("一键生成成功", r["status"] == "done", r.get("error", ""))
check("没有给片尾配音", not any("片尾" in t for t in synth_texts), synth_texts)
cards.clear()
got, _ = render(pid)
check("成片仍然没有片尾", got == {"intro"}, got)

print("\n== 再勾回来 ==")
c.patch(f"/api/projects/{pid}", json={"settings": {"outro_enabled": True}})
synth_texts.clear()
r = wait(c.post(f"/api/projects/{pid}/tts", json={}).json())
check("补上了片尾配音", any("片尾" in t for t in synth_texts), synth_texts)
got, _ = render(pid)
check("片尾回来了", got == {"intro", "outro"}, got)

print("\n== 片头关掉也不再配音（顺带修）==")
c.patch(f"/api/projects/{pid}", json={"intro": "新的片头旁白。", "settings": {"intro_enabled": False}})
synth_texts.clear()
wait(c.post(f"/api/projects/{pid}/tts", json={}).json())
check("关掉的片头没有配音", "新的片头旁白。" not in synth_texts, synth_texts)

print("\n== 全局设置 / 单次渲染参数 ==")
pid2 = selftest.build_project("片尾全局")
c.post("/api/settings", json={"outro_enabled": False})
check("全局设置已保存", c.get("/api/settings").json().get("outro_enabled") is False)
got, _ = render(pid2)
check("全局关掉后新项目没有片尾", "outro" not in got, got)
c.patch(f"/api/projects/{pid2}", json={"settings": {"outro_enabled": True}})
got, _ = render(pid2)
check("项目自己勾上优先于全局", "outro" in got, got)
got, _ = render(pid2, outro_enabled=False)
check("渲染接口参数 outro_enabled=false 生效", "outro" not in got, got)
c.post("/api/settings", json={"outro_enabled": True})

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
