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
LLuna v7.1 - Argument Normalizer
================================
Fixes small model argument name mistakes BEFORE MCP invocation.

Problem: Small models (4B-8B) often use generic argument names:
  {"key": "/path/to/file", "value": "recursive"}
Instead of schema-correct:
  {"path": "/path/to/file", "recursive": true}

Solution: Schema-aware normalization with validation.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger(__name__)


class ArgumentNormalizer:
    """
    Normalizes tool arguments from small model mistakes.
    Validates against tool schema before MCP invocation.
    """
    
    # Generic names that often mean "path"
    PATH_ALIASES = {"key", "file", "filename", "filepath", "directory", "dir", 
                    "folder", "location", "target", "name", "input"}
    
    # Generic names that often mean "content"
    CONTENT_ALIASES = {"value", "text", "data", "body", "message", "string"}
    
    # Generic names for source/destination
    SOURCE_ALIASES = {"src", "from", "origin", "input", "source_path"}
    DEST_ALIASES = {"dst", "to", "dest", "target", "output", "destination_path"}
    
    # Boolean-like string values
    BOOL_TRUE_VALUES = {"true", "yes", "1", "recursive", "force", "enabled", "on"}
    BOOL_FALSE_VALUES = {"false", "no", "0", "disabled", "off"}
    
    def normalize(
        self, 
        tool_name: str, 
        arguments: Dict[str, Any], 
        schema: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Normalize arguments to match schema.
        
        Returns:
            (normalized_args, warnings)
        """
        if not schema or not arguments:
            return arguments, []
        
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        
        if not properties:
            return arguments, []
        
        normalized = {}
        warnings = []
        used_keys = set()
        
        # First pass: exact matches
        for key, value in arguments.items():
            if key in properties:
                normalized[key] = self._normalize_value(value, properties[key])
                used_keys.add(key)
        
        # Second pass: map aliases for missing required fields
        remaining = {k: v for k, v in arguments.items() if k not in used_keys}
        
        for prop_name, prop_schema in properties.items():
            if prop_name in normalized:
                continue
            
            # Try to find a matching alias
            matched_key, matched_value = self._find_alias_match(
                prop_name, prop_schema, remaining
            )
            
            if matched_key:
                normalized[prop_name] = self._normalize_value(matched_value, prop_schema)
                remaining.pop(matched_key, None)
                warnings.append(f"Mapped '{matched_key}' → '{prop_name}'")
        
        # Check for missing required fields
        missing = required - set(normalized.keys())
        if missing:
            warnings.append(f"Missing required: {missing}")
        
        return normalized, warnings
    
    def _find_alias_match(
        self, 
        prop_name: str, 
        prop_schema: Dict, 
        remaining: Dict[str, Any]
    ) -> Tuple[Optional[str], Any]:
        """Find an alias match for a property."""
        
        prop_type = prop_schema.get("type", "string")
        
        # Determine which alias set to use based on property name
        if prop_name in ("path", "file", "directory", "filename"):
            aliases = self.PATH_ALIASES
        elif prop_name in ("content", "text", "data", "body"):
            aliases = self.CONTENT_ALIASES
        elif prop_name in ("source", "src", "from"):
            aliases = self.SOURCE_ALIASES
        elif prop_name in ("destination", "dest", "to", "target"):
            aliases = self.DEST_ALIASES
        else:
            aliases = set()
        
        # Special case: if we have "key" and need "path"
        if prop_name == "path":
            aliases = self.PATH_ALIASES
        elif prop_name == "content":
            aliases = self.CONTENT_ALIASES
        elif prop_name == "source":
            aliases = self.SOURCE_ALIASES | self.PATH_ALIASES
        elif prop_name == "destination":
            aliases = self.DEST_ALIASES | self.PATH_ALIASES
        
        # Look for matching alias in remaining args
        for key, value in remaining.items():
            key_lower = key.lower()
            
            # Direct alias match
            if key_lower in aliases:
                return key, value
            
            # For boolean properties, check if value looks like it's meant to be this prop
            if prop_type == "boolean":
                value_str = str(value).lower().strip()
                # If value IS the property name, it's probably meant to be True
                if value_str == prop_name or value_str in self.BOOL_TRUE_VALUES:
                    # Check if any remaining key has this as value
                    for k, v in remaining.items():
                        if str(v).lower().strip() == prop_name:
                            return k, True
        
        return None, None
    
    def _normalize_value(self, value: Any, prop_schema: Dict) -> Any:
        """Normalize a value according to its schema type."""
        prop_type = prop_schema.get("type", "string")
        
        if prop_type == "boolean":
            return self._to_bool(value)
        elif prop_type == "integer":
            return self._to_int(value)
        elif prop_type == "number":
            return self._to_float(value)
        elif prop_type == "string":
            return str(value) if value is not None else ""
        elif prop_type == "array":
            if isinstance(value, list):
                return value
            if isinstance(value, str):
                # Try to parse comma-separated
                return [v.strip() for v in value.split(",") if v.strip()]
            return [value] if value else []
        
        return value
    
    def _to_bool(self, value: Any) -> bool:
        """Convert value to boolean."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            value_lower = value.lower().strip()
            if value_lower in self.BOOL_TRUE_VALUES:
                return True
            if value_lower in self.BOOL_FALSE_VALUES:
                return False
        return bool(value)
    
    def _to_int(self, value: Any) -> int:
        """Convert value to integer."""
        if isinstance(value, int):
            return value
        try:
            return int(float(str(value)))
        except (ValueError, TypeError):
            return 0
    
    def _to_float(self, value: Any) -> float:
        """Convert value to float."""
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value))
        except (ValueError, TypeError):
            return 0.0
    
    def validate(
        self, 
        arguments: Dict[str, Any], 
        schema: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """
        Validate arguments against schema.
        Returns (is_valid, errors).
        """
        if not schema:
            return True, []
        
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        errors = []
        
        # Check required fields
        for req in required:
            if req not in arguments:
                errors.append(f"Missing required argument: '{req}'")
            elif arguments[req] is None or arguments[req] == "":
                errors.append(f"Empty required argument: '{req}'")
        
        # Type validation
        for key, value in arguments.items():
            if key in properties:
                prop_schema = properties[key]
                prop_type = prop_schema.get("type")
                
                if prop_type == "string" and not isinstance(value, str):
                    # Acceptable, will be converted
                    pass
                elif prop_type == "boolean" and not isinstance(value, bool):
                    # Will be converted, but warn
                    if not isinstance(value, (bool, int, str)):
                        errors.append(f"'{key}' should be boolean, got {type(value).__name__}")
                elif prop_type == "integer" and not isinstance(value, int):
                    try:
                        int(str(value))
                    except:
                        errors.append(f"'{key}' should be integer, got '{value}'")
        
        return len(errors) == 0, errors


def normalize_tool_arguments(
    tool_name: str,
    arguments: Dict[str, Any],
    schema: Dict[str, Any]
) -> Tuple[Dict[str, Any], bool, str]:
    """
    Convenience function to normalize and validate arguments.
    
    Returns:
        (normalized_args, is_valid, error_message)
    """
    normalizer = ArgumentNormalizer()
    
    # Normalize
    normalized, warnings = normalizer.normalize(tool_name, arguments, schema)
    
    # Validate
    is_valid, errors = normalizer.validate(normalized, schema)
    
    if not is_valid:
        error_msg = f"Invalid arguments for {tool_name}: {'; '.join(errors)}"
        if warnings:
            error_msg += f" (attempted mappings: {', '.join(warnings)})"
        return arguments, False, error_msg
    
    if warnings:
        logger.info(f"Normalized {tool_name} args: {', '.join(warnings)}")
    
    return normalized, True, ""
