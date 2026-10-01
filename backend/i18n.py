"""界面语言（工作语言）。

中文原文就是翻译的 key：代码里写 t("项目不存在")，static/locales/{lang}.json 里存
{"项目不存在": "Project not found"}。中文界面不需要翻译文件，缺译文时也退回中文原文。

  * 占位符：t("已删除 {n} 个", n=3)
  * 数量（ICU 复数）：译文里写 "{n, plural, one {# page} other {# pages}}"，
    波兰语还有 few / many。中文原文不需要。
  * N_("...")：只标记「这是要翻译的文字」，不当场翻译（给提取工具看的）。

界面文字跟 ui_language；写进视频 / 文档的文字（片尾默认的「完成！」、「步骤 3」这类）
跟项目的解说语言，用 content_lang(proj.language)。
"""
from __future__ import annotations

import html as _html
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

LANGS: Dict[str, str] = {
    "zh": "中文", "en": "English", "de": "Deutsch", "fr": "Français",
    "pl": "Polski", "it": "Italiano", "es": "Español", "nl": "Nederlands",
}
FALLBACK = "en"

_lock = threading.Lock()
_cache: Dict[str, tuple] = {}          # lang -> (mtime, dict)


def N_(s: str) -> str:
    return s


def locale_dir() -> Path:
    return config.STATIC_DIR / "locales"


def normalize(lang: Any) -> str:
    """'de-DE' / 'de_AT' / 'DE' -> 'de'；不支持的返回 ''。"""
    code = re.split(r"[-_]", str(lang or "").strip().lower())[0]
    return code if code in LANGS else ""


def current() -> str:
    return normalize(config.get("ui_language")) or FALLBACK


def content_lang(language: str) -> str:
    """项目解说语言对应的文字语言（写进视频 / 文档里的默认文字用）。"""
    return normalize(language) or FALLBACK


def catalog(lang: str) -> Dict[str, str]:
    lang = normalize(lang)
    if not lang or lang == "zh":
        return {}
    path = locale_dir() / f"{lang}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        hit = _cache.get(lang)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data = {k: v for k, v in data.items() if isinstance(v, str) and v}
        except (OSError, ValueError):
            data = {}
        _cache[lang] = (mtime, data)
        return data


def t(key: str, _lang: Optional[str] = None, **params: Any) -> str:
    lang = normalize(_lang) or current()
    text = catalog(lang).get(key, key)
    return format_message(text, params, lang) if (params or "{" in text) else text


# ---- ICU 复数（和 static/i18n.js 里的实现保持一致）-------------------------------

def plural_category(lang: str, n: Any) -> str:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "other"
    integer = x.is_integer()
    i = int(abs(x)) if integer else None
    if lang == "zh":
        return "other"
    if lang == "fr":
        return "one" if int(abs(x)) in (0, 1) else "other"
    if lang == "pl":
        if not integer:
            return "other"
        if i == 1:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    return "one" if integer and i == 1 else "other"


def _match_brace(s: str, start: int) -> int:
    depth = 0
    for j in range(start, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _branches(spec: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    i = 0
    while i < len(spec):
        m = re.compile(r"\s*(=\d+|zero|one|two|few|many|other)\s*\{").match(spec, i)
        if not m:
            break
        open_at = m.end() - 1
        close_at = _match_brace(spec, open_at)
        if close_at < 0:
            break
        out[m.group(1)] = spec[open_at + 1:close_at]
        i = close_at + 1
    return out


def format_message(text: str, params: Dict[str, Any], lang: str = "zh") -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "{":
            out.append(ch)
            i += 1
            continue
        j = _match_brace(text, i)
        if j < 0:
            out.append(text[i:])
            break
        inner = text[i + 1:j]
        name, _, rest = inner.partition(",")
        name = name.strip()
        kind, _, spec = rest.partition(",")
        if kind.strip() == "plural" and name in params:
            n = params[name]
            br = _branches(spec)
            chosen = None
            try:
                if float(n).is_integer():
                    chosen = br.get(f"={int(float(n))}")
            except (TypeError, ValueError):
                pass
            if chosen is None:
                chosen = br.get(plural_category(lang, n), br.get("other", ""))
            out.append(format_message(chosen.replace("#", str(n)), params, lang))
        elif not rest and name in params:
            out.append(str(params[name]))
        else:
            out.append(text[i:j + 1])
        i = j + 1
    return "".join(out)


# ---- HTML（服务端翻译 index.html 的静态文字）-----------------------------------

HAN = re.compile(r"[一-鿿]")
_SKIP = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->)", re.S | re.I)
_I18N_EL = re.compile(r"(<(?P<tag>[a-zA-Z0-9]+)\b[^>]*\bdata-i18n\b[^>]*>)(?P<body>.*?)(</(?P=tag)\s*>)", re.S)
_TEXT = re.compile(r">([^<>]+)<")
_ATTR = re.compile(r'(\s(?:placeholder|title|alt|aria-label|label)=")([^"]*)(")')


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _no_translate(seg: str, pos: int) -> bool:
    """文字所在的标签带 translate="no"（比如语言名「中文」「日本語」）就不翻译。"""
    tag = seg[seg.rfind("<", 0, pos):pos]
    return not tag.startswith("</") and 'translate="no"' in tag


def html_keys(src: str):
    """提取 HTML 里要翻译的 key（和 translate_html 用同一套规则）。"""
    keys = []

    def collect(seg: str):
        def el(m):
            keys.append(norm_text(m.group("body")))
            return m.group(1) + "\x00" + m.group(4)
        seg = _I18N_EL.sub(el, seg)
        for m in _ATTR.finditer(seg):
            v = norm_text(_html.unescape(m.group(2)))
            if HAN.search(v):
                keys.append(v)
        for m in _TEXT.finditer(seg):
            v = norm_text(_html.unescape(m.group(1)))
            if HAN.search(v) and not _no_translate(seg, m.start() + 1):
                keys.append(v)

    for part in _SKIP.split(src):
        if part and not _SKIP.fullmatch(part):
            collect(part)
    return keys


def translate_html(src: str, lang: str) -> str:
    cat = catalog(lang)
    if not cat:
        return src

    def tr(key: str) -> str:
        return cat.get(key, key)

    def do(seg: str) -> str:
        stash = []

        def el(m):
            body = m.group("body")
            key = norm_text(body)
            new_body = tr(key) if HAN.search(key) else body
            stash.append(new_body)
            return m.group(1) + f"\x00{len(stash) - 1}\x00" + m.group(4)

        seg = _I18N_EL.sub(el, seg)

        def attr(m):
            key = norm_text(_html.unescape(m.group(2)))
            if not HAN.search(key):
                return m.group(0)
            return m.group(1) + _html.escape(tr(key), quote=True) + m.group(3)

        seg = _ATTR.sub(attr, seg)

        def text(m):
            raw = m.group(1)
            key = norm_text(_html.unescape(raw))
            if not HAN.search(key) or _no_translate(seg, m.start() + 1):
                return m.group(0)
            lead = raw[:len(raw) - len(raw.lstrip())]
            tail = raw[len(raw.rstrip()):]
            return ">" + lead + _html.escape(tr(key), quote=False) + tail + "<"

        seg = _TEXT.sub(text, seg)
        return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], seg)

    return "".join(p if (not p or _SKIP.fullmatch(p)) else do(p) for p in _SKIP.split(src))
