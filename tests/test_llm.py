"""多家大模型接入：配置保存、OpenAI 兼容协议、Claude（SDK + 流式）、停止、端到端生成解说。

全程用本机假服务器，不联网、不花任何额度。
"""
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SP = Path(sys.argv[1])
DATA = SP / "llm_data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir(parents=True)
os.environ["VT_DATA_DIR"] = str(DATA)
for var in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "AZURE_OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
    os.environ.pop(var, None)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, main, storage  # noqa: E402
from backend.services import jobs, llm, llm_openai, script_gen  # noqa: E402
import selftest  # noqa: E402

config.CONFIG_PATH = DATA / "config.json"     # 不碰真实的 config.json
config.DEFAULTS["ui_language"] = "zh"      # 下面按中文报错文字核对
config._cache = None

c = TestClient(main.app, base_url="http://127.0.0.1:8756", headers={"Origin": "http://127.0.0.1:8756"})
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


def wait(j, timeout=60):
    assert "id" in j, j
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/jobs/{j['id']}").json()
        if r["status"] not in ("pending", "running"):
            return r
        time.sleep(0.1)
    raise TimeoutError


def save_settings(body):
    r = c.post("/api/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def reset_config():
    if config.CONFIG_PATH.exists():
        config.CONFIG_PATH.unlink()
    config._cache = None


SCRIPT_REPLY = {"title": "假模型标题", "subtitle": "副标题", "intro": "片头。", "outro": "片尾。",
                "steps": [{"i": i, "title": f"第{i}步", "narration": f"假模型写的第 {i} 步解说。",
                           "caption": f"假模型写的第 {i} 步解说。"} for i in range(3)]}


# ================================================================ 假 OpenAI 兼容服务
class OAI(BaseHTTPRequestHandler):
    log = []
    script = []          # 依次返回的 (status, body)；空了就正常回答

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        OAI.log.append({"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "fake-large"}, {"id": "fake-small"}]})
        self._send(404, {"error": "nope"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        OAI.log.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if OAI.script:
            status, reply = OAI.script.pop(0)
            if status != 200:
                return self._send(status, reply)
        prompt = body["messages"][-1]["content"]
        if "Reply with" in prompt:
            content = "可用"
        elif body.get("response_format") or "写解说" in prompt or "JSON" in json.dumps(body["messages"], ensure_ascii=False):
            content = json.dumps(SCRIPT_REPLY, ensure_ascii=False)
        else:
            content = "改写后的解说。"
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}]})


# ================================================================ 假 Claude（Messages API，SSE 流式）
class ANT(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    log = []
    mode = "ok"          # ok | refusal | slow | 401
    disconnected = threading.Event()

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        ANT.log.append({"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        if ANT.mode == "401":
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        path = self.path.split("?")[0]
        model = {"type": "model", "id": "claude-opus-5", "display_name": "Claude Opus 5",
                 "created_at": "2026-01-01T00:00:00Z"}
        if path == "/v1/models":
            return self._json(200, {"data": [model, {**model, "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"}],
                                    "has_more": False, "first_id": "claude-opus-5", "last_id": "claude-sonnet-5"})
        if path.startswith("/v1/models/"):
            mid = path.rsplit("/", 1)[1]
            if mid not in ("claude-opus-5", "claude-sonnet-5"):
                return self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "model not found"}})
            return self._json(200, {**model, "id": mid})
        self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": "nope"}})

    def _event(self, name, data):
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ANT.log.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if ANT.mode == "401":
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._event("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"], "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}})
        if ANT.mode == "refusal":
            self._event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None},
                                          "usage": {"output_tokens": 1}})
            self._event("message_stop", {"type": "message_stop"})
            return
        self._event("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}})
        if ANT.mode == "slow":
            try:
                for _ in range(100):
                    self._event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                        "delta": {"type": "text_delta", "text": "慢"}})
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                ANT.disconnected.set()
            return
        system = body.get("system", "")
        text = json.dumps(SCRIPT_REPLY, ensure_ascii=False) if "JSON" in system else "Claude 改写后的解说。"
        for i in range(0, len(text), 40):         # 分片发送，验证流式拼接
            self._event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": text[i:i + 40]}})
        self._event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                      "usage": {"output_tokens": 50}})
        self._event("message_stop", {"type": "message_stop"})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


oai_srv, OAI_URL = serve(OAI)
ant_srv, ANT_URL = serve(ANT)

# ---------------------------------------------------------------- 1. 老配置兼容
print("\n== 1. 老版本只配了 DeepSeek 的用户，升级后照常能用 ==")
reset_config()
config.CONFIG_PATH.write_text(json.dumps({"deepseek_api_key": "sk-legacy-1234567890abcdef",
                                          "deepseek_model": "deepseek-reasoner"}), encoding="utf-8")
