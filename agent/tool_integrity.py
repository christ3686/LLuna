# Copyright (C) 2025-2026 [Vasile Sabo / Remotex]
#
# This file is part of LLuna.
#
# LLuna is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# LLuna is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with LLuna. If not, see <https://www.gnu.org/licenses/>.

"""
LLuna v7.1 - Tool Execution Integrity Module
=============================================
HARD RULES:
- Every tool call has unique ID, origin prompt, timestamp
- Results ONLY consumed by originating prompt
- No phantom completion - requires ACTUAL output
- Stale tools NEVER execute
- Approval → immediate execution or fail fast
- Missing executor response = FAILURE
"""

import uuid
import time
import logging
import re
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Set
from threading import Lock

logger = logging.getLogger(__name__)


class ToolExecutionState(str, Enum):
    """
    Strict state machine. NO auto-advancement.
    Each transition requires explicit executor confirmation.
    """
    REQUESTED = "requested"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED_SUCCESS = "completed_success"
    COMPLETED_EMPTY = "completed_empty"
    FAILED = "failed"
    STALE = "stale"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    EXECUTOR_MISSING = "executor_missing"  # NEW: MCP didn't respond


# Valid transitions - strictly enforced
VALID_TRANSITIONS: Dict[ToolExecutionState, Set[ToolExecutionState]] = {
    ToolExecutionState.REQUESTED: {
        ToolExecutionState.PENDING_APPROVAL,
        ToolExecutionState.EXECUTING,
        ToolExecutionState.STALE,
    },
    ToolExecutionState.PENDING_APPROVAL: {
        ToolExecutionState.APPROVED,
        ToolExecutionState.REJECTED,
        ToolExecutionState.STALE,
        ToolExecutionState.TIMEOUT,
    },
    ToolExecutionState.APPROVED: {
        ToolExecutionState.EXECUTING,
        ToolExecutionState.FAILED,
        ToolExecutionState.STALE,
        ToolExecutionState.TIMEOUT,
    },
    ToolExecutionState.EXECUTING: {
        ToolExecutionState.COMPLETED_SUCCESS,
        ToolExecutionState.COMPLETED_EMPTY,
        ToolExecutionState.FAILED,
        ToolExecutionState.TIMEOUT,
        ToolExecutionState.EXECUTOR_MISSING,
    },
    # Terminal states
    ToolExecutionState.REJECTED: set(),
    ToolExecutionState.COMPLETED_SUCCESS: set(),
    ToolExecutionState.COMPLETED_EMPTY: set(),
    ToolExecutionState.FAILED: set(),
    ToolExecutionState.STALE: set(),
    ToolExecutionState.TIMEOUT: set(),
    ToolExecutionState.EXECUTOR_MISSING: set(),
}


