"""
LLuna v6.3 - Filesystem Context Tracker
========================================
Tracks filesystem state for correct path resolution.

Problems solved:
1. Relative paths resolved against wrong directory
2. Last-listed directory not tracked
3. No state precedence between contexts
4. Case-sensitive path mismatches (Desktop vs desktop)
"""

import os
import logging
import re
import sys
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Detect case-insensitive filesystem (Windows, macOS)
_IS_CASE_INSENSITIVE = sys.platform in ('win32', 'darwin')


def canonical_path(path: str) -> str:
    """
    Convert path to canonical form for comparison.
    Handles case sensitivity and symlinks.
    """
    if not path:
        return path
    
    # Expand ~ and normalize
    path = os.path.expanduser(path)
    path = os.path.normpath(path)
    
    # Try to resolve to real path (follows symlinks)
    try:
        real = os.path.realpath(path)
        if os.path.exists(real):
            path = real
    except (OSError, ValueError):
        pass
    
    # On case-insensitive systems, lowercase for comparison
    if _IS_CASE_INSENSITIVE:
        path = path.lower()
    
    return path


def paths_match(path1: str, path2: str) -> bool:
    """Check if two paths refer to the same location."""
    return canonical_path(path1) == canonical_path(path2)


@dataclass
class FilesystemContext:
    """
    Tracks filesystem context for path resolution.
    State precedence: last_listed > last_modified > cwd
    """
    # Current working directory (from tool outputs)
    cwd: str = field(default_factory=lambda: os.path.expanduser("~"))
    
    # Last directory that was listed (highest precedence for relative paths)
    last_listed_directory: Optional[str] = None
    
    # Last file/directory that was created/modified
    last_modified_path: Optional[str] = None
    
    # Known valid paths (from tool outputs only) - CANONICAL
    valid_paths: Set[str] = field(default_factory=set)
    
    # Path aliases - maps canonical -> display path
    path_aliases: Dict[str, str] = field(default_factory=dict)
    
    # Path history for context
    path_history: List[str] = field(default_factory=list)
    
    def _register_path(self, display_path: str):
        """Register a path with its canonical form."""
        canon = canonical_path(display_path)
        self.valid_paths.add(canon)
        self.path_aliases[canon] = display_path
    
    def _unregister_path(self, path: str):
        """Remove a path from valid paths."""
        canon = canonical_path(path)
        self.valid_paths.discard(canon)
        self.path_aliases.pop(canon, None)
    
    def get_display_path(self, path: str) -> str:
        """Get display form of a path (preserves original case)."""
        canon = canonical_path(path)
        return self.path_aliases.get(canon, path)
    
    def update_from_tool(self, tool_name: str, arguments: Dict, output: str):
        """Update context based on tool execution."""
        
        # Extract path from arguments
        path = arguments.get("path", "")
        
        if tool_name == "list_directory" and path:
            resolved = self._resolve_path(path)
            self.last_listed_directory = resolved
            self._add_to_history(resolved)
            self._register_path(resolved)
            logger.info(f"Context: last_listed_directory = {resolved}")
            
            # Extract paths from listing output
            self._extract_paths_from_listing(resolved, output)
        
        elif tool_name in ("read_file", "file_exists") and path:
            resolved = self._resolve_path(path)
            self._register_path(resolved)
            # Also track parent directory
            parent = os.path.dirname(resolved)
            if parent:
                self._register_path(parent)
        
        elif tool_name in ("write_file", "create_file", "append_file") and path:
            resolved = self._resolve_path(path)
            self.last_modified_path = resolved
            self._register_path(resolved)
            self._add_to_history(resolved)
        
        elif tool_name == "create_directory" and path:
            resolved = self._resolve_path(path)
            self.last_modified_path = resolved
            self._register_path(resolved)
            self._add_to_history(resolved)
        
        elif tool_name in ("delete_file", "delete_directory") and path:
            resolved = self._resolve_path(path)
            # Remove from valid paths
            self._unregister_path(resolved)
            self._add_to_history(resolved)
        
        elif tool_name in ("move_file", "copy_file"):
            source = arguments.get("source", "")
            dest = arguments.get("destination", "")
            if dest:
                resolved = self._resolve_path(dest)
                self.last_modified_path = resolved
                self._register_path(resolved)
            if source and tool_name == "move_file":
                self._unregister_path(self._resolve_path(source))
        
        elif tool_name == "get_cwd" and output:
            # Update cwd from tool output
            cwd_match = re.search(r'(/[^\s\n]+)', output)
            if cwd_match:
                self.cwd = cwd_match.group(1)
    
    def _extract_paths_from_listing(self, directory: str, output: str):
        """Extract file/directory paths from list_directory output."""
        lines = output.strip().split('\n')
        for line in lines:
            # Look for file/directory names
            line = line.strip()
            if line and not line.startswith('[') and not line.startswith('Total'):
                # Extract name (might have type indicator)
                name = re.sub(r'\s*\[.*\]$', '', line).strip()
                name = re.sub(r'^[📁📄]\s*', '', name).strip()
                if name and not name.startswith('.'):
                    full_path = os.path.join(directory, name)
                    self._register_path(full_path)
    
    def _add_to_history(self, path: str):
        """Add path to history, keep last 10."""
        if path not in self.path_history:
            self.path_history.append(path)
            if len(self.path_history) > 10:
                self.path_history.pop(0)
    
    def _resolve_path(self, path: str) -> str:
        """Resolve path to absolute."""
        path = os.path.expanduser(path)
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.cwd, path))
    
    def resolve_relative_path(self, relative_path: str) -> str:
        """
        Resolve a relative path using context precedence:
        1. last_listed_directory (if set)
        2. last_modified_path's parent (if set)
        3. cwd
        """
        if os.path.isabs(relative_path):
            return os.path.normpath(os.path.expanduser(relative_path))
        
        relative_path = os.path.expanduser(relative_path)
        
        # Precedence 1: last listed directory
        if self.last_listed_directory:
            resolved = os.path.join(self.last_listed_directory, relative_path)
            logger.debug(f"Resolved '{relative_path}' via last_listed: {resolved}")
            return os.path.normpath(resolved)
        
        # Precedence 2: last modified path's parent
        if self.last_modified_path:
            parent = os.path.dirname(self.last_modified_path)
            resolved = os.path.join(parent, relative_path)
            logger.debug(f"Resolved '{relative_path}' via last_modified parent: {resolved}")
            return os.path.normpath(resolved)
        
        # Precedence 3: cwd
        resolved = os.path.join(self.cwd, relative_path)
        logger.debug(f"Resolved '{relative_path}' via cwd: {resolved}")
        return os.path.normpath(resolved)
    
    def is_path_valid(self, path: str) -> bool:
        """Check if path was seen in tool output (case-insensitive on relevant systems)."""
        resolved = self._resolve_path(path)
        return canonical_path(resolved) in self.valid_paths
    
    def get_context_for_prompt(self) -> str:
        """Get context string for system prompt."""
        lines = [f"CWD: {self.cwd}"]
        if self.last_listed_directory:
            lines.append(f"LAST_DIR: {self.last_listed_directory}")
        if self.last_modified_path:
            lines.append(f"LAST_MOD: {self.last_modified_path}")
        return " | ".join(lines)
    
    def clear(self):
        """Clear all context."""
        self.cwd = os.path.expanduser("~")
        self.last_listed_directory = None
        self.last_modified_path = None
        self.valid_paths.clear()
        self.path_aliases.clear()
        self.path_history.clear()


class PathNormalizer:
    """
    Normalizes paths in tool arguments using filesystem context.
    """
    
    # Tools that take path arguments
    PATH_TOOLS = {
        "read_file": ["path"],
        "write_file": ["path"],
        "append_file": ["path"],
        "list_directory": ["path"],
        "delete_file": ["path"],
        "delete_directory": ["path"],
        "create_directory": ["path"],
        "file_exists": ["path"],
        "move_file": ["source", "destination"],
        "copy_file": ["source", "destination"],
    }
    
    def __init__(self, context: FilesystemContext):
        self.context = context
    
    def normalize_arguments(self, tool_name: str, arguments: Dict) -> Dict:
        """Normalize path arguments using context."""
        if tool_name not in self.PATH_TOOLS:
            return arguments
        
        path_args = self.PATH_TOOLS[tool_name]
        normalized = dict(arguments)
        
        for arg_name in path_args:
            if arg_name in normalized:
                original = normalized[arg_name]
                if original and not os.path.isabs(original):
                    resolved = self.context.resolve_relative_path(original)
                    normalized[arg_name] = resolved
                    logger.info(f"Path normalized: {original} → {resolved}")
        
        return normalized
