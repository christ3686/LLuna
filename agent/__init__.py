"""
LLuna v7 Agent Module
======================
Tool Execution Integrity + Anti-Hallucination + Loop Discipline + 
Path Resolution + Tool Session Cache + Sudo Handling
"""

from .core import (
    AutonomousAgent,
    CognitiveState,
    CognitiveEvent,
    DangerLevel,
    Message,
    ContextWindow,
)

from .tool_integrity import (
    ToolExecution,
    ToolExecutionState,
    ToolExecutionRegistry,
    HallucinationDetector,
    VALID_TRANSITIONS,
)

from .loop_discipline import (
    LoopDiscipline,
    LoopState,
    ReasoningUnit,
    ConfidenceEstimator,
    ModelSize,
    MODEL_LIMITS,
)

from .argument_normalizer import (
    ArgumentNormalizer,
    normalize_tool_arguments,
)

from .filesystem_context import (
    FilesystemContext,
    PathNormalizer,
    canonical_path,
    paths_match,
)

from .tool_cache import (
    ToolCache,
    CachedTool,
    SudoState,
    is_meta_question,
)

__all__ = [
    "AutonomousAgent",
    "CognitiveState",
    "CognitiveEvent",
    "DangerLevel",
    "Message",
    "ContextWindow",
    "ToolExecution",
    "ToolExecutionState",
    "ToolExecutionRegistry",
    "HallucinationDetector",
    "VALID_TRANSITIONS",
    "LoopDiscipline",
    "LoopState",
    "ReasoningUnit",
    "ConfidenceEstimator",
    "ModelSize",
    "MODEL_LIMITS",
    "ArgumentNormalizer",
    "normalize_tool_arguments",
    "FilesystemContext",
    "PathNormalizer",
    "canonical_path",
    "paths_match",
    "ToolCache",
    "CachedTool",
    "SudoState",
    "is_meta_question",
]
