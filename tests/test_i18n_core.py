import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import i18n

f = i18n.format_message
assert f("{n, plural, one {# Seite} other {# Seiten}}", {"n": 1}, "de") == "1 Seite"
assert f("{n, plural, one {# Seite} other {# Seiten}}", {"n": 5}, "de") == "5 Seiten"
pl = "{n, plural, one {# strona} few {# strony} many {# stron} other {# strony}}"
assert [f(pl, {"n": k}, "pl") for k in (1, 2, 5, 12, 22, 25, 2.5)] == \
    ["1 strona", "2 strony", "5 stron", "12 stron", "22 strony", "25 stron", "2.5 strony"]
assert f("{n, plural, =0 {aucune page} one {# page} other {# pages}}", {"n": 0}, "fr") == "aucune page"
assert f("{n, plural, one {# page} other {# pages}}", {"n": 1}, "fr") == "1 page"
assert f("{a} / {b}", {"a": 1}, "en") == "1 / {b}"
assert f("已选 {a}，共 {n, plural, one {# step} other {# steps: {a}}}", {"a": "x", "n": 3}, "en") == "已选 x，共 3 steps: x"
assert f("unbalanced { brace", {}, "en") == "unbalanced { brace"

src = '''<title>VideoTutorial 编辑器</title>
<p data-i18n>点击 <b id="x">选择</b>，或拖到这里</p>
<input placeholder="教程名称" title="a &amp; 中文">
<span>
   步骤 <b id="stepCount">0</b></span>
<option value="1">tiny（75MB，最快）</option>
<script>var x = "中文不翻";</script>
<!-- 注释 -->'''
keys = i18n.html_keys(src)
print(keys)
assert sorted(keys) == sorted(["VideoTutorial 编辑器", '点击 <b id="x">选择</b>，或拖到这里', "教程名称", "a & 中文", "步骤", "tiny（75MB，最快）"]), keys

i18n.catalog = lambda lang: {"VideoTutorial 编辑器": "VideoTutorial Editor",
                             '点击 <b id="x">选择</b>，或拖到这里': '<b id="x">Choose a file</b> or drop it here',
                             "教程名称": 'Tutorial "name"', "a & 中文": "a & English", "步骤": "Steps",
                             "tiny（75MB，最快）": "tiny (75 MB, fastest) <x>"}
out = i18n.translate_html(src, "en")
print(out)
assert "<title>VideoTutorial Editor</title>" in out
assert '<p data-i18n><b id="x">Choose a file</b> or drop it here</p>' in out
assert 'placeholder="Tutorial &quot;name&quot;"' in out and 'title="a &amp; English"' in out
assert "<span>\n   Steps <b" in out
assert "tiny (75 MB, fastest) &lt;x&gt;</option>" in out
assert 'var x = "中文不翻";' in out and "<!-- 注释 -->" in out
assert i18n.normalize("zh-CN") == "zh" and i18n.normalize("pt-BR") == ""
print("core ok")
