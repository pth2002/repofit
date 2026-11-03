"""Optional model backends for the CLI. The core never needs one: the same prompt can be run
by any agent and its answer imported with `repofit profile apply`."""

from __future__ import annotations

import inspect
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

# provider -> (env vars holding the key, base URL, default model)
_PRESETS = {
    "deepseek": (("DEEPSEEK_API_KEY",), "https://api.deepseek.com", "deepseek-chat"),
    "openai": (("OPENAI_API_KEY",), "https://api.openai.com/v1", None),
    "ollama": ((), "http://localhost:11434/v1", None),
    "compatible": ((), None, None),  # any OpenAI-compatible endpoint, needs --base-url and --model
    "anthropic": (("ANTHROPIC_API_KEY",), None, "claude-opus-5-5"),
}
PROVIDERS = tuple(_PRESETS)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str | None
    api_key: str | None

    def label(self) -> str:
        where = f" at {self.base_url}" if self.base_url and self.provider in ("compatible", "ollama") else ""
        return f"{self.provider}/{self.model}{where}"


def resolve(provider: str | None = None, model: str | None = None, base_url: str | None = None) -> LLMConfig | None:
    """Pick a backend from flags, then REPOFIT_* env vars, then whichever provider key is set."""
    provider = provider or os.environ.get("REPOFIT_PROVIDER")
    if not provider:
        provider = next((p for p, (keys, _, _) in _PRESETS.items() if any(os.environ.get(k) for k in keys)), None)
    if not provider:
        return None
    if provider not in _PRESETS:
        raise LLMError(f"unknown provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    keys, default_url, default_model = _PRESETS[provider]
    model = model or os.environ.get("REPOFIT_MODEL") or default_model
    base_url = base_url or os.environ.get("REPOFIT_BASE_URL") or default_url
    api_key = os.environ.get("REPOFIT_API_KEY") or next((os.environ[k] for k in keys if os.environ.get(k)), None)
    if not model:
        raise LLMError(f"provider {provider!r} needs a model: pass --model or set REPOFIT_MODEL")
    if provider == "compatible" and not base_url:
        raise LLMError("provider 'compatible' needs --base-url or REPOFIT_BASE_URL")
    if provider not in ("ollama", "compatible", "anthropic") and not api_key:
        raise LLMError(f"no API key for {provider}: set {' or '.join(keys)} or REPOFIT_API_KEY")
    return LLMConfig(provider, model, base_url, api_key)


def chat_json(cfg: LLMConfig, system: str, user: str) -> dict:
    text = _anthropic(cfg, system, user) if cfg.provider == "anthropic" else _openai_compatible(cfg, system, user)
    return parse_json(text)


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMError(f"model did not return JSON: {text[:200]!r}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise LLMError(f"model returned malformed JSON: {exc}") from exc


def _openai_compatible(cfg: LLMConfig, system: str, user: str) -> str:
    body = {"model": cfg.model, "temperature": 0.2, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    try:
        data = _post(cfg, body)
    except LLMError as exc:
        if "response_format" not in str(exc):
            raise
        body.pop("response_format")  # some local servers reject it; the prompt still asks for JSON
        data = _post(cfg, body)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected response shape: {str(data)[:200]}") from exc


def _post(cfg: LLMConfig, body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LLMError(f"HTTP {exc.code} from {cfg.provider}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"cannot reach {cfg.base_url}: {exc.reason}") from exc


def _anthropic(cfg: LLMConfig, system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise LLMError("the anthropic provider needs the SDK: pip install 'repofit[anthropic]'") from exc
    client = anthropic.Anthropic(api_key=cfg.api_key) if cfg.api_key else anthropic.Anthropic()
    kwargs = {"model": cfg.model, "max_tokens": 16000, "system": system,
              "messages": [{"role": "user", "content": user}], "betas": ["server-side-fallback-2026-06-01"]}
    # A safety decline on the main model is retried on the fallback model inside the same call.
    fallbacks = [{"model": "claude-opus-4-8"}]
    if "fallbacks" in inspect.signature(client.beta.messages.create).parameters:
        kwargs["fallbacks"] = fallbacks
    else:  # SDK releases that predate the parameter still send it as a body field
        kwargs["extra_body"] = {"fallbacks": fallbacks}
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise LLMError("Anthropic rejected the credentials: set ANTHROPIC_API_KEY or run `ant auth login`") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("Anthropic rate limit hit; retry later") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("cannot reach the Anthropic API") from exc
    if response.stop_reason == "refusal":
        raise LLMError("the model declined this request")
    return "".join(block.text for block in response.content if block.type == "text")
