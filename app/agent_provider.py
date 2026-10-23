"""Private provider configuration and bounded HTTPS tool-calling adapters."""

from __future__ import annotations

import base64
import copy
import http.client
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

PROVIDERS = {
    "openai": {
        "label": "OpenAI（ChatGPT 系列）",
        "default_model": "gpt-6.1-sol",
        "host": "api.openai.com",
        "path": "/v1/responses",
        "models_path": "/v1/models",
        "models": [],
        "environment": "OPENAI_API_KEY",
    },
    "deepseek": {
        "label": "DeepSeek",
        "default_model": "deepseek-flash",
        "host": "api.deepseek.com",
        "path": "/chat/completions",
        "models_path": "/models",
        "models": [
            {"id": "deepseek-flash", "label": "DeepSeek Flash"},
            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
        ],
        "environment": "DEEPSEEK_API_KEY",
    },
}
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_OUTPUT_TOKENS = 4096
MAX_CALLS_PER_RESPONSE = 8


class AgentError(Exception):
    """An intentionally public message, never a raw upstream exception."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ProviderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "deepseek"]
    model: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]*$")
    api_key: SecretStr | None = None
    clear_api_key: bool = False

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None and not re.fullmatch(r"[!-~]{8,4096}", value.get_secret_value()):
            raise ValueError("API Key 必须是 8–4096 个不含空格的 ASCII 字符。")
        return value


def private_directory(runtime_root: Path) -> Path:
    directory = runtime_root / "agent"
    if directory.is_symlink():
        raise AgentError("AI 配置目录不能是符号链接。", 409)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


class ProviderConfig:
    def __init__(self, runtime_root: Path):
        self.directory = private_directory(runtime_root)
        self.path = self.directory / "providers.json"
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if self.path.is_symlink():
            raise AgentError("AI 配置文件不能是符号链接。", 409)
        if not self.path.exists():
            return {"provider": "openai", "profiles": {}}
        try:
            if self.path.stat().st_size > 32 * 1024:
                raise ValueError
            self.path.chmod(0o600)
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("provider") not in PROVIDERS or not isinstance(value.get("profiles"), dict):
                raise ValueError
            for provider, profile in value["profiles"].items():
                ProviderPatch(provider=provider, model=profile["model"], api_key=profile.get("api_key"))
            return value
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            raise AgentError("AI 配置无法读取，请检查本机配置文件权限或重新配置。", 409) from exc

    @staticmethod
    def _profile(value: dict, provider: str) -> dict:
        saved = value["profiles"].get(provider, {})
        environment = os.environ.get(PROVIDERS[provider]["environment"], "").strip()
        key = saved.get("api_key") or environment
        return {
            "model": saved.get("model", PROVIDERS[provider]["default_model"]),
            "is_saved": bool(saved),
            "has_api_key": bool(key),
            "key_source": "saved" if saved.get("api_key") else "environment" if environment else "none",
        }

    def public(self) -> dict:
        with self._lock:
            value = self._read()
            provider = value["provider"]
            return {
                "provider": provider,
                **self._profile(value, provider),
                "providers": [
                    {
                        "id": key,
                        "label": item["label"],
                        "default_model": item["default_model"],
                        "models": copy.deepcopy(item["models"]),
                        "endpoint": f"https://{item['host']}{item['path']}",
                    }
                    for key, item in PROVIDERS.items()
                ],
                "profiles": {key: self._profile(value, key) for key in PROVIDERS},
            }

    def save(self, patch: ProviderPatch) -> dict:
        with self._lock:
            if patch.clear_api_key and patch.api_key is not None:
                raise AgentError("不能同时保存和清除 API Key。")
            value = self._read()
            profile = value["profiles"].setdefault(patch.provider, {})
            profile["model"] = patch.model
            if patch.clear_api_key:
                profile.pop("api_key", None)
            elif patch.api_key is not None:
                profile["api_key"] = patch.api_key.get_secret_value()
            value["provider"] = patch.provider
            descriptor, temporary = tempfile.mkstemp(prefix=".providers-", dir=self.directory)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(value, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self.public()

    def credentials(self, provider: str | None = None) -> tuple[str, str, str]:
        with self._lock:
            value = self._read()
            provider = provider or value["provider"]
            if provider not in PROVIDERS:
                raise AgentError("不支持此模型服务商。")
            profile = value["profiles"].get(provider, {})
            model = profile.get("model", PROVIDERS[provider]["default_model"])
            key = profile.get("api_key") or os.environ.get(PROVIDERS[provider]["environment"], "").strip()
            if not key:
                raise AgentError("请先保存 API Key，或设置服务端对应的 API Key 环境变量。", 409)
            # Environment credentials receive the same header-injection checks.
            try:
                ProviderPatch(provider=provider, model=model, api_key=key)
            except ValueError:
                raise AgentError("服务端 API Key 或模型配置格式无效，请检查配置。", 409) from None
            return provider, model, key

    def sanitize(self, value):
        with self._lock:
            configuration = self._read()
            keys = [profile.get("api_key", "") for profile in configuration["profiles"].values()]
            keys.extend(os.environ.get(item["environment"], "").strip() for item in PROVIDERS.values())
            for key in keys:
                if key:
                    value = redact(value, key)
            return value


def redact(value, api_key: str):
    """Do not persist or display a credential even if an upstream echoes it."""
    if isinstance(value, str):
        return value.replace(api_key, "[API Key 已隐藏]") if api_key else value
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if isinstance(value, dict):
        return {redact(key, api_key): redact(item, api_key) for key, item in value.items()}
    return value


def proxy_for(host: str) -> tuple[str, int, dict] | None:
    """Use server proxy settings while keeping the TLS destination fixed."""
    proxies = getproxies_environment()
    if proxy_bypass_environment(f"{host}:443", proxies):
        return None
    address = proxies.get("https") or proxies.get("all")
    if not address:
        return None
    try:
        if re.search(r"[\s\x00-\x1f\x7f]", address):
            raise ValueError
        parsed = urlsplit(address)
        if parsed.scheme not in {"http", "https", "socks", "socks4", "socks5", "socks5h"}:
            raise ValueError
        if parsed.scheme != "http":
            raise AgentError("代理协议暂不支持；请将服务端 HTTPS_PROXY 设置为 http:// 地址，使用 HTTP CONNECT 代理。", 502)
        if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        headers = {}
        if parsed.username is not None:
            username = unquote(parsed.username, errors="strict")
            password = unquote(parsed.password or "", errors="strict")
            if ":" in username or re.search(r"[\x00-\x1f\x7f]", username + password):
                raise ValueError
            credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Proxy-Authorization"] = f"Basic {credentials}"
        return parsed.hostname, port, headers
    except (ValueError, UnicodeError):
        raise AgentError("服务端代理配置无效；请检查 HTTPS_PROXY 或 ALL_PROXY 的 HTTP 地址、端口和凭据格式。", 502) from None


def upstream_error(status: int) -> AgentError:
    """Classify status codes without displaying untrusted upstream error bodies."""
    messages = {
        400: "模型请求参数无效（HTTP 400），请核对模型名称及工具调用支持。",
        401: "模型服务认证失败（HTTP 401），请检查此服务商的 API Key 是否有效。",
        402: "模型服务账户余额不足或计费不可用（HTTP 402），请在服务商平台检查余额。",
        403: "模型服务拒绝访问（HTTP 403），请检查账户、模型权限及服务可用地区。",
        404: "模型或接口不存在（HTTP 404），请核对模型名称及访问权限。",
        422: "模型服务无法处理请求参数（HTTP 422），请检查模型的工具调用支持。",
        429: "模型服务限流或额度不足（HTTP 429），请检查账户额度和请求频率。",
    }
    if 300 <= status < 400:
        message = "模型服务要求重定向，已停止请求；请检查网络代理是否改写了官方 API 地址。"
    else:
        message = messages.get(status, f"模型服务暂时不可用（HTTP {status}），请稍后再试。")
    return AgentError(message, 429 if status == 429 else 502)


def post_json(provider: str, api_key: str, payload: dict, *, timeout: float = 60) -> dict:
    return request_json(provider, api_key, payload, timeout=timeout)


def request_json(provider: str, api_key: str, payload: dict | None = None, *, timeout: float = 60) -> dict:
    """Fixed official TLS destinations, optional HTTP CONNECT, no redirects or retries."""
    destination = PROVIDERS[provider]
    deadline = time.monotonic() + timeout
    proxy = proxy_for(destination["host"])
    connection = None
    try:
        options = {"timeout": min(20, timeout), "context": ssl.create_default_context()}
        if proxy:
            host, port, headers = proxy
            connection = http.client.HTTPSConnection(host, port=port, **options)
            # HTTPSConnection validates TLS against the tunnel host. The model
            # credential is sent only after CONNECT and the TLS handshake.
            connection.set_tunnel(destination["host"], port=443, headers=headers)
        else:
            connection = http.client.HTTPSConnection(destination["host"], **options)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None
        if body is not None and len(body) > 512 * 1024:
            raise AgentError("本次对话上下文过长，请新建对话。", 413)
        connection.request(
            "POST" if payload is not None else "GET",
            destination["path"] if payload is not None else destination["models_path"],
            body=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        if connection.sock:
            connection.sock.settimeout(max(0.1, deadline - time.monotonic()))
        response = connection.getresponse()
        if response.status != 200:
            raise upstream_error(response.status)
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise AgentError("模型响应过大，已停止读取。", 502)
        result = json.loads(chunks)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except AgentError:
        raise
    except TimeoutError:
        message = (
            "经代理请求模型服务超时，请检查代理是否正常及服务商状态。"
            if proxy
            else "请求模型服务超时，请检查服务器网络；需要代理时请设置服务端 HTTPS_PROXY。"
        )
        raise AgentError(message, 502) from None
    except socket.gaierror:
        message = (
            "代理服务器的 DNS 解析失败，请检查服务端代理地址和 DNS 设置。"
            if proxy
            else "模型服务的 DNS 解析失败，请检查服务器网络和 DNS 设置。"
        )
        raise AgentError(message, 502) from None
    except ssl.SSLError:
        raise AgentError("模型服务 TLS 连接或证书校验失败，请检查系统时间、CA 证书及代理证书配置。", 502) from None
    except (OSError, http.client.HTTPException) as exc:
        if proxy and str(exc).startswith("Tunnel connection failed: 407 "):
            message = "代理认证失败（HTTP 407），请检查服务端 HTTPS_PROXY 中的代理凭据。"
        elif proxy and str(exc).startswith("Tunnel connection failed: "):
            message = "代理未能建立 CONNECT 隧道，请检查代理的访问权限及连接状态。"
        else:
            message = (
                "经代理连接模型服务失败，请检查代理进程、端口及网络。"
                if proxy
                else "连接模型服务失败，请检查服务器网络；需要代理时请设置服务端 HTTPS_PROXY。"
            )
        raise AgentError(message, 502) from None
    except (ValueError, UnicodeError):
        raise AgentError("模型服务返回了无效响应。", 502) from None
    finally:
        if connection is not None:
            connection.close()


class ProviderClient:
    def __init__(self, provider: str, model: str, api_key: str):
        self.provider, self.model, self.api_key = provider, model, api_key

    def list_models(self) -> list[dict]:
        raw = redact(request_json(self.provider, self.api_key, timeout=20), self.api_key)
        items = raw.get("data")
        if not isinstance(items, list) or len(items) > 1000:
            raise AgentError("模型服务返回的模型列表格式无效。", 502)
        models = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,119}", model_id):
                continue
            label = item.get("name")
            models[model_id] = {"id": model_id, "label": label[:160] if isinstance(label, str) and label else model_id}
        if not models:
            raise AgentError("服务商未返回可用模型，请检查账户权限，或手动填写模型 ID。", 502)
        return list(models.values())

    def user_message(self, content: str) -> dict:
        return {"role": "user", "content": content}

    def tool_result(self, call_id: str, result: dict) -> dict:
        content = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if self.provider == "openai":
            return {"type": "function_call_output", "call_id": call_id, "output": content}
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    def complete(self, history: list, instructions: str, definitions: list, *, timeout: float = 60, require_tool: bool = False) -> dict:
        if self.provider == "openai":
            payload = {
                "model": self.model,
                "instructions": instructions,
                "input": history,
                "tools": [{"type": "function", **item, "strict": False} for item in definitions],
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "parallel_tool_calls": False,
                "store": False,
                # Required for stateless continuation of reasoning models.
                "include": ["reasoning.encrypted_content"],
            }
        else:
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": instructions}, *history],
                "tools": [{"type": "function", "function": item} for item in definitions],
                "max_tokens": MAX_OUTPUT_TOKENS,
            }
            if require_tool:
                # DeepSeek rejects forced tool calls while thinking is enabled.
                payload["thinking"] = {"type": "disabled"}
        payload["tool_choice"] = "required" if require_tool else "auto"
        raw = post_json(self.provider, self.api_key, payload, timeout=timeout)
        try:
            result = self._normalize(raw)
            return redact(result, self.api_key)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise AgentError("模型返回的消息或工具调用格式无效；没有执行任务。", 502) from exc

    def _normalize(self, raw: dict) -> dict:
        calls, texts = [], []
        if self.provider == "openai":
            if raw.get("status") not in {None, "completed"} or raw.get("error"):
                raise ValueError
            output = raw["output"]
            if not isinstance(output, list) or len(output) > 32:
                raise ValueError
            for item in output:
                if item["type"] == "function_call":
                    calls.append({"id": item["call_id"], "name": item["name"], "arguments": item["arguments"]})
                elif item["type"] == "message":
                    for content in item["content"]:
                        if content["type"] == "output_text":
                            texts.append(content["text"])
                        elif content["type"] == "refusal":
                            texts.append(content["refusal"])
                elif item["type"] != "reasoning":
                    raise ValueError
            continuation = copy.deepcopy(output)
        else:
            choice = raw["choices"][0]
            if choice.get("finish_reason") not in {None, "stop", "tool_calls"}:
                raise ValueError
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError
            if message.get("content"):
                texts.append(message["content"])
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise ValueError
            for call in tool_calls:
                if call.get("type") != "function":
                    raise ValueError
                calls.append({"id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
            # DeepSeek thinking-mode tool calls require reasoning_content back.
            continuation = [
                {key: copy.deepcopy(message[key]) for key in ("role", "content", "tool_calls", "reasoning_content") if key in message}
            ]
        if len(calls) > MAX_CALLS_PER_RESPONSE or len({call["id"] for call in calls}) != len(calls):
            raise ValueError
        for call in calls:
            if not isinstance(call["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call["id"]):
                raise ValueError
            if not isinstance(call["name"], str) or not re.fullmatch(r"[a-z_]{1,80}", call["name"]):
                raise ValueError
            if not isinstance(call["arguments"], str) or len(call["arguments"]) > 32 * 1024:
                raise ValueError
        if not all(isinstance(text, str) for text in texts) or len("\n".join(texts)) > 32 * 1024:
            raise ValueError
        if not calls and not texts:
            raise ValueError
        return {"text": "\n".join(texts), "calls": calls, "continuation": continuation}
