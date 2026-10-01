"""片头 / 片尾自定义背景：接口 + 渲染。在隔离目录里做，不碰用户项目，也不调用本机 PowerPoint。"""
import io, os, shutil, subprocess, sys, time
from pathlib import Path
SP = Path(sys.argv[1]); DATA = SP / "cards_projects"
PROBE, FF = shutil.which("ffprobe"), shutil.which("ffmpeg")
shutil.rmtree(DATA, ignore_errors=True); DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA); os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
config.CONFIG_PATH = DATA / "config.json"; config._cache = None
from fixtures_build import make_capture_project  # 假的网页录制项目，不用真实项目
PID = make_capture_project(DATA)
config.save({"ui_language": "zh"})
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from backend import main, storage
from backend.services import slides as S
S.powerpoint_available = lambda: False
S._soffice = lambda: None                      # 模拟同事电脑：没有 PowerPoint 也没有 LibreOffice
c = TestClient(main.app, base_url="http://127.0.0.1:8756"); H = {"Origin": "http://127.0.0.1:8756"}
fails = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else "")); cond or fails.append(name)
def wait(j):
    while True:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"): return r
        time.sleep(0.2)

# 老项目（没有 intro_card 字段）也能读
p = c.get(f"/api/projects/{PID}").json()
check("老项目读出默认的 intro_card", p["intro_card"]["image"] == "" and p["outro_card"]["fit"] == "contain")

# 1) 片头：上传一张亮色照片（4:3，测试「完整显示」时两边用模糊图补齐 + 叠字暗带）
img = Image.new("RGB", (1600, 1200), (250, 240, 200)); d = ImageDraw.Draw(img)
for i in range(12):
    d.ellipse((i * 130, 200 + (i % 3) * 200, i * 130 + 300, 500 + (i % 3) * 200), fill=(255, 120 + i * 10, 60))
d.rectangle((0, 1000, 1600, 1200), fill=(30, 90, 200))
buf = io.BytesIO(); img.save(buf, "JPEG")
r = wait(c.post(f"/api/projects/{PID}/card/intro/background", files={"file": ("封面照片.jpg", buf.getvalue())}, headers=H).json())
ic = r.get("result") or {}
check("上传图片作片头背景", r["status"] == "done" and ic.get("image", "").endswith("image.png"), r.get("error") or ic)
check("图片默认叠加标题文字", ic.get("show_text") is True and ic.get("pages") == 0 and ic.get("source") == "封面照片.jpg")
c.patch(f"/api/projects/{PID}", json={"title": "在示例后台新建机构", "subtitle": "3 分钟上手"}, headers=H)
prev = c.get(f"/api/projects/{PID}/card/intro/preview?t=1.4&scale=0.5")
check("片头预览图", prev.status_code == 200 and prev.headers["content-type"] == "image/jpeg")
(SP / "card_intro_prev.jpg").write_bytes(prev.content)

# 2) 片尾：上传 3 页的 PDF，换到第 2 页
with open(SP / "pptspike" / "export_slides.pdf", "rb") as f:
    r = wait(c.post(f"/api/projects/{PID}/card/outro/background", files={"file": ("结尾页.pdf", f)}, headers=H).json())
oc = r.get("result") or {}
check("上传 PDF 作片尾背景", r["status"] == "done" and oc.get("pages") == 3 and oc.get("image", "").endswith("slide_001.png"), r.get("error") or oc)
check("PDF 默认不叠字（页面上一般已经有字）", oc.get("show_text") is False)
p = c.patch(f"/api/projects/{PID}/card/outro", json={"page": 2}, headers=H).json()
check("换到第 2 页", p["outro_card"]["page"] == 2 and p["outro_card"]["image"].endswith("slide_002.png"))
bad = c.patch(f"/api/projects/{PID}/card/outro", json={"page": 9}, headers=H)
check("页码超出范围报错", bad.status_code == 400 and "共 3 页" in bad.json()["detail"], bad.json().get("detail"))
bad = c.patch(f"/api/projects/{PID}/card/outro", json={"page": "abc"}, headers=H)
check("页码不是数字报错（不是 500）", bad.status_code == 400, bad.status_code)
c.patch(f"/api/projects/{PID}", json={"outro": ""}, headers=H)          # 片尾没有文案：只显示背景页
(SP / "card_outro_prev.jpg").write_bytes(c.get(f"/api/projects/{PID}/card/outro/preview?t=1.4&scale=0.5").content)

