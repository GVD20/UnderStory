from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

credential_cache: dict[str, str] = {}


@dataclass(slots=True)
class ModelSettings:
    provider: str = "MiniMax TokenPlan"
    fast_model: str = "MiniMax-M3"
    advanced_model: str = "MiniMax-M3"
    base_url: str = "https://api.minimax.io/v1"
    api_key: str = ""
    reasoning_split: bool = True
    composer_concurrency: int = 3
    preview_concurrency: int = 3


@dataclass(slots=True)
class EmbeddingSettings:
    provider: str = "OpenAI-compatible Embedding"
    model: str = ""
    base_url: str = ""
    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url)


class SettingsRepository:
    def __init__(self, workspace: Path, data_dir: Path | None = None) -> None:
        self.workspace = workspace
        self.path = Path(os.environ.get("UNDERSTORY_DEVICE", str(data_dir or workspace / "data"))) / "model-settings.json"

    def load(self) -> ModelSettings:
        payload = self._stored_payload()
        shared_concurrency = _shared_concurrency(payload, 3)
        settings = ModelSettings(
            provider=str(payload.get("provider") or "MiniMax TokenPlan"),
            fast_model=str(payload.get("fast_model") or payload.get("model") or os.getenv("LIGHTROCKET_FAST_MODEL") or os.getenv("LIGHTROCKET_MODEL") or "MiniMax-M3"),
            advanced_model=str(payload.get("advanced_model") or payload.get("model") or os.getenv("LIGHTROCKET_ADVANCED_MODEL") or os.getenv("LIGHTROCKET_MODEL") or "MiniMax-M3"),
            base_url=str(payload.get("base_url") or os.getenv("MINIMAX_BASE_URL") or "https://api.minimax.io/v1").rstrip("/"),
            api_key=str(payload.get("api_key") or os.getenv("MINIMAX_API_KEY") or ""),
            reasoning_split=bool(payload.get("reasoning_split", True)),
            composer_concurrency=shared_concurrency,
            preview_concurrency=shared_concurrency,
        )
        if not settings.api_key:
            settings.api_key = _env_value(self.workspace / ".env", "MINIMAX_API_KEY") or ""
        return settings

    def public(self) -> dict[str, object]:
        settings = self.load()
        return {
            "provider": settings.provider,
            "fast_model": settings.fast_model,
            "advanced_model": settings.advanced_model,
            "base_url": settings.base_url,
            "api_key_configured": bool(settings.api_key),
            "api_key_hint": f"•••• {settings.api_key[-4:]}" if settings.api_key else "",
            "reasoning_split": settings.reasoning_split,
            "composer_concurrency": settings.composer_concurrency,
            "preview_concurrency": settings.preview_concurrency,
        }

    def save(self, payload: dict[str, object]) -> dict[str, object]:
        updated = self.resolve(payload)
        if not updated.fast_model or not updated.advanced_model or not updated.base_url:
            raise ValueError("快速模型、高级模型与 API Base URL 不能为空")
        if not updated.api_key:
            raise ValueError("请输入 API Key 后再保存")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = "embedding" if self.path.name.startswith("embedding") else "model"
        credential_cache[name] = updated.api_key
        public = asdict(updated)
        public.pop("api_key", None)
        self.path.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.public()

    def resolve(self, payload: dict[str, object]) -> ModelSettings:
        current = self.load()
        stored = self._stored_payload()
        shared_concurrency = _shared_concurrency(payload, current.composer_concurrency)
        return ModelSettings(
            provider=str(payload.get("provider") or current.provider).strip() or "OpenAI-compatible API",
            fast_model=str(payload.get("fast_model") or current.fast_model).strip(),
            advanced_model=str(payload.get("advanced_model") or current.advanced_model).strip(),
            base_url=str(payload.get("base_url") or current.base_url).strip().rstrip("/"),
            api_key=str(payload.get("api_key") or stored.get("api_key") or current.api_key or "").strip(),
            reasoning_split=bool(payload.get("reasoning_split", current.reasoning_split)),
            composer_concurrency=shared_concurrency,
            preview_concurrency=shared_concurrency,
        )

    def list_models(self, payload: dict[str, object]) -> list[str]:
        settings = self.resolve(payload)
        response = _request(
            "GET", f"{settings.base_url}/models", settings.api_key,
        )
        try:
            models = [
                str(item["id"])
                for item in response.json().get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        except (AttributeError, ValueError) as error:
            raise ValueError("模型列表响应格式无法识别") from error
        if not models:
            raise ValueError("接口连接成功，但没有返回可用模型")
        return models

    def test_connection(self, payload: dict[str, object]) -> str:
        settings = self.resolve(payload)
        request_payload: dict[str, object] = {
            "model": settings.fast_model,
            "messages": [
                {"role": "system", "content": "Reply briefly."},
                {"role": "user", "content": "Reply OK."},
            ],
            "temperature": 1.0,
            "max_completion_tokens": 128,
        }
        if settings.reasoning_split:
            request_payload["reasoning_split"] = True
        response = _request(
            "POST", f"{settings.base_url}/chat/completions", settings.api_key,
            json=request_payload,
        )
        try:
            data = response.json()
        except ValueError as error:
            raise ValueError("模型接口返回了无法解析的响应") from error
        if not data.get("choices"):
            raise ValueError("接口已响应，但没有返回有效的模型结果")
        return str(data.get("model") or settings.fast_model)

    def _stored_payload(self) -> dict[str, object]:
        if not self.path.exists():
            return {"api_key": credential_cache.get("embedding" if self.path.name.startswith("embedding") else "model", "")}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict): value["api_key"] = credential_cache.get("embedding" if self.path.name.startswith("embedding") else "model", "")
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}


