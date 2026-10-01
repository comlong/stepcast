"""界面语言：自动选择、老用户、切换、后端提示、内容语言、页面无残留中文、扩展词典。"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "i18n_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, i18n, main, storage  # noqa: E402
from backend.services import tts  # noqa: E402
import selftest  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
tts.synth = lambda text, voice, out, *a, **k: (out.write_bytes(b"\0" * 2048), (1.0, []))[1]

c = TestClient(main.app, base_url="http://127.0.0.1:8756", headers={"Origin": "http://127.0.0.1:8756"})
fails = []
HAN = re.compile(r"[\u4e00-\u9fff]")


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


def reset(file_content=None):
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    if file_content is not None:
        config.CONFIG_PATH.write_text(json.dumps(file_content), encoding="utf-8")
    config._cache = None


def wait(j):
    while True:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.1)


def visible_han(html):
    body = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", "", html, flags=re.S)
    body = re.sub(r"<[^>]*translate=\"no\"[^>]*>[^<]*<", "<", body)
    return sorted(set(m.group(0) for m in re.finditer(r"[^<>\"]*[\u4e00-\u9fff][^<>\"]*", body)))


print("\n== 1. 全新安装：一律英语（不按浏览器语言猜） ==")
reset()
r = c.get("/", headers={"accept-language": "pl-PL,pl;q=0.9,en;q=0.8"})
check("波兰语浏览器也是英文界面", '<html lang="en">' in r.text and "⚙ Settings" in r.text)
check("只是打开页面不会写配置", not config.CONFIG_PATH.exists())
check("默认解说语言 / 音色是英语", config.load()["language"] == "en-US" and config.load()["voice"] == "en-US-AriaNeural")
check("页面上没有残留中文", not visible_han(r.text), visible_han(r.text)[:5])
h = c.get("/api/health", headers={"accept-language": "de-DE"}).json()
check("扩展看到的界面语言也是英语", h["ui_language"] == "en")

print("\n== 2. 没设置过界面语言的旧配置：也是英语 ==")
reset({"deepseek_api_key": "sk-old", "language": "zh-CN"})
r = c.get("/", headers={"accept-language": "de-DE"})
check("旧配置 -> 英语界面", '<html lang="en"' in r.text and "⚙ Settings" in r.text)
c.post("/api/settings", json={"tts_rate": "+10%"})
config._cache = None
cfg = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
check("保存别的设置后界面仍是英语", c.get("/api/health").json()["ui_language"] == "en" and cfg["ui_language"] == "en")
check("旧配置自己的解说语言不被改动", cfg["language"] == "zh-CN")
reset({"ui_language": "zh"})
check("明确选过中文的保持中文", '<html lang="zh"' in c.get("/").text)

print("\n== 3. 切换界面语言 ==")
s = c.post("/api/settings", json={"ui_language": "de-AT"}).json()
check("de-AT 归一成 de", s["ui_language"] == "de")
r = c.get("/")
check("刷新后是德语", '<html lang="de">' in r.text and "⚙ Einstellungen" in r.text)
s = c.post("/api/settings", json={"ui_language": "klingon"}).json()
check("不支持的语言不会覆盖", s["ui_language"] == "de", s["ui_language"])
check("健康检查带上界面语言", c.get("/api/health").json()["ui_language"] == "de")
cat = c.get("/api/i18n").json()
check("/api/i18n 返回词典", cat["lang"] == "de" and cat["dict"]["片尾"] == "Outro" and len(cat["langs"]) == 8)
check("注入到页面的词典和文件一致", json.dumps(i18n.catalog("de"), ensure_ascii=False)[:200].replace("</", "<\\/") in r.text)

print("\n== 4. 后端提示跟界面语言 ==")
for lang, expect in [("en", "Project not found"), ("fr", "Projet introuvable"), ("nl", "Project niet gevonden"), ("zh", "项目不存在")]:
    c.post("/api/settings", json={"ui_language": lang})
    d = c.get("/api/projects/p_000000000000").json().get("detail", "")
    check(f"{lang}: 404 提示", d == expect, d)

c.post("/api/settings", json={"ui_language": "pl"})
pid = selftest.build_project("i18n")
r = wait(c.post(f"/api/projects/{pid}/tts", json={}).json())
check("波兰语进度：复数 many", r["message"] == "Głos gotowy: wygenerowane fragmenty: 5", r["message"])
c.post("/api/settings", json={"ui_language": "de"})
r = wait(c.post(f"/api/projects/{pid}/tts", json={"only_missing": False}).json())
check("德语进度：复数 other", r["message"] == "Sprachausgabe fertig: 5 Clips erzeugt", r["message"])
check("de: 单数", i18n.t("{n} 步", _lang="de", n=1) == "1 Schritt" and i18n.t("{n} 步", _lang="pl", n=3) == "3 kroki")
check("未翻译的 key 原样返回并格式化", i18n.t("不存在的 {x}", _lang="de", x=1) == "不存在的 1")

print("\n== 5. 写进视频 / 文档的文字跟项目的解说语言 ==")
c.patch(f"/api/projects/{pid}", json={"language": "fr-FR"})
p = storage.load(pid)
p.steps[0].title = ""
storage.save(p)
md = c.get(f"/api/projects/{pid}/export/markdown").text
check("界面德语、项目法语 → 文档里是 Étape", "## 1. Étape 1" in md, md.split("\n")[4:6])
c.patch(f"/api/projects/{pid}", json={"language": "ja-JP"})
md = c.get(f"/api/projects/{pid}/export/markdown").text
check("不支持的解说语言 → 英文", "## 1. Step 1" in md)

print("\n== 6. 每种语言的编辑器页面都没有残留中文 ==")
for lang in i18n.LANGS:
    if lang == "zh":
        continue
    c.post("/api/settings", json={"ui_language": lang})
    han = visible_han(c.get("/").text)
    check(f"{lang}: 静态页面无中文", not han, han[:3])

print("\n== 7. 扩展词典和主词典同步 ==")
import i18n_extract  # noqa: E402
for lang in ("de", "pl"):
    ext = json.loads(Path(ROOT / "extension" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    check(f"{lang}: 扩展词典是主词典的子集且完整", ext == i18n_extract.extension_catalog(lang) and ext.get("录制中") and len(ext) > 50, len(ext))
man = json.loads(Path(ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
check("manifest 用 _locales", man["name"] == "__MSG_extName__" and man["default_locale"] == "en"
      and man["content_scripts"][0]["js"][0] == "i18n.js")
for loc in ("en", "zh_CN", "de", "fr", "pl", "it", "es", "nl"):
    m = json.loads(Path(ROOT / "extension" / "_locales" / loc / "messages.json").read_text(encoding="utf-8"))
    if not all(m[k]["message"] for k in ("extName", "extDescription", "cmdToggle", "cmdCapture")):
        check(f"_locales/{loc} 完整", False)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
