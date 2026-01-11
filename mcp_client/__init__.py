"""
LLuna v6.0 MCP Client - Hardened Execution
==========================================
HARD RULES:
- Executors MUST emit explicit completion/failure
- No tool handler returns without final state
- Hanging handlers timeout and fail loudly
- Missing response = FAILURE
- No acknowledgment before execution starts
"""

import asyncio
import json
import logging
import os
import sys
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Any, Optional, Callable
from threading import Lock

logger = logging.getLogger(__name__)


class ServerStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    ERROR = "error"


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    server_name: str = ""
    category: str = ""


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""
    category: str = "general"


@dataclass
class MCPServer:
    config: MCPServerConfig
    status: ServerStatus = ServerStatus.STOPPED
    process: Optional[subprocess.Popen] = None
    tools: List[MCPTool] = field(default_factory=list)
    error_message: str = ""
    request_id: int = 0
    start_time: float = 0
    response_times: List[int] = field(default_factory=list)
    
    def next_request_id(self) -> int:
        self.request_id += 1
        return self.request_id


class MCPClient:
    """
    Hardened MCP Client with strict execution guarantees.
    """
    
    def __init__(self, project_dir: Optional[str] = None):
        self.servers: Dict[str, MCPServer] = {}
        self._lock = asyncio.Lock()
        self.project_dir = project_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._mount_callbacks: List[Callable] = []
    
    def on_mount_progress(self, callback: Callable):
        self._mount_callbacks.append(callback)
    
    def _emit_progress(self, server_name: str, status: str, current: int, total: int):
        for cb in self._mount_callbacks:
            try:
                cb(server_name, status, current, total)
            except:
                pass
    
    def load_servers_from_config(self, config: Dict[str, Any]) -> None:
        for name, sc in config.get("mcpServers", {}).items():
            self.servers[name] = MCPServer(
                config=MCPServerConfig(
                    name=name,
                    command=sc.get("command", "python"),
                    args=sc.get("args", []),
                    env=sc.get("env", {}),
                    enabled=sc.get("enabled", True),
                    description=sc.get("description", ""),
                    category=sc.get("category", "general"),
                )
            )
    
    def auto_discover_servers(self) -> int:
        mcp_dir = os.path.join(self.project_dir, "mcp_servers")
        if not os.path.exists(mcp_dir):
            return 0
        
        discovered = 0
        for f in os.listdir(mcp_dir):
            if f.endswith(".py") and not f.startswith("_"):
                name = f[:-3]
                if name not in self.servers:
                    self.servers[name] = MCPServer(
                        config=MCPServerConfig(
                            name=name,
                            command="python",
                            args=[f"mcp_servers/{f}"],
                            enabled=True,
                            description=f"Auto: {name}",
                            category="discovered"
                        )
                    )
                    discovered += 1
        return discovered
    
    async def start_server(self, name: str) -> bool:
        async with self._lock:
            if name not in self.servers:
                return False
            
            server = self.servers[name]
            if server.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
                return True
            
            server.status = ServerStatus.STARTING
            server.error_message = ""
            server.start_time = time.time()
            
            try:
                env = os.environ.copy()
                env["PYTHONPATH"] = self.project_dir
                env["PYTHONUNBUFFERED"] = "1"
                
                for k, v in server.config.env.items():
                    env[k] = os.path.expandvars(v)
                
                cmd = [sys.executable] + server.config.args if server.config.command == "python" else [server.config.command] + server.config.args
                
                server.process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    bufsize=1,
                    cwd=self.project_dir
                )
                
                await asyncio.sleep(0.2)
                
                if server.process.poll() is not None:
                    stderr = server.process.stderr.read() if server.process.stderr else ""
                    server.status = ServerStatus.ERROR
                    server.error_message = f"Exit: {stderr[:200]}"
                    return False
                
                if await self._init_server(server):
                    server.status = ServerStatus.RUNNING
                    return True
                else:
                    await self._stop_process(server)
                    server.status = ServerStatus.ERROR
                    return False
                    
            except Exception as e:
                server.status = ServerStatus.ERROR
                server.error_message = str(e)
                return False
    
    async def _init_server(self, server: MCPServer) -> bool:
        try:
            r = await self._request(server, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "lluna", "version": "6.0.0"}
            }, timeout=10)
            
            if not r or "error" in r:
                server.error_message = r.get("error", {}).get("message", "Init failed") if r else "No response"
                return False
            
            await self._notify(server, "notifications/initialized", {})
            
            tr = await self._request(server, "tools/list", {}, timeout=10)
            if tr and "result" in tr:
                server.tools = [
                    MCPTool(
                        name=t.get("name", ""),
                        description=t.get("description", "")[:100],
                        input_schema=t.get("inputSchema", {}),
                        server_name=server.config.name,
                        category=server.config.category
                    )
                    for t in tr["result"].get("tools", [])
                ]
            
            return True
        except Exception as e:
            server.error_message = str(e)
            return False
    
    async def _request(self, server: MCPServer, method: str, params: Dict, timeout: float = 30) -> Optional[Dict]:
        """Send request with strict timeout"""
        if not server.process or server.process.poll() is not None:
            return None
        
        req_id = server.next_request_id()
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        
        start = time.time()
        try:
            server.process.stdin.write(json.dumps(request) + "\n")
            server.process.stdin.flush()
            
            loop = asyncio.get_event_loop()
            line = await asyncio.wait_for(
                loop.run_in_executor(None, server.process.stdout.readline),
                timeout=timeout
            )
            
            elapsed = int((time.time() - start) * 1000)
            server.response_times.append(elapsed)
            
            if not line:
                return {"error": {"message": "Empty response from executor"}}
            
            return json.loads(line.strip())
        except asyncio.TimeoutError:
            logger.error(f"MCP timeout: {server.config.name}/{method}")
            return {"error": {"message": f"Timeout ({timeout}s)"}}
        except json.JSONDecodeError as e:
            return {"error": {"message": f"Invalid JSON: {e}"}}
        except Exception as e:
            return {"error": {"message": str(e)}}
    
    async def _notify(self, server: MCPServer, method: str, params: Dict):
        if server.process and server.process.poll() is None:
            try:
                server.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
                server.process.stdin.flush()
            except:
                pass
    
    async def stop_server(self, name: str) -> bool:
        async with self._lock:
            if name not in self.servers:
                return False
            server = self.servers[name]
            await self._stop_process(server)
            server.status = ServerStatus.STOPPED
            server.tools = []
            return True
    
    async def _stop_process(self, server: MCPServer):
        if server.process and server.process.poll() is None:
            try:
                server.process.terminate()
                try:
                    server.process.wait(timeout=3)
                except:
                    server.process.kill()
            except:
                pass
    
    def get_tool_schema(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """Get input schema for a tool."""
        for server in self.servers.values():
            if server.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
                for tool in server.tools:
                    if tool.name == tool_name:
                        return tool.input_schema
        return None
    
    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict) -> Dict[str, Any]:
        """
        Execute tool with STRICT completion requirements.
        Returns error if executor doesn't confirm completion.
        """
        if server_name not in self.servers:
            return {"error": f"Server not found: {server_name}"}
        
        server = self.servers[server_name]
        if server.status not in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
            return {"error": f"Server not running: {server_name}"}
        
        # Execute with strict timeout
        r = await self._request(
            server,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=60
        )
        
        # Validate response
        if r is None:
            return {"error": "No response from executor", "executor_confirmed": False}
        
        # Check top-level error
        if "error" in r:
            return {"error": r["error"].get("message", "Unknown error"), "executor_confirmed": True}
        
        result = r.get("result", {})
        
        # CHECK isError FLAG - this was missing!
        if result.get("isError", False):
            content = result.get("content", [])
            error_text = ""
            for item in content:
                if item.get("type") == "text":
                    error_text = item.get("text", "")
                    break
            return {"error": error_text or "Tool execution failed", "executor_confirmed": True}
        
        content = result.get("content", [])
        
        if not content:
            return {"error": "Executor returned empty content", "executor_confirmed": True}
        
        # Extract text content
        texts = []
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    texts.append(text)
        
        output = "\n".join(texts)
        
        # Check for error patterns in output (KeyError, missing args, etc.)
        output_lower = output.lower()
        if output.startswith("Error:") or "keyerror" in output_lower or "missing" in output_lower:
            return {"error": output, "executor_confirmed": True}
        
        # Validate output is not placeholder
        if not output.strip():
            return {"result": "", "is_empty": True, "executor_confirmed": True}
        
        placeholders = ["done", "ok", "success", "completed", "executed"]
        if output.strip().lower() in placeholders:
            return {"result": output, "is_placeholder": True, "executor_confirmed": True}
        
        return {"result": output, "executor_confirmed": True}
    
    def get_all_tools(self) -> List[MCPTool]:
        tools = []
        for s in self.servers.values():
            if s.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
                tools.extend(s.tools)
        return tools
    
    def get_all_tools_deduplicated(self) -> List[MCPTool]:
        """
        Get all tools with conflict resolution.
        Priority: filesystem_read/write > bash > others
        """
        seen: Dict[str, MCPTool] = {}
        priority_order = ["filesystem_read", "filesystem_write", "os_tools", "bash_executor"]
        
        # First pass: collect all tools
        all_tools = []
        for s in self.servers.values():
            if s.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
                for t in s.tools:
                    all_tools.append((s.config.name, t))
        
        # Sort by priority
        def get_priority(item):
            server_name, _ = item
            try:
                return priority_order.index(server_name)
            except ValueError:
                return len(priority_order)
        
        all_tools.sort(key=get_priority)
        
        # Deduplicate by tool name (first wins = highest priority)
        for server_name, tool in all_tools:
            if tool.name not in seen:
                seen[tool.name] = tool
            else:
                logger.debug(f"Tool conflict: {tool.name} from {server_name} shadowed by {seen[tool.name].server_name}")
        
        return list(seen.values())
    
    def get_tool_conflicts(self) -> Dict[str, List[str]]:
        """Detect overlapping tools across servers."""
        tool_servers: Dict[str, List[str]] = {}
        
        for s in self.servers.values():
            if s.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
                for t in s.tools:
                    if t.name not in tool_servers:
                        tool_servers[t.name] = []
                    tool_servers[t.name].append(s.config.name)
        
        # Return only conflicts (tools in multiple servers)
        return {name: servers for name, servers in tool_servers.items() if len(servers) > 1}
    
    def get_server_status(self) -> Dict[str, Dict]:
        return {
            n: {
                "status": s.status.value,
                "description": s.config.description,
                "enabled": s.config.enabled,
                "category": s.config.category,
                "tools_count": len(s.tools),
                "tools": [{"name": t.name, "description": t.description} for t in s.tools],
                "error": s.error_message,
            }
            for n, s in self.servers.items()
        }
    
    def find_tool_server(self, tool_name: str) -> Optional[str]:
        for s in self.servers.values():
            if s.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY):
                for t in s.tools:
                    if t.name == tool_name:
                        return s.config.name
        return None
    
    async def start_all_servers(self, parallel: bool = True) -> Dict[str, bool]:
        enabled = [(n, s) for n, s in self.servers.items() if s.config.enabled]
        total = len(enabled)
        results = {}
        
        if parallel:
            tasks = []
            for i, (name, _) in enumerate(enabled):
                self._emit_progress(name, "starting", i + 1, total)
                tasks.append(self.start_server(name))
            
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for (name, _), outcome in zip(enabled, outcomes):
                results[name] = outcome if isinstance(outcome, bool) else False
                status = "running" if results[name] else "error"
                self._emit_progress(name, status, enabled.index((name, _)) + 1, total)
        else:
            for i, (name, _) in enumerate(enabled):
                self._emit_progress(name, "starting", i + 1, total)
                results[name] = await self.start_server(name)
                status = "running" if results[name] else "error"
                self._emit_progress(name, status, i + 1, total)
        
        return results
    
    async def stop_all_servers(self):
        for name in list(self.servers.keys()):
            await self.stop_server(name)
    
    def get_stats(self) -> Dict[str, Any]:
        running = sum(1 for s in self.servers.values() if s.status in (ServerStatus.RUNNING, ServerStatus.HEALTHY))
        return {
            "total_servers": len(self.servers),
            "running_servers": running,
            "total_tools": len(self.get_all_tools()),
        }