# 3) 没有 PowerPoint / LibreOffice 时上传 PPTX：给出能看懂的提示
with open(SP / "notes_deck.pptx", "rb") as f:
    r = wait(c.post(f"/api/projects/{PID}/card/intro/background", files={"file": ("封面.pptx", f)}, headers=H).json())
check("没有 PowerPoint 时 PPT 报清楚的错", r["status"] == "error" and "PowerPoint" in r["error"] and "PDF" in r["error"], r.get("error"))
p = c.get(f"/api/projects/{PID}").json()
check("失败时原来的片头背景不受影响", p["intro_card"]["image"] == ic["image"])
bad = c.post(f"/api/projects/{PID}/card/intro/background", files={"file": ("a.txt", b"x")}, headers=H)
check("不支持的文件类型直接 400", bad.status_code == 400)

# 4) 停留时间 + 渲染
c.patch(f"/api/projects/{PID}/card/intro", json={"duration": 5}, headers=H)
r = wait(c.post(f"/api/projects/{PID}/render", json={"width": 1280, "height": 720, "fps": 15}, headers=H).json())
check("渲染成功", r["status"] == "done", r.get("error"))
p = c.get(f"/api/projects/{PID}").json()
mp4 = DATA / PID / "output" / p["output"]
dur = float(subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                            str(mp4)], capture_output=True, text=True).stdout)
steps = sum(s["duration"] for s in p["steps"] if s["include"])
ia = DATA / PID / "audio" / "__intro__.mp3"
ad = float(subprocess.run([PROBE, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                           str(ia)], capture_output=True, text=True).stdout or 0) if ia.exists() else 0
intro = max(5, ad + 0.3) if ad else 5          # 设了 5 秒，但不能比片头配音短
check("片头按设的停留时间（不短于配音）、片尾（只有背景、没文案）也在视频里",
      abs(dur - (steps + intro + 2.6)) < 0.8,
      f"总长 {dur:.1f}s = 步骤 {steps:.1f}s + 片头 {intro:.1f}s（配音 {ad:.1f}s）+ 片尾 2.6s")
# 没有配音时，停留时间就是设的值
c.patch(f"/api/projects/{PID}", json={"intro": ""}, headers=H)
from backend.services import video as V
proj = storage.load(PID)
plan_intro = V._card_duration(proj.intro_card, 0.0, 3.0)
check("没有配音时片头停留时间 = 设的 5 秒", plan_intro == 5.0, plan_intro)
for name, ts in (("intro", 2.5), ("outro", dur - 1.2)):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{ts:.2f}", "-i", str(mp4),
                    "-frames:v", "1", str(SP / f"card_{name}_video.png")], check=True)

# 5) 恢复默认：删掉文件夹，停留时间保留
folder = DATA / PID / "cards" / ic["image"].split("/")[0]
p = c.delete(f"/api/projects/{PID}/card/intro/background", headers=H).json()
check("恢复默认背景", p["intro_card"]["image"] == "" and not folder.exists())
check("恢复默认后停留时间保留", p["intro_card"]["duration"] == 5)
# 背景文件被手动删掉：回到默认背景，不报错
shutil.rmtree(DATA / PID / "cards", ignore_errors=True)
prev = c.get(f"/api/projects/{PID}/card/outro/preview?t=1.4&scale=0.5")
check("背景文件丢了也能出预览（回到默认背景）", prev.status_code == 200)
print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
