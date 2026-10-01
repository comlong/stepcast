"""敏感信息识别与打码。

两条路：
  1. 录制时：Chrome 扩展遍历 DOM，用 Range 拿到每个命中子串的**精确**矩形（最准）
  2. 事后：在编辑器里按关键词 / 内置规则扫描本机存下来的文字索引（整段文字打码，保守）

除了画面打码，还会把命中的文字从 page_title / element_text / 输入值里抹掉 ——
这三个字段是唯一会发给大模型的内容，抹掉就不会外泄。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from ..models import Project, Redaction, Step, TextNode

# ---- 内置规则 -------------------------------------------------------------

PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("id_card", re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                           r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d{1,3}[- ]?)?\(?\d{3,4}\)?[- ]\d{3,4}[- ]?\d{4}(?!\d)")),
    ("bank", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("ip", re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")),
]

# 标签邻近法：「姓名：张三」「Name: John Smith」
LABELED_NAME = re.compile(
    r"(?:姓\s*名|名\s*字|联系人|负责人|申请人|操作人|创建人|员工|学员|讲师|用户名|"
    r"Name|Owner|Contact|Employee|User)\s*[:：]\s*"
    r"([一-龥]{2,4}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})"
)

# 百家姓（覆盖常见姓氏；两字复姓单列）
SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
COMPOUND_SURNAMES = {"欧阳", "司马", "诸葛", "上官", "夏侯", "皇甫", "尉迟", "公孙",
                     "慕容", "长孙", "宇文", "司徒", "鲜于", "东方", "独孤", "南宫"}

# 首字是姓氏、但在后台界面里几乎必然是普通词的，别误伤
NAME_STOPLIST = {
    "任务", "任意", "任何", "付款", "付费", "方式", "方法", "方案", "方向", "方便",
    "高级", "高度", "高亮", "高效", "金额", "金融", "白色", "白名", "马上", "石油",
    "董事", "史料", "于是", "何时", "江苏", "毛利", "龙头", "严重", "武汉", "孔径",
    "向上", "向下", "段落", "雷达", "顾客", "万能", "钱包", "夏季", "莫非", "常规",
    "姓名", "名称", "全部", "更多", "范围", "康复", "田间", "叶子", "程度", "苏州",
    "余额", "杜绝", "汪洋", "陆续", "钟表", "谭某", "贾某", "秦皇", "邱某", "江西",
    "尹某", "薛某", "段某", "雷同", "侯某", "陶瓷", "黎明", "贺卡", "郝某", "龚某",
    "邵某", "覃某", "戴上", "孔子", "汤水", "韦编", "付出", "白天", "高峰", "金牌",
}

KIND_LABEL = {
    "email": "邮箱", "phone": "手机号", "id_card": "身份证", "bank": "银行卡",
    "ip": "IP 地址", "name": "人名", "keyword": "关键词", "manual": "手动",
}

# 纯数字里这些明显不是敏感信息
_NOT_BANK = re.compile(r"^(?:19|20)\d{2}[01]\d[0-3]\d")   # 看着像日期串


def mask(text: str, kind: str = "") -> str:
    """把一段命中的文字换成掩码。"""
    text = text or ""
    if kind == "name" and 1 < len(text) <= 4 and all("一" <= c <= "龥" for c in text):
        return text[0] + "*" * (len(text) - 1)
    if kind == "email" and "@" in text:
        head = text.split("@")[0]
        return (head[:1] or "*") + "***@***"
    if len(text) <= 3:
        return "***"
    return text[:1] + "*" * min(6, len(text) - 2) + text[-1:]


# ---- 文本扫描 -------------------------------------------------------------

def find_matches(text: str, keywords: Iterable[str] = (), builtin: bool = True,
                 names: bool = True,
                 names_guess: bool = False) -> List[Tuple[str, int, int, str]]:
    """返回 [(kind, start, end, 原文)]，按起点排序且不重叠。"""
    text = text or ""
    hits: List[Tuple[str, int, int, str]] = []

    for kw in keywords:
        kw = (kw or "").strip()
        if len(kw) < 2:
            continue
        for m in re.finditer(re.escape(kw), text, re.I):
            hits.append(("keyword", m.start(), m.end(), m.group(0)))

    if builtin:
        for kind, pat in PATTERNS:
            for m in pat.finditer(text):
                s = m.group(0)
                if kind == "bank" and _NOT_BANK.match(s):
                    continue
                hits.append((kind, m.start(), m.end(), s))

    if names:
        # 「姓名：张三」这种带标签的，准确率高，默认开
        for m in LABELED_NAME.finditer(text):
            hits.append(("name", m.start(1), m.end(1), m.group(1)))

    if names_guess:
        # 整段就是一个像中文名的短串。会误报（「任务」「付款」首字也是姓），默认关
        t = text.strip()
        if 2 <= len(t) <= 4 and all("一" <= c <= "龥" for c in t) \
                and t not in NAME_STOPLIST \
                and (t[:2] in COMPOUND_SURNAMES or t[0] in SURNAMES):
            off = text.find(t)
            hits.append(("name", off, off + len(t), t))

    hits.sort(key=lambda h: (h[1], -(h[2] - h[1])))
    out: List[Tuple[str, int, int, str]] = []
    last_end = -1
    for h in hits:
        if h[1] >= last_end:
            out.append(h)
            last_end = h[2]
    return out


def mask_text(text: str, keywords: Iterable[str] = (), builtin: bool = True,
              names: bool = True, names_guess: bool = False) -> Tuple[str, int]:
    """把一段文字里的敏感内容替换成掩码，返回 (新文本, 命中数)。"""
    hits = find_matches(text, keywords, builtin, names, names_guess)
    if not hits:
        return text, 0
    out = []
    prev = 0
    for kind, s, e, raw in hits:
        out.append(text[prev:s])
        out.append(mask(raw, kind))
        prev = e
    out.append(text[prev:])
    return "".join(out), len(hits)


# ---- 区域计算 -------------------------------------------------------------

MULTILINE_H = 36.0     # 超过这个高度就当成多行，整块打码而不是按字符估算


def _rect_for(node: TextNode, start: int, end: int) -> Tuple[float, float, float, float]:
    """在一段文字节点里，估算某个子串的矩形。"""
    n = max(1, len(node.t))
    if node.h > MULTILINE_H or (end - start) >= n:
        return (node.x, node.y, node.w, node.h)
    pad = node.w / n * 0.35
    x = node.x + node.w * (start / n) - pad
    w = node.w * ((end - start) / n) + pad * 2
    x = max(node.x, x)
    w = min(node.w - (x - node.x), w)
    return (x, node.y, w, node.h)


def _overlaps(a: Redaction, b: Tuple[float, float, float, float], thresh: float = 0.5) -> bool:
    bx, by, bw, bh = b
    ix = max(0.0, min(a.x + a.w, bx + bw) - max(a.x, bx))
    iy = max(0.0, min(a.y + a.h, by + bh) - max(a.y, by))
    inter = ix * iy
    small = max(1.0, min(a.w * a.h, bw * bh))
    return inter / small > thresh


def scan_step(step: Step, keywords: Iterable[str] = (), builtin: bool = True,
              names: bool = True, names_guess: bool = False, mode: str = "blur",
              also_mask_text: bool = True) -> Dict[str, int]:
    """扫描一个步骤：加打码区域 + 抹掉文字字段里的敏感内容。"""
    keywords = [k for k in keywords if (k or "").strip()]
    added = 0
    pad = 3.0

    for node in step.text_nodes:
        # 录制时判定这段文字在「姓名」列里，那它整段就是人名，不用猜姓氏
        guess = names_guess or (names and node.n)
        for kind, s, e, raw in find_matches(node.t, keywords, builtin, names, guess):
            x, y, w, h = _rect_for(node, s, e)
            rect = (x - pad, y - pad, w + pad * 2, h + pad * 2)
            if any(_overlaps(r, rect) for r in step.redactions):
                continue
            step.redactions.append(Redaction(
                x=rect[0], y=rect[1], w=rect[2], h=rect[3],
                mode=mode, kind=kind, label=raw[:60], auto=True))
            added += 1

    masked = mask_step_texts(step, keywords, builtin, names, names_guess) if also_mask_text else 0
    return {"regions": added, "masked": masked}


def scan_project(proj: Project, keywords: Iterable[str] = (), builtin: bool = True,
                 names: bool = True, names_guess: bool = False, mode: str = "blur",
                 only_steps: Optional[Iterable[str]] = None) -> Dict[str, int]:
    ids = set(only_steps or [])
    targets = [s for s in proj.steps if not ids or s.id in ids]
    total = {"regions": 0, "masked": 0, "steps": 0}

    # 第一遍：画面打码，同时把命中的原文收集起来
    found: set[str] = set()
    for s in targets:
        r = scan_step(s, keywords, builtin, names, names_guess, mode,
                      also_mask_text=False)
        total["regions"] += r["regions"]
        if r["regions"]:
            total["steps"] += 1
        for red in s.redactions:
            if red.label and len(red.label) >= 2:
                found.add(red.label)

    # 第二遍：用「全项目找到的所有敏感值」清洗会外发的文字字段
    kw2 = list(keywords) + sorted(found)
    for s in targets:
        n = mask_step_texts(s, kw2, builtin, names, names_guess)
        total["masked"] += n
    return total


def mask_step_texts(step: Step, keywords: Iterable[str] = (), builtin: bool = True,
                    names: bool = True, names_guess: bool = False) -> int:
    """只清洗文字字段（页面标题 / 元素文字 / 输入值），不动画面。"""
    masked = 0
    for attr in ("page_title", "value"):
        new, n = mask_text(getattr(step, attr), keywords, builtin, names, names_guess)
        if n:
            setattr(step, attr, new)
            masked += n
    if step.target:
        for attr in ("text", "name"):
            new, n = mask_text(getattr(step.target, attr), keywords, builtin, names, names_guess)
            if n:
                setattr(step.target, attr, new)
                masked += n
    return masked


def clear_auto(proj: Project, only_steps: Optional[Iterable[str]] = None) -> int:
    """删掉所有自动识别的打码框（手动画的保留）。"""
    ids = set(only_steps or [])
    n = 0
    for s in proj.steps:
        if ids and s.id not in ids:
            continue
        before = len(s.redactions)
        s.redactions = [r for r in s.redactions if not r.auto]
        n += before - len(s.redactions)
    return n


def clear_index(proj: Project) -> int:
    """清空本机存的页面文字索引（清空后就不能再按关键词补打码了）。"""
    n = 0
    for s in proj.steps:
        n += len(s.text_nodes)
        s.text_nodes = []
    return n


def stats(proj: Project) -> Dict[str, int]:
    return {
        "regions": sum(len(s.redactions) for s in proj.steps),
        "auto": sum(1 for s in proj.steps for r in s.redactions if r.auto),
        "indexed": sum(len(s.text_nodes) for s in proj.steps),
    }
