from __future__ import annotations

import copy
import http.client
import io
import json
import os
import socket
import ssl
import stat

import pytest
from pydantic import ValidationError

from app import agent_provider as provider

FAKE_KEY = "test-key-do-not-use-0123456789"
SECOND_KEY = "test-another-key-9876543210"
TOOLS = [{"name": "list_jobs", "description": "List local jobs", "parameters": {"type": "object", "properties": {}}}]


@pytest.fixture
def config(tmp_path, monkeypatch):
    for item in provider.PROVIDERS.values():
        monkeypatch.delenv(item["environment"], raising=False)
    return provider.ProviderConfig(tmp_path / "runtime")


def patch(name="openai", **kwargs):
    return provider.ProviderPatch(provider=name, model=provider.PROVIDERS[name]["default_model"], **kwargs)


def test_saved_keys_are_private_and_retained_independently_when_switching(config):
    first = config.save(patch(api_key=FAKE_KEY))
    assert FAKE_KEY not in json.dumps(first)
    assert first["has_api_key"] and first["key_source"] == "saved"
    config.save(patch("deepseek", api_key=SECOND_KEY))
    result = config.save(patch())
    assert config.credentials() == ("openai", provider.PROVIDERS["openai"]["default_model"], FAKE_KEY)
    assert all(profile["has_api_key"] for profile in result["profiles"].values())
    assert not any(key in json.dumps(result) for key in (FAKE_KEY, SECOND_KEY))
    result = config.save(patch(clear_api_key=True))
    assert not result["has_api_key"]
    assert result["profiles"]["deepseek"]["has_api_key"]
    assert FAKE_KEY not in config.path.read_text()
    config.save(patch("deepseek"))
    assert config.credentials()[2] == SECOND_KEY