config._cache = None
r = llm.resolve()
check("默认仍是 DeepSeek", r.preset.id == "deepseek")
check("读到老的 Key；老模型名统一显示成 deepseek",
      r.api_key == "sk-legacy-1234567890abcdef" and r.model == "deepseek", r)
h = c.get("/api/health").json()
check("健康检查显示已配置", h["llm"] == {"provider": "deepseek", "name": "DeepSeek", "model": "deepseek",
                                    "configured": True}, h.get("llm"))
s = c.get("/api/settings").json()
check("设置接口不泄露 Key", "sk-legacy-1234567890abcdef" not in json.dumps(s), "")
ds = next(p for p in s["llm"]["providers"] if p["id"] == "deepseek")
check("只给掩码", ds["key_masked"] == "sk-leg…cdef", ds["key_masked"])
check("列出 12 家服务商（含豆包、通义、GLM、MiniMax）", [p["id"] for p in s["llm"]["providers"]] ==
      ["deepseek", "doubao", "qwen", "glm", "minimax", "openai", "anthropic", "mistral", "gemini", "azure", "ollama", "custom"])

# ---------------------------------------------------------------- 2. 保存设置
print("\n== 2. 切换服务商、保存 Key ==")
s = save_settings({"llm_provider": "mistral",
                   "llm_providers": {"mistral": {"api_key": "mk-secret-abcdefghijklmnop", "model": "mistral-small-latest"}}})
check("切到 Mistral", s["llm"]["provider"] == "mistral" and s["llm"]["model"] == "mistral-small-latest", s["llm"]["provider"])
check("返回里没有明文 Key", "mk-secret-abcdefghijklmnop" not in json.dumps(s))
raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
check("Key 存进了本机 config.json", raw["llm_providers"]["mistral"]["api_key"] == "mk-secret-abcdefghijklmnop")
check("DeepSeek 的老 Key 没被动", raw["deepseek_api_key"] == "sk-legacy-1234567890abcdef")
s = save_settings({"llm_provider": "mistral", "llm_providers": {"mistral": {"model": "mistral-large-latest", "api_key": ""}}})
raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
check("只改模型不会清掉 Key", raw["llm_providers"]["mistral"].get("api_key") == "mk-secret-abcdefghijklmnop"
      and raw["llm_providers"]["mistral"]["model"] == "mistral-large-latest")
s = save_settings({"llm_provider": "hacker", "llm_providers": {"hacker": {"api_key": "x"}}})
raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
check("不认识的服务商被忽略", raw["llm_provider"] == "mistral" and "hacker" not in raw["llm_providers"])
s = save_settings({"llm_providers": {"mistral": {"clear_key": True}}})
raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
check("可以清除 Key", "api_key" not in raw["llm_providers"]["mistral"])
check("清除后显示未配置", s["llm"]["configured"] is False)
os.environ["MISTRAL_API_KEY"] = "env-mistral-key-0000000000"
s = c.get("/api/settings").json()
mi = next(p for p in s["llm"]["providers"] if p["id"] == "mistral")
check("环境变量里的 Key 生效并标明来源", mi["configured"] and mi["key_env"] == "MISTRAL_API_KEY", mi["key_env"])
os.environ.pop("MISTRAL_API_KEY")

print("\n== 3. 缺配置时的提示 ==")
for pid, expect in (("openai", "未配置 OpenAI 的 API Key"), ("azure", "未配置 Azure OpenAI 的 API Key")):
    try:
        llm.get_client(pid)
        check(f"{pid} 缺 Key 报错", False)
    except llm.LLMError as e:
        check(f"{pid} 缺 Key 报错", expect in str(e), str(e))
try:
    llm.get_client("azure", api_key="k")
    check("Azure 缺接口地址报错", False)
except llm.LLMError as e:
    check("Azure 缺接口地址报错", "接口地址" in str(e), str(e))
check("本地 Ollama 不需要 Key", llm.get_client("ollama").model == "qwen3:8b")

# ---------------------------------------------------------------- 4. OpenAI 兼容协议
print("\n== 4. OpenAI 兼容协议（OpenAI / Azure / 自定义）==")
OAI.log.clear()
cl = llm.get_client("openai", api_key="sk-oai-test", base_url=OAI_URL + "/v1", model="gpt-5-mini")
out = cl.chat_json([{"role": "system", "content": "sys"}, {"role": "user", "content": "写解说"}], temperature=0.7)
req = OAI.log[-1]
check("请求路径 /v1/chat/completions", req["path"] == "/v1/chat/completions", req["path"])
check("Bearer 鉴权", req["headers"].get("authorization") == "Bearer sk-oai-test")
check("OpenAI 用 max_completion_tokens，并给推理模型留足额度",
      req["body"].get("max_completion_tokens") == 16000 and "max_tokens" not in req["body"], req["body"].keys())
