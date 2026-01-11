"""
Tool Cache and Capability Manager for LLuna v7
===============================================
- Caches tool names and capabilities per session
- Handles tool not found with suggestions
- Manages sudo requirements
"""

import logging
import difflib
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)


def is_meta_question(user_message: str) -> bool:
    """
    Detect if user is asking a meta-question about agent capabilities.
    These questions should NOT trigger hallucination detection.
    """
    if not user_message:
        return False
    
    msg_lower = user_message.lower()
    
    # Patterns for meta-questions
    meta_patterns = [
        "what tools",
        "which tools",
        "what can you",
        "what are you able",
        "your capabilities",
        "your tools",
        "tools list",
        "list tools",
        "list your",
        "available tools",
        "available commands",
        "what commands",
        "which commands",
        "what do you have",
        "help me understand",
        "what kind of",
        "what types of",
        "can you help",
        "are you able",
        "do you have access",
        "do you have tools",
    ]
    
    return any(p in msg_lower for p in meta_patterns)


class SudoState(Enum):
    """State of sudo authentication"""
    NONE = "none"
    REQUESTED = "requested"
    APPROVED = "approved"
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass
class CachedTool:
    """Cached tool information"""
    name: str
    server: str
    description: str
    parameters: List[str]
    required_params: List[str]
    category: str = "other"
    requires_sudo: bool = False
    
    def match_score(self, query: str) -> float:
        """Calculate how well this tool matches a query"""
        query_lower = query.lower()
        name_lower = self.name.lower()
        
        # Exact match
        if query_lower == name_lower:
            return 1.0
        
        # Starts with
        if name_lower.startswith(query_lower):
            return 0.9
        
        # Contains
        if query_lower in name_lower:
            return 0.7
        
        # Fuzzy match
        ratio = difflib.SequenceMatcher(None, query_lower, name_lower).ratio()
        return ratio * 0.6


