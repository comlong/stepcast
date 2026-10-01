"""OpenAI-compatible protocol (/chat/completions): DeepSeek, Doubao, Qwen, Zhipu GLM, MiniMax, OpenAI, Mistral, Gemini,
Azure OpenAI, Ollama and others."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from .. import i18n
from .llm import LLMError, Resolved, parse_json, request_model, run_cancellable, sleep_cancellable, strip_think


class OpenAICompatClient:
    def __init__(self, r: Resolved):
        self.preset = r.preset
        self.provider = r.preset.id
        self.name = i18n.t(r.preset.name)
        self.api_key = r.api_key
        self.base_url = r.base_url
        self.model = r.model
        self.api_model, self.extra = request_model(r)      # mapping of old DeepSeek model names, see llm.request_model

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            if self.preset.auth == "azure":
                h["api-key"] = self.api_key
            else:
                h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.6,
        json_mode: bool = False,
        max_tokens: int = 8000,
        timeout: int = 180,
        retries: int = 2,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        tokens = max(max_tokens, self.preset.token_floor)
        payload: Dict[str, Any] = {
            "model": self.api_model,
            "messages": messages,
            "temperature": temperature,
            ("max_completion_tokens" if self.preset.new_token_param else "max_tokens"): tokens,
            "stream": False,
        }
        payload.update(self.extra)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err: Optional[Exception] = None
        adjustments = 0
        attempt = 0
        while attempt <= retries:
            try:
                r = run_cancellable(lambda stop: requests.post(
                    url, headers=self._headers(), json=payload, timeout=timeout))
            except LLMError:
                raise
            except Exception as e:  # retry on network hiccups
                last_err = e
                attempt += 1
                if attempt <= retries:
                    sleep_cancellable(1.5 * attempt)
                continue

            if r.status_code == 400 and adjustments < 3 and _drop_unsupported(payload, r.text):
                adjustments += 1          # some models reject temperature / max_tokens / JSON mode: drop it and try again; doesn't count as a retry
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                last_err = LLMError(i18n.t("{name} 返回 {status}", name=self.name, status=r.status_code))
                attempt += 1
                sleep_cancellable(3.0 * attempt)
                continue
            if r.status_code >= 400:
                raise LLMError(self._status_message(r))
            return self._content(r)
        raise LLMError(i18n.t("调用 {name} 失败：{error}", name=self.name, error=last_err))

    def chat_json(self, messages: List[Dict[str, str]], **kw) -> Any:
        return parse_json(self.chat(messages, json_mode=True, **kw))

    def ping(self) -> str:
        txt = self.chat([{"role": "user", "content": "Reply with the single word: OK"}],
                        temperature=0, max_tokens=64, retries=0, timeout=40)
        return txt.strip()[:50] or i18n.t("连接成功")

    def list_models(self) -> List[str]:
        r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=20)
        if r.status_code >= 400:
            raise LLMError(self._status_message(r))
        data = r.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        return [str(m.get("id") or m.get("name") or "").removeprefix("models/")
                for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))]

    # ---- internals ----

    def _content(self, r: requests.Response) -> str:
        try:
            data = r.json()
            choice = data["choices"][0]
        except Exception:
            raise LLMError(i18n.t("{name} 返回了看不懂的内容：{body}", name=self.name, body=r.text[:300]))
        content = (choice.get("message") or {}).get("content") or ""
        if isinstance(content, list):           # a few services return the content in parts
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        content = strip_think(content) if "<think>" in content else content
        if not content.strip() and choice.get("finish_reason") == "length":
            raise LLMError(i18n.t("{name} 的输出被截断了（模型的思考用完了输出额度），换个模型或稍后重试。", name=self.name))
        return content

    def _status_message(self, r: requests.Response) -> str:
        body = r.text[:400]
        if r.status_code in (401, 403):
            return i18n.t("{name} 鉴权失败（{status}），请检查 API Key。", name=self.name, status=r.status_code) + " " + body[:160]
        if r.status_code == 402:
            return i18n.t("{name} 账户余额不足（402）。", name=self.name)
        if r.status_code == 404:
            return i18n.t("{name} 返回 404：接口地址或模型名「{model}」不对。", name=self.name, model=self.api_model) + " " + body[:160]
        if r.status_code == 429:
            return i18n.t("{name} 请求太频繁或额度用完（429）。", name=self.name) + " " + body[:160]
        return i18n.t("{name} 返回 {status}", name=self.name, status=r.status_code) + ": " + body


def _drop_unsupported(payload: Dict[str, Any], body: str) -> bool:
    """Remove parameters the model doesn't support, based on the 400 error. Returns True if anything changed."""
    text = body.lower()
    if "temperature" in text and "temperature" in payload:
        payload.pop("temperature")
        return True
    # note that "max_tokens" isn't a substring of "max_completion_tokens"; check the one actually used in the request
    if "max_tokens" in text and "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        return True
    if "max_completion_tokens" in text and "max_completion_tokens" in payload:
        payload["max_tokens"] = payload.pop("max_completion_tokens")
        return True
    if ("response_format" in text or "json_object" in text) and "response_format" in payload:
        payload.pop("response_format")
        return True
    for k in ("thinking", "enable_thinking", "reasoning_split"):     # parameters for switching thinking off; some models / self-hosted APIs reject them
        if k in text and k in payload:
            payload.pop(k)
            return True
    return False