check("JSON 模式", req["body"].get("response_format") == {"type": "json_object"})
check("解析出 JSON", out["title"] == "假模型标题")

OAI.log.clear()
OAI.script = [(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.7 with this model."}})]
cl.chat([{"role": "user", "content": "改写"}], temperature=0.7)
check("不支持 temperature 时自动去掉重试", len(OAI.log) == 2 and "temperature" not in OAI.log[1]["body"],
      [list(x["body"].keys()) for x in OAI.log])

OAI.log.clear()
cm = llm.get_client("custom", base_url=OAI_URL + "/v1", model="some-model")
OAI.script = [(400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                                          "Use 'max_completion_tokens' instead."}}),
              (400, {"error": {"message": "response_format json_object is not supported"}})]
out = cm.chat_json([{"role": "user", "content": "写解说"}])
check("自定义接口：自动换成 max_completion_tokens、去掉 JSON 模式", len(OAI.log) == 3
      and "max_completion_tokens" in OAI.log[2]["body"] and "response_format" not in OAI.log[2]["body"]
      and out["title"] == "假模型标题", [list(x["body"].keys()) for x in OAI.log])
check("没填 Key 时不发鉴权头", "authorization" not in OAI.log[0]["headers"])

OAI.log.clear()
OAI.script = [(429, {"error": "slow down"})]
t0 = time.time()
cm.chat([{"role": "user", "content": "改写"}])
check("429 会等一会儿再重试", len(OAI.log) == 2 and time.time() - t0 >= 2.5, f"{time.time() - t0:.1f}s")

OAI.script = [(401, {"error": {"message": "Incorrect API key"}})]
try:
    cm.chat([{"role": "user", "content": "改写"}])
    check("401 报鉴权失败", False)
except llm.LLMError as e:
    check("401 报鉴权失败", "鉴权失败" in str(e), str(e))
OAI.script = []

OAI.log.clear()
az = llm.get_client("azure", api_key="azure-key", base_url=OAI_URL + "/openai/v1", model="my-deploy")
az.chat([{"role": "user", "content": "改写"}])
check("Azure 用 api-key 头", OAI.log[-1]["headers"].get("api-key") == "azure-key"
      and "authorization" not in OAI.log[-1]["headers"] and OAI.log[-1]["body"]["model"] == "my-deploy")

r = c.post("/api/settings/llm-models", json={"provider": "custom", "base_url": OAI_URL + "/v1"}).json()
check("获取模型列表", r["ok"] and r["models"] == ["fake-large", "fake-small"], r)
r = c.post("/api/settings/test-key", json={"provider": "custom", "base_url": OAI_URL + "/v1", "model": "fake-large"}).json()
check("测试连接", r["ok"] and r["message"] == "可用" and r["model"] == "fake-large", r)

# ---------------------------------------------------------------- 5. Claude
print("\n== 5. Claude（官方 SDK，流式）==")
ANT.log.clear()
ANT.mode = "ok"
cc = llm.get_client("anthropic", api_key="sk-ant-test", base_url=ANT_URL, model="claude-opus-5")
out = cc.chat_json([{"role": "system", "content": "你是解说员"}, {"role": "user", "content": "写解说"}], temperature=0.7)
req = ANT.log[-1]
body = req["body"]
check("请求 /v1/messages", req["path"].startswith("/v1/messages"), req["path"])
check("x-api-key 鉴权", req["headers"].get("x-api-key") == "sk-ant-test")
check("system 单独传，并要求只输出 JSON", "你是解说员" in body.get("system", "") and "JSON" in body["system"]
      and all(m["role"] != "system" for m in body["messages"]))
check("不发 temperature（新模型会拒绝）", "temperature" not in body)
check("流式请求、输出额度给足", body.get("stream") is True and body["max_tokens"] == 64000)
check("claude-opus-5 开启被拒时自动换模型兜底", body.get("fallbacks") == "default"
      and "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", ""),
      (body.get("fallbacks"), req["headers"].get("anthropic-beta")))
check("分片流式结果拼回完整 JSON", out == SCRIPT_REPLY)

ANT.log.clear()
cs = llm.get_client("anthropic", api_key="sk-ant-test", base_url=ANT_URL, model="claude-sonnet-5")
txt = cs.chat([{"role": "user", "content": "改写"}], max_tokens=500)
check("claude-sonnet-5 不带兜底参数", "fallbacks" not in ANT.log[-1]["body"]
      and "server-side-fallback" not in ANT.log[-1]["headers"].get("anthropic-beta", ""))
check("普通文本返回", txt == "Claude 改写后的解说。", txt)

