"""解说语言 / 第二字幕的语言列表：顺序和分组、每种都有音色和试听句、语音识别的语言代码、
备注语言判断（后端 langdetect 和编辑器 app.js 的 guessLang 必须判断得一样）。不联网，不花额度。"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "languages_data"
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

from backend import main  # noqa: E402
from backend.services import asr, dialogue, langdetect, script_gen, tts  # noqa: E402
from lang_samples import SAMPLES  # noqa: E402

c = TestClient(main.app, base_url="http://127.0.0.1:8756")
H = {"Origin": "http://127.0.0.1:8756"}
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


print("\n== 1. 语言列表 ==")
langs = c.get("/api/languages").json()["languages"]
codes = [x["code"] for x in langs]
groups = [x["group"] for x in langs]
check("中文在最前，然后欧洲语言，最后其他国家的语言", groups == sorted(groups, key=["zh", "europe", "other"].index)
      and codes[:2] == ["zh-CN", "zh-TW"], groups)
eu = [x["code"] for x in langs if x["group"] == "europe"]
check("常用的欧洲语言排在欧洲组前面", eu[:9] == ["en-GB", "en-US", "de-DE", "fr-FR", "es-ES", "it-IT", "nl-NL", "pl-PL", "pt-PT"],
      eu[:9])
new_eu = {"pt-PT", "sv-SE", "da-DK", "nb-NO", "fi-FI", "is-IS", "cs-CZ", "sk-SK", "hu-HU", "ro-RO", "bg-BG", "el-GR",
          "hr-HR", "sl-SI", "sr-RS", "bs-BA", "mk-MK", "sq-AL", "et-EE", "lv-LV", "lt-LT", "uk-UA", "ga-IE", "cy-GB",
          "ca-ES", "gl-ES", "mt-MT", "tr-TR", "ru-RU"}
check("新加的欧洲语言都在欧洲组", new_eu <= set(eu), new_eu - set(eu))
check("日韩、巴西葡语、越泰、阿拉伯、印地在后面", [x["code"] for x in langs if x["group"] == "other"]
      == ["ja-JP", "ko-KR", "pt-BR", "vi-VN", "th-TH", "ar-SA", "hi-IN"])
check("代码不重复、名字都不一样", len(set(codes)) == len(codes) and len({x["name"] for x in langs}) == len(langs))
check("Chrome 扩展用的状态接口里也有同样的列表", c.get("/api/health").json().get("languages") == langs)


print("\n== 2. 每种语言都有默认音色（女声主讲 + 双人问答的男声）==")
bad = [k for k in codes if not tts.DEFAULT_VOICES.get(k, "").startswith(k + "-")
       or not dialogue.MALE_VOICES.get(k, "").startswith(k + "-")]
check("默认音色和语言对得上", not bad, bad)
check("新语言的项目默认用这种语言的音色", tts.default_voice("sv-SE") == "sv-SE-SofieNeural"
      and dialogue.male_voice("uk-UA") == "uk-UA-OstapNeural")

said = {}


def fake_synth(text, voice, path, *a, **k):
    said[voice] = text
    Path(path).write_bytes(b"\xff\xfb" + b"\0" * 600)
    return 1.0, []


real_synth, tts.synth = tts.synth, fake_synth
for k in codes:
    c.post("/api/tts/preview", json={"voice": tts.DEFAULT_VOICES[k]}, headers=H)
tts.synth = real_synth
english = "Hello, this is a preview of this voice."
bad = [k for k in codes if not k.startswith("en") and said.get(tts.DEFAULT_VOICES[k]) in (None, english)]
check("试听句用音色自己的语言（不是都念英文）", not bad, bad)


print("\n== 3. 语音识别（边录边讲 / 视频讲话转字幕）的语言代码 ==")
check("挪威语 nb-NO → Whisper 的 no", asr.whisper_lang("nb-NO") == "no")
check("普通的语言直接用基础代码", asr.whisper_lang("sv-SE") == "sv" and asr.whisper_lang("uk-UA") == "uk")
if asr.installed():
    check("模型不认识的语言（爱尔兰语）交给模型自己判断", asr.whisper_lang("ga-IE") is None)


print("\n== 4. 备注语言判断 ==")
wrong = [(k, langdetect.detect(t)) for k, t in SAMPLES.items() if k != "vi" and not langdetect.related(langdetect.detect(t), k)]
check("各语言的样例都判断对（近亲语言算对）", not wrong, wrong)
check("分得清近亲：丹麦语 / 挪威语、葡萄牙语、马其顿语 / 塞尔维亚语",
      [langdetect.detect(SAMPLES[k]) for k in ("da", "nb", "sv", "pt", "mk", "sr", "uk", "bg", "ru")]
      == ["da", "nb", "sv", "pt", "mk", "sr", "uk", "bg", "ru"])
check("英文备注配任何一种欧洲语言的解说：都判成「不是同一种」",
      not [k for k in new_eu if langdetect.same_language(SAMPLES["en"], k)])
check("欧洲语言的备注配中文解说：都判成「不是同一种」",
      not [k for k, t in SAMPLES.items() if k not in ("zh", "vi") and langdetect.same_language(t, "zh-CN")])
check("近亲语言算同一种（宁可照念，不乱改）", langdetect.same_language(SAMPLES["da"], "nb-NO")
      and langdetect.same_language(SAMPLES["sk"], "cs-CZ") and langdetect.same_language(SAMPLES["gl"], "pt-PT"))
check("俄文备注配乌克兰语解说：不是同一种", not langdetect.same_language(SAMPLES["ru"], "uk-UA"))
check("太短 / 分不出来的当作同一种", langdetect.same_language("OK", "zh-CN") and langdetect.same_language(SAMPLES["vi"], "zh-CN"))

node = shutil.which("node")
if not node:
    print("  （没装 node，跳过和 app.js 的对比）")
else:
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("// 粗判文字语言"):js.index("/** 一批备注大多是什么语言 */")]
    bases = sorted({k.split("-")[0] for k in codes} | {"cyrl"})
    extra = {"short": "OK", "mixed_ja": "新製品の説明資料をご覧ください。今日は新しい機能を紹介します。",
             "no_marks": "Мама мила раму, папа читал книгу дома вечером"}
    texts = {**SAMPLES, **extra}
    (DATA / "texts.json").write_text(json.dumps({"texts": texts, "bases": bases}, ensure_ascii=False), encoding="utf-8")
    script = block + """
