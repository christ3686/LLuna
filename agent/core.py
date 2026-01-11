"""
LLuna v7.0 - Autonomous Agent Core
===================================
HARD RULES:
- Tools = ground truth
- No pretending execution
- No inferred reality
- Prefer refusal over hallucination
- Constrain small models aggressively
- Determinism > helpfulness

V7 ADDITIONS:
- Tool session cache for better tool awareness
- Meta-question handling (no false hallucination on capability questions)
- Tool-not-found suggestions
- Sudo handling with approval flow
"""

import json
import logging
import time
import re
import asyncio
import threading
import os
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable
from enum import Enum
from collections import deque

from .tool_integrity import (
    ToolExecution,
    ToolExecutionState,
    ToolExecutionRegistry,
    HallucinationDetector,
)
from .loop_discipline import (
    LoopDiscipline,
    LoopState,
    ReasoningUnit,
    ConfidenceEstimator,
    extract_reasoning_unit,
    ModelSize,
)
from .argument_normalizer import normalize_tool_arguments
from .filesystem_context import FilesystemContext, PathNormalizer
from .tool_cache import ToolCache, SudoState, is_meta_question

logger = logging.getLogger(__name__)


class CognitiveState(str, Enum):
    IDLE = "idle"
    PERCEIVING = "perceiving"
    REASONING = "reasoning"
    TOOL_REQUESTED = "tool_requested"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    OBSERVING = "observing"
    RESPONDING = "responding"
    COMPLETE = "complete"
    STOPPED = "stopped"
    ERROR = "error"
    HALLUCINATION_BLOCKED = "hallucination_blocked"
    LOOP_ABORTED = "loop_aborted"


