"""Maintenance tool for interface translations.

    python tools/i18n_extract.py            # check: Chinese not wrapped in t(), missing translations per language, mismatched placeholders
    python tools/i18n_extract.py --keys keys.json   # also write all keys (with source files) to keys.json
    python tools/i18n_extract.py --missing de   # list the keys German is still missing (JSON, easy to fill in)

Rules:
  * JS: t('中文') / t("中文") / N_('中文'); don't put Chinese directly into template strings, use t('… {x} …', { x })
  * Python: t("中文") / N_("中文"); Chinese users never see (prompts, regexes) gets `# i18n: ignore` on that line
  * HTML: static text is replaced server-side by its source text; elements whose text contains <b>/<code> get data-i18n and are translated as a whole
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import i18n  # noqa: E402

HAN = re.compile(r"[一-鿿]")
LOCALES = ROOT / "static" / "locales"

HTML_FILES = [ROOT / "static" / "index.html", *sorted((ROOT / "extension").glob("*.html"))]
JS_FILES = [ROOT / "static" / "app.js", *sorted(p for p in (ROOT / "extension").glob("*.js") if p.name != "i18n.js")]
PY_FILES = [ROOT / "app.py", *sorted((ROOT / "backend").rglob("*.py"))]
# whole files that contain no interface text (detection rules, regexes)
PY_IGNORE_FILES = {"backend/services/redact.py", "backend/i18n.py"}


# ---- JS ----------------------------------------------------------------------

def _js_tokens(src: str):
    """Yield (kind, text, start) one by one: kind is str / tpl / code. Comments are skipped."""
    i, n = 0, len(src)
    code_start = 0
    while i < n:
        ch = src[i]
        if src.startswith("//", i):
            yield "code", src[code_start:i], code_start
            j = src.find("\n", i)
            i = n if j < 0 else j
            code_start = i
            continue
        if src.startswith("/*", i):
            yield "code", src[code_start:i], code_start
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
            code_start = i
            continue
        if ch in "'\"`":
            yield "code", src[code_start:i], code_start
            j = i + 1
            depth = 0
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if ch == "`" and src.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if ch == "`" and depth and c == "}":
                    depth -= 1
                    j += 1
                    continue
                if c == ch and not depth:
                    break
                j += 1
            yield ("tpl" if ch == "`" else "str"), src[i:j + 1], i
            i = j + 1
            code_start = i
            continue
        if ch == "/" and _regex_allowed(src, i):
            j = i + 1
            in_class = False
            while j < n and src[j] != "\n":
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    yield "code", src[code_start:], code_start


def _regex_allowed(src: str, i: int) -> bool:
    k = i - 1
    while k >= 0 and src[k] in " \t":
        k -= 1
    return k < 0 or src[k] in "(,=:[!&|?{};\n" or src[max(0, k - 5):k + 1].endswith("return")


def _js_unquote(lit: str) -> str:
    body = lit[1:-1]
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"', "`": "`"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _template_parts(tpl: str):
    """Split a template string into (static text, [(expression, start in tpl)])."""
    static, exprs = [], []
    i, n = 1, len(tpl) - 1
    while i < n:
        if tpl[i] == "\\":
            static.append(tpl[i:i + 2])
            i += 2
            continue
        if tpl.startswith("${", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if tpl[j] == "{":
                    depth += 1
                elif tpl[j] == "}":
                    depth -= 1
                elif tpl[j] in "'\"`":          # strings inside the expression: skip the brackets in them
                    q, j = tpl[j], j + 1
                    while j < n and tpl[j] != q:
                        j += 2 if tpl[j] == "\\" else 1
                j += 1
            exprs.append((tpl[i + 2:j - 1], i + 2))
            i = j
            continue
        static.append(tpl[i])
        i += 1
    return "".join(static), exprs


def _js_scan_src(src: str, full: str, base: int, keys: list, leaks: list):
    lines = full.split("\n")
    prev_code = ""
    for kind, text, pos in _js_tokens(src):
        if kind == "code":
            prev_code = text
            continue
        wrapped = re.search(r"\b(?:t|N_)\(\s*$", prev_code) is not None
        prev_code = ""
        line = full.count("\n", 0, base + pos) + 1
        ignored = "i18n: ignore" in lines[line - 1]
        if kind == "str":
            if wrapped:
                keys.append(_js_unquote(text))
            elif HAN.search(text) and not ignored:
                leaks.append((line, text[:80]))
            continue
        static, exprs = _template_parts(text)
        if wrapped and not exprs:
            keys.append(_js_unquote(text))
            continue
        if HAN.search(static) and not ignored:
            leaks.append((line, static.strip()[:80]))
        for expr, off in exprs:
            _js_scan_src(expr, full, base + pos + off, keys, leaks)


def js_scan(path: Path):
    src = path.read_text(encoding="utf-8")
    keys, leaks = [], []
    _js_scan_src(src, src, 0, keys, leaks)
    return keys, leaks


# ---- Python ------------------------------------------------------------------

def py_scan(path: Path):
    rel = path.relative_to(ROOT).as_posix()
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    tree = ast.parse(src)
    keys, leaks = [], []
    safe = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            safe.add(id(body[0].value))          # docstring
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in ("t", "N_") and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                keys.append(node.args[0].value)
                safe.add(id(node.args[0]))
    if rel in PY_IGNORE_FILES:
        return keys, []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                safe.add(id(v))
            txt = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            if HAN.search(txt) and not _ignored(lines, node):
                leaks.append((node.lineno, txt[:80]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and HAN.search(node.value) \
                and id(node) not in safe and not _ignored(lines, node):
            leaks.append((node.lineno, node.value[:80]))
    return keys, leaks


def _ignored(lines, node) -> bool:
    end = getattr(node, "end_lineno", node.lineno)
    return any("i18n: ignore" in lines[k - 1] for k in range(node.lineno, end + 1))


# ---- summary ---------------------------------------------------------------------

def extension_keys():
    keys = set()
    for f in (ROOT / "extension").glob("*.html"):
        keys.update(i18n.html_keys(f.read_text(encoding="utf-8")))
    for f in (ROOT / "extension").glob("*.js"):
        if f.name != "i18n.js":
            keys.update(js_scan(f)[0])
    return keys


def extension_catalog(lang: str):
    """Dictionary for the extension package: only keys used by the extension."""
    path = LOCALES / f"{lang}.json"
    full = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {k: full[k] for k in sorted(extension_keys()) if full.get(k)}


def collect():
    keys, leaks = {}, []
    for f in HTML_FILES:
        for k in i18n.html_keys(f.read_text(encoding="utf-8")):
            keys.setdefault(k, f.relative_to(ROOT).as_posix())
    for f in JS_FILES:
        ks, ls = js_scan(f)
        for k in ks:
            keys.setdefault(k, f.relative_to(ROOT).as_posix())
        leaks += [(f.relative_to(ROOT).as_posix(), *x) for x in ls]
    for f in PY_FILES:
        ks, ls = py_scan(f)
        for k in ks:
            keys.setdefault(k, f.relative_to(ROOT).as_posix())
        leaks += [(f.relative_to(ROOT).as_posix(), *x) for x in ls]
    return keys, leaks


TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def placeholders(text: str) -> set:
    """Find the variable names used, by the rules of format_message (text inside plural branches doesn't count)."""
    names, i = set(), 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        j = i18n._match_brace(text, i)
        if j < 0:
            break
        name, _, rest = text[i + 1:j].partition(",")
        kind, _, spec = rest.partition(",")
        if kind.strip() == "plural":
            names.add(name.strip())
            for body in i18n._branches(spec).values():
                names |= placeholders(body)
        elif not rest and re.fullmatch(r"\s*[A-Za-z_]\w*\s*", name):
            names.add(name.strip())
        i = j + 1
    return names


def check_translation(key: str, value: str):
    problems = []
    if HAN.search(value):
        problems.append("译文里还有中文")
    if placeholders(key) != placeholders(value):
        problems.append(f"占位符不一致 {sorted(placeholders(key))} vs {sorted(placeholders(value))}")
    if sorted(TAG.findall(key)) != sorted(TAG.findall(value)):
        problems.append("HTML 标签不一致")
    if value.count("{") != value.count("}"):
        problems.append("花括号不配对")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default="", help="把所有 key（附出处）写到这个 JSON 文件")
    ap.add_argument("--missing", default="")
    ap.add_argument("--sync-extension", action="store_true", help="把扩展用到的译文复制到 extension/locales")
    args = ap.parse_args()

    keys, leaks = collect()
    ext_dir = ROOT / "extension" / "locales"
    if args.sync_extension:
        ext_dir.mkdir(exist_ok=True)
        for lang in i18n.LANGS:
            if lang != "zh":
                (ext_dir / f"{lang}.json").write_text(
                    json.dumps(extension_catalog(lang), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("已同步 extension/locales")
    if args.keys:
        Path(args.keys).write_text(json.dumps(dict(sorted(keys.items())), ensure_ascii=False, indent=1), encoding="utf-8")
    if args.missing:
        path = LOCALES / f"{args.missing}.json"
        have = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        print(json.dumps([k for k in sorted(keys) if not have.get(k)], ensure_ascii=False, indent=1))
        return 0

    bad = 0
    print(f"共 {len(keys)} 条界面文字")
    if leaks:
        bad += len(leaks)
        print(f"\n没有用 t() 包起来的中文 {len(leaks)} 处：")
        for f, line, txt in leaks:
            print(f"  {f}:{line}  {txt!r}")
    for lang in i18n.LANGS:
        if lang == "zh":
            continue
        path = LOCALES / f"{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        missing = [k for k in keys if not data.get(k)]
        extra = [k for k in data if k not in keys]
        broken = [(k, p) for k in keys if data.get(k) for p in check_translation(k, data[k])]
        status = "OK" if not (missing or broken) else "!!"
        print(f"\n[{status}] {lang}: 缺 {len(missing)}，多余 {len(extra)}，有问题 {len(broken)}")
        for k in missing[:5]:
            print(f"    缺：{k[:70]!r}")
        for k, p in broken[:10]:
            print(f"    {p}：{k[:60]!r}")
        bad += len(missing) + len(broken)
        ext_path = ext_dir / f"{lang}.json"
        ext_now = json.loads(ext_path.read_text(encoding="utf-8")) if ext_path.exists() else None
        if ext_now != extension_catalog(lang):
            print("    extension/locales 和 static/locales 不一致，运行 --sync-extension")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
