"""Print exactly what would be sent to the LLM, without going online.

A fake client replaces the real model, captures the messages to be sent and aborts, so no credits or API key are needed.
The message content is the same for every provider; only the outer protocol (OpenAI-compatible / Claude) differs.

    python tools/dump_llm_payload.py                # the most recent project
    python tools/dump_llm_payload.py p_xxxxxxxx     # a specific project
    python tools/dump_llm_payload.py --save out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import storage
from backend.services import llm, script_gen


class Intercepted(BaseException):
    """Raised after capturing a request so it is never sent. Derived from BaseException so retry logic can't swallow it."""

    def __init__(self, payload):
        self.payload = payload


class CapturingClient:
    """Same interface as the real client, but records instead of sending."""

    def __init__(self):
        r = llm.resolve()
        self.provider, self.name, self.model = r.preset.id, r.preset.name, r.model
        self.url = r.base_url or ("https://api.anthropic.com" if r.preset.kind == "anthropic" else "")

    def chat(self, messages, temperature=0.6, json_mode=False, **kw):
        raise Intercepted({"provider": self.name, "url": self.url, "model": self.model,
                           "temperature": temperature, "json_mode": json_mode, "messages": messages})

    def chat_json(self, messages, **kw):
        return self.chat(messages, json_mode=True, **kw)


def capture(fn, *args, **kw):
    """Run one action that calls the LLM and return what it would have sent."""
    try:
        fn(*args, **kw)
        return None
    except Intercepted as e:
        return e.payload


def show(name, purpose, used_for, payload):
    print("\n" + "=" * 78)
    print(f"  【{name}】")
    print(f"  目的：{purpose}")
    print(f"  结果用在：{used_for}")
    print("=" * 78)
    if not payload:
        print("  （这次没有产生网络请求）")
        return
    print(f"发往：{payload['provider']}  {payload['url']}")
    print(f"model={payload['model']}  temperature={payload['temperature']}  "
          f"要求 JSON 输出={payload['json_mode']}")
    print()
    for m in payload["messages"]:
        print(f"---------- role: {m['role']} ----------")
        print(m["content"])
        print()
    size = len(json.dumps(payload["messages"], ensure_ascii=False))
    print(f"[请求体共 {size} 字符；其中不含任何图片 —— 截图始终留在本机]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id", nargs="?", default="")
    ap.add_argument("--save", default="", help="同时把原始 JSON 存到文件")
    args = ap.parse_args()

    pid = args.project_id
    if not pid:
        lst = storage.list_projects()
        if not lst:
            print("没有项目。先录一段，或先跑 python tools/selftest.py --no-tts")
            return 1
        pid = lst[0]["id"]
    proj = storage.load(pid)
    if not proj:
        print(f"项目不存在：{pid}")
        return 1
    print(f"项目：{proj.name}（{pid}），{len(proj.steps)} 步")

    client = CapturingClient()      # no key needed, the request is intercepted
    print(f"当前设置的模型：{client.name} / {client.model}")

    dumps = {}

    if proj.source != "slides":
        dumps["script"] = capture(
            script_gen.generate_script, proj, style="friendly",
            overwrite=True, client=client)
        show("① 生成解说脚本", "让模型看懂这串操作在干什么，给整片起标题、写片头片尾、给每步写旁白和字幕",
             "项目标题 / 副标题 / 片头 / 片尾 / 每步的 title+narration+caption；"
             "narration 随后被送去配音，caption 变成字幕",
             dumps["script"])

    if proj.source == "slides":
        dumps["slides"] = capture(
            script_gen.generate_slides_script, proj,
            notes_mode=(proj.settings or {}).get("slides_notes_mode", "verbatim"),
            detail=(proj.settings or {}).get("slides_detail", "standard"),
            missing=(proj.settings or {}).get("slides_missing", "ai"),
            overwrite=True, client=client)
        show("①b 幻灯片解说", "根据每页标题、页面文字、演讲者备注写讲师旁白",
             "每页的 title + narration；备注原文模式下有备注的页不会出现在请求里",
             dumps["slides"])

    if proj.steps:
        dumps["rewrite"] = capture(
            script_gen.rewrite_step, proj, proj.steps[0],
            "更简短一点", client=client)
        show("② AI 改写单句", "按你在编辑器里打的要求，重写某一步的旁白",
             "只替换这一步的 narration / caption，并作废它已生成的语音",
             dumps["rewrite"])

    dumps["translate"] = capture(
        script_gen.translate_project, proj, "en-US", apply=False, client=client)
    show("③ 翻译", "把已有的中文脚本整体译成目标语言",
         "替换标题 / 片头片尾 / 每步旁白，并换成目标语言的音色重新配音",
         dumps["translate"])

    print("\n" + "=" * 78)
    print("  不会发送的东西")
    print("=" * 78)
    print("""  · 截图 / 幻灯片图片（PNG/JPEG）—— 从头到尾只存在本机
  · 你的录音 —— 语音识别用本机的 faster-whisper，不经过任何云服务
  · 音频文件、成片 MP4
  · 元素的 CSS selector、DOM 结构、页面 HTML
  · Cookie、localStorage、请求头、页面上的其他文字
  · 密码框内容 —— 扩展在录制阶段就已替换成 ••••••••，本机存的也是掩码""")

    if args.save:
        Path(args.save).write_text(
            json.dumps(dumps, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n原始 JSON 已写入 {args.save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