ANT.mode = "refusal"
try:
    cc.chat([{"role": "user", "content": "x"}])
    check("拒绝时给出明确提示", False)
except llm.LLMError as e:
    check("拒绝时给出明确提示", "拒绝" in str(e), str(e))

ANT.mode = "401"
try:
    cc.chat([{"role": "user", "content": "x"}])
    check("Claude 401 报鉴权失败", False)
except llm.LLMError as e:
    check("Claude 401 报鉴权失败", "鉴权失败" in str(e), str(e))
ANT.mode = "ok"

ANT.log.clear()
r = llm.test_connection("anthropic", api_key="sk-ant-test", base_url=ANT_URL, model="claude-opus-5")
check("测试连接只查模型信息，不生成（不花 token）", r["ok"] and "Claude Opus 5" in r["message"]
      and all(x["method"] == "GET" for x in ANT.log), r)
r = llm.test_connection("anthropic", api_key="sk-ant-test", base_url=ANT_URL, model="claude-opus-9")
check("模型名写错时提示", not r["ok"] and "claude-opus-9" in r["message"], r)
r = llm.list_models("anthropic", api_key="sk-ant-test", base_url=ANT_URL)
check("Claude 模型列表", r["ok"] and r["models"] == ["claude-opus-5", "claude-sonnet-5"], r)

# ---------------------------------------------------------------- 6. 停止
print("\n== 6. 生成中点停止 ==")
ANT.mode = "slow"
ANT.disconnected.clear()


def slow_job(job):
    cc.chat([{"role": "user", "content": "写很长的解说"}])


j = jobs.submit("script", slow_job, project_id="p_000000000000")
time.sleep(1.2)
t0 = time.time()
jobs.cancel(j["id"])
while jobs.get(j["id"])["status"] in ("pending", "running"):
    time.sleep(0.05)
secs = time.time() - t0
check("Claude 流式生成中停止，1 秒内停下", jobs.get(j["id"])["status"] == "cancelled" and secs < 1.0,
      f"{jobs.get(j['id'])['status']} {secs:.2f}s")
check("连接被关掉，服务端不再继续生成", ANT.disconnected.wait(3))
ANT.mode = "ok"

# ---------------------------------------------------------------- 7. 端到端
print("\n== 7. 端到端：在设置里换服务商后生成解说、AI 改写 ==")
reset_config()
save_settings({"llm_provider": "custom", "llm_providers": {"custom": {"base_url": OAI_URL + "/v1", "model": "fake-large"}}})
pid = selftest.build_project("多模型")
c.patch(f"/api/projects/{pid}/steps/{storage.load(pid).steps[0].id}", json={"narration": "", "caption": ""})
OAI.log.clear()
r = wait(c.post(f"/api/projects/{pid}/script", json={"overwrite": True}).json())
p = storage.load(pid)
check("自定义接口生成解说成功", r["status"] == "done" and p.steps[0].narration == "假模型写的第 0 步解说。",
      r.get("error") or p.steps[0].narration)
check("请求发到了设置里填的地址", OAI.log and OAI.log[0]["path"] == "/v1/chat/completions")

save_settings({"llm_provider": "anthropic",
               "llm_providers": {"anthropic": {"api_key": "sk-ant-e2e-0000000000", "base_url": ANT_URL,
                                               "model": "claude-opus-5"}}})
ANT.log.clear()
r = wait(c.post(f"/api/projects/{pid}/script", json={"overwrite": True}).json())
p = storage.load(pid)
check("切到 Claude 后生成解说成功", r["status"] == "done" and p.title == "假模型标题", r.get("error"))
check("用的是 Claude", ANT.log and ANT.log[-1]["headers"].get("x-api-key") == "sk-ant-e2e-0000000000")
sid = p.steps[1].id
r = c.post(f"/api/projects/{pid}/steps/{sid}/rewrite", json={"instruction": "更简短"})
check("AI 改写也走 Claude", r.status_code == 200 and storage.load(pid).steps[1].narration == "Claude 改写后的解说。",
      r.text[:200])

h = c.get("/api/health").json()
check("顶栏显示 Claude", h["llm"]["name"].startswith("Claude") and h["llm"]["configured"], h["llm"])

print("\n== 8. 查看发送内容的工具 ==")
import subprocess  # noqa: E402
env = {**os.environ, "VT_DATA_DIR": str(DATA)}
out = subprocess.run([sys.executable, str(ROOT / "tools" / "dump_llm_payload.py"), pid],
                     capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
check("dump_llm_payload 能跑、不联网", out.returncode == 0 and "role: system" in out.stdout
      and "不会发送的东西" in out.stdout, (out.stdout[-300:] + out.stderr[-500:]))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：{fails}"))
