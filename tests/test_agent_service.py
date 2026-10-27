from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import agent_provider, agent_service
from app.agent_provider import AgentError, ProviderPatch
from app.agent_service import AgentService
from app.agent_tools import AgentTools
from app.gromacs import Step
from app.job_store import JobStore

FAKE_KEY = "service-test-key-not-a-real-credential"


def model_response(*calls, text=""):
    normalized = [
        {"id": f"call_{index}", "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}
        for index, (name, arguments) in enumerate(calls)
    ]
    continuation = [
        {"type": "function_call", "call_id": item["id"], "name": item["name"], "arguments": item["arguments"]} for item in normalized
    ]
    if text:
        continuation.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    return {"calls": normalized, "text": text, "continuation": continuation}


def provider_script(monkeypatch, *responses):
    recorded = []
    pending = iter(responses)

    def complete(client, history, instructions, definitions, **kwargs):
        recorded.append(
            {"provider": client.provider, "model": client.model, "history": copy.deepcopy(history), "definitions": definitions, **kwargs}
        )
        response = next(pending)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    monkeypatch.setattr(agent_provider.ProviderClient, "complete", complete)
    return recorded


@pytest.fixture
def local(tmp_path, monkeypatch):
    for provider in agent_provider.PROVIDERS.values():
        monkeypatch.delenv(provider["environment"], raising=False)
    # A missed mock must never make a real model request or spend API credits.
    monkeypatch.setattr(agent_provider, "post_json", Mock(side_effect=AssertionError("Unexpected external model request")))
    store = JobStore(tmp_path / "runtime")
    settings = {"gmx_bin": "gmx", "default_ntmpi": 1, "default_ntomp": 2, "analysis_parallel": 2, "max_parallel": 1}
    service = AgentService(store.runtime_root)
    service.config.save(ProviderPatch(provider="openai", model="test-model", api_key=FAKE_KEY))
    tools = AgentTools(store, settings, Path(__file__).resolve().parents[1], lambda: {"available": True, "state": "ready"})
    return SimpleNamespace(service=service, store=store, tools=tools, settings=settings)


def queued_job(local):
    steps = [Step("Production", ["gmx", "mdrun", "-s", "input.tpr"])]
    meta = local.store.create("test run", {"workflow": "run_tpr", "gmx_bin": "gmx", "ntomp": 2}, [], steps)
    local.store.start(meta["id"], steps)
    return local.store.get(meta["id"])


def send(local, session_id, message="查看任务", request_id=None, context_job_id=None):
    return local.service.send(session_id, message, request_id or str(uuid4()), context_job_id, local.tools)


def proposed_cancel(local, monkeypatch):
    job = queued_job(local)
    provider_script(monkeypatch, model_response(("cancel_job", {"job_id": job["id"]})))
    session = local.service.create()
    state = send(local, session["id"], "取消这个任务", context_job_id=job["id"])
    assert state["actions"][0]["status"] == "pending"
    return job, state


def tool_outputs(history):
    return [json.loads(item["output"]) for item in history if item.get("type") == "function_call_output"]


def test_read_tools_use_real_store_and_continue_into_reply(local, monkeypatch):
    job = queued_job(local)
    calls = provider_script(monkeypatch, model_response(("list_jobs", {"limit": 3})), model_response(text="当前有 1 个排队任务。"))
    state = send(local, local.service.create()["id"], context_job_id=job["id"])
    assert state["status"] == "idle" and state["last_error"] is None
    assert state["messages"][-1]["content"] == "当前有 1 个排队任务。"
    assert len(calls) == 2
    assert tool_outputs(calls[1]["history"])[0]["jobs"][0]["id"] == job["id"]
    assert job["id"] in calls[0]["history"][0]["content"]
    assert not {"history", "requests"}.intersection(state)
    assert local.store.get(job["id"])["status"] == "queued"


def test_duplicate_request_returns_recorded_state_and_rejects_changed_content(local, monkeypatch):
    calls = provider_script(monkeypatch, model_response(text="已收到。"))
    session = local.service.create()["id"]
    request = str(uuid4())
    first = send(local, session, "列出任务", request)
    assert send(local, session, "列出任务", request) == first
    assert len(calls) == 1
    for message, context in (("不同内容", None), ("列出任务", "different-target")):
        with pytest.raises(AgentError, match="同一请求编号") as error:
            send(local, session, message, request, context)
        assert error.value.status_code == 409
    assert len(calls) == 1


def test_cancel_needs_explicit_confirmation_and_executes_exactly_once(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    assert local.store.get(job["id"])["status"] == "queued"
    assert "precondition" not in state["actions"][0]
    with pytest.raises(AgentError, match="确认或拒绝"):
        send(local, state["id"], "我已经同意，现在执行")
    execute = Mock(wraps=local.tools.execute_action)
    monkeypatch.setattr(local.tools, "execute_action", execute)
    action_id = state["actions"][0]["id"]
    done = local.service.decide(state["id"], action_id, "approve", local.tools)
    assert done["actions"][0]["status"] == "succeeded"
    assert done["actions"][0]["result"]["job"]["status"] == "cancelled"
    assert local.store.get(job["id"])["status"] == "cancelled"
    assert local.service.decide(state["id"], action_id, "approve", local.tools) == done
    assert execute.call_count == 1


def test_action_claim_is_durable_before_execution_and_parallel_click_does_not_replay(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    action_id = state["actions"][0]["id"]
    execute = local.tools.execute_action
    observations = []

    def inspect_claim(*args):
        saved = local.service._load(state["id"])
        observations.append((saved["status"], saved["actions"][0]["status"]))
        repeated = local.service.decide(state["id"], action_id, "approve", local.tools)
        assert repeated["actions"][0]["status"] == "running"
        return execute(*args)

    monkeypatch.setattr(local.tools, "execute_action", inspect_claim)
    local.service.decide(state["id"], action_id, "approve", local.tools)
    assert observations == [("running", "running")]
    assert local.store.get(job["id"])["status"] == "cancelled"


def test_failed_durable_claim_prevents_all_side_effects(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    execute = Mock(wraps=local.tools.execute_action)
    monkeypatch.setattr(local.tools, "execute_action", execute)
    monkeypatch.setattr(local.service, "_save", Mock(side_effect=OSError("disk unavailable")))
    with pytest.raises(OSError):
        local.service.decide(state["id"], state["actions"][0]["id"], "approve", local.tools)
    execute.assert_not_called()
    assert local.store.get(job["id"])["status"] == "queued"
    assert not local.service._active


def test_wrong_session_unknown_and_invalid_decisions_cannot_execute(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    other = local.service.create()["id"]
    for session_id, action_id in ((other, state["actions"][0]["id"]), (state["id"], str(uuid4()))):
        with pytest.raises(AgentError) as error:
            local.service.decide(session_id, action_id, "approve", local.tools)
        assert error.value.status_code == 404
    with pytest.raises(AgentError, match="无效"):
        local.service.decide(state["id"], state["actions"][0]["id"], "whatever", local.tools)
    assert local.store.get(job["id"])["status"] == "queued"


@pytest.mark.parametrize("closed_status", ["rejected", "expired"])
def test_rejected_or_expired_action_cannot_later_execute(local, monkeypatch, closed_status):
    job, state = proposed_cancel(local, monkeypatch)
    action_id = state["actions"][0]["id"]
    if closed_status == "rejected":
        local.service.decide(state["id"], action_id, "reject", local.tools)
    else:
        saved = local.service._load(state["id"])
        saved["actions"][0]["expires_epoch"] = time.time() - 1
        local.service._save(saved)
    result = local.service.decide(state["id"], action_id, "approve", local.tools)
    assert result["actions"][0]["status"] == closed_status
    assert local.store.get(job["id"])["status"] == "queued"


def test_restarted_running_action_becomes_uncertain_and_never_replays(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    saved = local.service._load(state["id"])
    saved["status"] = "running"
    saved["actions"][0]["status"] = "running"
    local.service._save(saved)
    restarted = AgentService(local.store.runtime_root)
    recovered = restarted.get(state["id"])
    assert recovered["status"] == "idle"
    assert recovered["actions"][0]["status"] == "uncertain"
    result = restarted.decide(state["id"], state["actions"][0]["id"], "approve", local.tools)
    assert result["actions"][0]["status"] == "uncertain"
    assert local.store.get(job["id"])["status"] == "queued"


@pytest.mark.parametrize("error,status", [(ValueError("private-path"), "failed"), (OSError("secret-credential"), "uncertain")])
def test_failed_execution_is_sanitized_and_never_retried(local, monkeypatch, error, status):
    _, state = proposed_cancel(local, monkeypatch)
    execute = Mock(side_effect=error)
    monkeypatch.setattr(local.tools, "execute_action", execute)
    action_id = state["actions"][0]["id"]
    result = local.service.decide(state["id"], action_id, "approve", local.tools)
    assert result["actions"][0]["status"] == status
    assert str(error) not in json.dumps(result)
    local.service.decide(state["id"], action_id, "approve", local.tools)
    assert execute.call_count == 1


def test_unknown_tools_invalid_json_and_schema_errors_return_safe_results(local, monkeypatch):
    responses = [
        model_response(
            ("run_shell", {"command": "cat /secret/path"}),
            ("list_jobs", "{invalid-json-with-secret"),
            ("list_jobs", {"limit": "secret-parameter"}),
            ("list_jobs", "[]"),
            ("list_jobs", '{"limit":NaN}'),
        ),
        model_response(text="请提供有效的任务查询条件。"),
    ]
    calls = provider_script(monkeypatch, *responses)
    execute = Mock(wraps=local.tools.execute_action)
    monkeypatch.setattr(local.tools, "execute_action", execute)
    state = send(local, local.service.create()["id"])
    outputs = tool_outputs(calls[1]["history"])
    assert len(outputs) == 5 and all("error" in item for item in outputs)
    assert "secret" not in json.dumps(outputs)
    assert state["actions"] == []
    execute.assert_not_called()


def test_tool_exception_paths_and_credentials_are_not_returned_to_provider(local, monkeypatch):
    calls = provider_script(monkeypatch, model_response(("list_jobs", {})), model_response(text="查询暂时失败。"))
    monkeypatch.setattr(local.tools, "run_read", Mock(side_effect=RuntimeError(f"/private/runtime {FAKE_KEY}")))
    state = send(local, local.service.create()["id"])
    outputs = tool_outputs(calls[1]["history"])
    assert outputs[0]["error"]
    assert "/private" not in json.dumps(outputs) and FAKE_KEY not in json.dumps(outputs)
    assert state["last_error"] is None


def test_unexpected_provider_exception_is_safe_and_releases_capacity(local, monkeypatch):
    calls = provider_script(monkeypatch, RuntimeError(f"upstream leaked {FAKE_KEY}"), model_response(text="恢复正常。"))
    session = local.service.create()["id"]
    state = send(local, session)
    assert state["status"] == "idle" and state["last_error"]
    assert FAKE_KEY not in json.dumps(state)
    assert send(local, session)["last_error"] is None
    assert len(calls) == 2


def test_round_budget_stops_model_polling(local, monkeypatch):
    calls = provider_script(monkeypatch, *[model_response(("workspace_summary", {})) for _ in range(6)])
    state = send(local, local.service.create()["id"])
    assert len(calls) == 6
    assert "模型调用上限" in state["last_error"]
    assert state["status"] == "idle"


def test_tool_budget_stops_before_excess_calls_run(local, monkeypatch):
    responses = [model_response(*[("workspace_summary", {}) for _ in range(8)]) for _ in range(3)]
    calls = provider_script(monkeypatch, *responses)
    run_read = Mock(wraps=local.tools.run_read)
    monkeypatch.setattr(local.tools, "run_read", run_read)
    state = send(local, local.service.create()["id"])
    assert "工具调用已达到上限" in state["last_error"]
    assert len(calls) == 3 and run_read.call_count == 16


def test_at_most_four_actions_are_proposed_per_turn(local, monkeypatch):
    job = queued_job(local)
    calls = provider_script(monkeypatch, model_response(*[("cancel_job", {"job_id": job["id"]}) for _ in range(6)]))
    state = send(local, local.service.create()["id"])
    assert len(state["actions"]) == 4 and len(calls) == 1
    assert all(action["status"] == "pending" for action in state["actions"])
    assert local.store.get(job["id"])["status"] == "queued"


def test_turn_budget_preserves_existing_history(local, monkeypatch):
    monkeypatch.setattr(agent_service, "MAX_TURNS", 2)
    calls = provider_script(monkeypatch, model_response(text="第一次。"), model_response(text="第二次。"))
    session = local.service.create()["id"]
    send(local, session)
    final = send(local, session)
    with pytest.raises(AgentError, match="上下文上限"):
        send(local, session)
    assert local.service.get(session) == final and len(calls) == 2


def test_session_storage_limit_requires_clearing_history(local, monkeypatch):
    monkeypatch.setattr(agent_service, "MAX_SESSIONS", 2)
    first = local.service.create()["id"]
    second = local.service.create()["id"]
    with pytest.raises(AgentError) as error:
        local.service.create()
    assert error.value.status_code == 409
    local.service.delete(first)
    assert local.service.create()["id"] != second


def test_context_budget_stops_continuation_and_large_tool_result_is_replaced(local, monkeypatch):
    calls = provider_script(monkeypatch, model_response(("list_jobs", {})), model_response(text="请缩小范围。"))
    monkeypatch.setattr(local.tools, "run_read", lambda *args: {"data": "x" * (25 * 1024)})
    state = send(local, local.service.create()["id"])
    assert state["last_error"] is None
    assert "查询结果过大" in tool_outputs(calls[1]["history"])[0]["error"]
    monkeypatch.setattr(agent_service, "MAX_CONTEXT_BYTES", 300)
    calls = provider_script(monkeypatch, model_response(("list_jobs", {})))
    monkeypatch.setattr(local.tools, "run_read", lambda *args: {"data": "x" * 500})
    state = send(local, local.service.create()["id"])
    assert "上下文上限" in state["last_error"] and len(calls) == 1


def test_turn_deadline_prevents_provider_call(local, monkeypatch):
    monkeypatch.setattr(agent_service, "TURN_SECONDS", 0)
    calls = provider_script(monkeypatch)
    state = send(local, local.service.create()["id"])
    assert "时间上限" in state["last_error"] and not calls


def test_single_session_exclusion_global_capacity_and_live_restore(local, monkeypatch):
    entered = threading.Barrier(3)
    release = threading.Event()
    calls = []

    def blocked(client, *args, **kwargs):
        calls.append(client.provider)
        entered.wait(timeout=10)
        assert release.wait(timeout=10)
        return model_response(text="已完成。")

    monkeypatch.setattr(agent_provider.ProviderClient, "complete", blocked)
    first, second, third = [local.service.create()["id"] for _ in range(3)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send, local, session) for session in (first, second)]
        try:
            entered.wait(timeout=10)
            assert local.service.get(first)["status"] == "running"
            with pytest.raises(AgentError) as error:
                send(local, first)
            assert error.value.status_code == 409
            with pytest.raises(AgentError) as error:
                local.service.delete(first)
            assert error.value.status_code == 409
            with pytest.raises(AgentError) as error:
                send(local, third)
            assert error.value.status_code == 429
        finally:
            release.set()
        assert all(future.result(timeout=10)["status"] == "idle" for future in futures)
    assert len(calls) == 2
    assert local.service.get(third)["messages"] == []


def test_provider_switch_requires_new_session_and_history_survives_restart(local, monkeypatch):
    calls = provider_script(monkeypatch, model_response(text="第一段。"), model_response(text="新的服务商。"))
    original = local.service.create()["id"]
    old_state = send(local, original)
    local.service.config.save(ProviderPatch(provider="deepseek", model="deepseek-test", api_key=FAKE_KEY))
    with pytest.raises(AgentError, match="服务商已切换"):
        send(local, original)
    restarted = AgentService(local.store.runtime_root)
    assert restarted.get(original) == old_state
    new = local.service.create()["id"]
    assert send(local, new)["provider"] == "deepseek"
    assert [call["provider"] for call in calls] == ["openai", "deepseek"]
    assert local.service.delete(original) == {"deleted": original}
    with pytest.raises(AgentError) as error:
        restarted.get(original)
    assert error.value.status_code == 404


@pytest.fixture
def api(local, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "STORE", local.store)
    monkeypatch.setattr(main, "load_settings", lambda _: local.settings)
    monkeypatch.setattr(main, "read_worker_status", lambda _: {"available": True, "state": "ready"})
    # No lifespan context: do not acquire application locks or start a worker.
    client = TestClient(main.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    yield SimpleNamespace(client=client, main=main, **vars(local))
    client.close()


def test_api_runs_real_read_and_confirmed_cancel_with_private_responses(api, monkeypatch):
    job = queued_job(api)
    calls = provider_script(
        monkeypatch,
        model_response(("list_jobs", {})),
        model_response(text="有一个任务。"),
        model_response(("cancel_job", {"job_id": job["id"]})),
    )
    session = api.client.post("/api/agent/sessions").json()["id"]
    response = api.client.post(f"/api/agent/sessions/{session}/messages", json={"message": "查看任务", "request_id": str(uuid4())})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["messages"][-1]["content"] == "有一个任务。"
    response = api.client.post(f"/api/agent/sessions/{session}/messages", json={"message": "取消任务", "request_id": str(uuid4())})
    action = response.json()["actions"][0]
    assert local_status(api, job) == "queued"
    confirmed = api.client.post(f"/api/agent/sessions/{session}/actions/{action['id']}", json={"decision": "approve"})
    assert confirmed.status_code == 200 and local_status(api, job) == "cancelled"
    assert len(calls) == 3
    assert FAKE_KEY not in confirmed.text


def local_status(api, job):
    return api.store.get(job["id"])["status"]


def test_model_catalog_uses_selected_saved_profile_without_changing_configuration(api, monkeypatch):
    deepseek_key = "test-deepseek-credential-for-model-list"
    api.service.config.save(ProviderPatch(provider="deepseek", model="deepseek-flash", api_key=deepseek_key))
    api.service.config.save(ProviderPatch(provider="openai", model="test-model"))
    before = api.service.config.path.read_bytes()
    calls = []

    def models(client):
        calls.append((client.provider, client.api_key))
        return [{"id": "deepseek-v4-pro", "label": f"Pro {FAKE_KEY}"}]

    monkeypatch.setattr(agent_provider.ProviderClient, "list_models", models)
    response = api.client.get("/api/agent/models?provider=deepseek")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert calls == [("deepseek", deepseek_key)]
    assert response.json()["models"][0]["id"] == "deepseek-v4-pro"
    assert FAKE_KEY not in response.text and deepseek_key not in response.text
    assert api.service.config.path.read_bytes() == before
    assert api.client.get("/api/agent/models?provider=unknown").status_code == 422
    assert len(calls) == 1


def test_model_switch_reuses_saved_key_and_same_deepseek_session(local, monkeypatch):
    local.service.config.save(ProviderPatch(provider="deepseek", model="deepseek-flash", api_key=FAKE_KEY))
    calls = provider_script(monkeypatch, model_response(text="First"), model_response(text="Second"))
    session = local.service.create()["id"]
    send(local, session, "你好")
    local.service.config.save(ProviderPatch(provider="deepseek", model="deepseek-v4-pro"))
    state = send(local, session, "继续")
    assert [call["model"] for call in calls] == ["deepseek-flash", "deepseek-v4-pro"]
    assert state["id"] == session and state["model"] == "deepseek-v4-pro"
    assert local.service.config.credentials()[2] == FAKE_KEY


def test_session_conditionals_skip_unchanged_payloads_and_invalidate_on_change(api, monkeypatch):
    session = api.client.post("/api/agent/sessions").json()["id"]
    url = f"/api/agent/sessions/{session}"
    first = api.client.get(url)
    etag = first.headers["etag"]
    unchanged = api.client.get(url, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304 and unchanged.content == b""
    assert unchanged.headers["cache-control"] == "no-store"
    provider_script(monkeypatch, model_response(text="状态已更新"))
    assert api.client.post(url + "/messages", json={"message": "你好", "request_id": str(uuid4())}).status_code == 200
    changed = api.client.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert changed.json()["messages"][-1]["content"] == "状态已更新"


def test_model_catalog_missing_key_or_failure_releases_capacity(local, monkeypatch):
    models = Mock(side_effect=AgentError("模型列表不可用", 502))
    monkeypatch.setattr(agent_provider.ProviderClient, "list_models", models)
    with pytest.raises(AgentError, match="API Key"):
        local.service.list_models("deepseek")
    models.assert_not_called()
    with pytest.raises(AgentError, match="模型列表不可用"):
        local.service.list_models("openai")
    assert local.service._capacity.acquire(blocking=False)
    assert local.service._capacity.acquire(blocking=False)
    local.service._capacity.release()
    local.service._capacity.release()


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://evil.example"}, {"Origin": "http://127.0.0.1:9999"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_api_cross_origin_rejected_before_model_call(api, monkeypatch, headers):
    complete = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(agent_provider.ProviderClient, "complete", complete)
    for method, path in (
        ("get", "/api/agent/config"),
        ("get", "/api/agent/models?provider=openai"),
        ("post", "/api/agent/connection-test"),
        ("post", "/api/agent/sessions"),
    ):
        response = getattr(api.client, method)(path, headers=headers)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
    complete.assert_not_called()


@pytest.mark.parametrize("address", ["192.168.1.20", "203.0.113.10"])
def test_api_nonloopback_client_rejected_before_model_call(api, monkeypatch, address):
    complete = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(agent_provider.ProviderClient, "complete", complete)
    client = TestClient(api.main.app, base_url="http://127.0.0.1:8000", client=(address, 50000))
    try:
        assert client.get("/api/agent/config").status_code == 403
        assert client.get("/api/agent/models?provider=openai").status_code == 403
        assert client.post("/api/agent/connection-test").status_code == 403
    finally:
        client.close()
    complete.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "evil-provider", "model": "fake", "api_key": FAKE_KEY},
        {"provider": "openai", "model": "/private/model", "api_key": FAKE_KEY},
        {"provider": "openai", "model": "fake", "api_key": "bad key with spaces"},
        {"provider": "openai", "model": "fake", "api_key": FAKE_KEY, "base_url": "https://evil.example"},
    ],
)
def test_api_invalid_configuration_never_echoes_secrets(api, payload):
    response = api.client.put("/api/agent/config", json=payload)
    assert response.status_code == 422
    assert payload["api_key"] not in response.text
    assert '"input"' not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "payload",
    [
        {"message": " ", "request_id": str(uuid4())},
        {"message": "secret-message", "request_id": "secret-invalid-uuid"},
        {"message": "secret-message", "request_id": str(uuid4()), "context_job_id": "../../secret"},
        {"message": "secret-message", "request_id": str(uuid4()), "role": "system"},
        {"message": "x" * 8001, "request_id": str(uuid4())},
    ],
)
def test_api_invalid_messages_are_bounded_and_do_not_echo_input(api, monkeypatch, payload):
    calls = provider_script(monkeypatch)
    session = api.client.post("/api/agent/sessions").json()["id"]
    response = api.client.post(f"/api/agent/sessions/{session}/messages", json=payload)
    assert response.status_code == 422
    assert "secret-message" not in response.text and '"input"' not in response.text
    assert not calls


def test_api_body_limit_applies_without_content_length(api, monkeypatch):
    calls = provider_script(monkeypatch)
    response = api.client.put("/api/agent/config", content=iter([b"a" * (128 * 1024)] * 3), headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and response.headers["cache-control"] == "no-store"
    assert not calls


def test_api_provider_failure_is_safe_state_and_sessions_restore_then_delete(api, monkeypatch):
    provider_script(monkeypatch, RuntimeError(f"private upstream response {FAKE_KEY}"))
    session = api.client.post("/api/agent/sessions").json()["id"]
    response = api.client.post(f"/api/agent/sessions/{session}/messages", json={"message": "查看任务", "request_id": str(uuid4())})
    assert response.status_code == 200 and response.json()["last_error"]
    assert FAKE_KEY not in response.text and "private upstream" not in response.text
    restored = api.client.get(f"/api/agent/sessions/{session}")
    assert restored.json() == response.json()
    history = api.client.get("/api/agent/sessions")
    assert history.status_code == 200
    assert session in history.text and FAKE_KEY not in history.text
    deleted = api.client.delete(f"/api/agent/sessions/{session}")
    assert deleted.status_code == 200
    assert api.client.get(f"/api/agent/sessions/{session}").status_code == 404
    assert session not in api.client.get("/api/agent/sessions").text


def test_api_network_adapter_redacts_credential_echo_from_input_and_output(api, monkeypatch):
    posted = []

    def post(provider, key, payload, **kwargs):
        posted.append(copy.deepcopy(payload))
        return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"echo {FAKE_KEY}"}]}]}

    monkeypatch.setattr(agent_provider, "post_json", post)
    session = api.client.post("/api/agent/sessions").json()["id"]
    response = api.client.post(
        f"/api/agent/sessions/{session}/messages", json={"message": f"我误贴了 {FAKE_KEY}", "request_id": str(uuid4())}
    )
    assert response.status_code == 200
    assert FAKE_KEY not in response.text
    assert FAKE_KEY not in json.dumps(posted)
    stored = AgentService(api.store.runtime_root)._load(session)
    assert FAKE_KEY not in json.dumps(stored)


