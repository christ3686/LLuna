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
LLuna v7.1 - Loop Discipline Module
====================================
For small models (4B-8B), aggressive constraints:
- Confidence scoring (0.0-1.0)
- Early exit on sufficient confidence
- Abort on repeated conclusions
- Model-specific reflection limits
- Structured reasoning units
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger(__name__)


class ModelSize(str, Enum):
    SMALL_4B = "4b"
    MEDIUM_8B = "8b"
    LARGE = "large"


# Model-specific limits
MODEL_LIMITS = {
    ModelSize.SMALL_4B: {
        "max_reflects": 1,
        "max_iterations": 15,
        "confidence_threshold": 0.7,
        "abort_on_repeat": 2,
    },
    ModelSize.MEDIUM_8B: {
        "max_reflects": 2,
        "max_iterations": 20,
        "confidence_threshold": 0.75,
        "abort_on_repeat": 2,
    },
    ModelSize.LARGE: {
        "max_reflects": 3,
        "max_iterations": 25,
        "confidence_threshold": 0.8,
        "abort_on_repeat": 3,
    },
}


@dataclass
class ReasoningUnit:
    """
    Structured reasoning unit - replaces verbose thoughts.
    Token-efficient for small models.
    """
    decision: str           # What to do
    confidence: float       # 0.0-1.0
    next_action: str        # tool_call | respond | reflect | abort
    reasoning: str = ""     # Brief explanation (optional)
    iteration: int = 0
    
    def to_compact(self) -> str:
        """Compact string representation"""
        return f"[{self.next_action}|{self.confidence:.2f}] {self.decision}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence": self.confidence,
            "next_action": self.next_action,
            "reasoning": self.reasoning,
            "iteration": self.iteration,
        }


@dataclass
class LoopState:
    """
    Tracks loop state for discipline enforcement.
    """
    iteration: int = 0
    reflect_count: int = 0
    tool_calls_this_prompt: int = 0
    last_tool_call_iteration: int = 0
    last_tool_had_output: bool = False
    conclusions: List[str] = field(default_factory=list)
    confidence_history: List[float] = field(default_factory=list)
    reasoning_units: List[ReasoningUnit] = field(default_factory=list)
    
    def add_conclusion(self, conclusion: str):
        """Track conclusion for repeat detection"""
        # Normalize
        normalized = conclusion.lower().strip()[:200]
        self.conclusions.append(normalized)
    
    def has_repeated_conclusion(self, threshold: int = 2) -> bool:
        """Check if same conclusion appeared multiple times"""
        if len(self.conclusions) < threshold:
            return False
        
        recent = self.conclusions[-5:]  # Check last 5
        for conclusion in set(recent):
            if recent.count(conclusion) >= threshold:
                return True
        return False
    
    def iterations_since_tool_output(self) -> int:
        """How many iterations since last tool output"""
        if not self.last_tool_had_output:
            return self.iteration
        return self.iteration - self.last_tool_call_iteration
    
    def average_confidence(self) -> float:
        """Average confidence over recent iterations"""
        if not self.confidence_history:
            return 0.0
        recent = self.confidence_history[-5:]
        return sum(recent) / len(recent)
    
    def reset(self):
        """Reset for new prompt"""
        self.iteration = 0
        self.reflect_count = 0
        self.tool_calls_this_prompt = 0
        self.last_tool_call_iteration = 0
        self.last_tool_had_output = False
        self.conclusions.clear()
        self.confidence_history.clear()
        self.reasoning_units.clear()


