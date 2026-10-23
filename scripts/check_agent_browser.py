"""Exercise AI orchestration UI against intercepted APIs without external calls."""

from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

APP = Path(__file__).resolve().parents[1] / "app"
ORIGIN = "http://gromacs.test"


def check(artifact_dir: Path | None = None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height, scheme in ((1440, 1000, "light"), (768, 1024, "light"), (390, 844, "dark"), (320, 740, "dark")):
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
                errors = []
                calls = []
                sessions = {}
                control = {"fail_config": True}
                providers = [
                    {
                        "id": "openai",
                        "label": "OpenAI",
                        "default_model": "gpt-6.1-sol",
                        "endpoint": "https://api.openai.com/v1",
                        "models": [],
                    },
                    {
                        "id": "deepseek",
                        "label": "DeepSeek",
                        "default_model": "deepseek-flash",
                        "endpoint": "https://api.deepseek.com",
                        "models": [
                            {"id": "deepseek-flash", "label": "DeepSeek Flash"},
                            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro"},
                        ],
                    },
                ]
                config = {
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                    "has_api_key": False,
                    "key_source": "none",
                    "providers": providers,
                    "profiles": {
                        provider["id"]: {"model": provider["default_model"], "has_api_key": False, "key_source": "none", "is_saved": False}
                        for provider in providers
                    },
                }
                job = {"id": "source", "name": "测试任务", "workflow": "protein_md", "status": "completed", "params": {}, "tags": []}

                def fixture(route):
                    path = urlsplit(route.request.url).path
                    body = route.request.post_data_json if route.request.post_data else None
                    calls.append((route.request.method, path, body))
                    if path == "/api/agent/config":
                        if route.request.method == "GET" and control["fail_config"]:
                            route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "暂时无法读取模型设置"}))
                            return
                        if route.request.method == "PUT":
                            if control.get("fail_save"):
                                route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "设置暂时无法保存"}))
                                return
                            profile = config["profiles"][body["provider"]]
                            profile["model"] = body["model"]
                            profile["is_saved"] = True
                            if body.get("clear_api_key"):
                                profile.update(has_api_key=False, key_source="none")
                            elif body.get("api_key"):
                                profile.update(has_api_key=True, key_source="saved")
                            config.update(provider=body["provider"], **profile)
                        payload = config
                    elif path == "/api/agent/models":
                        if control.get("fail_models"):
                            route.fulfill(status=502, content_type="application/json", body=json.dumps({"detail": "模型列表暂时不可用"}))
                            return
                        selected = parse_qs(urlsplit(route.request.url).query)["provider"][0]
                        assert selected == "deepseek"
                        payload = {
                            "provider": selected,
                            "models": [
                                *providers[1]["models"],
                                {"id": "deepseek-new-series", "label": '<img src=x onerror="window.agentXss=true">'},
                            ],
                        }
                    elif path == "/api/agent/connection-test":
                        if control.get("fail_test"):
                            route.fulfill(
                                status=502,
                                content_type="application/json",
                                body=json.dumps({"detail": "连接模型服务超时，请检查服务器网络。"}),
                            )
                            return
                        payload = {"ok": True, "message": "连接成功", "provider": config["provider"], "model": config["model"]}
                    elif path == "/api/agent/sessions":
                        if route.request.method == "GET":
                            payload = {
                                "sessions": [
                                    {
                                        "id": item["id"],
                                        "title": item["messages"][0]["content"] if item["messages"] else "新对话",
                                        "provider": item["provider"],
                                        "updated_at": "2026-10-22T12:00:00Z",
                                    }
                                    for item in sessions.values()
                                ],
                                "limit": 200,
                            }
                        else:
                            session_id = f"session-{len(sessions) + 1}"
                            payload = {
                                "id": session_id,
                                "provider": config["provider"],
                                "model": config["model"],
                                "status": "idle",
                                "messages": [],
                                "actions": [],
                                "last_error": None,
                            }
                            sessions[session_id] = payload
                    elif path.startswith("/api/agent/sessions/"):
                        parts = path.split("/")
                        payload = sessions[parts[4]]
                        if route.request.method == "DELETE":
                            del sessions[parts[4]]
                            payload = {"deleted": parts[4]}
                        elif len(parts) > 5 and parts[5] == "messages":
                            if body["message"] == "测试错误":
                                route.fulfill(
                                    status=401, content_type="application/json", body=json.dumps({"detail": "密钥不可用，请重新配置。"})
                                )
                                return
                            payload["messages"].append({"role": "user", "content": body["message"]})
                            payload["messages"].append(
                                {"role": "assistant", "content": '<img src=x onerror="window.agentXss=true">\n请核对操作预览。'}
                            )
                            if body["message"] == "生成方案":
                                payload["actions"] = [
                                    {
                                        "id": "retry",
                                        "tool": "retry_job",
                                        "title": "重试任务",
                                        "arguments": {"job_id": "source"},
                                        "preview": {"任务": "source"},
                                        "status": "pending",
                                    },
                                    {
                                        "id": "cancel",
                                        "tool": "cancel_job",
                                        "title": "取消任务",
                                        "preview": {"任务": "source"},
                                        "status": "pending",
                                    },
                                    {
                                        "id": "draft",
                                        "tool": "prepare_simulation",
                                        "title": "准备模拟草案",
                                        "preview": {"workflow": "protein_md", "mdp_temperature_k": 305},
                                        "status": "pending",
                                    },
                                ]
                        elif len(parts) > 5 and parts[5] == "actions":
                            action = next(item for item in payload["actions"] if item["id"] == parts[6])
                            action["status"] = "rejected" if body["decision"] == "reject" else "succeeded"
                            if body["decision"] == "approve":
                                action["result"] = (
                                    {"kind": "simulation_draft", "params": {"workflow": "protein_md", "mdp_temperature_k": 305}}
                                    if action["id"] == "draft"
                                    else {"kind": "job", "job": job}
                                )
                    elif path == "/api/health":
                        payload = {
                            "settings": {"gmx_bin": "gmx", "max_parallel": 1, "analysis_parallel": 2},
                            "gromacs": {"available": False},
                            "diagnostics": {},
                        }
                    elif path == "/api/product":
                        payload = {"templates": [], "projects": [], "defaults": {}}
                    elif path == "/api/preview":
                        payload = {"commands": [], "mdp_overrides": []}
                    elif path == "/api/protocol-preview":
                        payload = {"stages": []}
                    elif path in {"/api/jobs", "/api/research/jobs"}:
                        assert route.request.method == "GET", "AI must not bypass the operation confirmation API"
                        payload = {"jobs": [job], "total": 1, "has_more": False}
                    elif path.endswith("/files"):
                        payload = {"files": [], "truncated": False}
                    elif path.endswith("/summary"):
                        payload = {"quality": [], "input_count": 2, "manifest": True}
                    elif path.endswith("/plots"):
                        payload = {"plots": [], "metrics": [], "replicas": []}
                    elif path == "/api/jobs/source":
                        payload = job
                    elif path.startswith("/api/"):
                        raise AssertionError(f"Unexpected API {path}")
                    else:
                        target = APP / "templates/index.html" if path == "/" else APP / path.lstrip("/")
                        target.resolve().relative_to(APP.resolve())
                        route.fulfill(
                            content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes()
                        )
                        return
                    route.fulfill(content_type="application/json", body=json.dumps(payload))

                page.route("**/*", fixture)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("window.EventSource = class extends EventTarget { close() {} };")

                def open_agent():
                    page.locator("#open-agent").click()
                    expect(page.locator("#agent-panel")).to_be_visible()
                    expect(page.locator("#agent-message")).to_be_focused()

                def open_settings():
                    page.locator("#agent-open-settings").click()
                    expect(page.locator("#agent-settings")).to_be_visible()
                    expect(page.locator("#agent-config-fields")).to_be_enabled()

                def fill_model(model):
                    page.locator("#agent-model-choice").select_option("__custom__")
                    page.locator("#agent-model").fill(model)

                page.goto(ORIGIN, wait_until="networkidle")
                panel = page.locator("#agent-panel")
                settings_dialog = page.locator("#agent-settings")
                expect(page.locator("#open-agent")).to_be_in_viewport()
                expect(panel).to_be_in_viewport()
                expect(settings_dialog).to_be_hidden()
                expect(page.locator("#agent-error")).to_contain_text("暂时无法读取")
                expect(page.locator("#agent-send")).to_be_disabled()
                control["fail_config"] = False
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-error")).to_be_hidden()
                expect(page.locator("#agent-config-notice")).to_be_visible()
                expect(page.locator("#agent-message")).to_be_in_viewport()
                expect(settings_dialog).to_be_hidden()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if artifact_dir:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(artifact_dir / f"agent-initial-{width}-{scheme}.png"))
                launch_message = "检查当前任务状态，帮我安排下一步。"
                page.locator("#agent-message").fill(launch_message)
                page.locator("[data-agent-starter]").first.click()
                expect(page.locator("#agent-status")).to_contain_text("已有未发送")
                expect(page.locator("#agent-message")).to_have_value(launch_message)
                page.locator("#agent-message").fill("")
                for shortcut in page.locator("[data-agent-starter]").all():
                    prompt = shortcut.get_attribute("data-agent-starter")
                    shortcut.click()
                    expect(page.locator("#agent-message")).to_have_value(prompt)
                    page.locator("#agent-message").fill("")
                assert not any(method != "GET" and path.startswith("/api/agent/") for method, path, _ in calls), (
                    "Automatic initialization, typing and shortcuts must not send messages, test keys or create sessions"
                )
                page.locator("[data-agent-configure]").first.click()
                expect(page.locator("#agent-config-fields")).to_be_enabled()
                expect(page.locator("#agent-provider")).to_be_focused()
                expect(page.locator("#agent-saved-profiles .agent-saved-profile")).to_have_count(2)
                expect(page.locator('#agent-saved-profiles [data-provider="openai"]')).to_contain_text("尚未配置")
                expect(page.locator("#agent-load-models")).to_be_disabled()
                expect(page.locator("#agent-test")).to_be_disabled()
                page.locator("#agent-api-key").fill("never-store-this-secret")
                expect(page.locator("#agent-test")).to_be_enabled()
                fill_model("   ")
                invalid_test_start = len(calls)
                page.locator("#agent-test").click()
                assert not calls[invalid_test_start:], "Invalid models must not save settings or call the provider"
                expect(page.locator("#agent-api-key")).to_have_value("never-store-this-secret")
                fill_model("gpt-6.1-sol")
                first_test_start = len(calls)
                page.locator("#agent-test").click()
                expect(page.locator("#agent-settings-status")).to_contain_text("连接成功")
                expect(settings_dialog).to_be_visible()
                first_test_calls = [call for call in calls[first_test_start:] if call[1].startswith("/api/agent/")]
                assert first_test_calls == [
                    ("PUT", "/api/agent/config", {"provider": "openai", "model": "gpt-6.1-sol", "api_key": "never-store-this-secret"}),
                    ("POST", "/api/agent/connection-test", {}),
                ], "A first key must be saved before explicitly testing the connection"
                tests_before_save = len([path for _, path, _ in calls if path == "/api/agent/connection-test"])
                page.locator("#agent-save").click()
                expect(settings_dialog).to_be_hidden()
                assert len([path for _, path, _ in calls if path == "/api/agent/connection-test"]) == tests_before_save
                expect(page.locator("#agent-config-notice")).to_be_hidden()
                expect(page.locator("#agent-connection-status")).to_contain_text("设置已保存")
                expect(page.locator("#agent-message")).to_be_focused()
                expect(page.locator("#agent-api-key")).to_have_value("")
                assert "never-store-this-secret" not in page.locator("body").inner_text()
                open_settings()
                expect(page.locator("#agent-key-status")).to_contain_text("已保存密钥")
                expect(page.locator("#agent-settings-title")).to_be_focused()
                expect(page.locator("#agent-saved-title")).to_be_in_viewport()
                if artifact_dir:
                    page.screenshot(path=str(artifact_dir / f"agent-saved-overview-{width}-{scheme}.png"))
                expect(page.locator('#agent-saved-profiles [data-provider="openai"]')).to_contain_text("当前使用")
                expect(page.locator('#agent-saved-profiles [data-provider="openai"]')).to_contain_text("已保存 · 自动沿用")
                page.locator("#agent-test").click()
                expect(page.locator("#agent-settings-status")).to_contain_text("连接成功")
                page.locator("#agent-api-key").fill("discard-on-provider-change")
                page.locator("#agent-provider").select_option("deepseek")
                expect(page.locator("#agent-api-key")).to_have_value("")
                expect(page.locator("#agent-model")).to_have_value("deepseek-flash")
                expect(page.locator('#agent-model-choice option[value="deepseek-v4-pro"]')).to_have_count(1)
                page.locator("#agent-model-choice").select_option("deepseek-v4-pro")
                expect(page.locator("#agent-model")).to_have_value("deepseek-v4-pro")
                expect(page.locator("#agent-test")).to_be_disabled()
                page.locator("#agent-api-key").fill("deepseek-test-secret")
                fill_model("deepseek-custom")
                control["fail_test"] = True
                changed_test_start = len(calls)
                page.locator("#agent-test").click()
                expect(page.locator("#agent-settings-error")).to_contain_text("连接模型服务超时")
                expect(page.locator("#agent-settings-status")).to_contain_text("设置已保存，连接测试未通过")
                expect(settings_dialog).to_be_visible()
                expect(page.locator("#agent-test")).to_be_enabled()
                changed_test_calls = [call for call in calls[changed_test_start:] if call[1].startswith("/api/agent/")]
                assert changed_test_calls == [
                    ("PUT", "/api/agent/config", {"provider": "deepseek", "model": "deepseek-custom", "api_key": "deepseek-test-secret"}),
                    ("POST", "/api/agent/connection-test", {}),
                ], "Connection tests must use the currently edited provider, model and key"
                assert "密钥" not in page.locator("#agent-settings-status").inner_text(), (
                    "Network failures must not be mislabeled as credential errors"
                )
                control["fail_test"] = False
                control["fail_save"] = True
                failed_save_start = len(calls)
                page.locator("#agent-test").click()
                expect(page.locator("#agent-settings-error")).to_contain_text("设置暂时无法保存")
                expect(settings_dialog).to_be_visible()
                assert not any(path == "/api/agent/connection-test" for _, path, _ in calls[failed_save_start:]), (
                    "A failed save must never test stale settings"
                )
                control["fail_save"] = False
                page.locator("#agent-test").click()
                expect(page.locator("#agent-settings-status")).to_contain_text("连接成功（deepseek · deepseek-custom）")
                expect(page.locator("#agent-settings-error")).to_be_hidden()
                last_test_config = next(body for method, path, body in reversed(calls) if method == "PUT" and path == "/api/agent/config")
                assert "api_key" not in last_test_config, "Testing with a blank key must retain the saved provider key"
                page.locator("#agent-save").click()
                expect(settings_dialog).to_be_hidden()
                expect(page.locator("#agent-current-model")).to_contain_text("deepseek-custom")
                open_settings()
                page.locator("#agent-provider").select_option("openai")
                expect(page.locator("#agent-model")).to_have_value("gpt-6.1-sol")
                page.locator("#agent-clear-key").click()
                expect(page.locator("#agent-key-status")).to_contain_text("尚未配置")
                expect(settings_dialog).to_be_visible()
                page.locator("#agent-provider").select_option("deepseek")
                expect(page.locator("#agent-model")).to_have_value("deepseek-custom")
                page.locator("#agent-save").click()
                expect(settings_dialog).to_be_hidden()
                last_config = next(body for method, path, body in reversed(calls) if method == "PUT" and path == "/api/agent/config")
                assert "api_key" not in last_config, "Blank key input must preserve the stored provider key"
                open_settings()
                expect(page.locator('#agent-saved-profiles [data-provider="deepseek"]')).to_contain_text("deepseek-custom")
                expect(page.locator('#agent-saved-profiles [data-provider="deepseek"]')).to_contain_text("当前使用")
                assert not any(path == "/api/agent/models" for _, path, _ in calls), (
                    "Opening saved configuration must not contact providers"
                )
                control["fail_models"] = True
                catalog_start = len(calls)
                page.locator("#agent-load-models").click()
                expect(page.locator("#agent-settings-error")).to_contain_text("模型列表暂时不可用")
                expect(page.locator("#agent-model")).to_have_value("deepseek-custom")
                control["fail_models"] = False
                page.locator("#agent-load-models").click()
                expect(page.locator("#agent-model-list-status")).to_contain_text("读取 3 个模型")
                expect(page.locator("#agent-settings-error")).to_be_hidden()
                expect(page.locator("#agent-model-choice")).to_have_value("deepseek-custom")
                expect(page.locator('#agent-model-choice option[value="deepseek-new-series"]')).to_have_count(1)
                assert not page.locator("#agent-settings img").count(), "Model names must render as text"
                assert not page.evaluate("window.agentXss || false")
                assert all(method == "GET" for method, path, _ in calls[catalog_start:] if path.startswith("/api/agent/"))
                page.locator('#agent-saved-profiles [data-provider="openai"] button').click()
                expect(page.locator("#agent-provider")).to_have_value("openai")
                page.locator('#agent-saved-profiles [data-provider="deepseek"] button').click()
                expect(page.locator("#agent-provider")).to_have_value("deepseek")
                expect(page.locator("#agent-model")).to_have_value("deepseek-custom")
                if artifact_dir:
                    page.screenshot(path=str(artifact_dir / f"agent-settings-{width}-{scheme}.png"))
                page.locator("#agent-api-key").fill("discard-on-close")
                page.keyboard.press("Escape")
                expect(settings_dialog).to_be_hidden()
                expect(page.locator("#agent-api-key")).to_have_value("")
                expect(page.locator("#agent-open-settings")).to_be_focused()

                # A saved key is restored automatically after reload; no settings or key write is required.
                saved_reload_start = len(calls)
                writes_before_reload = len([path for method, path, _ in calls if method != "GET" and path.startswith("/api/agent/")])
                page.reload(wait_until="networkidle")
                expect(settings_dialog).to_be_hidden()
                expect(page.locator("#agent-connection-status")).to_contain_text("设置已保存")
                expect(page.locator("#agent-config-notice")).to_be_hidden()
                expect(page.locator("#agent-message")).to_be_in_viewport()
                expect(page.locator("#agent-current-model")).to_contain_text("deepseek-custom")
                assert writes_before_reload == len(
                    [path for method, path, _ in calls if method != "GET" and path.startswith("/api/agent/")]
                )
                page.locator("[data-agent-starter]").first.click()
                expect(page.locator("#agent-message")).to_have_value("查看当前项目的任务状态，帮我安排下一步。")
                page.locator("#agent-message").fill("")
                for scroll_position in ("node.scrollHeight", "0"):
                    page.locator("#agent-scroll").evaluate(
                        f"node => {{ node.scrollTop = {scroll_position}; node.dispatchEvent(new Event('scroll')); }}"
                    )
                    expect(page.locator("#agent-scroll-latest")).to_be_hidden()
                if artifact_dir:
                    page.screenshot(path=str(artifact_dir / f"agent-empty-{width}-{scheme}.png"))
                expect(page.locator("#agent-context-label")).to_contain_text("source")
                expect(page.locator("#agent-include-context")).not_to_be_checked()
                page.locator("#agent-include-context").check()
                page.locator("#agent-message").fill("  \n  ")
                expect(page.locator("#agent-send")).to_be_disabled()
                page.locator("#agent-message").fill("中文输入中")
                message_count_before = len([path for _, path, _ in calls if path.endswith("/messages")])
                page.locator("#agent-message").evaluate("""node => {
                    node.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
                    node.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, isComposing: true, bubbles: true}));
                    node.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true}));
                    node.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', metaKey: true, keyCode: 229, bubbles: true}));
                }""")
                assert len([path for _, path, _ in calls if path.endswith("/messages")]) == message_count_before
                expect(page.locator("#agent-message")).to_have_value("中文输入中")
                page.locator("#agent-message").press("End")
                page.locator("#agent-message").press("Enter")
                expect(page.locator("#agent-message")).to_have_value("中文输入中\n")
                page.locator("#agent-message").fill("多行输入\n" * 30)
                assert 100 < page.locator("#agent-message").evaluate("node => node.getBoundingClientRect().height") <= 162
                assert page.locator("#agent-message").evaluate("node => getComputedStyle(node).overflowY === 'auto'")
                page.locator("#agent-message").focus()
                page.keyboard.press("Control+A")
                page.keyboard.insert_text("生成方案")
                expect(page.locator("#agent-message-count")).to_have_text("4 / 8000")
                page.keyboard.press("Control+Enter")
                expect(page.locator("#agent-actions .agent-action")).to_have_count(3)
                expect(page.locator("#agent-send")).to_be_disabled()
                expect(page.locator("#agent-status")).to_contain_text("逐项确认或拒绝")
                expect(page.locator("#agent-messages")).to_contain_text("<img src=x")
                assert page.locator("#agent-messages img").count() == 0
                assert page.evaluate("window.agentXss === undefined")
                sent = [body for method, path, body in calls if path.endswith("/messages")]
                assert len(sent) == 1 and sent[0]["context_job_id"] == "source"
                assert sent[0]["request_id"]
                assert not any(method != "GET" and path == "/api/agent/config" for method, path, _ in calls[saved_reload_start:]), (
                    "A configured page must send directly after reload without saving the API key again"
                )
                page.locator('[data-workspace-view="detail"]').click()
                page.locator("#open-agent").click()
                expect(page.locator("#agent-send")).to_be_disabled()
                expect(page.locator("#agent-message")).to_have_value("")
                assert len([path for _, path, _ in calls if path.endswith("/messages")]) == 1
                page.locator('[data-action-id="cancel"] [data-decision="reject"]').click()
                expect(page.locator('[data-action-id="cancel"]')).to_contain_text("已拒绝")

                # Lose an action's response after the server commits it. Recover with GET, never resend.
                page.evaluate("""() => {
                    const originalFetch = window.fetch;
                    window.fetch = async (url, options) => {
                        const response = await originalFetch(url, options);
                        if (String(url).endsWith('/actions/retry')) throw new TypeError('模拟响应丢失');
                        return response;
                    };
                }""")
                page.locator('[data-action-id="retry"] [data-decision="approve"]').click()
                expect(page.locator('[data-action-id="retry"]')).to_contain_text("已完成")
                expect(page.locator('[data-action-id="retry"]').get_by_role("button", name="查看任务")).to_be_visible()
                assert len([path for method, path, body in calls if path.endswith("/actions/retry")]) == 1
                page.locator('[data-action-id="draft"] [data-decision="approve"]').click()
                expect(page.locator('[data-action-id="draft"]')).to_contain_text("应用草案到新任务表单")
                expect(page.locator("#agent-action-history")).to_have_attribute("open", "")
                expect(page.locator("#agent-action-history-count")).to_have_text("3")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert panel.evaluate("node => node.scrollWidth <= node.clientWidth"), panel.evaluate(
                    "node => [...node.querySelectorAll('*')].filter(item => item.getBoundingClientRect().right > node.getBoundingClientRect().right).map(item => [item.tagName, item.id, item.clientWidth, item.scrollWidth])"
                )
                if artifact_dir:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(artifact_dir / f"agent-plans-{width}-{scheme}.png"))
                page.get_by_role("button", name="应用草案到新任务表单").click()
                expect(page.locator('[name="mdp_temperature_k"]')).to_have_value("305")
                expect(page.locator("#job-form")).to_be_in_viewport()
                open_agent()
                page.locator("#agent-message").fill("测试错误")
                page.locator("#agent-send").click()
                expect(page.locator("#agent-error")).to_contain_text("密钥不可用")
                expect(page.locator("#agent-message")).to_have_value("测试错误")
                expect(page.locator("#agent-send")).to_be_enabled()

                # Hold a message request to check double-submit protection, then lose the response.
                page.evaluate("""() => {
                    const originalFetch = window.fetch;
                    window.fetch = async (url, options) => {
                        if (String(url).endsWith('/messages') && JSON.parse(options.body).message === '恢复发送') {
                            await new Promise(resolve => { window.releaseAgentRequest = resolve; });
                            await originalFetch(url, options);
                            throw new TypeError('模拟响应丢失');
                        }
                        return originalFetch(url, options);
                    };
                }""")
                page.locator("#agent-message").fill("恢复发送")
                page.locator("#agent-send").click()
                expect(page.locator("#agent-send")).to_be_disabled()
                page.locator('[data-workspace-view="detail"]').click()
                page.locator("#open-agent").click()
                expect(page.locator("#agent-message")).to_have_value("恢复发送")
                expect(page.locator("#agent-message")).to_be_disabled()
                page.locator("#agent-message-form").evaluate("form => form.dispatchEvent(new Event('submit', {cancelable: true}))")
                page.evaluate("window.releaseAgentRequest()")
                expect(page.locator("#agent-messages")).to_contain_text("恢复发送")
                expect(page.locator("#agent-message")).to_have_value("")
                expect(page.locator("#agent-send")).to_be_disabled()
                recovered = [body for method, path, body in calls if path.endswith("/messages") and body["message"] == "恢复发送"]
                assert len(recovered) == 1
                # If neither the response nor a status read succeeds, block resubmission until
                # a successful GET. An unaccepted retry reuses its original idempotency key.
                page.evaluate(r"""() => {
                    const originalFetch = window.fetch;
                    window.agentRetryIds = [];
                    window.fetch = async (url, options) => {
                        if (String(url).endsWith('/messages') && JSON.parse(options.body).message === '恢复重试') {
                            window.agentRetryIds.push(JSON.parse(options.body).request_id);
                            if (window.agentRetryIds.length === 1) {
                                window.failAgentGet = true;
                                throw new TypeError('模拟发送前断网');
                            }
                        }
                        if (window.failAgentGet && /\/api\/agent\/sessions\/[^/]+$/.test(String(url))) {
                            throw new TypeError('模拟读取状态失败');
                        }
                        return originalFetch(url, options);
                    };
                }""")
                page.locator("#agent-message").fill("恢复重试")
                page.locator("#agent-send").click()
                expect(page.locator("#agent-status")).to_contain_text("暂时无法核实")
                expect(page.locator("#agent-send")).to_be_disabled()
                page.evaluate("window.failAgentGet = false")
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-send")).to_be_enabled()
                page.locator("#agent-send").click()
                expect(page.locator("#agent-message")).to_have_value("")
                retry_ids = page.evaluate("window.agentRetryIds")
                assert len(retry_ids) == 2 and retry_ids[0] == retry_ids[1]
                session_id = page.evaluate("sessionStorage.getItem('gromacs.agent-session')")
                assert session_id
                storage = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
                assert all(
                    secret not in storage for secret in ["never-store-this-secret", "deepseek-test-secret", "discard-on-provider-change"]
                )
                assert page.locator("#agent-message").get_attribute("maxlength") == "8000"

                # Updating a conversation keeps unchanged messages, action previews and scroll position.
                page.evaluate("""() => {
                    window.agentFirstMessage = document.querySelector('#agent-messages .agent-message');
                    window.agentFirstAction = document.querySelector('#agent-actions .agent-action');
                    document.querySelector('#agent-actions .agent-action-details').open = true;
                }""")
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-refresh")).to_be_enabled()
                assert page.evaluate("window.agentFirstMessage === document.querySelector('#agent-messages .agent-message')")
                assert page.evaluate("window.agentFirstAction === document.querySelector('#agent-actions .agent-action')")
                expect(page.locator("#agent-actions .agent-action-details").first).to_have_attribute("open", "")
                expect(page.locator("#agent-action-history")).not_to_have_attribute("open", "")
                sessions[session_id]["messages"].extend(
                    {"role": "assistant", "content": f"历史消息 {index}：这是可滚动的任务分析说明。"} for index in range(20)
                )
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-messages")).to_contain_text("历史消息 19")
                page.locator("#agent-scroll").evaluate("node => { node.scrollTop = 0; node.dispatchEvent(new Event('scroll')); }")
                expect(page.locator("#agent-scroll-latest")).to_be_visible()
                sessions[session_id]["messages"].append({"role": "assistant", "content": "新的分析结果已到达"})
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-messages")).to_contain_text("新的分析结果已到达")
                assert page.locator("#agent-scroll").evaluate("node => node.scrollTop") <= 1
                expect(page.locator("#agent-scroll-latest")).to_contain_text("有新消息")
                page.locator("#agent-scroll-latest").click()
                expect(page.locator("#agent-scroll-latest")).to_be_hidden()
                assert page.locator("#agent-scroll").evaluate("node => node.scrollHeight - node.clientHeight - node.scrollTop <= 64")
                formatted_reply = "## 下一步建议\n\n**体系准备已完成**，可以检查以下分析结果。\n\n| 指标 | 建议 |\n| --- | --- |\n| RMSD | 检查平衡区间 |\n| 回转半径 | 比较结构紧致程度 |\n\n```bash\ngmx rms -s md.tpr -f md.xtc -o rmsd.xvg\n```\n\n最新结果自动跟随"
                sessions[session_id]["messages"].append({"role": "assistant", "content": formatted_reply})
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-messages")).to_contain_text("最新结果自动跟随")
                assert page.locator("#agent-scroll").evaluate("node => node.scrollHeight - node.clientHeight - node.scrollTop <= 64")
                assert page.evaluate("window.agentFirstMessage === document.querySelector('#agent-messages .agent-message')")
                expect(page.locator("#agent-conversation-title")).to_have_text("生成方案")
                expect(page.locator("#agent-current-model")).to_contain_text("本次对话：DeepSeek · deepseek-custom")
                page.evaluate("""() => {
                    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                        writeText: async text => { window.agentCopiedText = text; }
                    }});
                }""")
                page.locator("#agent-messages .agent-message").last.get_by_role("button", name="复制消息").click()
                assert page.evaluate("window.agentCopiedText") == formatted_reply
                page.locator("#agent-messages .agent-message").last.get_by_role("button", name="复制代码").click()
                assert page.evaluate("window.agentCopiedText") == "gmx rms -s md.tpr -f md.xtc -o rmsd.xvg"
                page.locator("#agent-scroll").evaluate("node => { node.scrollTop = node.scrollHeight; }")
                if artifact_dir:
                    page.wait_for_function("getComputedStyle(document.getElementById('toast')).opacity === '0'")
                    page.screenshot(path=str(artifact_dir / f"agent-chat-{width}-{scheme}.png"))

                # Changing model within the same provider affects the next reply, while the previous model stays clear.
                page.locator("#agent-open-settings").click()
                expect(page.locator("#agent-config-fields")).to_be_enabled()
                expect(page.locator("#agent-saved-title")).to_be_in_viewport()
                page.locator("#agent-model-choice").select_option("deepseek-v4-pro")
                page.locator("#agent-save").click()
                expect(page.locator("#agent-status")).to_contain_text("下次发送将使用新模型")
                expect(page.locator("#agent-current-model")).to_have_text("上次：DeepSeek · deepseek-custom；下次：deepseek-v4-pro")
                expect(settings_dialog).to_be_hidden()
                open_settings()
                expect(page.locator("#agent-model-choice")).to_have_value("deepseek-v4-pro")
                expect(page.locator('#agent-saved-profiles [data-provider="deepseek"]')).to_contain_text("deepseek-v4-pro")
                expect(page.locator("#agent-api-key")).to_have_value("")
                last_switch = next(body for method, path, body in reversed(calls) if method == "PUT" and path == "/api/agent/config")
                assert last_switch == {"provider": "deepseek", "model": "deepseek-v4-pro"}, "Switching series must retain the saved key"
                fill_model("deepseek-custom")
                page.locator("#agent-save").click()
                expect(page.locator("#agent-current-model")).to_have_text("本次对话：DeepSeek · deepseek-custom")
                expect(settings_dialog).to_be_hidden()

                # Background tabs and the detail workspace pause polling; returning resumes only GET synchronization.
                sessions[session_id]["status"] = "running"
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-status")).to_contain_text("助手正在处理")
                expect(page.locator("#agent-refresh")).to_be_enabled()
                expect(page.locator("#agent-message")).to_be_disabled()
                page.evaluate("""() => {
                    window.agentPageHidden = true;
                    Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.agentPageHidden});
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                reads = len([path for method, path, _ in calls if method == "GET" and path == f"/api/agent/sessions/{session_id}"])
                page.wait_for_timeout(1800)
                assert reads == len([path for method, path, _ in calls if method == "GET" and path == f"/api/agent/sessions/{session_id}"])
                sessions[session_id]["status"] = "idle"
                page.evaluate("window.agentPageHidden = false; document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator("#agent-message")).to_be_enabled()
                sessions[session_id]["status"] = "running"
                page.locator("#agent-refresh").click()
                expect(page.locator("#agent-status")).to_contain_text("助手正在处理")
                expect(page.locator("#agent-refresh")).to_be_enabled()
                expect(page.locator("#agent-message")).to_be_disabled()
                page.locator('[data-workspace-view="detail"]').click()
                reads = len([path for method, path, _ in calls if method == "GET" and path == f"/api/agent/sessions/{session_id}"])
                page.wait_for_timeout(1800)
                assert reads == len([path for method, path, _ in calls if method == "GET" and path == f"/api/agent/sessions/{session_id}"])

                # A reload restores an in-flight conversation automatically using only its id, then polls.
                sessions[session_id]["status"] = "running"
                writes_before_reload = len([path for method, path, _ in calls if method != "GET" and path.startswith("/api/agent/")])
                page.reload(wait_until="networkidle")
                expect(page.locator("#agent-messages")).to_contain_text("恢复发送")
                expect(settings_dialog).to_be_hidden()
                assert writes_before_reload == len(
                    [path for method, path, _ in calls if method != "GET" and path.startswith("/api/agent/")]
                )
                expect(page.locator("#open-agent")).to_be_in_viewport()
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                expect(page.locator("#open-agent")).to_be_in_viewport()
                if width <= 1180:
                    page.locator("#toggle-sidebar").click()
                page.locator("#open-agent").click()
                assert not page.locator("body").evaluate("node => node.classList.contains('sidebar-open')")
                expect(page.locator("#toggle-sidebar")).to_have_attribute("aria-expanded", "false")
                expect(page.locator("#agent-messages")).to_contain_text("恢复发送")
                expect(page.locator("#agent-send")).to_be_disabled()
                sessions[session_id]["status"] = "idle"
                expect(page.locator("#agent-message")).to_be_enabled(timeout=10000)
                expect(page.locator("#agent-send")).to_be_disabled()
                assert page.evaluate("sessionStorage.getItem('gromacs.agent-session')") == session_id
                open_settings()
                page.locator("#agent-provider").select_option("openai")
                page.locator("#agent-save").click()
                expect(page.locator("#agent-settings-status")).to_contain_text("此对话仍绑定原服务商")
                expect(settings_dialog).to_be_visible()
                page.locator("#agent-settings-close").click()
                expect(page.locator("#agent-send")).to_be_disabled()
                page.locator("#agent-message").fill("使用新服务商安排任务")
                expect(page.locator("#agent-send")).to_be_disabled()
                open_settings()
                page.locator("#agent-provider").select_option("deepseek")
                page.locator("#agent-save").click()
                expect(settings_dialog).to_be_hidden()
                page.locator("#agent-message").fill("旧对话独立草稿")
                page.locator("#agent-new-session").click()
                expect(page.locator("#agent-actions .agent-action")).to_have_count(0)
                expect(page.locator("#agent-messages")).not_to_contain_text("恢复发送")
                new_session_id = page.evaluate("sessionStorage.getItem('gromacs.agent-session')")
                assert new_session_id != session_id
                assert session_id in sessions, "Starting a new conversation must preserve the previous conversation"
                expect(page.locator("#agent-message")).to_have_value("")
                page.locator("#agent-message").fill("新对话独立草稿")
                page.locator('[data-workspace-view="detail"]').click()
                open_agent()
                expect(page.locator("#agent-message")).to_have_value("新对话独立草稿")
                page.locator("#agent-history").select_option(session_id)
                expect(page.locator("#agent-messages")).to_contain_text("恢复发送")
                expect(page.locator("#agent-message")).to_have_value("旧对话独立草稿")
                page.locator("#agent-history").select_option(new_session_id)
                expect(page.locator("#agent-message")).to_have_value("新对话独立草稿")
                expect(page.locator("#agent-messages")).not_to_contain_text("恢复发送")
                storage = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
                assert "独立草稿" not in storage, "Drafts must stay in memory instead of browser storage"
                page.locator("#agent-delete-session").click()
                expect(page.locator("#submit-warning")).to_be_visible()
                page.locator("#submit-warning-confirm").click()
                expect(page.locator("#agent-status")).to_contain_text("本次对话已清除")
                assert page.evaluate("sessionStorage.getItem('gromacs.agent-session')") is None
                assert len(sessions) == 1
                page.locator("#agent-history").select_option(session_id)
                expect(page.locator("#agent-messages")).to_contain_text("恢复发送")
                page.locator("#agent-delete-session").click()
                page.locator("#submit-warning-confirm").click()
                expect(page.locator("#agent-history")).to_be_disabled()
                assert not sessions
                assert not errors, errors
                page.close()
        finally:
            browser.close()
    print(
        "Inline AI chat, automatic saved configuration/session recovery, settings retry, keyboard/IME input, rendering, isolated drafts and confirmed actions passed on four desktop/mobile widths."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    check(parser.parse_args().artifact_dir)
