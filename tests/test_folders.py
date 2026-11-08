"""Folders in the project list: moving a project in and out keeps its place in the "last changed" order, names are tidied up, renaming
and dissolving only touch that folder (no project is deleted), and a background job finishing meanwhile keeps the folder. Fake data only."""
import os
import shutil
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "folders_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
os.environ["VT_DISABLE_POWERPOINT"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh"})
from fastapi.testclient import TestClient  # noqa: E402

from backend import main, storage  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


def listed():
    return {p["id"]: p for p in c.get("/api/projects").json()["projects"]}


print("== 1. 移进、移出文件夹 ==")
a = storage.create("周报教程", "zh-CN")
time.sleep(0.02)
b = storage.create("报销教程", "zh-CN")
time.sleep(0.02)
d = storage.create("请假教程", "zh-CN")
before = listed()
check("新项目不在文件夹里", all(p["folder"] == "" for p in before.values()), [p["folder"] for p in before.values()])
r = c.post(f"/api/projects/{a.id}/folder", headers=H, json={"folder": "  财务\n 流程  "})
check("移进文件夹：名字两头的空格、中间的换行和多余空格都整理掉", r.status_code == 200 and r.json()["folder"] == "财务 流程", r.json())
after = listed()
check("列表里带着文件夹名", after[a.id]["folder"] == "财务 流程")
check("只是移动：「最后修改」时间不变，项目不会跳到列表最前面", after[a.id]["updated_at"] == before[a.id]["updated_at"]
      and [p["id"] for p in c.get("/api/projects").json()["projects"]] == [d.id, b.id, a.id])
c.post(f"/api/projects/{b.id}/folder", headers=H, json={"folder": "财务 流程"})
c.post(f"/api/projects/{d.id}/folder", headers=H, json={"folder": "人事"})
r = c.post(f"/api/projects/{d.id}/folder", headers=H, json={"folder": ""})
check("移出文件夹", r.json()["folder"] == "" and listed()[d.id]["folder"] == "")
long = c.post(f"/api/projects/{d.id}/folder", headers=H, json={"folder": "长" * 100}).json()["folder"]
check(f"名字最多 {storage.FOLDER_MAX} 个字", len(long) == storage.FOLDER_MAX, len(long))
c.post(f"/api/projects/{d.id}/folder", headers=H, json={"folder": ""})
check("不存在的项目：404", c.post("/api/projects/p_000000000000/folder", headers=H, json={"folder": "x"}).status_code == 404)

print("\n== 2. 文件夹改名、解散 ==")
r = c.post("/api/folders/rename", headers=H, json={"old": "财务 流程", "new": " 财务 "})
now = listed()
check("改名：里面两个项目都跟着改，别的项目不动", r.json()["moved"] == 2 and now[a.id]["folder"] == now[b.id]["folder"] == "财务"
      and now[d.id]["folder"] == "", {k: v["folder"] for k, v in now.items()})
check("返回保存下来的新名字（编辑器用它记住展开的文件夹）", r.json()["folder"] == "财务", r.json())
check("改名不改「最后修改」时间", now[a.id]["updated_at"] == before[a.id]["updated_at"])
r = c.post("/api/folders/rename", headers=H, json={"old": "财务", "new": ""})
now = listed()
check("解散：项目都还在，回到最外层", r.json()["moved"] == 2 and len(now) == 3 and all(p["folder"] == "" for p in now.values()))
check("名字没变或文件夹不存在：什么也不做", c.post("/api/folders/rename", headers=H, json={"old": "没有这个", "new": "x"}).json()["moved"] == 0)

print("\n== 3. 后台任务（比如渲染）合并结果时不会把文件夹改回去 ==")
snap = storage.load(a.id)                                   # a job takes its copy …
c.post(f"/api/projects/{a.id}/folder", headers=H, json={"folder": "进行中"})     # … the user files the project meanwhile …
done = snap.model_copy(deep=True)
done.output = "video.mp4"
storage.commit(a.id, snap, done, project_fields=("output",))   # … and the job finishes
p = storage.load(a.id)
check("任务的结果合并进来了，文件夹还在", p.output == "video.mp4" and p.folder == "进行中", (p.output, p.folder))
old = storage.load(b.id)
check("旧版本的项目（没有这一项）：不在文件夹里", old.folder == "")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
