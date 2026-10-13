from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from pathlib import Path

import httpx


BASE_URL = "https://api.minimax.io/v1"
MODEL_ID = "MiniMax-M3"


def load_minimax_key(workspace: Path) -> str | None:
    if value := os.environ.get("MINIMAX_API_KEY"):
        return value.strip()
    env_file = workspace / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "MINIMAX_API_KEY":
                return value.strip().strip('"\'') or None
    return None


def load_minimax_base_url(workspace: Path) -> str:
    if value := os.environ.get("MINIMAX_BASE_URL"):
        return value.strip().rstrip("/")
    env_file = workspace / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "MINIMAX_BASE_URL":
                return value.strip().strip('"\'').rstrip("/") or BASE_URL
    return BASE_URL


# Kept temporarily for internal imports and older local tests.
load_deepseek_key = load_minimax_key


@dataclass(slots=True)
class CompletionResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    system_fingerprint: str


@dataclass(slots=True)
class ToolCallResult:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class AgentCompletionResult:
    content: str
    tool_calls: list[ToolCallResult]
    assistant_message: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    system_fingerprint: str
    finish_reason: str = ""
    reasoning_content_chars: int = 0


class MiniMaxOutputTruncated(ValueError):
    def __init__(self, message: str, usage: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        usage = usage or {}
        self.input_tokens = int(usage.get("prompt_tokens", 0))
        self.output_tokens = int(usage.get("completion_tokens", 0))
        self.cache_hit_tokens = _cache_hit_tokens(usage)
        self.cache_miss_tokens = max(0, self.input_tokens - self.cache_hit_tokens)


class MiniMaxAPIError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class MiniMaxClient:
    def __init__(self, api_key: str, timeout_seconds: float = 180.0, base_url: str = BASE_URL,
                 model: str = MODEL_ID, reasoning_split: bool = True) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reasoning_split = reasoning_split

    def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            timeout = httpx.Timeout(
                connect=min(30.0, self.timeout_seconds),
                read=self.timeout_seconds,
                write=min(120.0, self.timeout_seconds),
                pool=min(30.0, self.timeout_seconds),
            )
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as error:
            raise MiniMaxAPIError(
                f"模型 API 在 {self.timeout_seconds:g} 秒内未完成响应，分析已中断，可从断点恢复"
            ) from error
        except httpx.NetworkError as error:
            raise MiniMaxAPIError(f"模型 API 网络连接失败：{type(error).__name__}") from error
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:500]
            if response.status_code in {401, 403}:
                message = "模型 API 认证失败或当前凭据无权限"
            elif response.status_code in {402, 429}:
                message = "模型 API 额度耗尽或触发速率限制"
            elif response.status_code >= 500:
                message = "模型 API 服务端暂时不可用"
            else:
                message = f"模型 API 请求失败（HTTP {response.status_code}）"
            raise MiniMaxAPIError(
                f"{message}：{detail or '无详细信息'}",
                retryable=response.status_code in {408, 409, 425, 429} or response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError as error:
            raise MiniMaxAPIError("模型 API 返回了无法解析的响应") from error
        if not data.get("choices"):
            raise MiniMaxAPIError("模型 API 未返回有效 choices，分析已中断")
        return data

    def list_models(self) -> list[str]:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
        return [item["id"] for item in response.json().get("data", [])]

    def complete_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 6000,
        temperature: float = 1.0, reasoning_split: bool | None = None,
        reasoning_effort: str | None = None,
        json_mode: bool = False,
    ) -> CompletionResult:
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.reasoning_split if reasoning_split is None else reasoning_split:
            payload["reasoning_split"] = True
        payload.update(self._reasoning_controls(model or self.model, reasoning_effort))
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post_chat(payload)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise MiniMaxOutputTruncated("Model output was truncated", data.get("usage"))
        content = choice.get("message", {}).get("content") or ""
        if not content.strip():
            raise ValueError("Model returned empty JSON content")
        usage = data.get("usage", {})
        return CompletionResult(
            content=content,
            model=data.get("model", model or self.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cache_hit_tokens=_cache_hit_tokens(usage),
            cache_miss_tokens=max(0, int(usage.get("prompt_tokens", 0)) - _cache_hit_tokens(usage)),
            system_fingerprint=data.get("system_fingerprint", ""),
        )

    def complete_agent(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 20000,
        reasoning_effort: str | None = None,
    ) -> AgentCompletionResult:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": 1.0,
        }
        if self.reasoning_split:
            payload["reasoning_split"] = True
        payload.update(self._reasoning_controls(model or self.model, reasoning_effort))
        data = self._post_chat(payload)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise MiniMaxOutputTruncated("Agent output was truncated")
        message = choice.get("message") or {}
        calls = [
            ToolCallResult(
                id=call["id"],
                name=call["function"]["name"],
                arguments=call["function"].get("arguments") or "{}",
            )
            for call in message.get("tool_calls") or []
        ]
        usage = data.get("usage", {})
        assistant_message: dict[str, Any] = dict(message)
        assistant_message.setdefault("role", "assistant")
        assistant_message.setdefault("content", "")
        return AgentCompletionResult(
            content=message.get("content") or "",
            tool_calls=calls,
            assistant_message=assistant_message,
            model=data.get("model", model or self.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cache_hit_tokens=_cache_hit_tokens(usage),
            cache_miss_tokens=max(0, int(usage.get("prompt_tokens", 0)) - _cache_hit_tokens(usage)),
            system_fingerprint=data.get("system_fingerprint", ""),
            finish_reason=str(choice.get("finish_reason") or ""),
            reasoning_content_chars=len(str(message.get("reasoning_content") or message.get("reasoning_details") or "")),
        )

    def _reasoning_controls(self, model: str, effort: str | None) -> dict[str, Any]:
        """Map product effort onto the selected OpenAI-compatible provider.

        MiniMax does not implement reasoning_effort: M3 exposes only adaptive
        versus disabled thinking, while M2.x cannot disable it. Other compatible
        endpoints receive the standard top-level reasoning_effort field.
        """
        if not effort:
            return {}
        is_minimax = "minimax" in self.base_url.casefold() or model.casefold().startswith("minimax-")
        if is_minimax:
            if model.casefold().startswith("minimax-m3"):
                return {"thinking": {"type": "disabled" if effort in {"none", "low"} else "adaptive"}}
            return {}
        if "deepseek" in self.base_url.casefold() or model.casefold().startswith("deepseek-"):
            if effort == "none":
                return {"thinking": {"type": "disabled"}}
            return {"reasoning_effort": effort, "thinking": {"type": "enabled"}}
        controls: dict[str, Any] = {"reasoning_effort": effort}
        return controls


def _cache_hit_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or {}
    return int(
        usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
        or details.get("cached_tokens")
        or 0
    )


# Compatibility alias while the provider-neutral protocol is extracted.
DeepSeekClient = MiniMaxClient
