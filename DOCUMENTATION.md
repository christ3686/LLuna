# 🌙 LLuna v7.1 - Complete Documentation

## Overview

LLuna is an autonomous AI agent framework with **integrity enforcement** - making it technically impossible for the agent to pretend execution, infer external state, or stall silently. Designed for small language models (4B-8B parameters).

## Table of Contents

1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Architecture](#architecture)
4. [Core Components](#core-components)
5. [MCP Servers](#mcp-servers)
6. [Configuration](#configuration)
7. [API Reference](#api-reference)
8. [UI Features](#ui-features)
9. [Troubleshooting](#troubleshooting)

---

## Installation

### Requirements
- Python 3.10+
- Ollama or LM Studio (for LLM)
- 8GB+ RAM recommended

### Setup

```bash
# Extract the archive
unzip lluna_v7.zip
cd lluna_v7

# Install dependencies
pip install -r requirements.txt

# Start the server
python app.py
```

### Dependencies
```
flask>=3.0.0
flask-socketio>=5.3.0
pyyaml>=6.0
requests>=2.31.0
```

---

## Quick Start

1. **Start LLuna**: `python app.py`
2. **Open browser**: http://localhost:5000
3. **Select LLM provider**: Ollama or LM Studio
4. **Connect to a model**: Select and click "Connect"
5. **Start MCP servers**: Click "Start" to enable tools
6. **Chat**: Ask LLuna to perform tasks

### Example Prompts
- "List my desktop files"
- "What's the IP address of google.com?"
- "Create a file called test.txt with 'Hello World'"
- "Ping 8.8.8.8"

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        LLuna v7.1                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐   ┌─────────────────┐   ┌─────────────────┐  │
│  │   Flask UI   │   │  AutonomousAgent │   │   MCP Client    │  │
│  │  (Socket.IO) │◄──│                  │──►│                 │  │
│  └──────────────┘   │  ┌────────────┐  │   │  ┌───────────┐  │  │
│                     │  │ToolCache   │  │   │  │filesystem │  │  │
│  ┌──────────────┐   │  │ToolRegistry│  │   │  │network    │  │  │
│  │  LLM Manager │◄──│  │Hallucinator│  │   │  │bash       │  │  │
│  │ Ollama/LMS   │   │  │LoopControl │  │   │  │memory     │  │  │
│  └──────────────┘   │  └────────────┘  │   │  └───────────┘  │  │
│                     └─────────────────┘   └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow
```
User Input → Agent.process() → LLM → Extract Tool → Validate → Execute → Result → LLM → Response
                  │                       │              │
                  ▼                       ▼              ▼
            ToolCache.has()     Registry.register()  MCP.call()
```

---

## Core Components

### 1. AutonomousAgent (`agent/core.py`)

The main agent class that orchestrates all operations.

**Key Methods:**
- `process(message)` - Main entry point for user messages
- `refresh_tool_cache()` - Refresh available tools
- `approve_pending()` / `reject_pending()` - Handle approval requests

**Features:**
- Tool session cache
- Hallucination detection
- Loop discipline (max 20 iterations)
- Confidence scoring
- Path normalization

### 2. ToolCache (`agent/tool_cache.py`)

Caches available tools for fast lookup and similarity matching.

**Features:**
- `has_tool(name)` - Check if tool exists
- `find_similar(name)` - Fuzzy match suggestions
- `get_tool_list_for_prompt()` - Format tools for LLM
- `is_meta_question()` - Detect capability questions

### 3. HallucinationDetector (`agent/tool_integrity.py`)

Detects when LLM makes claims about external state without tool evidence.

**Detection Types:**
- `external_state` - Claims about files/network without tool
- `instruction_fallback` - Telling user what to do instead of doing
- `unknown_path` - Mentioning paths not in tool output

**Context-Aware:**
- Allows responses after tool output
- Skips meta-questions about capabilities

### 4. ToolExecutionRegistry (`agent/tool_integrity.py`)

State machine for tool execution lifecycle.

**States:**
```
REGISTERED → APPROVED → EXECUTING → COMPLETED_SUCCESS
                 │                  → COMPLETED_EMPTY
                 │                  → FAILED
                 └→ REJECTED
                 └→ TIMEOUT
```

### 5. LoopDiscipline (`agent/loop_discipline.py`)

Prevents infinite loops and ensures progress.

**Features:**
- Max iterations (default 20)
- Confidence tracking
- Repetition detection
- Early exit on high confidence

---

## MCP Servers

### Available Servers

| Server | Tools | Description |
|--------|-------|-------------|
| `filesystem_read` | 3 | read_file, list_directory, file_exists |
| `filesystem_write` | 5 | write_file, create_directory, delete_file, etc. |
| `bash_executor` | 1 | execute_command |
| `network_tools` | 3 | ping, check_connection, dns_lookup |
| `memory_store` | 3 | store_memory, recall_memory, list_memories |
| `git_tools` | 5 | git status, commit, branch, etc. |
| `python_executor` | 1 | run_python |
| `web_fetch` | 1 | fetch_url |

### Creating Custom Servers

```python
# mcp_servers/my_tool.py
import json
import sys

TOOLS = [
    {
        "name": "my_tool",
        "description": "Does something useful",
        "inputSchema": {
            "type": "object",
            "properties": {
                "param": {"type": "string", "description": "Parameter"}
            },
            "required": ["param"]
        }
    }
]

def handle_my_tool(arguments):
    param = arguments.get("param", "")
    # Do something
    return {"result": f"Did something with {param}"}

def main():
    # MCP protocol loop
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("method") == "tools/list":
            print(json.dumps({"result": {"tools": TOOLS}}))
        elif request.get("method") == "tools/call":
            result = handle_my_tool(request["params"]["arguments"])
            print(json.dumps({"result": {"content": [{"type": "text", "text": json.dumps(result)}]}}))
        sys.stdout.flush()

if __name__ == "__main__":
    main()
```

### Server Auto-Discovery

Servers are auto-discovered from `mcp_servers/` directory. Any `.py` file (except `__init__.py`) is registered automatically.

---

## Configuration

### config.yaml

```yaml
mcpServers:
  filesystem_read:
    command: python
    args: ["mcp_servers/filesystem_read.py"]
    enabled: true
    description: "File reading operations"
    category: "filesystem"
  
  network_tools:
    command: python
    args: ["mcp_servers/network_tools.py"]
    enabled: true
    description: "Network utilities"
    category: "network"

settings:
  max_iterations: 20
  approval_timeout: 300
  auto_approve_safe: false
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLUNA_HOST` | `0.0.0.0` | Server host |
| `LLUNA_PORT` | `5000` | Server port |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API URL |
| `LMSTUDIO_URL` | `http://localhost:1234` | LM Studio URL |

---

## API Reference

### REST Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/llm/status` | GET | LLM connection status |
| `/api/llm/providers` | GET | Available providers/models |
| `/api/servers` | GET | Server status |
| `/api/servers/<name>/start` | POST | Start a server |
| `/api/servers/<name>/stop` | POST | Stop a server |
| `/api/tools` | GET | List all tools |
| `/api/agent/stats` | GET | Agent statistics |
| `/api/chat/clear` | POST | Clear conversation |

### WebSocket Events

**Client → Server:**
- `connect_llm` - Connect to LLM provider
- `disconnect_llm` - Disconnect from LLM
- `chat` - Send message
- `stop_agent` - Stop current operation
- `approve_tool` - Approve pending tool
- `reject_tool` - Reject pending tool
- `start_servers` - Start all servers
- `stop_all_servers` - Stop all servers

**Server → Client:**
- `status` - Full status update
- `llm_status` - LLM status change
- `servers_status` - Servers status change
- `cognitive_event` - Agent thinking/execution events
- `response` - Final response
- `error` - Error message
- `agent_stopped` - Agent stopped

---

## UI Features

### Dark Mode
- Toggle with moon/sun icon in header
- Persists in localStorage
- Follows system preference if not set

### Servers Panel
- All servers visible with scroll
- Status indicators: green (running), amber (starting), red (error), gray (stopped)
- Hover to show start/stop buttons
- Tool count shown for running servers

### Tool Output
- Expandable - click to expand/collapse
- Shows full output (not truncated)
- Syntax highlighting for JSON
- Copy-friendly formatting

### Input Box
- Auto-expands with text
- Max height with scroll
- Enter to send, Shift+Enter for newline

### Stats Display
- Context usage percentage
- Confidence score
- Hallucination blocks count
- Loop aborts count

---

## Troubleshooting

### Common Issues

**"No LLM connected"**
- Ensure Ollama/LM Studio is running
- Check provider URL in environment
- Try refreshing the page

**"Tool not found"**
- Check if server is running (green indicator)
- Verify tool name spelling
- Look for similar suggestions

**Hallucination blocked repeatedly**
- Clear conversation and try again
- Rephrase request more specifically
- Check if asking about external state

**Server won't start**
- Check Python path
- Look for import errors in console
- Verify mcp_servers/*.py files are valid

### Debug Mode

```bash
# Run with debug logging
FLASK_DEBUG=1 python app.py
```

### Logs

Check console output for:
- `[INFO]` - Normal operations
- `[WARNING]` - Potential issues
- `[ERROR]` - Failures

---

## Version History

### v7.1 (Current)
- Dark theme with toggle
- Scrollable servers/tools panels
- Expandable tool output
- Auto-expanding input
- Malformed JSON recovery
- Fixed sudo_handler reference

### v7.0
- Tool session cache
- Meta-question detection
- Tool-not-found suggestions
- Improved hallucination patterns

### v6.x
- Thread-safe approval
- Narration detection
- UI duplicate prevention
- Canonical path memory

---

### License: 
GNU Affero General Public License v3.0 (AGPLv3). 
This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation.

---

## Support

For issues, open a GitHub issue or contact the development team.