def wait_idle(service, session_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = service.get(session_id)
        if state["status"] == "idle":
            return state
        time.sleep(0.01)
    pytest.fail("Background operation did not finish")


def test_background_preparation_exposes_progress_and_releases_model_slots(local, monkeypatch):
    from app.operations import current_operation

    entered = threading.Barrier(3)
    release = threading.Event()

    def read(*_):
        operation = current_operation()
        operation.report({"phase": "preflight", "message": "正在检查轨迹…"})
        entered.wait(timeout=5)
        while not release.wait(0.01):
            operation.checkpoint()
        operation.checkpoint()
        return {"ok": True}

    monkeypatch.setattr(local.tools, "run_read", read)
    provider_script(
        monkeypatch,
        model_response(("list_jobs", {})),
        model_response(("list_jobs", {})),
        model_response(text="模型仍可响应。"),
        model_response(text="第二项完成。"),
    )
    first, second, third = [local.service.create()["id"] for _ in range(3)]
    try:
        for session in (first, second):
            state = local.service.send(session, "检查输入", str(uuid4()), None, local.tools, background=True)
            assert state["status"] == "running"
        entered.wait(timeout=5)
        assert local.service.get(first)["operation"]["phase"] == "preflight"
        assert send(local, third)["messages"][-1]["content"] == "模型仍可响应。"
        assert local.service.cancel(first)["operation"]["cancel_requested"]
        cancelled = wait_idle(local.service, first)
        assert "已取消" in cancelled["last_error"] and cancelled["actions"] == []
        assert local.service.get(second)["status"] == "running"
        release.set()
        assert wait_idle(local.service, second)["last_error"] is None
    finally:
        release.set()
        local.service.close()
    assert local.service.get(second)["last_error"] is None


def test_cancelled_approved_background_action_never_executes_or_replays(local, monkeypatch):
    from app.operations import current_operation

    job, state = proposed_cancel(local, monkeypatch)
    entered = threading.Event()
    original = local.tools.execute_action

    def prepare(*args):
        operation = current_operation()
        operation.report({"phase": "copy", "copied_bytes": 10, "total_bytes": 100})
        entered.set()
        while True:
            operation.checkpoint()
            time.sleep(0.01)
        return original(*args)

    execute = Mock(side_effect=prepare)
    monkeypatch.setattr(local.tools, "execute_action", execute)
    action_id = state["actions"][0]["id"]
    try:
        running = local.service.decide(state["id"], action_id, "approve", local.tools, background=True)
        assert running["actions"][0]["status"] == "running"
        assert entered.wait(5)
        assert local.service.get(state["id"])["operation"]["copied_bytes"] == 10
        local.service.cancel(state["id"])
        # Transport retries of the cancellation never replay the approval.
        local.service.cancel(state["id"])
        result = wait_idle(local.service, state["id"])
        assert result["actions"][0]["status"] == "cancelled"
        assert local.store.get(job["id"])["status"] == "queued"
        assert local.service.decide(state["id"], action_id, "approve", local.tools) == result
        assert execute.call_count == 1
    finally:
        local.service.close()


def test_cancel_after_commit_keeps_successful_action_result(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    committed, release = threading.Event(), threading.Event()
    original = local.tools.execute_action

    def execute(*args):
        result = original(*args)
        committed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(local.tools, "execute_action", execute)
    try:
        local.service.decide(state["id"], state["actions"][0]["id"], "approve", local.tools, background=True)
        assert committed.wait(5)
        result = local.service.cancel(state["id"])
        assert not result["operation"]["cancellable"]
        assert not result["operation"].get("cancel_requested")
        assert local.store.get(job["id"])["status"] == "cancelled"
        release.set()
        assert wait_idle(local.service, state["id"])["actions"][0]["status"] == "succeeded"
    finally:
        release.set()
        local.service.close()


def test_local_budget_is_separate_from_model_budget(local, monkeypatch):
    ticks = [1000.0]
    monkeypatch.setattr(agent_service.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(agent_service, "TURN_SECONDS", 10)
    responses = iter([model_response(("list_jobs", {})), model_response(text="预检完成。")])

    def complete(*args, **kwargs):
        ticks[0] += 2
        return next(responses)

    def read(*args):
        ticks[0] += 200
        return {"ok": True}

    monkeypatch.setattr(agent_provider.ProviderClient, "complete", complete)
    monkeypatch.setattr(local.tools, "run_read", read)
    state = send(local, local.service.create()["id"])
    assert state["last_error"] is None
    assert state["messages"][-1]["content"] == "预检完成。"


def test_local_budget_timeout_prevents_action_preparation(local, monkeypatch):
    monkeypatch.setattr(agent_service, "LOCAL_SECONDS", 0)
    job = queued_job(local)
    provider_script(monkeypatch, model_response(("cancel_job", {"job_id": job["id"]})))
    prepare = Mock(wraps=local.tools.prepare_action)
    monkeypatch.setattr(local.tools, "prepare_action", prepare)
    state = send(local, local.service.create()["id"])
    assert "本地准备操作已达到时间上限" in state["last_error"]
    assert state["actions"] == [] and local.store.get(job["id"])["status"] == "queued"
    prepare.assert_not_called()


def test_api_background_request_and_local_only_cancel(api, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def complete(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return model_response(text="这条已取消的回复不能发布。")

    monkeypatch.setattr(agent_provider.ProviderClient, "complete", complete)
    session = api.client.post("/api/agent/sessions").json()["id"]
    response = api.client.post(
        f"/api/agent/sessions/{session}/messages",
        headers={"Prefer": "respond-async"},
        json={"message": "检查输入", "request_id": str(uuid4())},
    )
    assert response.status_code == 200 and response.json()["status"] == "running"
    try:
        assert entered.wait(5)
        forbidden = api.client.post(f"/api/agent/sessions/{session}/cancel", headers={"Origin": "https://evil.example"})
        assert forbidden.status_code == 403
        response = api.client.post(f"/api/agent/sessions/{session}/cancel")
        assert response.json()["operation"]["cancel_requested"]
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = api.client.get(f"/api/agent/sessions/{session}")
            if response.json()["status"] == "idle":
                break
            time.sleep(0.01)
        assert response.json()["status"] == "idle"
        assert "已取消" in response.json()["last_error"]
        assert "这条已取消的回复" not in response.text
    finally:
        release.set()


def test_executor_failure_releases_capacity_and_never_executes_approval(local, monkeypatch):
    job, state = proposed_cancel(local, monkeypatch)
    monkeypatch.setattr(local.service._executor, "submit", Mock(side_effect=RuntimeError("executor unavailable")))
    execute = Mock(wraps=local.tools.execute_action)
    monkeypatch.setattr(local.tools, "execute_action", execute)
    result = local.service.decide(state["id"], state["actions"][0]["id"], "approve", local.tools, background=True)
    assert result["status"] == "idle" and result["actions"][0]["status"] == "failed"
    assert "后台操作无法启动" in result["last_error"]
    assert not local.service._active and not local.service._operations
    assert local.service._local_capacity.acquire(blocking=False)
    assert local.service._local_capacity.acquire(blocking=False)
    local.service._local_capacity.release()
    local.service._local_capacity.release()
    execute.assert_not_called()
    assert local.store.get(job["id"])["status"] == "queued"
