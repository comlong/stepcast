"""Anthropic Claude（官方 SDK）。"""
from __future__ import annotations

from typing import Any, Dict, List

from .. import i18n
from .llm import LLMError, Resolved, parse_json, run_cancellable

try:
    import anthropic
except ImportError:  # 老环境没装
    anthropic = None

# 这些模型的安全分类器偶尔会拒绝请求；开启服务端兜底后，被拒的请求会自动换一个模型重跑
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

JSON_RULE = "只输出一个 JSON 对象，不要任何解释，也不要用 ``` 包裹。"  # i18n: ignore


class AnthropicClient:
    def __init__(self, r: Resolved):
        if anthropic is None:
            raise LLMError(i18n.t("缺少 anthropic 库。请重新运行 setup.bat，或执行：pip install anthropic"))
        self.provider = r.preset.id
        self.name = i18n.t(r.preset.name)
        self.model = r.model
        # 没填 Key 时交给 SDK 自己找（ANTHROPIC_API_KEY 等）
        self.client = anthropic.Anthropic(api_key=r.api_key or None, base_url=r.base_url or None,
                                          max_retries=2, timeout=600)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.6,       # 新模型不接受采样参数，统一不发
        json_mode: bool = False,
        max_tokens: int = 8000,         # 思考也占输出额度，统一给足，实际按用量计费
        timeout: int = 180,
        retries: int = 2,
    ) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if json_mode:
            system = (system + "\n\n" + JSON_RULE).strip()
        params: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 64000,
            "messages": [{"role": m["role"], "content": m["content"]}
                         for m in messages if m["role"] != "system"],
        }
        if system:
            params["system"] = system

        def run(stop):
            if self.model in FALLBACK_MODELS:
                ctx = self.client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                ctx = self.client.messages.stream(**params)
            with ctx as stream:
                for _ in stream:
                    if stop.is_set():       # 用户点了停止：关掉连接，不再生成、不再计费
                        return None
                return stream.get_final_message()

        try:
            msg = run_cancellable(run, name="claude-http")
        except anthropic.AuthenticationError:
            raise LLMError(i18n.t("Claude 鉴权失败（401），请检查 API Key。"))
        except anthropic.PermissionDeniedError as e:
            raise LLMError(i18n.t("Claude 拒绝访问（403）：{error}", error=e.message))
        except anthropic.NotFoundError:
            raise LLMError(i18n.t("Claude 没有这个模型：{model}", model=self.model))
        except anthropic.RateLimitError:
            raise LLMError(i18n.t("Claude 请求太频繁或额度用完（429），稍后再试。"))
        except anthropic.BadRequestError as e:
            raise LLMError(i18n.t("Claude 请求有误：{error}", error=e.message))
        except anthropic.APIStatusError as e:
            raise LLMError(i18n.t("Claude 返回 {status}：{error}", status=e.status_code, error=e.message))
        except anthropic.APIConnectionError:
            raise LLMError(i18n.t("连不上 Claude（网络问题），请检查网络或代理。"))
        except anthropic.AnthropicError as e:      # 例如没有任何可用的凭据
            raise LLMError(i18n.t("调用 Claude 失败：{error}", error=e))

        if msg.stop_reason == "refusal":
            raise LLMError(i18n.t("Claude 拒绝了这次请求（安全策略）。可以调整页面内容或换个模型再试。"))
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens" and not text.strip():
            raise LLMError(i18n.t("Claude 的输出被截断了，请稍后重试。"))
        return text

    def chat_json(self, messages: List[Dict[str, str]], **kw) -> Any:
        return parse_json(self.chat(messages, json_mode=True, **kw))

    def ping(self) -> str:
        """只查模型信息：能验证 Key 和模型名，不花 token。"""
        try:
            m = self.client.models.retrieve(self.model)
        except anthropic.AuthenticationError:
            raise LLMError(i18n.t("Claude 鉴权失败（401），请检查 API Key。"))
        except anthropic.NotFoundError:
            raise LLMError(i18n.t("Key 可用，但没有这个模型：{model}", model=self.model))
        except anthropic.APIStatusError as e:
            raise LLMError(i18n.t("Claude 返回 {status}：{error}", status=e.status_code, error=e.message))
        except anthropic.APIConnectionError:
            raise LLMError(i18n.t("连不上 Claude（网络问题），请检查网络或代理。"))
        except anthropic.AnthropicError as e:
            raise LLMError(i18n.t("调用 Claude 失败：{error}", error=e))
        return i18n.t("连接成功：{model}", model=m.display_name)

    def list_models(self) -> List[str]:
        try:
            return [m.id for m in self.client.models.list()]
        except anthropic.AuthenticationError:
            raise LLMError(i18n.t("Claude 鉴权失败（401），请检查 API Key。"))
        except anthropic.APIStatusError as e:
            raise LLMError(i18n.t("Claude 返回 {status}：{error}", status=e.status_code, error=e.message))
        except anthropic.APIConnectionError:
            raise LLMError(i18n.t("连不上 Claude（网络问题），请检查网络或代理。"))
        except anthropic.AnthropicError as e:
            raise LLMError(i18n.t("调用 Claude 失败：{error}", error=e))
