"""维护与材料生命周期独立的当前环境上下文。"""

from companion.environment.models import FrozenEnvironmentSnapshot
from companion.environment.service import EnvironmentContextService

__all__ = ["EnvironmentContextService", "FrozenEnvironmentSnapshot"]