@dataclass
class ToolExecution:
    """
    Immutable record of tool execution.
    All fields tracked for audit.
    """
    # Identity - REQUIRED
    tool_call_id: str
    origin_prompt_id: str
    tool_name: str
    tool_server: str
    arguments: Dict[str, Any]
    
    # State
    state: ToolExecutionState = ToolExecutionState.REQUESTED
    
    # Timestamps
    created_at: float = field(default_factory=time.time)
    approved_at: Optional[float] = None
    execution_started_at: Optional[float] = None
    execution_completed_at: Optional[float] = None
    
    # Results - REQUIRED for success
    result_raw: Optional[str] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    
    # Error tracking
    error_message: Optional[str] = None
    
    # Metadata
    danger_level: str = "safe"
    execution_duration_ms: int = 0
    executor_acknowledged: bool = False  # NEW: MCP confirmed receipt
    executor_completed: bool = False     # NEW: MCP confirmed completion
    
    def transition(self, new_state: ToolExecutionState, error: str = None) -> bool:
        """Attempt state transition. Rejects invalid transitions."""
        if new_state not in VALID_TRANSITIONS.get(self.state, set()):
            logger.error(f"INVALID TRANSITION: {self.tool_call_id} {self.state} → {new_state}")
            return False
        
        old_state = self.state
        self.state = new_state
        
        now = time.time()
        if new_state == ToolExecutionState.APPROVED:
            self.approved_at = now
        elif new_state == ToolExecutionState.EXECUTING:
            self.execution_started_at = now
        elif new_state in (
            ToolExecutionState.COMPLETED_SUCCESS,
            ToolExecutionState.COMPLETED_EMPTY,
            ToolExecutionState.FAILED,
            ToolExecutionState.TIMEOUT,
            ToolExecutionState.EXECUTOR_MISSING,
        ):
            self.execution_completed_at = now
            if self.execution_started_at:
                self.execution_duration_ms = int((now - self.execution_started_at) * 1000)
        
        if error:
            self.error_message = error
        
        logger.debug(f"Tool {self.tool_call_id}: {old_state} → {new_state}")
        return True
    
    def is_terminal(self) -> bool:
        return len(VALID_TRANSITIONS.get(self.state, set())) == 0
    
    def is_success(self) -> bool:
        """ONLY completed_success counts as success"""
        return self.state == ToolExecutionState.COMPLETED_SUCCESS
    
    def is_consumable(self, prompt_id: str) -> bool:
        """Result can ONLY be consumed by originating prompt"""
        return (
            self.origin_prompt_id == prompt_id and
            self.state in (
                ToolExecutionState.COMPLETED_SUCCESS,
                ToolExecutionState.COMPLETED_EMPTY,
                ToolExecutionState.FAILED,
                ToolExecutionState.EXECUTOR_MISSING,
            )
        )
    
    def validate_output(self) -> bool:
        """
        Validate we have ACTUAL execution output.
        Empty/None/placeholder = NOT success.
        """
        # Must have executor confirmation
        if not self.executor_completed:
            return False
        
        # Check for actual content
        has_stdout = self.stdout and len(self.stdout.strip()) > 0
        has_raw = self.result_raw and len(self.result_raw.strip()) > 0
        has_error = self.error_message and len(self.error_message.strip()) > 0
        
        # Reject placeholder responses
        placeholders = [
            "tool execution completed",
            "command executed",
            "done",
            "ok",
            "success",
        ]
        
        if has_raw:
            raw_lower = self.result_raw.lower().strip()
            if raw_lower in placeholders or len(raw_lower) < 3:
                return False
        
        return has_stdout or has_raw or has_error
    
    def get_result_for_context(self) -> str:
        """Format result for LLM context - ONLY if validated"""
        if self.state == ToolExecutionState.COMPLETED_SUCCESS:
            output = self.result_raw or self.stdout or ""
            return f"[TOOL OUTPUT: {self.tool_name} | ID:{self.tool_call_id}]\n{output}"
        elif self.state == ToolExecutionState.COMPLETED_EMPTY:
            return f"[TOOL EMPTY: {self.tool_name}]\nExecution produced no output."
        elif self.state == ToolExecutionState.FAILED:
            return f"[TOOL FAILED: {self.tool_name}]\n{self.error_message or 'Unknown error'}"
        elif self.state == ToolExecutionState.EXECUTOR_MISSING:
            return f"[TOOL NO RESPONSE: {self.tool_name}]\nExecutor did not confirm completion."
        elif self.state == ToolExecutionState.TIMEOUT:
            return f"[TOOL TIMEOUT: {self.tool_name}]\nExecution exceeded time limit."
        elif self.state == ToolExecutionState.REJECTED:
            return f"[TOOL REJECTED: {self.tool_name}]\nUser denied execution."
        elif self.state == ToolExecutionState.STALE:
            return f"[TOOL STALE: {self.tool_name}]\nInvalidated - new prompt started."
        else:
            return f"[TOOL {self.state.value.upper()}: {self.tool_name}]"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "origin_prompt_id": self.origin_prompt_id,
            "tool_name": self.tool_name,
            "state": self.state.value,
            "executor_acknowledged": self.executor_acknowledged,
            "executor_completed": self.executor_completed,
            "has_valid_output": self.validate_output(),
            "duration_ms": self.execution_duration_ms,
            "error": self.error_message,
        }