const data = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const out = { detect: {}, related: [] };
for (const [k, t] of Object.entries(data.texts)) out.detect[k] = guessLang(t);
for (const a of data.bases) for (const b of data.bases) if (langRelated(a, b)) out.related.push(a + '|' + b);
console.log(JSON.stringify(out));
"""
    (DATA / "guess.js").write_text(script, encoding="utf-8")
    r = subprocess.run([node, str(DATA / "guess.js"), str(DATA / "texts.json")], capture_output=True, text=True,
                       encoding="utf-8")
    got = json.loads(r.stdout or "{}")
    py = {k: langdetect.detect(t) for k, t in texts.items()}
    diff = {k: (py[k], got.get("detect", {}).get(k)) for k in texts if py[k] != got.get("detect", {}).get(k)}
    check("编辑器（app.js）和后端判断的语言完全一样", r.returncode == 0 and not diff, diff or r.stderr[:300])
    py_rel = {f"{a}|{b}" for a in bases for b in bases if langdetect.related(a, b)}
    check("「算不算同一种」两边也一样", py_rel == set(got.get("related", [])),
          py_rel ^ set(got.get("related", [])))


print("\n== 5. 第二字幕接受新语言 ==")
from backend.services import second_subs  # noqa: E402

check("新加的语言做第二字幕也默认斜体", second_subs.italic("sv-SE") and second_subs.italic("uk-UA"))
check("可以选的第二字幕语言就是这张表", all(k in script_gen.LANG_NAMES for k in ("sv-SE", "uk-UA", "pt-PT", "tr-TR")))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
