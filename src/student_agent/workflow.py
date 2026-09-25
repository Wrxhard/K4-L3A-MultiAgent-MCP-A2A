from __future__ import annotations

from typing import Any

from .agent_contracts import AgentRegistry
from .coordinator import Coordinator
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    *,
    registry: AgentRegistry | None = None,
) -> dict[str, Any]:
    """Run one case through the coordinator with explicitly wired team agents."""
    del gateway
    if registry is None:
        raise RuntimeError(
            "AgentRegistry is required until the team agent implementations are wired"
        )
    return await Coordinator().solve(case, registry, trace)