def test_environment_fallback_is_not_written_to_disk(config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    assert config.public()["key_source"] == "environment"
    assert config.credentials()[2] == FAKE_KEY
    config.save(patch(api_key=SECOND_KEY))
    assert config.credentials()[2] == SECOND_KEY
    result = config.save(patch(clear_api_key=True))
    assert result["key_source"] == "environment"
    assert result["has_api_key"]
    assert config.credentials()[2] == FAKE_KEY
    assert FAKE_KEY not in config.path.read_text()


def test_saved_overview_and_model_presets_do_not_expose_keys(config):
    empty = config.public()
    assert not empty["profiles"]["deepseek"]["is_saved"]
    config.save(patch("deepseek", api_key=FAKE_KEY))
    config.save(patch(api_key=SECOND_KEY))
    public = config.public()
    deepseek = next(item for item in public["providers"] if item["id"] == "deepseek")
    assert {item["id"] for item in deepseek["models"]} == {"deepseek-flash", "deepseek-v4-pro"}
    assert all(profile["is_saved"] for profile in public["profiles"].values())
    assert config.credentials("deepseek")[2] == FAKE_KEY
    assert config.public()["provider"] == "openai"
    assert not any(key in json.dumps(public) for key in (FAKE_KEY, SECOND_KEY))


def test_sanitize_covers_saved_and_environment_keys_for_both_providers(config, monkeypatch):
    environment_openai = "test-env-openai-0123456789"
    environment_deepseek = "test-env-deepseek-9876543210"
    monkeypatch.setenv("OPENAI_API_KEY", environment_openai)
    monkeypatch.setenv("DEEPSEEK_API_KEY", environment_deepseek)
    config.save(patch(api_key=FAKE_KEY))
    config.save(patch("deepseek", api_key=SECOND_KEY))
    keys = (FAKE_KEY, SECOND_KEY, environment_openai, environment_deepseek)
    content = {key: {"message": f"Echo {key}"} for key in keys}
    result = config.sanitize(content)
    assert not any(key in json.dumps(result) for key in keys)
    assert content[FAKE_KEY]["message"] == f"Echo {FAKE_KEY}"


def test_missing_credentials_and_conflicting_save_fail_without_changes(config):
    with pytest.raises(provider.AgentError, match="API Key") as failure:
        config.credentials()
    assert failure.value.status_code == 409
    config.save(patch(api_key=FAKE_KEY))
    before = config.path.read_bytes()
    with pytest.raises(provider.AgentError, match="不能同时"):
        config.save(patch(api_key=SECOND_KEY, clear_api_key=True))
    assert config.path.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_provider_config_permissions_are_private_and_repaired(config):
    config.save(patch(api_key=FAKE_KEY))
    assert stat.S_IMODE(config.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.path.stat().st_mode) == 0o600
    config.directory.chmod(0o755)
    config.path.chmod(0o644)
    reopened = provider.ProviderConfig(config.directory.parent)
    assert reopened.credentials()[2] == FAKE_KEY
    assert stat.S_IMODE(config.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.path.stat().st_mode) == 0o600
    assert list(config.directory.iterdir()) == [config.path]


def test_provider_config_rejects_symlink_directory_and_file(tmp_path):
    runtime, outside = tmp_path / "runtime", tmp_path / "outside"
    runtime.mkdir()
    outside.mkdir()
    (runtime / "agent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(provider.AgentError, match="符号链接"):
        provider.ProviderConfig(runtime)
    (runtime / "agent").unlink()
    config = provider.ProviderConfig(runtime)
    target = outside / "untouched.json"
    target.write_text("untouched")
    config.path.symlink_to(target)
    for operation in (config.public, config.credentials, lambda: config.save(patch(api_key=FAKE_KEY))):
        with pytest.raises(provider.AgentError, match="符号链接"):
            operation()
    assert target.read_text() == "untouched"


@pytest.mark.parametrize(
    "content",
    [
        "[invalid json",
        "[]",
        '{"provider":"other","profiles":{}}',
        '{"provider":"openai","profiles":[]}',
        '{"provider":"openai","profiles":{"openai":{"model":"valid","api_key":"bad secret"}}}',
        '{"provider":"openai","profiles":{"other":{"model":"valid"}}}',
        '{"provider":"openai","profiles":{"openai":null}}',
        "x" * (32 * 1024 + 1),
    ],
)
def test_invalid_configuration_fails_closed_without_echoing_input(config, content):
    config.path.write_text(content)
    with pytest.raises(provider.AgentError, match="AI 配置无法读取") as failure:
        config.public()
    assert failure.value.status_code == 409
    assert content not in str(failure.value)


@pytest.mark.parametrize("key", ["short", "test key with spaces", "test\r\nX-Header: value", "非ASCII密钥"])
def test_patch_rejects_invalid_credentials(key):
    with pytest.raises(ValidationError):
        patch(api_key=key)


def test_invalid_environment_key_is_a_sanitized_public_error(config, monkeypatch):
    malformed = FAKE_KEY + "\nX-Injected: true"
    monkeypatch.setenv("OPENAI_API_KEY", malformed)
    with pytest.raises(provider.AgentError) as failure:
        config.credentials()
    assert failure.value.status_code == 409
    assert FAKE_KEY not in str(failure.value)


def openai_response(*items, **fields):
    return {"status": "completed", "output": list(items), **fields}


def openai_call(call_id="call_1", **fields):
    return {"type": "function_call", "call_id": call_id, "name": "list_jobs", "arguments": "{}", **fields}


def openai_message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def deepseek_response(message=None, finish="tool_calls"):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": message
                or {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "A private intermediate explanation",
                    "tool_calls": [{"type": "function", "id": "call_1", "function": {"name": "list_jobs", "arguments": "{}"}}],
                },
            }
        ]
    }


def mock_response(monkeypatch, response):
    requests = []

    def fake_post(name, key, payload, **kwargs):
        requests.append((name, key, copy.deepcopy(payload), kwargs))
        return copy.deepcopy(response)

    monkeypatch.setattr(provider, "post_json", fake_post)
    return requests