class LoopDiscipline:
    """
    Enforces loop discipline for small models.
    """
    
    def __init__(self, model_size: ModelSize = ModelSize.MEDIUM_8B):
        self.model_size = model_size
        self.limits = MODEL_LIMITS[model_size]
        self.state = LoopState()
    
    def set_model_size(self, model_name: str):
        """Detect model size from name"""
        model_lower = model_name.lower()
        
        if any(x in model_lower for x in ["3b", "4b", "phi-2", "tinyllama", "qwen2:1.5"]):
            self.model_size = ModelSize.SMALL_4B
        elif any(x in model_lower for x in ["7b", "8b", "mistral", "llama3.2", "qwen2:7"]):
            self.model_size = ModelSize.MEDIUM_8B
        else:
            self.model_size = ModelSize.LARGE
        
        self.limits = MODEL_LIMITS[self.model_size]
        logger.info(f"Model size: {self.model_size.value} -> limits: {self.limits}")
    
    def should_continue(self) -> tuple[bool, str]:
        """
        Check if loop should continue.
        Returns (should_continue, reason).
        """
        # Check max iterations
        if self.state.iteration >= self.limits["max_iterations"]:
            return False, f"max_iterations ({self.limits['max_iterations']})"
        
        # Check for repeated conclusions
        if self.state.has_repeated_conclusion(self.limits["abort_on_repeat"]):
            return False, "repeated_conclusion"
        
        # Check for stalled loop (no new signal)
        # More lenient: only abort if 5+ iterations without tool output AND we've had tools
        if self.state.iterations_since_tool_output() > 5 and self.state.tool_calls_this_prompt > 0:
            return False, "no_new_signal"
        
        return True, "ok"
    
    def can_reflect(self) -> bool:
        """Check if model can reflect more"""
        return self.state.reflect_count < self.limits["max_reflects"]
    
    def should_early_exit(self, confidence: float) -> bool:
        """Check if confidence is sufficient for early exit"""
        return confidence >= self.limits["confidence_threshold"]
    
    def record_iteration(
        self,
        reasoning_unit: ReasoningUnit,
        had_tool_call: bool = False,
        tool_had_output: bool = False
    ):
        """Record iteration for tracking"""
        self.state.iteration += 1
        reasoning_unit.iteration = self.state.iteration
        self.state.reasoning_units.append(reasoning_unit)
        self.state.confidence_history.append(reasoning_unit.confidence)
        
        if had_tool_call:
            self.state.tool_calls_this_prompt += 1
            self.state.last_tool_call_iteration = self.state.iteration
            self.state.last_tool_had_output = tool_had_output
        
        if reasoning_unit.next_action == "reflect":
            self.state.reflect_count += 1
    
    def record_conclusion(self, conclusion: str):
        """Record a conclusion for repeat detection"""
        self.state.add_conclusion(conclusion)
    
    def new_prompt(self):
        """Reset for new prompt"""
        self.state.reset()
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "model_size": self.model_size.value,
            "iteration": self.state.iteration,
            "reflect_count": self.state.reflect_count,
            "tool_calls": self.state.tool_calls_this_prompt,
            "avg_confidence": self.state.average_confidence(),
            "since_tool_output": self.state.iterations_since_tool_output(),
            "limits": self.limits,
        }


class ConfidenceEstimator:
    """
    Estimates confidence from LLM output.
    """
    
    # Confidence indicators (positive)
    HIGH_CONFIDENCE = [
        r"(?:i am |i'm )?(?:confident|certain|sure)",
        r"(?:this|the answer) is",
        r"(?:clearly|definitely|certainly)",
        r"based on (?:the|this) (?:output|result|information)",
    ]
    
    # Low confidence indicators
    LOW_CONFIDENCE = [
        r"(?:i think|i believe|perhaps|maybe|possibly)",
        r"(?:not sure|uncertain|unclear)",
        r"(?:might|could|may) be",
        r"(?:i need to|let me) (?:check|verify|confirm)",
        r"(?:i don't|i do not) (?:know|have)",
    ]
    
    # Tool dependency indicators (should call tool)
    NEEDS_TOOL = [
        r"(?:i need to|let me|i should|i'll) (?:check|look|read|list|run|execute)",
        r"(?:to find out|to determine|to verify)",
        r"(?:without|need) (?:checking|running|executing|looking)",
    ]
    
    def __init__(self):
        self._high_re = [re.compile(p, re.IGNORECASE) for p in self.HIGH_CONFIDENCE]
        self._low_re = [re.compile(p, re.IGNORECASE) for p in self.LOW_CONFIDENCE]
        self._tool_re = [re.compile(p, re.IGNORECASE) for p in self.NEEDS_TOOL]
    
    def estimate(self, llm_output: str, has_tool_output: bool = False) -> float:
        """
        Estimate confidence from LLM output.
        Returns 0.0-1.0
        """
        if not llm_output:
            return 0.0
        
        score = 0.5  # Base
        
        # Boost if based on tool output
        if has_tool_output:
            score += 0.2
        
        # Check indicators
        high_matches = sum(1 for p in self._high_re if p.search(llm_output))
        low_matches = sum(1 for p in self._low_re if p.search(llm_output))
        tool_matches = sum(1 for p in self._tool_re if p.search(llm_output))
        
        score += high_matches * 0.1
        score -= low_matches * 0.15
        score -= tool_matches * 0.2  # Needs tool = lower confidence
        
        # Length heuristic - very short responses often incomplete
        if len(llm_output) < 50:
            score -= 0.1
        
        return max(0.0, min(1.0, score))
    
    def needs_tool(self, llm_output: str) -> bool:
        """Check if output indicates need for tool"""
        return any(p.search(llm_output) for p in self._tool_re)


def extract_reasoning_unit(llm_output: str, confidence: float, has_tool_call: bool) -> ReasoningUnit:
    """
    Extract structured reasoning unit from LLM output.
    """
    # Determine next action
    if has_tool_call:
        next_action = "tool_call"
    elif confidence >= 0.7:
        next_action = "respond"
    elif "reflect" in llm_output.lower() or "think" in llm_output.lower():
        next_action = "reflect"
    else:
        next_action = "respond"
    
    # Extract decision (first sentence or summary)
    lines = llm_output.strip().split('\n')
    decision = lines[0][:100] if lines else "unknown"
    
    return ReasoningUnit(
        decision=decision,
        confidence=confidence,
        next_action=next_action,
        reasoning=llm_output[:200] if len(llm_output) > 200 else llm_output,
    )
