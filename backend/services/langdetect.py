"""粗判一段文字是什么语言（只需要分清「备注的语言和解说语言是不是同一种」）。

按文字系统：假名 → ja，谚文 → ko，汉字 → zh，希腊字母 → el，泰文、阿拉伯文、天城文；
西里尔字母再按特有的字母分俄 / 乌克兰 / 保加利亚 / 塞尔维亚 / 马其顿文，分不出来返回 cyrl；
拉丁字母的语言按常用虚词数一数。判断不出来返回空字符串。

static/app.js 的 guessLang / sameLang 是同一套规则，改这里要一起改（tests/test_languages.py 会对比）。
"""
from __future__ import annotations

import re
from typing import Dict, Set

_SCRIPTS = [
    ("ja", re.compile(r"[぀-ヿ]")),
    ("ko", re.compile(r"[가-힯]")),
    ("zh", re.compile(r"[一-鿿]")),  # i18n: ignore
    ("el", re.compile(r"[Ͱ-Ͽἀ-῿]")),
    ("cyrl", re.compile(r"[Ѐ-ӿ]")),
    ("th", re.compile(r"[฀-๿]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("hi", re.compile(r"[ऀ-ॿ]")),
]
_STOP: Dict[str, Set[str]] = {k: set(v.split()) for k, v in {
    "en": "the and is to of you this that with for it are on we our can",
    "de": "der die und das ist nicht mit sie ein eine zu auf für wir den",
    "fr": "le la les et est des une un pour que dans vous pas nous du",
    "es": "el la los las y es que para una un con por del se lo",
    "it": "il che è di per una un non sono con della gli le si",
    "nl": "de het een en van is dat niet met voor je op we zijn",
    "pl": "i w nie na się jest z do to że jak dla oraz są",
    "pt": "o os as e que para uma um com não você do da em ao pelo pela também muito quando já",
    "sv": "och att det som en på är av för med till den har inte om ett vi kan",
    "da": "og at det som en på er af for med til den har ikke om et vi kan hvad efter meget nogle",
    "nb": "og at det som en på er av for med til den har ikke om et vi kan hva etter mye noen",
    "fi": "ja on ei se että oli mutta kun tai ovat voi myös sekä tämä joka jos kuin",
    "is": "og að er í á sem um við ekki til var með en það þetta eru fyrir",
    "et": "ja on ei et see oli ka kui aga või mis ning siis nad oma veel kes seda",
    "lv": "un ir ar par no uz kas bet lai arī vai tas to ka nav kā var tā",
    "lt": "ir yra kad su į iš bet kaip tai ar jo o nuo už per taip buvo dar",
    "cs": "a je se na v že to s z do jsou pro ale jak také by k tak jsme",
    "sk": "a je sa na v že to s z do sú pre ale ako aj by k tak sme",
    "hu": "a az és hogy nem is egy van meg de ez csak már mint vagy ha el kell",
    "ro": "și şi si în de la cu este nu pe o un care din să se pentru mai sunt sau",
    "hr": "i je u na da se su za od koji ali kao što sa iz ili nije biti to",
    "sl": "in je v na da se so za od ki ali kot pa z iz tudi ni bi to",
    "sq": "dhe në të që është një për me nga i e janë por si nuk ka do",
    "ca": "el la els les i que de per amb una un no és del als més són aquest",
    "gl": "unha non con en un máis ou polo pola polos polas tamén moi cando xa",   # 和葡萄牙语共有的虚词不算
    "ga": "agus an na is tá ar le sa go ag i don seo sin atá níl bhí mar",
    "cy": "a y yr ac yn mae i o ar ei eu wedi hwn hon gyda am ond roedd fel",
    "mt": "il l u ta li fil tal biex huwa hija għal minn ma din dan jew kif wkoll",
    "tr": "ve bir bu da de için ile olarak çok daha ne gibi var ama en olan kadar mı",
}.items()}
# 靠虚词分不太清的近亲语言：判断成其中一种时，对同组的其他语言也算「同一种」（宁可照念，不乱改）
_FAMILIES = [{"da", "nb", "sv"}, {"cs", "sk"}, {"hr", "bs", "sr", "sl"}, {"gl", "pt"}, {"sr", "mk"}]
CYRILLIC = {"ru", "uk", "bg", "sr", "mk"}
_SR = set("је шта овај ова који која ће сам".split())
_MK = set("е што овој оваа кој која ќе сум".split())


def related(a: str, b: str) -> bool:
    """两个基础语言代码算不算同一种：相同、近亲，或者一边是「分不清的西里尔文」另一边是用西里尔字母的语言。"""
    if a == b:
        return True
    if "cyrl" in (a, b):
        return (b if a == "cyrl" else a) in CYRILLIC
    return any(a in f and b in f for f in _FAMILIES)


def _cyrillic(text: str) -> str:
    t = text.lower()

    def has(chars: str) -> bool:
        return any(c in t for c in chars)

    if has("іїєґ"):
        return "uk"
    if has("ќѓѕ"):
        return "mk"
    if has("ђћ"):
        return "sr"
    if has("јљњџ"):                                  # 塞尔维亚文、马其顿文都有这几个字母：数常用词
        words = re.findall(r"[^\W\d_]+", t)
        mk = sum(w in _MK for w in words)
        return "mk" if mk > sum(w in _SR for w in words) else "sr"
    if has("ыэё"):
        return "ru"
    if "ъ" in t:
        return "bg"
    return "cyrl"


def detect(text: str) -> str:
    """文字的语言（基础代码，比如 zh、en）；西里尔字母分不出具体语言时是 cyrl；太短或判断不出来返回 ''。"""
    text = text or ""
    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 12:
        return ""
    for code, rx in _SCRIPTS:
        n = len(rx.findall(text))
        if n >= max(4, letters * 0.2):
            if code == "zh" and _SCRIPTS[0][1].search(text):
                return "ja"                            # 汉字里夹着假名的是日文
            return _cyrillic(text) if code == "cyrl" else code
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 5:
        return ""
    scores = sorted(((sum(1 for w in words if w in stop) / len(words), code) for code, stop in _STOP.items()),
                    key=lambda x: (-x[0], x[1]))       # 分数一样时按代码字母排，和 app.js 一致
    best, code = scores[0]
    # 和「不是近亲的第二名」比：丹麦语 / 挪威语这种分不太清的，只要确定是这一组就行
    rival = next((s for s, c in scores[1:] if not related(c, code)), 0.0)
    return code if best >= 0.06 and best >= rival * 1.3 else ""


def same_language(text: str, lang: str) -> bool:
    """这段文字是不是这种语言（lang 可以是 zh-CN 这种完整代码）。判断不出来就当作是（不乱改）。"""
    got = detect(text)
    return not got or related(got, (lang or "").split("-")[0].lower())
