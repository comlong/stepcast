"""LLM access: one provider is chosen in the settings and used for writing narration, translating and AI rewrites.

Two protocols:
  * OpenAI-compatible (/chat/completions): DeepSeek, OpenAI, Mistral, Gemini, Azure OpenAI, local Ollama, other compatible services
  * Anthropic Claude: the official SDK

Settings live in config.json as llm_provider + llm_providers[{id}] = {api_key, base_url, model};
the old deepseek_api_key / deepseek_model fields still work.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from .. import config, i18n
from ..i18n import N_
from . import jobs


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    kind: str                         # openai (OpenAI-compatible protocol) | anthropic
    base_url: str = ""
    model: str = ""
    models: Tuple[str, ...] = ()      # dropdown suggestions; "Fetch model list" gets the full list
    env: Tuple[str, ...] = ()         # keys from environment variables take precedence over the settings page
    key_url: str = ""
    hint: str = ""                    # where data is processed and how to get a key
    needs_key: bool = True
    base_url_required: bool = False   # no public endpoint, must be filled in
    base_url_hint: str = ""
    auth: str = "bearer"              # bearer | azure (api-key header)
    token_floor: int = 0              # reasoning models count their thinking in max_tokens; a floor keeps the output from being cut off
    new_token_param: bool = False     # use max_completion_tokens instead of max_tokens
    params: Tuple[Tuple[str, Any], ...] = ()   # extra parameters sent with every request (e.g. to switch thinking off)


PRESETS: Dict[str, Preset] = {p.id: p for p in (
    Preset("deepseek", "DeepSeek", "openai", "https://api.deepseek.com", "deepseek",
           ("deepseek",), ("DEEPSEEK_API_KEY",),
           "https://platform.deepseek.com/api_keys",
           N_("中国公司，数据在中国境内处理。")),
    # The Chinese providers: all OpenAI-compatible. Writing narration doesn't need "deep thinking" (slow, expensive), so switch it off where possible
    Preset("doubao", N_("豆包（火山方舟）"), "openai", "https://ark.cn-beijing.volces.com/api/v3",
           "doubao-seed-2-1-pro-260628", ("doubao-seed-2-1-pro-260628", "doubao-seed-2-0-lite-260215"),
           ("ARK_API_KEY",), "https://console.volcengine.com/ark",
           N_("字节跳动，数据在中国境内处理。模型 ID 在火山方舟控制台的「模型广场」里复制，要先在控制台开通这个模型。"),
           params=(("thinking", {"type": "disabled"}),)),
    Preset("qwen", N_("通义千问（阿里云百炼）"), "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1",
           "qwen-plus", ("qwen-plus", "qwen-max", "qwen-flash"), ("DASHSCOPE_API_KEY",),
           "https://bailian.console.aliyun.com/",
           N_("阿里云，数据在中国境内处理（北京地域）。API Key 在阿里云百炼控制台申请。"),
           params=(("enable_thinking", False),)),
    Preset("glm", N_("智谱 GLM"), "openai", "https://open.bigmodel.cn/api/paas/v4", "glm-5",
           ("glm-5", "glm-4.7", "glm-4.5-air"), ("ZHIPUAI_API_KEY",),
           "https://open.bigmodel.cn/usercenter/apikeys",
           N_("智谱 AI，数据在中国境内处理。"),
           params=(("thinking", {"type": "disabled"}),)),
    Preset("minimax", "MiniMax", "openai", "https://api.minimax.cn/v1", "MiniMax-M3",
           ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"), ("MINIMAX_API_KEY",),
           "https://platform.minimaxi.com/",
           N_("上海稀宇科技，数据在中国境内处理。模型总是先思考再回答，比别家慢一些。"),
           token_floor=16000, params=(("reasoning_split", True),)),
    Preset("openai", "OpenAI", "openai", "https://api.openai.com/v1", "gpt-5-mini",
           ("gpt-5", "gpt-5-mini", "gpt-4.1"), ("OPENAI_API_KEY",),
           "https://platform.openai.com/api-keys",
           N_("美国公司。需要数据留在欧盟可以改用 Azure OpenAI 的欧洲区域，或者 Mistral。"),
           token_floor=16000, new_token_param=True),
    Preset("anthropic", N_("Claude（Anthropic）"), "anthropic", "", "claude-opus-5",
           ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"),
           ("ANTHROPIC_API_KEY",), "https://console.anthropic.com/settings/keys",
           N_("美国公司。claude-opus-5 质量最好；claude-sonnet-5 更快更便宜；claude-haiku-4-5 最便宜。")),
    Preset("mistral", N_("Mistral AI（法国）"), "openai", "https://api.mistral.ai/v1", "mistral-large-latest",
           ("mistral-large-latest", "mistral-medium-latest", "mistral-small-latest"), ("MISTRAL_API_KEY",),
           "https://console.mistral.ai/api-keys",
           N_("欧洲公司，API 数据默认在欧盟境内处理（以 Mistral 官方条款为准）。")),
    Preset("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
           "gemini-2.5-flash", ("gemini-2.5-flash", "gemini-2.5-pro"), ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
           "https://aistudio.google.com/apikey",
           N_("美国公司。免费档的数据可能被用于改进模型，处理客户资料请用付费档。"),
           token_floor=16000),
    Preset("azure", "Azure OpenAI", "openai", "", "", (), ("AZURE_OPENAI_API_KEY",),
           "https://portal.azure.com",
           N_("在欧洲区域（如 Sweden Central、France Central）建资源并用 EU Data Zone 部署，数据可以留在欧盟。模型名填你的部署名。"),
           base_url_required=True, base_url_hint=N_("https://你的资源名.openai.azure.com/openai/v1"),
           auth="azure", token_floor=16000, new_token_param=True),
    Preset("ollama", N_("本地 Ollama"), "openai", "http://localhost:11434/v1", "qwen3:8b",
           ("qwen3:8b", "qwen3:14b", "llama3.1:8b", "mistral-small3.2"), (),
           "https://ollama.com/download",
           N_("完全在本机运行，什么都不发出去；质量取决于模型大小和电脑配置。先在 Ollama 里 pull 好模型。"),
           needs_key=False),
    Preset("custom", N_("其他 OpenAI 兼容接口"), "openai", "", "", (), (), "",
           N_("OpenRouter、LM Studio、vLLM、Scaleway、IONOS 等只要兼容 OpenAI 的 /chat/completions 都能用。"),
           needs_key=False, base_url_required=True, base_url_hint="https://…/v1"),
)}

DEFAULT_PROVIDER = "deepseek"


class ChatClient(Protocol):
    provider: str
    name: str
    model: str

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.6, json_mode: bool = False,
             max_tokens: int = 8000, timeout: int = 180, retries: int = 2) -> str: ...

    def chat_json(self, messages: List[Dict[str, str]], **kw) -> Any: ...


@dataclass
class Resolved:
    preset: Preset
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    key_source: str = ""              # config | env:XXX | ""
    extra: Dict[str, Any] = field(default_factory=dict)


# DeepSeek renamed its models several times (deepseek-chat / deepseek-reasoner were retired on 2026-07-24, later renamed to deepseek-flash),
# and new models think by default (slower, more expensive). The settings show a single "deepseek" without -chat / -flash suffixes:
# requests use DeepSeek's current model name with thinking off, which behaves like the old deepseek-chat conversation mode.
# Names stored in old settings are treated as "deepseek" automatically; users don't need to change anything.
DEEPSEEK_MODEL = "deepseek"                 # shown in the settings
DEEPSEEK_API_MODEL = "deepseek-flash"       # actually sent
DEEPSEEK_LEGACY = {"", "deepseek", "deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-v4-flash"}


def request_model(r: Resolved) -> Tuple[str, Dict[str, Any]]:
    """Model name and extra parameters actually sent to the provider. The model name shown in the settings stays the same."""
    if r.preset.id != "deepseek":
        return r.model, {k: v for k, v in r.preset.params}
    model = DEEPSEEK_API_MODEL if r.model == DEEPSEEK_MODEL else r.model
    return model, {"thinking": {"type": "disabled"}}


# ---- settings ------------------------------------------------------------------

def provider_id(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else config.load()
    pid = cfg.get("llm_provider") or DEFAULT_PROVIDER
    return pid if pid in PRESETS else DEFAULT_PROVIDER


def resolve(pid: str = "", cfg: Optional[Dict[str, Any]] = None, api_key: str = "",
            base_url: str = "", model: str = "") -> Resolved:
    """Defaults < old deepseek_* fields < saved llm_providers values < values passed to this call; keys also come from environment variables."""
    cfg = cfg if cfg is not None else config.load()
    pid = pid if pid in PRESETS else provider_id(cfg)
    p = PRESETS[pid]
    saved = dict((cfg.get("llm_providers") or {}).get(pid) or {})
    r = Resolved(p, base_url=p.base_url, model=p.model)
    if pid == "deepseek":
        r.base_url = cfg.get("deepseek_base_url") or r.base_url
        r.model = cfg.get("deepseek_model") or r.model
        if cfg.get("deepseek_api_key"):
            r.api_key, r.key_source = cfg["deepseek_api_key"], "config"
    if saved.get("base_url"):
        r.base_url = saved["base_url"]
    if saved.get("model"):
        r.model = saved["model"]
    if saved.get("api_key"):
        r.api_key, r.key_source = saved["api_key"], "config"
    for var in p.env:
        if os.environ.get(var):
            r.api_key, r.key_source = os.environ[var], f"env:{var}"
            break
    if api_key:
        r.api_key, r.key_source = api_key, "input"
    if base_url:
        r.base_url = base_url
    if model:
        r.model = model
    r.base_url = (r.base_url or "").strip().rstrip("/")
    r.model = (r.model or "").strip()
    if pid == "deepseek" and r.model.lower() in DEEPSEEK_LEGACY:
        r.model = DEEPSEEK_MODEL      # deepseek-chat etc. stored in old settings are always shown as "deepseek"
    return r


def is_configured(r: Resolved) -> bool:
    if r.preset.base_url_required and not r.base_url:
        return False
    if not r.model:
        return False
    return bool(r.api_key) or not r.preset.needs_key


def display_name(cfg: Optional[Dict[str, Any]] = None) -> str:
    return i18n.t(PRESETS[provider_id(cfg)].name)


def get_client(pid: str = "", api_key: str = "", base_url: str = "", model: str = "") -> ChatClient:
    r = resolve(pid, api_key=api_key, base_url=base_url, model=model)
    p = r.preset
    name = i18n.t(p.name)
    if p.needs_key and not r.api_key:
        if p.env:
            raise LLMError(i18n.t("未配置 {name} 的 API Key。请在界面右上角「设置 → AI 与语音」里填写，或设置环境变量 {env}。",
                                  name=name, env=p.env[0]))
        raise LLMError(i18n.t("未配置 {name} 的 API Key。请在界面右上角「设置 → AI 与语音」里填写。", name=name))
    if p.base_url_required and not r.base_url:
        raise LLMError(i18n.t("{name} 需要填写接口地址（设置 → AI 与语音）。", name=name))
    if not r.model:
        raise LLMError(i18n.t("{name} 还没有选模型（设置 → AI 与语音）。", name=name))
    if p.kind == "anthropic":
        from .llm_anthropic import AnthropicClient
        return AnthropicClient(r)
    from .llm_openai import OpenAICompatClient
    return OpenAICompatClient(r)


# ---- for the UI ----------------------------------------------------------------

def _mask(key: str) -> str:
    if not key:
        return ""
    return (key[:6] + "…" + key[-4:]) if len(key) > 12 else i18n.t("已设置")


def public_state(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """For the settings page: each provider's preset and saved values. Keys are only returned masked."""
    cfg = cfg if cfg is not None else config.load()
    pid = provider_id(cfg)
    saved = cfg.get("llm_providers") or {}
    out = []
    for i, p in PRESETS.items():
        r = resolve(i, cfg)
        d = asdict(p)
        d.update(name=i18n.t(p.name), hint=i18n.t(p.hint),
                 base_url_hint=i18n.t(p.base_url_hint) if p.base_url_hint else "")
        d.update(
            saved_model=r.model, saved_base_url=r.base_url if r.base_url != p.base_url else "",
            key_masked=_mask(r.api_key),
            key_env=r.key_source[4:] if r.key_source.startswith("env:") else "",
            # providers without a key (Ollama etc.) only count as configured when really set up or in use, so they don't show ✓ when not installed
            configured=is_configured(r) and (bool(r.api_key) or i in saved or i == pid),
        )
        out.append(d)
    cur = resolve(pid, cfg)
    return {"provider": pid, "name": i18n.t(PRESETS[pid].name), "model": cur.model,
            "configured": is_configured(cur), "providers": out}