class ToolExecutionRegistry:
    """
    Central registry with strict enforcement.
    """
    
    def __init__(self):
        self._executions: Dict[str, ToolExecution] = {}
        self._by_prompt: Dict[str, List[str]] = {}
        self._executed_ids: set = set()  # IDEMPOTENCY GUARD
        self._lock = Lock()
        self._current_prompt_id: Optional[str] = None
        self._prompt_counter = 0
        
        # APPROVAL PERSISTENCE - remembers approved (tool_name, canonical_path)
        self._approved_actions: Dict[str, Set[str]] = {}  # tool_name -> set of canonical paths
        
        # COMPLETION TRACKING - prevents re-requesting completed tools
        self._completed_actions: Dict[str, str] = {}  # (tool_name, canonical_path) -> result summary
    
    def _get_canonical_key(self, tool_name: str, arguments: Dict) -> str:
        """Generate canonical key for tool+arguments (for deduplication)."""
        # Import here to avoid circular dependency
        from .filesystem_context import canonical_path
        
        # Extract path-like arguments
        path = arguments.get("path", "") or arguments.get("source", "") or arguments.get("destination", "")
        if path:
            path = canonical_path(path)
        
        return f"{tool_name}:{path}"
    
    def is_action_approved(self, tool_name: str, arguments: Dict) -> bool:
        """Check if this action was previously approved."""
        from .filesystem_context import canonical_path
        
        path = arguments.get("path", "") or arguments.get("source", "")
        if not path:
            return False
        
        canon = canonical_path(path)
        return canon in self._approved_actions.get(tool_name, set())
    
    def remember_approval(self, tool_name: str, arguments: Dict):
        """Remember that this action was approved."""
        from .filesystem_context import canonical_path
        
        path = arguments.get("path", "") or arguments.get("source", "")
        if not path:
            return
        
        canon = canonical_path(path)
        if tool_name not in self._approved_actions:
            self._approved_actions[tool_name] = set()
        self._approved_actions[tool_name].add(canon)
        logger.info(f"Approval remembered: {tool_name} on {canon}")
    
    def is_action_completed(self, tool_name: str, arguments: Dict) -> tuple[bool, Optional[str]]:
        """Check if this action was already completed. Returns (completed, result_summary)."""
        key = self._get_canonical_key(tool_name, arguments)
        if key in self._completed_actions:
            return True, self._completed_actions[key]
        return False, None
    
    def mark_action_completed(self, tool_name: str, arguments: Dict, result_summary: str):
        """Mark an action as completed."""
        key = self._get_canonical_key(tool_name, arguments)
        self._completed_actions[key] = result_summary
        logger.info(f"Action completed: {key}")
    
    def new_prompt_id(self) -> str:
        """Generate new prompt ID - INVALIDATES all pending from previous"""
        with self._lock:
            if self._current_prompt_id:
                self._invalidate_all_for_prompt(self._current_prompt_id)
            
            self._prompt_counter += 1
            self._current_prompt_id = f"p{self._prompt_counter}_{int(time.time()*1000)}"
            self._by_prompt[self._current_prompt_id] = []
            # Clear executed IDs for new prompt
            self._executed_ids.clear()
            # NOTE: Do NOT clear _approved_actions or _completed_actions - they persist across prompts
            
            logger.info(f"New prompt: {self._current_prompt_id}")
            return self._current_prompt_id
    
    def _invalidate_all_for_prompt(self, prompt_id: str):
        """Mark ALL non-terminal tools from prompt as STALE"""
        tool_ids = self._by_prompt.get(prompt_id, [])
        for tid in tool_ids:
            ex = self._executions.get(tid)
            if ex and not ex.is_terminal():
                ex.transition(ToolExecutionState.STALE, "New prompt started")
                logger.warning(f"STALE: {tid}")
    
    def invalidate_all_pending(self, reason: str = "stop"):
        """Invalidate ALL pending tools globally"""
        with self._lock:
            count = 0
            for ex in self._executions.values():
                if not ex.is_terminal():
                    ex.transition(ToolExecutionState.STALE, reason)
                    count += 1
            self._executed_ids.clear()
            logger.info(f"Invalidated {count} pending tools: {reason}")
    
    def clear_all_memory(self):
        """Clear approval and completion memory (use on context clear)."""
        self._approved_actions.clear()
        self._completed_actions.clear()
        logger.info("Cleared approval and completion memory")
    
    def register(
        self,
        tool_name: str,
        tool_server: str,
        arguments: Dict[str, Any],
        prompt_id: str,
        danger_level: str = "safe"
    ) -> ToolExecution:
        """Register new tool execution request"""
        with self._lock:
            # Validate prompt is current
            if prompt_id != self._current_prompt_id:
                raise ValueError(f"Cannot register for stale prompt {prompt_id}")
            
            tool_call_id = f"tc_{uuid.uuid4().hex[:8]}_{int(time.time()*1000)}"
            
            ex = ToolExecution(
                tool_call_id=tool_call_id,
                origin_prompt_id=prompt_id,
                tool_name=tool_name,
                tool_server=tool_server,
                arguments=arguments,
                danger_level=danger_level,
            )
            
            self._executions[tool_call_id] = ex
            self._by_prompt[prompt_id].append(tool_call_id)
            
            logger.info(f"Registered: {tool_call_id} ({tool_name})")
            return ex
    
    def get(self, tool_call_id: str) -> Optional[ToolExecution]:
        return self._executions.get(tool_call_id)
    
    def validate_can_execute(self, tool_call_id: str, prompt_id: str) -> tuple[bool, str]:
        """Validate tool can execute NOW"""
        ex = self._executions.get(tool_call_id)
        
        if not ex:
            return False, "Tool not found"
        
        if ex.origin_prompt_id != prompt_id:
            return False, f"Wrong prompt: {ex.origin_prompt_id} != {prompt_id}"
        
        if ex.state == ToolExecutionState.STALE:
            return False, "Tool is stale"
        
        if ex.is_terminal():
            return False, f"Terminal state: {ex.state.value}"
        
        if ex.state not in (ToolExecutionState.REQUESTED, ToolExecutionState.APPROVED):
            return False, f"Invalid state: {ex.state.value}"
        
        # IDEMPOTENCY GUARD - prevent duplicate execution
        if tool_call_id in self._executed_ids:
            return False, f"Already executed: {tool_call_id}"
        
        return True, "OK"
    
    def mark_executed(self, tool_call_id: str):
        """Mark tool as executed - prevents re-execution"""
        with self._lock:
            self._executed_ids.add(tool_call_id)
    
    def is_executed(self, tool_call_id: str) -> bool:
        """Check if tool was already executed"""
        return tool_call_id in self._executed_ids
    
    def complete_execution(
        self,
        tool_call_id: str,
        result: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        error: Optional[str] = None,
        executor_confirmed: bool = True
    ) -> bool:
        """Mark execution complete with validation"""
        ex = self._executions.get(tool_call_id)
        if not ex:
            logger.error(f"Cannot complete unknown: {tool_call_id}")
            return False
        
        # Store results
        ex.result_raw = result
        ex.exit_code = exit_code
        ex.stdout = stdout
        ex.stderr = stderr
        ex.error_message = error
        ex.executor_completed = executor_confirmed
        
        # Determine state based on output
        if not executor_confirmed:
            return ex.transition(ToolExecutionState.EXECUTOR_MISSING, "No executor response")
        elif error:
            return ex.transition(ToolExecutionState.FAILED, error)
        elif ex.validate_output():
            return ex.transition(ToolExecutionState.COMPLETED_SUCCESS)
        else:
            return ex.transition(ToolExecutionState.COMPLETED_EMPTY, "No valid output")
    
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            by_state = {}
            for ex in self._executions.values():
                state = ex.state.value
                by_state[state] = by_state.get(state, 0) + 1
            
            return {
                "total": len(self._executions),
                "current_prompt": self._current_prompt_id,
                "by_state": by_state,
            }