def test_openai_stateless_payload_and_encrypted_reasoning_continuation(monkeypatch):
    reasoning = {"type": "reasoning", "id": "reasoning_1", "summary": [], "encrypted_content": "opaque-ciphertext"}
    response = openai_response(reasoning, openai_call(), openai_message("Checking the queue"))
    requests = mock_response(monkeypatch, response)
    client = provider.ProviderClient("openai", "a-supported-model", FAKE_KEY)
    history = [client.user_message("List jobs")]
    result = client.complete(history, "Only use allowed tools", TOOLS, timeout=15, require_tool=True)
    name, key, payload, options = requests[0]
    assert (name, key, options) == ("openai", FAKE_KEY, {"timeout": 15})
    assert payload["store"] is False
    assert payload["parallel_tool_calls"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["input"] == history
    assert payload["instructions"] == "Only use allowed tools"
    assert payload["tool_choice"] == "required"
    assert payload["max_output_tokens"] == provider.MAX_OUTPUT_TOKENS
    assert payload["tools"] == [{"type": "function", **TOOLS[0], "strict": False}]
    assert result["text"] == "Checking the queue"
    assert result["calls"] == [{"id": "call_1", "name": "list_jobs", "arguments": "{}"}]
    assert result["continuation"] == response["output"]
    history.extend(result["continuation"])
    history.append(client.tool_result("call_1", {"jobs": []}))
    client.complete(history, "Only use allowed tools", TOOLS)
    assert requests[-1][2]["input"][1] == reasoning
    assert requests[-1][2]["input"][-1] == {"type": "function_call_output", "call_id": "call_1", "output": '{"jobs": []}'}


def test_deepseek_preserves_reasoning_and_tool_call_context(monkeypatch):
    response = deepseek_response()
    requests = mock_response(monkeypatch, response)
    client = provider.ProviderClient("deepseek", "deepseek-test", FAKE_KEY)
    history = [client.user_message("列出任务")]
    result = client.complete(history, "Allowed tools only", TOOLS)
    payload = requests[0][2]
    assert payload["messages"] == [{"role": "system", "content": "Allowed tools only"}, *history]
    assert payload["tools"] == [{"type": "function", "function": TOOLS[0]}]
    assert payload["tool_choice"] == "auto"
    assert "thinking" not in payload
    assert payload["max_tokens"] == provider.MAX_OUTPUT_TOKENS
    assert result["continuation"] == [response["choices"][0]["message"]]
    assert result["text"] == ""
    history.extend(result["continuation"])
    history.append(client.tool_result("call_1", {"jobs": []}))
    client.complete(history, "Allowed tools only", TOOLS)
    assert requests[-1][2]["messages"][-2]["reasoning_content"] == "A private intermediate explanation"
    assert requests[-1][2]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"jobs": []}'}


def test_deepseek_required_tool_avoids_thinking_mode_rejection(monkeypatch):
    def deepseek_post(name, key, payload, **kwargs):
        # The official API defaults to thinking mode and rejects required tools there.
        if payload["tool_choice"] == "required" and payload.get("thinking", {}).get("type", "enabled") != "disabled":
            raise provider.AgentError("DeepSeek rejects required tool choice in thinking mode", 502)
        return deepseek_response()

    monkeypatch.setattr(provider, "post_json", deepseek_post)
    result = provider.ProviderClient("deepseek", "deepseek-flash", FAKE_KEY).complete([], "Verify tools", TOOLS, require_tool=True)
    assert result["calls"] == [{"id": "call_1", "name": "list_jobs", "arguments": "{}"}]


@pytest.mark.parametrize(
    "response",
    [
        openai_response(openai_call(), openai_call()),
        openai_response(*(openai_call(f"call_{index}") for index in range(provider.MAX_CALLS_PER_RESPONSE + 1))),
        openai_response(openai_call(call_id="bad/id")),
        openai_response(openai_call(call_id=[])),
        openai_response(openai_call(name="run-shell")),
        openai_response(openai_call(arguments={})),
        openai_response(openai_call(arguments="x" * (32 * 1024 + 1))),
        openai_response(openai_message("x" * (32 * 1024 + 1))),
        openai_response(openai_message({"not": "text"})),
        openai_response({"type": "computer_call"}),
        openai_response(openai_call(), status="incomplete"),
        openai_response(openai_call(), status="failed", error={"message": FAKE_KEY}),
        openai_response(),
        {"output": None},
    ],
)
def test_openai_malformed_or_incomplete_responses_execute_no_tools(monkeypatch, response):
    mock_response(monkeypatch, response)
    with pytest.raises(provider.AgentError, match="没有执行任务") as failure:
        provider.ProviderClient("openai", "test-model", FAKE_KEY).complete([], "", TOOLS)
    assert failure.value.status_code == 502
    assert FAKE_KEY not in str(failure.value)


