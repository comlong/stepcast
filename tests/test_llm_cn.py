"""Chinese LLM providers (Doubao, Qwen, GLM, MiniMax): endpoints, default models, thinking switches, <think> removal, unsupported parameters dropped automatically (offline)."""
import json
import os
import shutil
import sys
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "llm_cn_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA / "projects")
for k in ("ARK_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY", "MINIMAX_API_KEY"):
    os.environ.pop(k, None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"
config._cache = None
config.save({"ui_language": "zh"})
from backend.services import llm, llm_openai  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail != "" else ""), flush=True)
    cond or fails.append(name)


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


SENT = []
REPLY = {"content": '{"ok": 1}', "status": 200, "reject": None}


def fake_post(url, headers=None, json=None, timeout=None):
    SENT.append((url, headers, dict(json)))
    rej = REPLY["reject"]
    if rej and rej in json:
        return Resp(400, {"error": {"message": f"Unrecognized request argument supplied: {rej}"}})
    return Resp(REPLY["status"], {"choices": [{"message": {"content": REPLY["content"]}, "finish_reason": "stop"}]})


llm_openai.requests.post = fake_post

EXPECT = {
    "doubao": ("https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-1-pro-260628", ("thinking", {"type": "disabled"})),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", ("enable_thinking", False)),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-5", ("thinking", {"type": "disabled"})),
    "minimax": ("https://api.minimax.cn/v1", "MiniMax-M3", ("reasoning_split", True)),
}
print("\n== 1. 四家的请求 ==")
for pid, (base, model, (pk, pv)) in EXPECT.items():
    SENT.clear()
    c = llm.get_client(pid, api_key="sk-test-123")
    out = c.chat_json([{"role": "user", "content": "给我 JSON"}])
    url, headers, payload = SENT[-1]
    check(f"{pid}：地址、默认模型、关思考、Bearer 鉴权",
          url == base + "/chat/completions" and payload["model"] == model and payload.get(pk) == pv
          and headers["Authorization"] == "Bearer sk-test-123" and out == {"ok": 1},
          (url, payload["model"], payload.get(pk)))
check("DeepSeek 照旧", (lambda c: (SENT.clear(), c.chat("hi"), SENT[-1][2]["model"])[2])(llm.get_client("deepseek", api_key="k"))
      == "deepseek-flash")

print("\n== 2. 推理模型的 <think> ==")
REPLY["content"] = '<think>先想想 {"不是": "答案"}</think>\n{"title": "好"}'
c = llm.get_client("minimax", api_key="k")
check("JSON 里的思考过程去掉", c.chat_json([{"role": "user", "content": "x"}]) == {"title": "好"})
check("普通文字里的思考过程也去掉", c.chat([{"role": "user", "content": "x"}]) == '{"title": "好"}')
REPLY["content"] = '{"ok": 1}'

print("\n== 3. 模型不认关思考的参数：自动去掉重试 ==")
for pid, key in (("qwen", "enable_thinking"), ("minimax", "reasoning_split"), ("glm", "thinking")):
    SENT.clear()
    REPLY["reject"] = key
    out = llm.get_client(pid, api_key="k").chat_json([{"role": "user", "content": "x"}])
    check(f"{pid}：去掉 {key} 后成功", out == {"ok": 1} and key in SENT[0][2] and key not in SENT[-1][2], len(SENT))
REPLY["reject"] = None

print("\n== 4. 没填 Key、环境变量 ==")
try:
    llm.get_client("glm")
    check("没填 Key 提示", False)
except llm.LLMError as e:
    check("没填 Key 提示", "智谱 GLM" in str(e) and "ZHIPUAI_API_KEY" in str(e), str(e))
os.environ["DASHSCOPE_API_KEY"] = "sk-env-9"
SENT.clear()
llm.get_client("qwen").chat("x")
check("环境变量里的 Key 也认", SENT[-1][1]["Authorization"] == "Bearer sk-env-9")
os.environ.pop("DASHSCOPE_API_KEY")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