class ToolCache:
    """
    Caches available tools for the session.
    Provides tool lookup, suggestions, and capability introspection.
    """
    
    # Tool categorization
    CATEGORIES = {
        'filesystem': ['read', 'write', 'list', 'delete', 'create', 'move', 'copy', 'file', 'directory', 'folder', 'path'],
        'network': ['ping', 'traceroute', 'nmap', 'port', 'connect', 'fetch', 'dns', 'whois', 'http', 'url'],
        'system': ['bash', 'shell', 'command', 'execute', 'run', 'env', 'system', 'process'],
        'memory': ['store', 'recall', 'memory', 'remember', 'save'],
        'git': ['git', 'commit', 'branch', 'push', 'pull', 'clone'],
    }
    
    # Tools that typically require sudo
    SUDO_TOOLS = {
        'nmap_scan': ['syn', 'udp', 'os'],  # Scan types requiring sudo
        'execute_command': ['apt', 'systemctl', 'service', 'mount', 'umount', 'fdisk', 'parted'],
        'run_bash': ['apt', 'systemctl', 'service', 'mount', 'umount', 'fdisk', 'parted'],
    }
    
    def __init__(self):
        self._tools: Dict[str, CachedTool] = {}
        self._by_category: Dict[str, List[str]] = {}
        self._by_server: Dict[str, List[str]] = {}
        self._initialized: bool = False
        self._sudo_state: SudoState = SudoState.NONE
        self._sudo_password: Optional[str] = None
    
    def cache_tools(self, tools: List[Any]):
        """Cache tools from MCP client"""
        self._tools.clear()
        self._by_category.clear()
        self._by_server.clear()
        
        for tool in tools:
            params = list(tool.input_schema.get("properties", {}).keys())
            required = tool.input_schema.get("required", [])
            
            # Determine category
            category = "other"
            name_lower = tool.name.lower()
            desc_lower = (tool.description or "").lower()
            
            for cat, keywords in self.CATEGORIES.items():
                if any(kw in name_lower or kw in desc_lower for kw in keywords):
                    category = cat
                    break
            
            # Check if requires sudo
            requires_sudo = tool.name in self.SUDO_TOOLS
            
            cached = CachedTool(
                name=tool.name,
                server=getattr(tool, 'server_name', 'unknown'),
                description=tool.description or "",
                parameters=params,
                required_params=required,
                category=category,
                requires_sudo=requires_sudo,
            )
            
            self._tools[tool.name] = cached
            
            # Index by category
            if category not in self._by_category:
                self._by_category[category] = []
            self._by_category[category].append(tool.name)
            
            # Index by server
            server = cached.server
            if server not in self._by_server:
                self._by_server[server] = []
            self._by_server[server].append(tool.name)
        
        self._initialized = True
        logger.info(f"Cached {len(self._tools)} tools in {len(self._by_category)} categories")
    
    def is_initialized(self) -> bool:
        return self._initialized
    
    def get_tool(self, name: str) -> Optional[CachedTool]:
        """Get cached tool by name"""
        return self._tools.get(name)
    
    def has_tool(self, name: str) -> bool:
        """Check if tool exists"""
        return name in self._tools
    
    def find_similar(self, name: str, limit: int = 3) -> List[Tuple[str, float]]:
        """Find similar tool names"""
        if not self._tools:
            return []
        
        scores = []
        for tool_name, tool in self._tools.items():
            score = tool.match_score(name)
            if score > 0.3:  # Minimum threshold
                scores.append((tool_name, score))
        
        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:limit]
    
    def get_tool_not_found_message(self, name: str) -> str:
        """Generate helpful message when tool not found"""
        similar = self.find_similar(name)
        
        if not similar:
            return f"[TOOL NOT FOUND: '{name}']\nNo similar tools available. Check available tools with the system prompt."
        
        suggestions = ", ".join(f"'{t[0]}'" for t in similar)
        return f"[TOOL NOT FOUND: '{name}']\nDid you mean: {suggestions}?\nUse one of these or check available tools."
    
    def get_tools_by_category(self, category: str) -> List[CachedTool]:
        """Get all tools in a category"""
        names = self._by_category.get(category, [])
        return [self._tools[n] for n in names if n in self._tools]
    
    def get_tools_by_server(self, server: str) -> List[CachedTool]:
        """Get all tools from a server"""
        names = self._by_server.get(server, [])
        return [self._tools[n] for n in names if n in self._tools]
    
    def get_capability_summary(self) -> str:
        """Generate human-readable capability summary"""
        if not self._tools:
            return "No tools available."
        
        lines = [f"I have access to {len(self._tools)} tools:"]
        
        for cat in ['filesystem', 'network', 'system', 'memory', 'git', 'other']:
            tools = self._by_category.get(cat, [])
            if tools:
                lines.append(f"\n**{cat.upper()}**: {', '.join(tools)}")
        
        return "\n".join(lines)
    
    def get_all_tool_names(self) -> List[str]:
        """Get all tool names"""
        return list(self._tools.keys())
    
    @property
    def tool_count(self) -> int:
        """Get number of cached tools"""
        return len(self._tools)
    
    def get_tool_list_for_prompt(self) -> str:
        """Generate tool list for system prompt"""
        if not self._tools:
            return ""
        
        lines = []
        
        # Group by category
        for cat in ['filesystem', 'network', 'system', 'memory', 'git', 'other']:
            cat_tools = self._by_category.get(cat, [])
            if cat_tools:
                lines.append(f"[{cat.upper()}]")
                for name in cat_tools:
                    tool = self._tools.get(name)
                    if tool:
                        params = tool.required_params[:3]
                        param_str = ",".join(f"{p}*" for p in params)
                        if len(tool.parameters) > len(tool.required_params):
                            opt_params = [p for p in tool.parameters if p not in tool.required_params][:2]
                            if opt_params:
                                param_str += "," + ",".join(opt_params)
                        lines.append(f"  {name}({param_str})")
        
        return "\n".join(lines)
    
    def check_sudo_requirement(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Check if a tool invocation requires sudo"""
        if tool_name not in self.SUDO_TOOLS:
            return False
        
        # Check arguments for sudo-requiring patterns
        sudo_triggers = self.SUDO_TOOLS.get(tool_name, [])
        
        for key, value in arguments.items():
            if isinstance(value, str):
                value_lower = value.lower()
                if any(trigger in value_lower for trigger in sudo_triggers):
                    return True
        
        return False
    
    # Sudo state management
    def request_sudo(self) -> bool:
        """Request sudo approval"""
        if self._sudo_state == SudoState.ACTIVE:
            return True  # Already have sudo
        
        self._sudo_state = SudoState.REQUESTED
        return False
    
    def approve_sudo(self, password: str):
        """Approve sudo with password"""
        self._sudo_state = SudoState.APPROVED
        self._sudo_password = password
    
    def activate_sudo(self):
        """Activate sudo after successful command"""
        self._sudo_state = SudoState.ACTIVE
    
    def revoke_sudo(self):
        """Revoke sudo access"""
        self._sudo_state = SudoState.REVOKED
        self._sudo_password = None
        logger.info("Sudo access revoked")
    
    def get_sudo_state(self) -> SudoState:
        return self._sudo_state
    
    def get_sudo_password(self) -> Optional[str]:
        """Get sudo password (only if approved)"""
        if self._sudo_state in (SudoState.APPROVED, SudoState.ACTIVE):
            return self._sudo_password
        return None
    
    def has_sudo(self) -> bool:
        return self._sudo_state == SudoState.ACTIVE
    
    def clear(self):
        """Clear cache"""
        self._tools.clear()
        self._by_category.clear()
        self._by_server.clear()
        self._initialized = False
        self.revoke_sudo()
    
    def refresh(self, tools: List[Any]) -> int:
        """Refresh cache with new tools. Returns count."""
        self.cache_tools(tools)
        return len(self._tools)


# Singleton for easy access
_tool_cache: Optional[ToolCache] = None

def get_tool_cache() -> ToolCache:
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = ToolCache()
    return _tool_cache
