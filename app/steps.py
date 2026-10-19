"""Workflow step data and internal-operation descriptors, independent of workflow builders."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Step:
    title: str
    args: list[str]
    stdin_text: str | None = None
    outputs: list[str] = field(default_factory=list)
    kind: str = "command"
    operation: str | None = None
    data: dict = field(default_factory=dict)
    parallel_group: str = ""


def internal_step(title: str, operation: str, data: dict, outputs: list[str] | None = None) -> Step:
    args = ["internal", operation]
    for key, value in data.items():
        if value is None or value == "":
            continue
        args.append(f"--{key}={value}")
    return Step(title=title, args=args, outputs=outputs or [], kind="internal", operation=operation, data=data)