class HallucinationDetector:
    """
    Detects when LLM hallucinates external state.
    HARD RULES:
    - Any claim about EXTERNAL filesystem/OS/network state requires validated tool result
    - File names/paths only if verbatim in tool output
    - Allow capability/meta questions about the agent itself
    - Silence > guessing
    """
    
    # Patterns indicating claims about external state
    EXTERNAL_STATE_PATTERNS = [
        # Filesystem claims - must be about SPECIFIC files, not general capability
        r"(?:the|your|this) (?:file|directory|folder) ['\"`]?[\w./]+['\"`]? (?:contains?|has|shows?|exists?)",
        r"(?:found|see|noticed|detected) (?:the|a|some) files? (?:named|called|at)",
        r"(?:there (?:is|are)) (?:a |the )?(?:file|folder) (?:named|called|at)",
        
        # Execution claims - past tense actions on specific targets
        r"(?:i |i've |i have )?(?:successfully )?(?:deleted|created|modified|moved|copied) (?:the |a )?(?:file|folder|directory) ['\"`]?[\w./]+",
        r"(?:i |i've |i have )?(?:executed|ran) (?:the )?(?:command|tool) .+ and (?:it |the )",
        
        # Specific system state claims
        r"(?:your|the) (?:system|computer) (?:is running|has) [\w\s]+ installed",
        
        # Network state claims - specific hosts
        r"(?:the )?(?:server|host) [\w.:]+ (?:is|was) (?:up|down|reachable|unreachable)",
    ]
    
    # Instruction fallback patterns - when LLM tells user to do something instead of doing it
    INSTRUCTION_PATTERNS = [
        r"(?:you )?(?:can|should|need to|would need to) (?:run|execute|use|type) (?:the |a )?(?:command|tool)",
        r"to (?:do|accomplish|check|see) (?:this|that),?\s*(?:you )?(?:can |should )?(?:run|execute|use)",
        r"try (?:running|executing|using)[:\s]+[`\"\']",
    ]
    
    # Allowed patterns - meta/capability questions
    ALLOWED_PATTERNS = [
        r"i (?:can|have|am able to) (?:use|access|help with)",  # Capability statements
        r"(?:my |the )?(?:tools|capabilities|functions) (?:include|are|allow)",
        r"i (?:have access to|can use) (?:tools|functions|commands)",
        r"(?:available|supported) (?:tools|commands|functions)",
        r"i (?:don't|do not|cannot) have (?:access|tools|capability)",
        r"let me (?:check|look|search|find|list)",
        r"i'll (?:check|look|search|find|list)",
    ]
    
    def __init__(self):
        self._external_re = [re.compile(p, re.IGNORECASE) for p in self.EXTERNAL_STATE_PATTERNS]
        self._instruction_re = [re.compile(p, re.IGNORECASE) for p in self.INSTRUCTION_PATTERNS]
        self._allowed_re = [re.compile(p, re.IGNORECASE) for p in self.ALLOWED_PATTERNS]
        self._known_paths: Set[str] = set()  # Paths from validated tool output
    
    def register_valid_paths(self, tool_output: str):
        """Extract and register paths from validated tool output"""
        # Extract file paths
        path_patterns = [
            r'[/~][\w\-./]+',  # Unix paths
            r'[A-Z]:\\[\w\-\\]+',  # Windows paths
        ]
        for pattern in path_patterns:
            for match in re.findall(pattern, tool_output):
                self._known_paths.add(match)
    
    def _is_allowed_response(self, llm_output: str) -> bool:
        """Check if response is allowed (capability/meta question)"""
        for pattern in self._allowed_re:
            if pattern.search(llm_output):
                return True
        return False
    
    def check(self, llm_output: str, has_tool_call: bool, last_tool_output: Optional[str] = None) -> Dict[str, Any]:
        """
        Check LLM output for hallucination indicators.
        """
        result = {
            "has_external_claim": False,
            "has_instruction_fallback": False,
            "has_unknown_path": False,
            "violations": [],
        }
        
        if has_tool_call:
            return result  # Has actual tool call, OK
        
        # Check if this is an allowed capability/meta response
        if self._is_allowed_response(llm_output):
            return result  # Allowed
        
        # If we have recent tool output, LLM is allowed to reference it
        has_recent_context = last_tool_output is not None and len(last_tool_output) > 0
        
        # Check for external state claims ONLY if no recent tool output
        if not has_recent_context:
            for pattern in self._external_re:
                match = pattern.search(llm_output)
                if match:
                    result["has_external_claim"] = True
                    result["violations"].append({
                        "type": "external_state_claim",
                        "match": match.group(),
                        "context": llm_output[max(0, match.start()-30):match.end()+30]
                    })
        
        # Check for instruction fallback (always check)
        for pattern in self._instruction_re:
            match = pattern.search(llm_output)
            if match:
                result["has_instruction_fallback"] = True
                result["violations"].append({
                    "type": "instruction_fallback",
                    "match": match.group(),
                })
        
        # Check for paths not in validated output
        path_mentions = re.findall(r'[`"\']([/~][\w\-./]+)[`"\']', llm_output)
        for path in path_mentions:
            if path not in self._known_paths and len(path) > 3:
                result["has_unknown_path"] = True
                result["violations"].append({
                    "type": "unknown_path",
                    "path": path,
                })
        
        return result
    
    def get_rejection_message(self, detection: Dict) -> str:
        """Generate correction message for violations"""
        if detection["has_external_claim"]:
            return (
                "[SYSTEM: HALLUCINATION DETECTED - You claimed external state without "
                "a validated tool result. You MUST call a tool to get actual information. "
                "Do NOT describe state you haven't verified. Call the appropriate tool or "
                "state that you cannot answer without executing it.]"
            )
        elif detection["has_instruction_fallback"]:
            return (
                "[SYSTEM: CAPABILITY VIOLATION - You gave instructions instead of "
                "executing the tool yourself. Either execute the tool or explicitly "
                "state you cannot perform this action.]"
            )
        elif detection["has_unknown_path"]:
            return (
                "[SYSTEM: PATH HALLUCINATION - You mentioned a file path that was not "
                "in any tool output. Only mention paths that appear verbatim in tool results.]"
            )
        return ""
    
    def clear_paths(self):
        """Clear known paths on new session"""
        self._known_paths.clear()