def merge_settings_patch(patch: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge llm_providers submitted by the UI into the saved values: an empty key keeps the existing one."""
    cfg = cfg if cfg is not None else config.load()
    if "llm_provider" in patch and patch["llm_provider"] not in PRESETS:
        patch.pop("llm_provider")
    incoming = patch.get("llm_providers")
    if incoming is None:
        return patch
    merged = {k: dict(v) for k, v in (cfg.get("llm_providers") or {}).items()
              if k in PRESETS and isinstance(v, dict)}
    if isinstance(incoming, dict):
        for pid, vals in incoming.items():
            if pid not in PRESETS or not isinstance(vals, dict):
                continue
            entry = merged.setdefault(pid, {})
            for f in ("model", "base_url"):
                if f in vals:
                    entry[f] = str(vals[f] or "").strip()
            if str(vals.get("api_key") or "").strip():
                entry["api_key"] = str(vals["api_key"]).strip()
            if vals.get("clear_key"):
                entry.pop("api_key", None)
                if pid == "deepseek":
                    patch["deepseek_api_key"] = ""
    patch["llm_providers"] = merged
    return patch


def test_connection(pid: str, api_key: str = "", base_url: str = "", model: str = "") -> Dict[str, Any]:
    try:
        c = get_client(pid, api_key, base_url, model)
        msg = c.ping()
        return {"ok": True, "message": msg, "model": c.model, "provider": c.name}
    except Exception as e:  # noqa: BLE001 — report it to the user as is
        return {"ok": False, "message": str(e)}


def list_models(pid: str, api_key: str = "", base_url: str = "") -> Dict[str, Any]:
    try:
        r = resolve(pid, api_key=api_key, base_url=base_url)
        c = get_client(pid, api_key, base_url, model=r.model or "-")
        if r.preset.id == "deepseek":
            return {"ok": True, "models": [DEEPSEEK_MODEL]}     # DeepSeek offers a single "deepseek", no specific model versions
        return {"ok": True, "models": sorted(set(c.list_models()))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e), "models": []}


# ---- shared helpers ----------------------------------------------------------------

def run_cancellable(fn: Callable[[threading.Event], Any], name: str = "llm-http") -> Any:
    """Send the request in a background thread while the caller checks every 0.2 s whether Stop was clicked.

    Writing narration can take tens of seconds and an HTTP request can't be interrupted from outside; after Stop we no longer wait for the result
    and set the stop event — providers with streaming (Claude) then close the connection immediately and stop billing.
    Outside a job thread (e.g. when testing the connection) this is an ordinary synchronous call.
    """
    box: Dict[str, Any] = {}
    stop = threading.Event()

    def run():
        try:
            box["r"] = fn(stop)
        except BaseException as e:      # noqa: BLE001 — handed back to the caller as is
            box["e"] = e

    th = threading.Thread(target=run, name=name, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(0.2)
            jobs.check_cancel()
    except BaseException:
        stop.set()
        raise
    if "e" in box:
        raise box["e"]
    return box.get("r")


def sleep_cancellable(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        jobs.check_cancel()
        time.sleep(min(0.2, max(0.0, end - time.time())))


def strip_think(text: str) -> str:
    """Reasoning models return their thinking inside <think>…</think> (MiniMax, some Qwen / DeepSeek-compatible APIs); remove it."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def parse_json(raw: str) -> Any:
    """Extract JSON from the model output as best we can."""
    raw = strip_think(raw or "").strip()
    if not raw:
        raise LLMError(i18n.t("模型返回为空。"))
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = raw.find(open_ch), raw.rfind(close_ch)
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except Exception:
                pass
    raise LLMError(i18n.t("无法解析模型返回的 JSON：{raw}", raw=raw[:300]))