class DangerLevel(str, Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


@dataclass
class CognitiveEvent:
    state: CognitiveState
    thought: str = ""
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_server: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    tool_error: Optional[str] = None
    tool_state: Optional[str] = None
    duration_ms: int = 0
    requires_approval: bool = False
    iteration: int = 0
    prompt_id: Optional[str] = None
    tokens_used: int = 0
    confidence: float = 0.0
    reasoning_unit: Optional[Dict] = None
    violation_type: Optional[str] = None


@dataclass
class Message:
    role: str
    content: str
    prompt_id: Optional[str] = None
    tokens: int = 0
    pinned: bool = False
    from_tool: bool = False
    
    def estimate_tokens(self) -> int:
        self.tokens = len(self.content) // 4 + 1
        return self.tokens


class ContextWindow:
    """Sliding window with prompt tracking"""
    
    def __init__(self, max_tokens: int = 4096, reserve_output: int = 1024):
        self.max_tokens = max_tokens
        self.reserve_output = reserve_output
        self.available_tokens = max_tokens - reserve_output
        self.messages: deque = deque()
        self.system_prompt: Optional[Message] = None
        self.tool_results: deque = deque(maxlen=3)
    
    def set_system_prompt(self, content: str):
        self.system_prompt = Message(role="system", content=content, pinned=True)
        self.system_prompt.estimate_tokens()
    
    def add_message(self, role: str, content: str, prompt_id: str = None, 
                    pinned: bool = False, from_tool: bool = False):
        msg = Message(role=role, content=content, prompt_id=prompt_id, 
                     pinned=pinned, from_tool=from_tool)
        msg.estimate_tokens()
        
        if from_tool:
            self.tool_results.append(msg)
        
        self.messages.append(msg)
        self._prune_if_needed()
    
    def _prune_if_needed(self):
        threshold = int(self.available_tokens * 0.8)
        while self._total_tokens() > threshold and len(self.messages) > 4:
            for i, msg in enumerate(self.messages):
                if not msg.pinned and msg not in self.tool_results:
                    del self.messages[i]
                    break
            else:
                if len(self.messages) > 2:
                    self.messages.popleft()
                break
    
    def _total_tokens(self) -> int:
        total = self.system_prompt.tokens if self.system_prompt else 0
        return total + sum(m.tokens for m in self.messages)
    
    def get_messages(self) -> List[Dict[str, str]]:
        result = []
        if self.system_prompt:
            result.append({"role": "system", "content": self.system_prompt.content})
        for msg in self.messages:
            result.append({"role": msg.role, "content": msg.content})
        return result
    
    def clear(self):
        self.messages.clear()
        self.tool_results.clear()
    
    def get_stats(self) -> Dict[str, int]:
        return {
            "total_tokens": self._total_tokens(),
            "max_tokens": self.available_tokens,
            "usage_percent": int(self._total_tokens() / self.available_tokens * 100)
        }


class AutonomousAgent:
    """
    LLuna v7.0 - Fully integrity-enforced autonomous agent.
    
    V7 Features:
    - Tool session cache for better tool awareness
    - Meta-question handling
    - Tool-not-found suggestions
    - Sudo handling with approval flow
    """
    
    CRITICAL_TOOLS = {"delete_file", "delete_directory", "memory_clear_all", "bash_script"}
    DANGEROUS_TOOLS = {"write_file", "create_file", "bash_execute", "run_python", "execute_command", "run_bash", "git_commit", "git_push"}
    MODERATE_TOOLS = {"set_env_var", "clipboard_write"}

    APPROVAL_TIMEOUT = 300
    EXECUTION_TIMEOUT = 60

    def __init__(self, llm_manager, mcp_client, max_iterations: int = 20,
                 auto_approve_safe: bool = True, max_context_tokens: int = 4096):
        self.llm = llm_manager
        self.mcp = mcp_client
        self.max_iterations = max_iterations
        self.auto_approve_safe = auto_approve_safe
        
        # Context
        self.context = ContextWindow(max_tokens=max_context_tokens)
        
        # Integrity systems
        self.tool_registry = ToolExecutionRegistry()
        self.hallucination_detector = HallucinationDetector()
        self.loop_discipline = LoopDiscipline()
        self.confidence_estimator = ConfidenceEstimator()
        
        # Filesystem context for path resolution
        self.fs_context = FilesystemContext()
        self.path_normalizer = PathNormalizer(self.fs_context)
        
        # V7: Tool session cache
        self.tool_cache = ToolCache()
        
        # V7: Sudo state
        self._sudo_pending: bool = False
        self._sudo_event: Optional[threading.Event] = None
        self._sudo_approved: bool = False
        self._sudo_password: Optional[str] = None
        
        # Kill switch
        self._stop_event = threading.Event()
        self._running = False
        
        # State
        self.current_state = CognitiveState.IDLE
        self._current_prompt_id: Optional[str] = None
        self._pending_execution: Optional[ToolExecution] = None
        self._approval_event: Optional[threading.Event] = None  # Thread-safe event
        self._last_tool_output: Optional[str] = None
        self._just_had_tool_output: bool = False  # For narration suppression
        self._last_user_message: str = ""  # V7: Track for meta-question detection
        self._executed_tools_this_prompt: List[str] = []  # V7: Track for hallucination check
        
        # Stats
        self.total_tool_calls = 0
        self.hallucination_blocks = 0
        self.loop_aborts = 0
        self.session_start = None
    
    def refresh_tool_cache(self):
        """Refresh tool cache from MCP servers"""
        tools = self.mcp.get_all_tools_deduplicated()
        count = self.tool_cache.refresh(tools)
        logger.info(f"Tool cache refreshed: {count} tools")
    
    def stop(self):
        """Kill switch - invalidates ALL pending"""
        self._stop_event.set()
        self.tool_registry.invalidate_all_pending("stop requested")
        logger.info("STOP: All pending tools invalidated")
    
    def reset_stop(self):
        self._stop_event.clear()
    
    def is_stopped(self) -> bool:
        return self._stop_event.is_set()
    
    def _classify_danger(self, tool_name: str) -> DangerLevel:
        if tool_name in self.CRITICAL_TOOLS:
            return DangerLevel.CRITICAL
        if tool_name in self.DANGEROUS_TOOLS:
            return DangerLevel.DANGEROUS
        if tool_name in self.MODERATE_TOOLS:
            return DangerLevel.MODERATE
        return DangerLevel.SAFE
    
    def _build_system_prompt(self) -> str:
        """Compact system prompt with integrity rules - V7"""
        home = os.path.expanduser("~")
        fs_ctx = self.fs_context.get_context_for_prompt()
        
        # V7: Use tool cache for better organization
        tools_str = self.tool_cache.get_tool_list_for_prompt()
        if not tools_str or self.tool_cache.tool_count == 0:
            tools_str = "NO TOOLS AVAILABLE - Check server status"
        
        # V7: Add tool count for awareness
        tool_count = self.tool_cache.tool_count
        
        return f'''You are LLuna, an AI agent. You have {tool_count} tools available.

HOW TO USE TOOLS:
- Output JSON: {{"tool":"tool_name","arguments":{{"param":"value"}}}}
- Wait for result before responding
- Don't narrate results - the user can see them

AVAILABLE TOOLS:
{tools_str}

WHEN ASKED "what tools do you have" or similar:
- Simply list your available tools from above
- No need to call any tool for this question

EXAMPLE:
User: what's on my desktop?
You: {{"tool":"list_directory","arguments":{{"path":"{home}/Desktop"}}}}
[Result appears]
You: You have 3 files: readme.txt, notes.md, photo.jpg

User: what tools do you have?
You: I have {tool_count} tools including list_directory, read_file, write_file, execute_command, and others for filesystem, network, and system tasks.

PATHS: home={home} | {fs_ctx}'''
    
    def _extract_tool_call(self, content: str) -> Optional[Dict[str, Any]]:
        """Extract tool call with strict validation and error recovery"""
        if not content:
            return None
        
        content = content.strip()
        
        # Direct JSON - try first
        try:
            data = json.loads(content)
            if isinstance(data.get("tool"), str) and data["tool"]:
                args = data.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                return {"tool": data["tool"], "arguments": args}
        except:
            pass
        
        # JSON in code block
        for pattern in [r'```json\s*\n?([\s\S]*?)\n?```', r'```\s*\n?(\{[\s\S]*?\})\n?```']:
            for match in re.finditer(pattern, content, re.DOTALL):
                try:
                    data = json.loads(match.group(1).strip())
                    if isinstance(data.get("tool"), str) and data["tool"]:
                        args = data.get("arguments", {})
                        if not isinstance(args, dict):
                            args = {}
                        return {"tool": data["tool"], "arguments": args}
                except:
                    continue
        
        # JSON object in text - more lenient pattern
        json_match = re.search(r'\{[^{}]*"tool"\s*:\s*"([^"]+)"[^{}]*\}', content)
        if json_match:
            try:
                data = json.loads(json_match.group())
                if isinstance(data.get("tool"), str):
                    args = data.get("arguments", {})
                    if not isinstance(args, dict):
                        args = {}
                    return {"tool": data["tool"], "arguments": args}
            except:
                pass
        
        # FALLBACK: Extract tool name even from malformed JSON
        # Handles cases like: {"tool":"ping","arguments":{"}}" 
        tool_match = re.search(r'"tool"\s*:\s*"([^"]+)"', content)
        if tool_match:
            tool_name = tool_match.group(1)
            
            # Try to extract arguments
            args = {}
            args_match = re.search(r'"arguments"\s*:\s*\{([^}]*)\}', content)
            if args_match:
                args_content = args_match.group(1).strip()
                if args_content and not args_content.startswith('"'):
                    # Malformed, try to extract key-value pairs
                    for kv in re.finditer(r'"([^"]+)"\s*:\s*"([^"]*)"', args_content):
                        args[kv.group(1)] = kv.group(2)
            
            logger.warning(f"Recovered malformed tool call: {tool_name} with args {args}")
            return {"tool": tool_name, "arguments": args}
        
        return None
    
    def _is_redundant_narration(self, llm_output: str) -> bool:
        """
        Detect if LLM is just narrating/repeating tool output.
        Returns True if the output appears redundant.
        """
        if not self._last_tool_output:
            return False
        
        output_lower = llm_output.lower().strip()
        tool_output_lower = self._last_tool_output.lower()
        
        # Check for narration patterns FIRST (even in short outputs)
        narration_patterns = [
            "the output shows",
            "the result shows",
            "the command returned",
            "the tool returned",
            "here's what",
            "here is what",
            "the directory contains",
            "the file contains",
            "i can see that",
            "as you can see",
            "the listing shows",
            "contains the following",
            "following items",
            "following files",
            "shows that",
            "returned the following",
            "the folder contains",
            "desktop directory contains",
            "desktop folder contains",
        ]
        
        has_narration = any(p in output_lower for p in narration_patterns)
        
        # If narration pattern found, check for ANY content overlap
        if has_narration:
            # Extract meaningful words (3+ chars)
            tool_words = {w for w in tool_output_lower.split() if len(w) >= 3}
            output_words = {w for w in output_lower.split() if len(w) >= 3}
            
            # If we find ANY filename/path from tool output in narration, it's redundant
            if tool_words and output_words:
                overlap = tool_words & output_words
                # Even 1 significant word overlap + narration pattern = redundant
                if len(overlap) >= 1:
                    logger.info(f"Narration detected: pattern match + overlap {overlap}")
                    return True
        
        # Also detect pure regurgitation (even without narration patterns)
        if len(output_lower) > 20:
            tool_words = {w for w in tool_output_lower.split() if len(w) >= 3}
            output_words = {w for w in output_lower.split() if len(w) >= 3}
            if tool_words and len(tool_words) >= 3:
                overlap_ratio = len(tool_words & output_words) / len(tool_words)
                if overlap_ratio > 0.5:  # More than half the tool words appear
                    logger.info(f"Regurgitation detected: {overlap_ratio:.0%} overlap")
                    return True
        
        return False
    
    def _filter_leaked_text(self, llm_output: str) -> str:
        """
        Filter out leaked system/instruction text from LLM output.
        Small models often regurgitate context markers.
        """
        # Patterns to remove
        leak_patterns = [
            r'\[INSTRUCTION:.*?\]',
            r'\[SYSTEM:.*?\]',
            r'\[TOOL OUTPUT:.*?\]',
            r'---\s*Task complete\?.*?tool\.',
            r'Task complete\? Reply briefly\. More work\? Call next tool\.',
            r'ID:tc_[a-f0-9_]+',  # Tool IDs
            r'tc_[a-f0-9]+_\d+',  # Tool call IDs
            r'✅\s*(?:Exists|Success)',  # Status markers
            r'google\.com:\d+ is open\s*(?:google\.com:\d+ is open\s*)+',  # Repeated status
        ]
        
        filtered = llm_output
        for pattern in leak_patterns:
            filtered = re.sub(pattern, '', filtered, flags=re.IGNORECASE | re.DOTALL)
        
        # Clean up multiple spaces and newlines
        filtered = re.sub(r'\n{3,}', '\n\n', filtered)
        filtered = re.sub(r' {2,}', ' ', filtered)
        
        return filtered.strip()
    
    async def _execute_tool_with_integrity(
        self,
        execution: ToolExecution,
        emit: Callable
    ) -> bool:
        """Execute with full integrity tracking"""
        # Validate
        can_execute, reason = self.tool_registry.validate_can_execute(
            execution.tool_call_id,
            self._current_prompt_id
        )
        
        if not can_execute:
            execution.transition(ToolExecutionState.FAILED, error=reason)
            return False
        
        # Transition to EXECUTING
        if not execution.transition(ToolExecutionState.EXECUTING):
            return False
        
        execution.executor_acknowledged = True
        
        emit(CognitiveEvent(
            state=CognitiveState.EXECUTING,
            thought=f"Executing {execution.tool_name}...",
            tool_call_id=execution.tool_call_id,
            tool_name=execution.tool_name,
            tool_server=execution.tool_server,
            tool_args=execution.arguments,
            tool_state=execution.state.value,
            prompt_id=self._current_prompt_id,
        ))
        
        # Execute with timeout
        try:
            result = await asyncio.wait_for(
                self.mcp.call_tool(
                    execution.tool_server,
                    execution.tool_name,
                    execution.arguments
                ),
                timeout=self.EXECUTION_TIMEOUT
            )
            
            # Validate MCP response
            if result is None:
                self.tool_registry.complete_execution(
                    execution.tool_call_id,
                    error="MCP returned None",
                    executor_confirmed=False
                )
            elif "error" in result:
                self.tool_registry.complete_execution(
                    execution.tool_call_id,
                    error=result["error"],
                    executor_confirmed=True
                )
            else:
                output = result.get("result", "")
                self.tool_registry.complete_execution(
                    execution.tool_call_id,
                    result=output,
                    executor_confirmed=True
                )
                
                # Register valid paths from output
                if output:
                    self.hallucination_detector.register_valid_paths(output)
                    self._last_tool_output = output
            
            self.total_tool_calls += 1
            return True
            
        except asyncio.TimeoutError:
            execution.transition(ToolExecutionState.TIMEOUT, f"Timeout ({self.EXECUTION_TIMEOUT}s)")
            return True
        except Exception as e:
            self.tool_registry.complete_execution(
                execution.tool_call_id,
                error=str(e),
                executor_confirmed=False
            )
            return True
    
    async def _probe_post_execution(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        emit: Callable
    ) -> Optional[str]:
        """
        Post-execution verification probe for mutating operations.
        Returns observable state to give LLM ground truth.
        """
        probe_tool = None
        probe_args = {}
        
        # Determine appropriate verification probe
        if tool_name in ("write_file", "append_file", "create_file"):
            path = arguments.get("path", "")
            if path:
                probe_tool = "file_exists"
                probe_args = {"path": path}
        
        elif tool_name == "delete_file":
            path = arguments.get("path", "")
            if path:
                probe_tool = "file_exists"
                probe_args = {"path": path}
        
        elif tool_name in ("create_directory", "delete_directory"):
            path = arguments.get("path", "")
            if path:
                # Check parent directory to confirm state
                parent = os.path.dirname(path.rstrip("/")) or path
                probe_tool = "list_directory"
                probe_args = {"path": parent}
        
        elif tool_name in ("move_file", "copy_file"):
            dest = arguments.get("destination", "")
            if dest:
                probe_tool = "file_exists"
                probe_args = {"path": dest}
        
        if not probe_tool:
            return None
        
        # Find server for probe tool
        probe_server = self.mcp.find_tool_server(probe_tool)
        if not probe_server:
            return None
        
        # Execute probe (silent - no full registration)
        try:
            result = await asyncio.wait_for(
                self.mcp.call_tool(probe_server, probe_tool, probe_args),
                timeout=10
            )
            
            if result and "result" in result:
                output = result["result"]
                return f"[VERIFICATION: {probe_tool}({probe_args.get('path', '')})]\n{output}"
            elif result and "error" in result:
                return f"[VERIFICATION: {probe_tool} failed: {result['error']}]"
        except Exception as e:
            logger.debug(f"Probe failed: {e}")
        
        return None
    
    async def process(self, user_message: str, on_event: Optional[Callable] = None) -> str:
        """Main cognitive loop with full integrity enforcement"""
        self.reset_stop()
        self._running = True
        self.session_start = time.time()
        self._last_tool_output = None
        self._last_user_message = user_message  # V7: Track for meta-question detection
        self._executed_tools_this_prompt = []  # V7: Track executed tools
        
        def emit(ev: CognitiveEvent):
            if on_event:
                ev.tokens_used = self.context.get_stats()["total_tokens"]
                on_event(ev)
        
        # V7: Emit sudo events through this wrapper
        def emit_sudo(data: Dict):
            if on_event:
                on_event(CognitiveEvent(
                    state=CognitiveState.AWAITING_APPROVAL,
                    thought=f"Sudo required: {data.get('reason', '')}",
                    prompt_id=self._current_prompt_id,
                    tool_name=data.get('tool_name'),
                    requires_approval=True,
                ))
        
        # Check LLM
        if not self.llm.is_connected:
            emit(CognitiveEvent(state=CognitiveState.ERROR, thought="No LLM connected"))
            return "Error: Connect to LLM first."
        
        # Set model size for loop discipline
        if self.llm.provider:
            self.loop_discipline.set_model_size(self.llm.provider.model)
        
        # V7: Refresh tool cache at start of each prompt
        self.refresh_tool_cache()
        
        # NEW PROMPT - invalidates ALL pending and clears context
        self._current_prompt_id = self.tool_registry.new_prompt_id()
        self.loop_discipline.new_prompt()
        self.hallucination_detector.clear_paths()
        self.context.clear()  # CRITICAL: Clear old messages to prevent bleeding between prompts
        
        emit(CognitiveEvent(
            state=CognitiveState.PERCEIVING,
            thought=f"Processing: {user_message[:50]}...",
            prompt_id=self._current_prompt_id
        ))
        
        # Initialize context
        self.context.set_system_prompt(self._build_system_prompt())
        self.context.add_message("user", user_message, self._current_prompt_id)
        
        final_response = ""
        
        for iteration in range(1, self.max_iterations + 1):
            # Check kill switch
            if self.is_stopped():
                self.current_state = CognitiveState.STOPPED
                emit(CognitiveEvent(state=CognitiveState.STOPPED, thought="Stopped", prompt_id=self._current_prompt_id))
                self._running = False
                return "⏹️ Stopped."
            
            # Check loop discipline
            should_continue, abort_reason = self.loop_discipline.should_continue()
            if not should_continue:
                self.loop_aborts += 1
                self.current_state = CognitiveState.LOOP_ABORTED
                emit(CognitiveEvent(
                    state=CognitiveState.LOOP_ABORTED,
                    thought=f"Loop aborted: {abort_reason}",
                    prompt_id=self._current_prompt_id
                ))
                final_response = f"I need to stop here. {abort_reason}"
                break
            
            self.current_state = CognitiveState.REASONING
            emit(CognitiveEvent(
                state=CognitiveState.REASONING,
                thought=f"Step {iteration}",
                iteration=iteration,
                prompt_id=self._current_prompt_id
            ))
            
            try:
                # Get LLM response
                from llm import Message as LLMMessage
                messages = self.context.get_messages()
                llm_msgs = [LLMMessage(role=m["role"], content=m["content"]) for m in messages]
                
                response = self.llm.chat(messages=llm_msgs, stream=False)
                llm_output = response.content.strip()
                
                if self.is_stopped():
                    continue
                
                # Extract tool call
                tool_call = self._extract_tool_call(llm_output)
                
                # Estimate confidence
                has_recent_tool_output = self._last_tool_output is not None
                confidence = self.confidence_estimator.estimate(llm_output, has_recent_tool_output)
                
                # V7: SKIP HALLUCINATION CHECK FOR META-QUESTIONS
                # Questions about tools, capabilities, problems should NOT trigger hallucination detection
                is_meta = is_meta_question(self._last_user_message)
                
                # Check for hallucination (but skip for meta-questions)
                hallucination_check = self.hallucination_detector.check(
                    llm_output,
                    tool_call is not None,
                    self._last_tool_output
                )
                
                # Only block hallucinations if NOT a meta-question
                if not is_meta and (hallucination_check["has_external_claim"] or hallucination_check["has_unknown_path"]):
                    self.hallucination_blocks += 1
                    
                    emit(CognitiveEvent(
                        state=CognitiveState.HALLUCINATION_BLOCKED,
                        thought="Hallucination blocked",
                        violation_type="external_state" if hallucination_check["has_external_claim"] else "unknown_path",
                        iteration=iteration,
                        prompt_id=self._current_prompt_id
                    ))
                    
                    rejection = self.hallucination_detector.get_rejection_message(hallucination_check)
                    self.context.add_message("user", rejection, self._current_prompt_id)
                    
                    # Record low confidence
                    reasoning_unit = ReasoningUnit(
                        decision="hallucination_blocked",
                        confidence=0.0,
                        next_action="retry",
                    )
                    self.loop_discipline.record_iteration(reasoning_unit)
                    continue
                
                if not is_meta and hallucination_check["has_instruction_fallback"] and not tool_call:
                    emit(CognitiveEvent(
                        state=CognitiveState.HALLUCINATION_BLOCKED,
                        thought="Instruction fallback blocked",
                        violation_type="instruction_fallback",
                        iteration=iteration,
                        prompt_id=self._current_prompt_id
                    ))
                    
                    rejection = self.hallucination_detector.get_rejection_message(hallucination_check)
                    self.context.add_message("user", rejection, self._current_prompt_id)
                    continue
                
                # Create reasoning unit
                reasoning_unit = extract_reasoning_unit(llm_output, confidence, tool_call is not None)
                
                if tool_call:
                    tool_name = tool_call.get("tool", "")
                    arguments = tool_call.get("arguments", {})
                    
                    # V7: Check tool cache first with suggestions
                    if not self.tool_cache.has_tool(tool_name):
                        # Get helpful suggestions
                        error_msg = self.tool_cache.get_tool_not_found_message(tool_name)
                        self.context.add_message(
                            "user",
                            f"[TOOL NOT FOUND]\n{error_msg}",
                            self._current_prompt_id,
                            from_tool=True
                        )
                        self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=False)
                        continue
                    
                    # Find server
                    server = self.mcp.find_tool_server(tool_name)
                    if not server:
                        # This shouldn't happen if cache is in sync, but handle gracefully
                        similar = self.tool_cache.find_similar(tool_name)
                        suggestions = [f"'{t}'" for t, _ in similar[:3]]
                        error = f"Tool '{tool_name}' not available. Try: {', '.join(suggestions) if suggestions else 'check server status'}"
                        self.context.add_message("user", f"[TOOL ERROR]\n{error}", self._current_prompt_id, from_tool=True)
                        self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=False)
                        continue
                    
                    # NORMALIZE ARGUMENTS - fix small model mistakes
                    schema = self.mcp.get_tool_schema(tool_name)
                    if schema:
                        arguments, is_valid, error_msg = normalize_tool_arguments(
                            tool_name, arguments, schema
                        )
                        if not is_valid:
                            # Return error to LLM so it can retry with correct args
                            self.context.add_message(
                                "user", 
                                f"[TOOL ARG ERROR: {tool_name}]\n{error_msg}\n\nSchema: {json.dumps(schema, indent=2)}", 
                                self._current_prompt_id, 
                                from_tool=True
                            )
                            self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=False)
                            continue
                    
                    # NORMALIZE PATHS - resolve relative paths using filesystem context
                    arguments = self.path_normalizer.normalize_arguments(tool_name, arguments)
                    
                    # CHECK IF ACTION ALREADY COMPLETED - prevent duplicate execution
                    was_completed, prev_result = self.tool_registry.is_action_completed(tool_name, arguments)
                    if was_completed:
                        logger.info(f"Action already completed: {tool_name}")
                        self.context.add_message(
                            "user",
                            f"[TOOL ALREADY DONE: {tool_name}]\nThis action was already completed: {prev_result}\n\n[Continue with next task or respond]",
                            self._current_prompt_id,
                            from_tool=True
                        )
                        self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=True)
                        continue
                    
                    danger = self._classify_danger(tool_name)
                    
                    # CHECK IF ACTION PREVIOUSLY APPROVED - skip approval if so
                    previously_approved = self.tool_registry.is_action_approved(tool_name, arguments)
                    
                    # Register execution
                    execution = self.tool_registry.register(
                        tool_name=tool_name,
                        tool_server=server,
                        arguments=arguments,
                        prompt_id=self._current_prompt_id,
                        danger_level=danger.value
                    )
                    
                    emit(CognitiveEvent(
                        state=CognitiveState.TOOL_REQUESTED,
                        thought=f"Tool: {tool_name}",
                        tool_call_id=execution.tool_call_id,
                        tool_name=tool_name,
                        tool_server=server,
                        tool_args=arguments,
                        tool_state=execution.state.value,
                        requires_approval=danger in (DangerLevel.DANGEROUS, DangerLevel.CRITICAL) and not previously_approved,
                        iteration=iteration,
                        prompt_id=self._current_prompt_id,
                        confidence=confidence,
                    ))
                    
                    # Approval check - skip if previously approved
                    needs_approval = (
                        danger in (DangerLevel.DANGEROUS, DangerLevel.CRITICAL)
                        and not self.auto_approve_safe
                        and not previously_approved
                    )
                    
                    if needs_approval:
                        execution.transition(ToolExecutionState.PENDING_APPROVAL)
                        self._pending_execution = execution
                        
                        emit(CognitiveEvent(
                            state=CognitiveState.AWAITING_APPROVAL,
                            thought=f"Approval needed: {tool_name}",
                            tool_call_id=execution.tool_call_id,
                            tool_name=tool_name,
                            tool_args=arguments,
                            tool_state=execution.state.value,
                            requires_approval=True,
                            prompt_id=self._current_prompt_id,
                        ))
                        
                        # Use thread-safe Event for cross-thread signaling
                        self._approval_event = threading.Event()
                        logger.info(f"Waiting for approval: {execution.tool_call_id}")
                        
                        # Wait for approval with polling (allows async context to breathe)
                        approval_start = time.time()
                        while not self._approval_event.is_set():
                            if time.time() - approval_start > self.APPROVAL_TIMEOUT:
                                logger.warning(f"Approval timeout: {execution.tool_call_id}")
                                execution.transition(ToolExecutionState.TIMEOUT, "Approval timeout")
                                self._pending_execution = None
                                self._approval_event = None
                                self.context.add_message("user", f"[TOOL TIMEOUT: {tool_name}]\nNo approval received.", self._current_prompt_id, from_tool=True)
                                self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=False)
                                break
                            if self.is_stopped():
                                logger.info(f"Approval interrupted by stop: {execution.tool_call_id}")
                                self._pending_execution = None
                                self._approval_event = None
                                break
                            await asyncio.sleep(0.1)  # Poll every 100ms
                        
                        if not self._approval_event or not self._approval_event.is_set():
                            continue
                        
                        logger.info(f"Approval received: {execution.tool_call_id}, state={execution.state}")
                        self._pending_execution = None
                        self._approval_event = None
                        
                        if execution.state == ToolExecutionState.REJECTED:
                            self.context.add_message("user", f"[TOOL REJECTED: {tool_name}]", self._current_prompt_id, from_tool=True)
                            self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=False)
                            continue
                        
                        if execution.state == ToolExecutionState.STALE:
                            continue
                        
                        if execution.state != ToolExecutionState.APPROVED:
                            continue
                        
                        # REMEMBER APPROVAL for future requests
                        self.tool_registry.remember_approval(tool_name, arguments)
                    
                    # EXECUTE NOW
                    await self._execute_tool_with_integrity(execution, emit)
                    
                    # MARK AS EXECUTED - idempotency guard
                    self.tool_registry.mark_executed(execution.tool_call_id)
                    
                    # MARK ACTION COMPLETED - prevents re-requests
                    if execution.is_success():
                        result_summary = (execution.result_raw or "")[:100]
                        self.tool_registry.mark_action_completed(tool_name, arguments, result_summary)
                    
                    # POST-EXECUTION VERIFICATION PROBE
                    # For mutating operations, inject state verification to give LLM ground truth
                    if execution.is_success() and needs_approval:
                        verification = await self._probe_post_execution(
                            tool_name, arguments, emit
                        )
                        if verification:
                            # Append verification to result
                            execution.result_raw = (execution.result_raw or "") + "\n\n" + verification
                            self._last_tool_output = execution.result_raw
                            self.hallucination_detector.register_valid_paths(verification)
                    
                    # UPDATE FILESYSTEM CONTEXT
                    if execution.is_success() and execution.result_raw:
                        self.fs_context.update_from_tool(
                            tool_name, 
                            arguments, 
                            execution.result_raw
                        )
                    
                    # LIFECYCLE CHECK - prevent zombie responses
                    if not execution.is_consumable(self._current_prompt_id):
                        logger.warning(f"Zombie response blocked: {execution.tool_call_id} state={execution.state}")
                        continue
                    
                    # Tool has output if it completed (success OR failure)
                    # Errors are also valid information for LLM to respond to
                    tool_had_output = execution.state in (ToolExecutionState.COMPLETED_SUCCESS, 
                                                          ToolExecutionState.COMPLETED_EMPTY,
                                                          ToolExecutionState.FAILED)
                    
                    emit(CognitiveEvent(
                        state=CognitiveState.OBSERVING,
                        thought=f"Result from {tool_name}",
                        tool_call_id=execution.tool_call_id,
                        tool_name=tool_name,
                        tool_result=execution.result_raw[:300] if execution.result_raw else None,
                        tool_error=execution.error_message,
                        tool_state=execution.state.value,
                        duration_ms=execution.execution_duration_ms,
                        prompt_id=self._current_prompt_id,
                    ))
                    
                    # Add result to context
                    # Use simple format that small models won't regurgitate
                    result_text = execution.get_result_for_context()
                    
                    # Simple instruction at end - models should not repeat this
                    self.context.add_message(
                        "user",
                        f"{result_text}\n\n---\nTask complete? Reply briefly. More work? Call next tool.",
                        self._current_prompt_id,
                        pinned=True,
                        from_tool=True
                    )
                    
                    # Track that we just had tool output - used for narration detection
                    self._just_had_tool_output = True
                    
                    self.loop_discipline.record_iteration(reasoning_unit, had_tool_call=True, tool_had_output=tool_had_output)
                    continue
                
                else:
                    # No tool call - check if this is a valid response
                    
                    # FILTER OUT LEAKED SYSTEM TEXT from LLM output
                    llm_output = self._filter_leaked_text(llm_output)
                    
                    # NARRATION DETECTION - suppress redundant tool output narration
                    if self._just_had_tool_output and self._is_redundant_narration(llm_output):
                        logger.info("Suppressing redundant narration")
                        self._just_had_tool_output = False
                        # Give LLM one more chance with clearer instruction
                        self.context.add_message(
                            "user",
                            "Just say 'Done.' or state what you need next.",
                            self._current_prompt_id
                        )
                        continue
                    
                    self._just_had_tool_output = False
                    
                    # Clear last tool output after successful response
                    self._last_tool_output = None
                    
                    # Check for early exit on high confidence
                    if self.loop_discipline.should_early_exit(confidence):
                        logger.info(f"Early exit: confidence={confidence:.2f}")
                    
                    # Record conclusion for repeat detection
                    self.loop_discipline.record_conclusion(llm_output[:200])
                    
                    # Final response
                    self.current_state = CognitiveState.RESPONDING
                    final_response = llm_output
                    
                    emit(CognitiveEvent(
                        state=CognitiveState.RESPONDING,
                        thought=final_response[:100],
                        iteration=iteration,
                        prompt_id=self._current_prompt_id,
                        confidence=confidence,
                        reasoning_unit=reasoning_unit.to_dict(),
                    ))
                    
                    self.loop_discipline.record_iteration(reasoning_unit)
                    self.context.add_message("assistant", final_response, self._current_prompt_id)
                    break
                    
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                emit(CognitiveEvent(state=CognitiveState.ERROR, thought=str(e), prompt_id=self._current_prompt_id))
                final_response = f"Error: {e}"
                break
        
        if not final_response:
            final_response = "Reached iteration limit."
        
        self.current_state = CognitiveState.COMPLETE
        self._running = False
        emit(CognitiveEvent(state=CognitiveState.COMPLETE, thought=final_response[:100], prompt_id=self._current_prompt_id))
        
        return final_response
    
    def approve_pending(self) -> bool:
        if self._pending_execution and self._approval_event:
            logger.info(f"Approving: {self._pending_execution.tool_call_id}")
            if self._pending_execution.transition(ToolExecutionState.APPROVED):
                self._approval_event.set()
                logger.info("Approval event set")
                return True
        logger.warning("No pending execution to approve")
        return False
    
    def reject_pending(self) -> bool:
        if self._pending_execution and self._approval_event:
            logger.info(f"Rejecting: {self._pending_execution.tool_call_id}")
            if self._pending_execution.transition(ToolExecutionState.REJECTED, "User rejected"):
                self._approval_event.set()
                return True
        return False
    
    def approve_sudo(self, password: str) -> bool:
        """Approve sudo with password"""
        if self._sudo_pending and self._sudo_event:
            logger.info("Sudo approved")
            self._sudo_approved = True
            self._sudo_password = password
            self._sudo_event.set()
            return True
        return False
    
    def reject_sudo(self) -> bool:
        """Reject sudo request"""
        if self._sudo_pending and self._sudo_event:
            logger.info("Sudo rejected")
            self._sudo_approved = False
            self._sudo_password = None
            self._sudo_event.set()
            return True
        return False
    
    def clear_sudo(self):
        """Clear sudo state after use"""
        self._sudo_pending = False
        self._sudo_approved = False
        self._sudo_password = None
        self._sudo_event = None
        logger.info("Sudo state cleared")
    
    def has_pending_action(self) -> bool:
        return self._pending_execution is not None
    
    def get_pending_action(self) -> Optional[Dict]:
        if self._pending_execution:
            return self._pending_execution.to_dict()
        return None
    
    def clear_context(self):
        self.context.clear()
        self.tool_registry.invalidate_all_pending("context cleared")
        self.tool_registry.clear_all_memory()  # Clear approval/completion memory
        self.hallucination_detector.clear_paths()
        self.fs_context.clear()
        self._pending_execution = None
        self._last_tool_output = None
        self._just_had_tool_output = False
        self.total_tool_calls = 0
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "context": self.context.get_stats(),
            "tools": self.tool_registry.get_stats(),
            "loop": self.loop_discipline.get_stats(),
            "total_tool_calls": self.total_tool_calls,
            "hallucination_blocks": self.hallucination_blocks,
            "loop_aborts": self.loop_aborts,
            "running": self._running,
            "state": self.current_state.value,
            "current_prompt_id": self._current_prompt_id,
        }
