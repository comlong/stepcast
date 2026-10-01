"""逐条出现（用本机 PowerPoint）：导入 → 每页的条目 → 配音 → 渲染。用的都是复制到工作目录的 PPT。

会启动 PowerPoint，所以只在 PowerPoint 没开着时跑（run_all.py --ppt 会先检查），用完只关自己开的。"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "reveal_ppt_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ.pop("VT_DISABLE_POWERPOINT", None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh", "language": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural"})
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402
from backend.services import llm, slides  # noqa: E402


def _no_ai(*a, **k):                 # 测试不花 AI 的钱：渲染前的对齐直接跳过
    raise llm.LLMError("test: no AI")


llm.get_client = _no_ai

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def wait(j, timeout=1200):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.3)
    raise TimeoutError


def pp_running(wait=15):
    """PowerPoint 退出要几秒：最多等 wait 秒。"""
    for _ in range(wait * 2):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"], capture_output=True, text=True).stdout
        if "POWERPNT" not in out.upper():
            return False
        time.sleep(0.5)
    return True


check("本机 PowerPoint 可用", slides.powerpoint_available())
was_running = pp_running(wait=0)


def import_deck(path, name, reveal=True, selected=None):
    with open(path, "rb") as f:
        man = wait(c.post("/api/import/slides", files={"file": (name, f)}, headers=H).json())["result"]
    body = {"notes_mode": "ignore", "missing": "empty", "language": "zh-CN", "auto_voice": False, "reveal": reveal}
    if selected:
        body["selected"] = selected
    t0 = time.time()
    r = wait(c.post(f"/api/import/slides/{man['id']}/create", json=body, headers=H).json())
    return man, r, time.time() - t0


# 内部真实 PPT 的几节（tests/private/，不提交）：有就一起测，没有就跳过
PRIVATE = Path(__file__).parent / "private" / "reveal_ppt_deck.py"
if PRIVATE.exists() and (SP / "fx" / "deck.pptx").exists():
    exec(compile(PRIVATE.read_text(encoding="utf-8"), str(PRIVATE), "exec"))
else:
    print("\n== 1~3. 卡片式 PPT：tests/private/ 里没有，跳过 ==")

print("\n== 4. 要点式 PPT：一个文本框里的几条要点 ==")
man, r, secs = import_deck(SP / "fx" / "bullets.pptx", "bullets.pptx")
pid2 = r["result"]["project_id"]
p2 = c.get(f"/api/projects/{pid2}").json()
b1 = p2["steps"][0]
items = (b1.get("reveal") or {}).get("items") or []
check("第 1 页按要点拆成 5 条（下级要点跟着上一级）", len(items) == 5 and "Choose the cost center" in items[1]["text"],
      [it["text"][:20] for it in items])

print("\n== 5. 不勾选逐条出现 ==")
man, r, secs = import_deck(SP / "fx" / "bullets.pptx", "bullets.pptx", reveal=False)
p3 = c.get(f"/api/projects/{r['result']['project_id']}").json()
check("不生成逐条出现的数据，项目设置关闭", not any(s.get("reveal") for s in p3["steps"])
      and not p3["settings"].get("slides_reveal"))
check("最后 PowerPoint 没有留下（原来没开着的话）", was_running or not pp_running())

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
