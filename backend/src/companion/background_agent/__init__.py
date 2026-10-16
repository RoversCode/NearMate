"""NearMate 后台智能体内核。"""

from companion.background_agent.contracts import (
    AgentCompletionPayload,
    AgentEvent,
    AgentMaterial,
    AgentTask,
)
from companion.background_agent.agent_session import (
    AgentSession,
    AgentSessionManager,
)
from companion.background_agent.skill_models import SkillExecutionSnapshot
from companion.background_agent.skill_registry import SkillRegistry

__all__ = [
    "AgentCompletionPayload",
    "AgentEvent",
    "AgentMaterial",
    "AgentTask",
    "AgentSession",
    "AgentSessionManager",
    "SkillExecutionSnapshot",
    "SkillRegistry",
]