@pytest.mark.parametrize("finish", ["length", "content_filter", "insufficient_system_resource", "unknown"])
def test_deepseek_incomplete_finish_rejects_tools(monkeypatch, finish):
    mock_response(monkeypatch, deepseek_response(finish=finish))
    with pytest.raises(provider.AgentError, match="没有执行任务"):
        provider.ProviderClient("deepseek", "test-model", FAKE_KEY).complete([], "", TOOLS)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "Wrong role"},
        {"role": "assistant", "content": []},
        {"role": "assistant", "tool_calls": "invalid"},
        {"role": "assistant", "tool_calls": [{"type": "computer", "id": "call_1"}]},
        {"role": "assistant", "content": {"wrong": "type"}},
    ],
)
def test_deepseek_malformed_responses_rejected(monkeypatch, message):
    mock_response(monkeypatch, deepseek_response(message=message))
    with pytest.raises(provider.AgentError, match="没有执行任务"):
        provider.ProviderClient("deepseek", "test-model", FAKE_KEY).complete([], "", TOOLS)


@pytest.mark.parametrize("invalid", ["duplicate", "too_many", "oversized", "invalid_id"])
def test_deepseek_invalid_tool_calls_are_rejected_before_execution(monkeypatch, invalid):
    response = deepseek_response()
    calls = response["choices"][0]["message"]["tool_calls"]
    if invalid == "duplicate":
        calls.append(copy.deepcopy(calls[0]))
    elif invalid == "too_many":
        calls.extend({**copy.deepcopy(calls[0]), "id": f"call_extra_{index}"} for index in range(provider.MAX_CALLS_PER_RESPONSE))
    elif invalid == "oversized":
        calls[0]["function"]["arguments"] = "x" * (32 * 1024 + 1)
    else:
        calls[0]["id"] = "bad/id"
    mock_response(monkeypatch, response)
    with pytest.raises(provider.AgentError, match="没有执行任务"):
        provider.ProviderClient("deepseek", "test-model", FAKE_KEY).complete([], "", TOOLS)


@pytest.mark.parametrize("name", ["openai", "deepseek"])
def test_upstream_secret_echo_is_redacted_from_display_arguments_and_history(monkeypatch, name):
    if name == "openai":
        response = openai_response(openai_message(FAKE_KEY), openai_call(arguments=json.dumps({"label": FAKE_KEY})))
    else:
        response = deepseek_response()
        message = response["choices"][0]["message"]
        message["content"] = message["reasoning_content"] = FAKE_KEY
        message["tool_calls"][0]["function"]["arguments"] = json.dumps({"label": FAKE_KEY})
    mock_response(monkeypatch, response)
    result = provider.ProviderClient(name, "test-model", FAKE_KEY).complete([], "", TOOLS)
    assert FAKE_KEY not in json.dumps(result)
    assert result["text"] == "[API Key 已隐藏]"
    assert json.loads(result["calls"][0]["arguments"])["label"] == "[API Key 已隐藏]"


def test_redaction_removes_credential_echoes_in_nested_object_keys(monkeypatch):
    response = openai_response(openai_message("Ready"))
    response["output"][0]["extra"] = {FAKE_KEY: [{"nested": FAKE_KEY}]}
    mock_response(monkeypatch, response)
    result = provider.ProviderClient("openai", "test-model", FAKE_KEY).complete([], "", TOOLS)
    assert FAKE_KEY not in json.dumps(result)


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status, self.offset = body, status, 0
        self.read_sizes = []

    def read1(self, size):
        self.read_sizes.append(size)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeConnection:
    def __init__(self, response):
        self.response, self.requests, self.timeouts = response, [], []
        self.sock = self
        self.closed = False
        self.error = None
        self.tunnels = []

    def set_tunnel(self, host, **kwargs):
        self.tunnels.append((host, kwargs))

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if self.error:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    connections, destinations = [], []
    for variable in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)

    def install(body=b'{"ok":true}', status=200, error=None):
        response = FakeResponse(body, status)
        connection = FakeConnection(response)
        connection.error = error

        def connect(host, **kwargs):
            destinations.append((host, kwargs))
            connections.append(connection)
            return connection

        monkeypatch.setattr(provider.http.client, "HTTPSConnection", connect)
        return connection, response, destinations

    return install


