"""A read-only tool gateway for an LLM agent.

Sanitized illustrative example — not production source. The production system
prompt, tool configuration and MCP server selection are not reproduced.

The design position (docs/06): an agent must not be *able* to write, rather
than being *instructed* not to. A prompt-level restriction is a request, and
requests do not survive a context that contains attacker-influenceable text --
which log data always does.

So capability is removed at two independent layers:

  1. IDENTITY. The upstream credential is read-only (a Viewer-role service
     account). Fails if someone grants that account more permissions.
  2. TOOL SURFACE. Mutating tools are stripped from the schema entirely, so
     they are never advertised to the model. Fails if someone removes the flag.

Neither alone is sufficient, and they fail differently -- which is the whole
point of having two. Note the structural property of layer 2: with the schema
absent, the capability is *absent*, not forbidden. There is no jailbreak for a
tool that was never described to the model.

A third, practical concern is enforced here too: TOOL SCHEMAS ARE A CONTEXT
BUDGET. In the measured production case, 15 tools cost ~7,100 tokens of a
9,216-token prompt -- 77% -- and prompt processing, not generation, was the
entire latency bottleneck. So the gateway can report its own schema cost, and
refuses to advertise a set that exceeds a declared budget.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Tool",
    "ToolAccess",
    "ToolGateway",
    "ToolRejected",
]


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolRejected(RuntimeError):
    """A call was refused before it reached the upstream system."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    access: ToolAccess
    description: str
    parameters: Mapping[str, str]
    handler: Callable[[Mapping[str, str]], str]

    def schema_tokens(self) -> int:
        """A rough token estimate for this tool's advertised schema.

        Deliberately crude -- ~4 characters per token. The purpose is to make
        the budget VISIBLE and comparable, not to be exact. An approximate
        number that gets checked beats an exact number that nobody computes.
        """
        blob = json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            }
        )
        return len(blob) // 4


@dataclass(slots=True)
class ToolGateway:
    """Advertises and dispatches a scoped, read-only tool set."""

    schema_token_budget: int = 8_000
    _tools: dict[str, Tool] = field(default_factory=dict)
    calls: list[tuple[str, Mapping[str, str]]] = field(default_factory=list)

    # Query text that must never reach an upstream datasource. This is a
    # backstop, NOT the control -- the control is that the credential and the
    # tool surface are read-only. A denylist of dangerous strings is exactly
    # the wrong primary defence, because it requires enumerating every
    # disaster; it is kept only to fail loudly if a write-capable tool is ever
    # registered by mistake.
    _FORBIDDEN = re.compile(
        r"\b(drop|delete|truncate|insert|update|alter|grant)\b", re.IGNORECASE
    )

    def register(self, tool: Tool) -> None:
        """Register a tool. Write-access tools are refused outright.

        Refusing at registration rather than at call time is deliberate: a
        write tool that exists but is blocked when invoked has still been
        advertised to the model, which means it appears in the schema, costs
        context, and invites the model to plan around it.
        """
        if tool.access is ToolAccess.WRITE:
            raise ToolRejected(
                f"tool {tool.name!r} declares write access; "
                "this gateway is read-only by construction"
            )
        if tool.name in self._tools:
            raise ToolRejected(f"tool {tool.name!r} already registered")

        projected = self.schema_cost() + tool.schema_tokens()
        if projected > self.schema_token_budget:
            raise ToolRejected(
                f"registering {tool.name!r} would take the advertised schema to "
                f"~{projected} tokens, over the {self.schema_token_budget} budget. "
                "Tool schemas are paid on every request; scope the set or raise "
                "the budget deliberately."
            )
        self._tools[tool.name] = tool

    def schema_cost(self) -> int:
        return sum(t.schema_tokens() for t in self._tools.values())

    def advertised(self) -> Sequence[Mapping[str, object]]:
        """The schema sent to the model. Read tools only, by construction."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": dict(t.parameters),
            }
            for t in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    def call(self, name: str, arguments: Mapping[str, str]) -> str:
        """Dispatch a tool call, recording it for audit.

        Every call is recorded. "What did the assistant actually look at?" must
        have an answer -- both for incident review and because a silent drop in
        tool-call rate is itself an alertable signal (docs/06: a truncated
        context makes a model stop calling tools without reporting anything).
        """
        tool = self._tools.get(name)
        if tool is None:
            # Do not reveal the valid set here. A model that hallucinated a
            # tool name should be told it was wrong, not handed an oracle.
            raise ToolRejected(f"unknown tool {name!r}")

        for key, value in arguments.items():
            if self._FORBIDDEN.search(value):
                raise ToolRejected(f"argument {key!r} contains a mutating keyword; refused")

        self.calls.append((name, dict(arguments)))
        return tool.handler(arguments)
