"""Local-only AI endpoints; requests never carry a client-supplied tool transcript."""

from __future__ import annotations

import ipaddress
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent_provider import AgentError, ProviderPatch
from .agent_service import AgentService
from .agent_tools import AgentTools


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    request_id: UUID
    context_job_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("message")
    @classmethod
    def meaningful_message(cls, value):
        if not value.strip():
            raise ValueError("请输入消息。")
        return value.strip()


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]


def agent_router(get_store, get_settings, check_origin, base_dir: Path, worker_status) -> APIRouter:
    def require_local(request: Request):
        check_origin(request)
        try:
            local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, detail="AI 调度仅允许本机连接，请在本机控制台使用。")

    router = APIRouter(prefix="/api/agent", dependencies=[Depends(require_local)])
    services: dict[Path, AgentService] = {}
    lock = threading.Lock()

    def service(response: Response) -> AgentService:
        response.headers["Cache-Control"] = "no-store"
        runtime = get_store().runtime_root.resolve()
        with lock:
            if runtime not in services:
                try:
                    services[runtime] = AgentService(runtime)
                except AgentError as exc:
                    raise HTTPException(exc.status_code, detail=str(exc)) from exc
                except (OSError, sqlite3.Error) as exc:
                    raise HTTPException(503, detail="AI 本机存储不可用，请检查目录权限和空间。") from exc
            return services[runtime]

    def tools():
        return AgentTools(get_store(), get_settings(), base_dir, worker_status)

    def invoke(function, *args):
        try:
            return function(*args)
        except AgentError as exc:
            raise HTTPException(exc.status_code, detail=str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, detail="AI 本机存储暂时不可用，请先检查任务状态，避免重复提交。") from exc
        except Exception as exc:
            logging.getLogger(__name__).warning("AI request failed (%s)", type(exc).__name__)
            raise HTTPException(500, detail="AI 请求未完成，请检查配置及任务状态。") from exc

    @router.get("/config")
    def configuration(agent: AgentService = Depends(service)):
        return invoke(agent.config.public)

    @router.put("/config")
    def configure(patch: ProviderPatch, agent: AgentService = Depends(service)):
        return invoke(agent.config.save, patch)

    @router.post("/connection-test")
    def connection_test(agent: AgentService = Depends(service)):
        return invoke(agent.test_connection)

    @router.get("/models")
    def models(provider: Literal["openai", "deepseek"], agent: AgentService = Depends(service)):
        return invoke(agent.list_models, provider)

    @router.post("/sessions")
    def create_session(agent: AgentService = Depends(service)):
        return invoke(agent.create)

    @router.get("/sessions")
    def list_sessions(agent: AgentService = Depends(service)):
        return invoke(agent.list_sessions)

    @router.get("/sessions/{session_id}")
    def get_session(session_id: UUID, agent: AgentService = Depends(service)):
        return invoke(agent.get, str(session_id))

    @router.delete("/sessions/{session_id}")
    def delete_session(session_id: UUID, agent: AgentService = Depends(service)):
        return invoke(agent.delete, str(session_id))

    @router.post("/sessions/{session_id}/messages")
    def send_message(session_id: UUID, message: ChatMessage, agent: AgentService = Depends(service)):
        return invoke(agent.send, str(session_id), message.message, str(message.request_id), message.context_job_id, tools())

    @router.post("/sessions/{session_id}/actions/{action_id}")
    def decide_action(session_id: UUID, action_id: UUID, decision: ActionDecision, agent: AgentService = Depends(service)):
        return invoke(agent.decide, str(session_id), str(action_id), decision.decision, tools())

    return router