@pytest.mark.parametrize("name", ["openai", "deepseek"])
def test_https_transport_uses_fixed_official_destination_and_closes(transport, name):
    connection, response, destinations = transport()
    assert provider.post_json(name, FAKE_KEY, {"message": "中文"}, timeout=7) == {"ok": True}
    assert len(destinations) == 1
    host, options = destinations[0]
    assert host == provider.PROVIDERS[name]["host"]
    assert not connection.tunnels
    assert options["timeout"] == 7
    assert options["context"].check_hostname
    method, path, request = connection.requests[0]
    assert (method, path) == ("POST", provider.PROVIDERS[name]["path"])
    assert request["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(request["body"]) == {"message": "中文"}
    assert response.offset == len(response.body)
    assert connection.closed
    assert all(0 < timeout <= 7 for timeout in connection.timeouts)


@pytest.mark.parametrize("name,path", [("openai", "/v1/models"), ("deepseek", "/models")])
def test_list_models_uses_fixed_get_and_only_returns_public_metadata(transport, name, path):
    connection, _, destinations = transport(
        json.dumps({"data": [{"id": "model-one", "name": f"Display {FAKE_KEY}", "private": FAKE_KEY}, {"id": "model-two"}]}).encode()
    )
    result = provider.ProviderClient(name, "unused", FAKE_KEY).list_models()
    assert result == [{"id": "model-one", "label": "Display [API Key 已隐藏]"}, {"id": "model-two", "label": "model-two"}]
    method, requested_path, request = connection.requests[0]
    assert (method, requested_path, request["body"]) == ("GET", path, None)
    assert destinations[0][0] == provider.PROVIDERS[name]["host"]
    assert FAKE_KEY not in json.dumps(result)
    assert connection.closed


@pytest.mark.parametrize("items", [None, {}, [], [None, {"id": "<invalid>"}], [{"id": "valid"}] * 1001])
def test_invalid_or_empty_model_catalog_is_a_safe_error(transport, items):
    transport(json.dumps({"data": items}).encode())
    with pytest.raises(provider.AgentError):
        provider.ProviderClient("deepseek", "unused", FAKE_KEY).list_models()


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"])
@pytest.mark.parametrize("name", ["openai", "deepseek"])
def test_proxy_connect_targets_official_tls_host_without_leaking_credentials(transport, monkeypatch, variable, name):
    monkeypatch.setenv(variable, "http://proxy-user:proxy-p%40ss@127.0.0.1:7688")
    connection, _, destinations = transport()
    assert provider.post_json(name, FAKE_KEY, {}) == {"ok": True}
    assert destinations[0][0] == "127.0.0.1"
    assert destinations[0][1]["port"] == 7688
    context = destinations[0][1]["context"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert connection.tunnels == [
        (provider.PROVIDERS[name]["host"], {"port": 443, "headers": {"Proxy-Authorization": "Basic cHJveHktdXNlcjpwcm94eS1wQHNz"}})
    ]
    assert FAKE_KEY not in json.dumps(connection.tunnels)
    assert "Proxy-Authorization" not in connection.requests[0][2]["headers"]
    assert connection.requests[0][2]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert connection.closed


@pytest.mark.parametrize("bypass", ["*", "api.openai.com", ".openai.com", "api.openai.com:443"])
def test_no_proxy_bypasses_proxy_for_matching_official_host(transport, monkeypatch, bypass):
    monkeypatch.setenv("HTTPS_PROXY", "socks5://proxy.invalid:1080")
    monkeypatch.setenv("NO_PROXY", bypass)
    connection, _, destinations = transport()
    provider.post_json("openai", FAKE_KEY, {})
    assert destinations[0][0] == "api.openai.com"
    assert not connection.tunnels


def test_proxy_environment_precedence_and_nonmatching_bypass(transport, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://uppercase.invalid:8080")
    monkeypatch.setenv("https_proxy", "http://lowercase.invalid:8081")
    monkeypatch.setenv("ALL_PROXY", "socks5://fallback.invalid:1080")
    monkeypatch.setenv("no_proxy", "api.openai.com:444,deepseek.com")
    connection, _, destinations = transport()
    provider.post_json("openai", FAKE_KEY, {})
    assert destinations[0][0] == "lowercase.invalid"
    assert destinations[0][1]["port"] == 8081
    assert connection.tunnels[0][0] == "api.openai.com"


@pytest.mark.parametrize(
    "address,message",
    [
        ("socks5://proxy.invalid:1080", "代理协议暂不支持"),
        ("https://proxy.invalid:8080", "代理协议暂不支持"),
        ("proxy.invalid:8080", "代理配置无效"),
        ("http://proxy.invalid:0", "代理配置无效"),
        ("http://proxy.invalid:65536", "代理配置无效"),
        ("http://proxy.invalid/path", "代理配置无效"),
        ("http://proxy.invalid?query=value", "代理配置无效"),
        ("http://proxy.invalid\r\nHeader:value", "代理配置无效"),
        ("http://user:%0d%0apassword@proxy.invalid", "代理配置无效"),
        (f"http://user:{FAKE_KEY}@proxy.invalid:invalid", "代理配置无效"),
    ],
)
def test_invalid_proxy_fails_safely_without_falling_back_to_direct(transport, monkeypatch, address, message):
    monkeypatch.setenv("HTTPS_PROXY", address)
    connection, _, destinations = transport()
    with pytest.raises(provider.AgentError, match=message) as failure:
        provider.post_json("openai", FAKE_KEY, {})
    assert FAKE_KEY not in str(failure.value)
    assert address not in str(failure.value)
    assert not destinations and not connection.requests


@pytest.mark.parametrize("code,message", [(407, "代理认证失败"), (403, "代理未能建立 CONNECT 隧道")])
def test_proxy_tunnel_errors_are_sanitized(transport, monkeypatch, code, message):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    connection, _, _ = transport(error=OSError(f"Tunnel connection failed: {code} {FAKE_KEY}"))
    with pytest.raises(provider.AgentError, match=message) as failure:
        provider.post_json("openai", FAKE_KEY, {})
    assert FAKE_KEY not in str(failure.value)
    assert connection.closed


@pytest.mark.parametrize("tunnel_status", [200, 403, 407])
def test_stdlib_tunnel_wraps_official_hostname_before_sending_model_authorization(monkeypatch, tunnel_status):
    for variable in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-user:proxy-password@127.0.0.1:7688")
    writes, tls_hosts, destinations = [], [], []

    class WireSocket:
        def setsockopt(self, *_args):
            pass

        def sendall(self, data):
            writes.append((bool(tls_hosts), bytes(data)))

        def makefile(self, *_args):
            response = (
                b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}'
                if tls_hosts
                else f"HTTP/1.1 {tunnel_status} Tunnel\r\n\r\n".encode()
            )
            return io.BytesIO(response)

        def settimeout(self, _timeout):
            pass

        def close(self):
            pass

    wire = WireSocket()

    def connect(destination, *_args, **_kwargs):
        destinations.append(destination)
        return wire

    context = ssl.create_default_context()

    def wrap(sock, *, server_hostname):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        tls_hosts.append(server_hostname)
        return sock

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(context, "wrap_socket", wrap)
    monkeypatch.setattr(provider.ssl, "create_default_context", lambda: context)
    if tunnel_status == 200:
        assert provider.post_json("openai", FAKE_KEY, {}) == {"ok": True}
    else:
        with pytest.raises(provider.AgentError, match="代理"):
            provider.post_json("openai", FAKE_KEY, {})
    assert destinations == [("127.0.0.1", 7688)]
    plain = b"".join(data for encrypted, data in writes if not encrypted)
    secure = b"".join(data for encrypted, data in writes if encrypted)
    assert b"CONNECT api.openai.com:443 HTTP/1." in plain
    assert b"Proxy-Authorization: Basic " in plain
    assert FAKE_KEY.encode() not in plain
    if tunnel_status != 200:
        assert not tls_hosts and not secure
        return
    assert tls_hosts == ["api.openai.com"]
    assert f"Authorization: Bearer {FAKE_KEY}".encode() in secure
    assert b"Host: api.openai.com\r\n" in secure
    assert b"Proxy-Authorization" not in secure


@pytest.mark.parametrize(
    "upstream_status,public_status,message",
    [
        (301, 502, "重定向"),
        (302, 502, "重定向"),
        (307, 502, "重定向"),
        (401, 502, "认证失败"),
        (402, 502, "账户余额不足"),
        (403, 502, "拒绝访问"),
        (429, 429, "限流或额度不足"),
        (400, 502, "参数无效"),
        (404, 502, "模型或接口不存在"),
        (422, 502, "无法处理请求参数"),
        (500, 502, "暂时不可用"),
    ],
)
def test_upstream_errors_and_redirects_are_sanitized_without_reading_body(transport, upstream_status, public_status, message):
    connection, response, destinations = transport(body=FAKE_KEY.encode(), status=upstream_status)
    with pytest.raises(provider.AgentError, match=message) as failure:
        provider.post_json("openai", FAKE_KEY, {})
    assert failure.value.status_code == public_status
    assert FAKE_KEY not in str(failure.value)
    assert not response.read_sizes
    assert len(destinations) == len(connection.requests) == 1
    assert connection.closed


@pytest.mark.parametrize(
    "error,message",
    [
        (TimeoutError(FAKE_KEY), "超时"),
        (OSError(FAKE_KEY), "连接模型服务失败"),
        (http.client.BadStatusLine(FAKE_KEY), "连接模型服务失败"),
        (socket.gaierror(FAKE_KEY), "DNS 解析失败"),
        (ssl.SSLError(FAKE_KEY), "TLS 连接或证书校验失败"),
    ],
)
@pytest.mark.parametrize("use_proxy", [False, True])
def test_network_errors_are_sanitized_and_connection_closed(transport, monkeypatch, error, message, use_proxy):
    if use_proxy:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    connection, _, _ = transport(error=error)
    with pytest.raises(provider.AgentError, match=message) as failure:
        provider.post_json("deepseek", FAKE_KEY, {})
    assert failure.value.status_code == 502
    assert FAKE_KEY not in str(failure.value)
    assert connection.closed


@pytest.mark.parametrize("body", [b"[]", b"null", b"not json", b'{"unterminated":', b"\xff"])
def test_non_object_or_invalid_json_responses_are_rejected(transport, body):
    connection, _, _ = transport(body)
    with pytest.raises(provider.AgentError, match="无效响应"):
        provider.post_json("openai", FAKE_KEY, {})
    assert connection.closed


def test_response_body_is_bounded_before_loading_json(transport):
    connection, response, _ = transport(b"x" * (provider.MAX_RESPONSE_BYTES + 10000))
    with pytest.raises(provider.AgentError, match="响应过大"):
        provider.post_json("openai", FAKE_KEY, {})
    assert response.offset == provider.MAX_RESPONSE_BYTES + 1
    assert max(response.read_sizes) <= 64 * 1024
    assert connection.closed


def test_oversized_request_fails_before_transmission(transport):
    connection, _, _ = transport()
    with pytest.raises(provider.AgentError, match="上下文过长") as failure:
        provider.post_json("openai", FAKE_KEY, {"content": "x" * (512 * 1024)})
    assert failure.value.status_code == 413
    assert not connection.requests
    assert connection.closed


def test_total_response_deadline_stops_slow_chunked_body(transport, monkeypatch):
    connection, response, _ = transport(b"x" * 65537)
    times = iter([100, 100, 100.5, 102])
    monkeypatch.setattr(provider.time, "monotonic", lambda: next(times))
    with pytest.raises(provider.AgentError, match="超时"):
        provider.post_json("deepseek", FAKE_KEY, {}, timeout=1)
    assert response.offset == 65536
    assert connection.closed