class EmbeddingSettingsRepository:
    def __init__(self, workspace: Path, data_dir: Path | None = None) -> None:
        self.workspace = workspace
        self.path = Path(os.environ.get("UNDERSTORY_DEVICE", str(data_dir or workspace / "data"))) / "embedding-settings.json"

    def load(self) -> EmbeddingSettings:
        payload = self._stored_payload()
        return EmbeddingSettings(
            provider=str(payload.get("provider") or "OpenAI-compatible Embedding"),
            model=str(payload.get("model") or "").strip(),
            base_url=str(payload.get("base_url") or "").strip().rstrip("/"),
            api_key=str(payload.get("api_key") or "").strip(),
        )

    def public(self) -> dict[str, object]:
        settings = self.load()
        return {
            "provider": settings.provider,
            "model": settings.model,
            "base_url": settings.base_url,
            "configured": settings.configured,
            "api_key_configured": bool(settings.api_key),
            "api_key_hint": f"•••• {settings.api_key[-4:]}" if settings.api_key else "",
        }

    def resolve(self, payload: dict[str, object]) -> EmbeddingSettings:
        current = self.load()
        stored = self._stored_payload()
        return EmbeddingSettings(
            provider=str(payload.get("provider") or current.provider).strip() or "OpenAI-compatible Embedding",
            model=str(payload.get("model") or current.model).strip(),
            base_url=str(payload.get("base_url") or current.base_url).strip().rstrip("/"),
            api_key=str(payload.get("api_key") or stored.get("api_key") or current.api_key or "").strip(),
        )

    def save(self, payload: dict[str, object]) -> dict[str, object]:
        updated = self.resolve(payload)
        if not updated.model or not updated.base_url:
            raise ValueError("向量化模型与 API Base URL 不能为空")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = "embedding" if self.path.name.startswith("embedding") else "model"
        credential_cache[name] = updated.api_key
        public = asdict(updated)
        public.pop("api_key", None)
        self.path.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.public()

    def list_models(self, payload: dict[str, object]) -> list[str]:
        settings = self.resolve(payload)
        response = _request("GET", f"{settings.base_url}/models", settings.api_key, require_key=False)
        try:
            models = [
                str(item["id"]) for item in response.json().get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        except (AttributeError, ValueError) as error:
            raise ValueError("模型列表响应格式无法识别") from error
        if not models:
            raise ValueError("接口连接成功，但没有返回可用模型")
        return models

    def test_connection(self, payload: dict[str, object]) -> tuple[str, int]:
        settings = self.resolve(payload)
        if not settings.model or not settings.base_url:
            raise ValueError("请先填写向量化模型与 API 地址")
        response = _request(
            "POST", f"{settings.base_url}/embeddings", settings.api_key,
            require_key=False, json={"model": settings.model, "input": ["LightRocket 向量化连接测试"]},
        )
        try:
            data = response.json().get("data")
            vector = data[0]["embedding"]
            dimensions = len(vector)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError("接口已响应，但结果不符合 /v1/embeddings 格式") from error
        if dimensions <= 0:
            raise ValueError("接口返回了空向量")
        return settings.model, dimensions

    def _stored_payload(self) -> dict[str, object]:
        if not self.path.exists():
            return {"api_key": credential_cache.get("embedding" if self.path.name.startswith("embedding") else "model", "")}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict): value["api_key"] = credential_cache.get("embedding" if self.path.name.startswith("embedding") else "model", "")
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}


def _env_value(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip('"\'') or None
    return None


def _concurrency(value: object) -> int:
    try:
        return max(1, min(8, int(value)))
    except (TypeError, ValueError):
        return 3


def _shared_concurrency(payload: dict[str, object], default: int) -> int:
    """Read the unified analysis concurrency while accepting legacy field names."""
    value = payload.get("analysis_concurrency")
    if value is None:
        value = payload.get("composer_concurrency")
    if value is None:
        value = payload.get("preview_concurrency", default)
    return _concurrency(value)


def _request(
    method: str, url: str, api_key: str, *, require_key: bool = True, **kwargs: object,
) -> httpx.Response:
    if require_key and not api_key:
        raise ValueError("请输入 API Key 后再测试")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.request(
            method, url,
            headers=headers,
            timeout=45,
            **kwargs,
        )
    except httpx.TimeoutException as error:
        raise ValueError("模型服务连接超时，请检查 API 地址或稍后重试") from error
    except httpx.NetworkError as error:
        raise ValueError(f"无法连接模型服务：{type(error).__name__}") from error
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:300]
        if response.status_code in {401, 403}:
            raise ValueError(f"API Key 认证失败，请检查密钥与 API 地址是否属于同一服务区域：{detail}")
        raise ValueError(f"模型接口测试失败（HTTP {response.status_code}）：{detail}")
    return response
