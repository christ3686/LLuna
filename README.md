# 🌙 LLuna v7.1 - Tool-Aware Integrity Mode

**It is technically impossible for this agent to pretend execution, infer external state, or stall silently.**

📚 **[Full Documentation](DOCUMENTATION.md)** - Complete guide with API reference, troubleshooting, and more.

## 🆕 v7.1 Features

### Dark Theme
- Click moon/sun icon to toggle
- Persists in localStorage
- Follows system preference by default

### Scrollable Panels
- Servers panel scrolls when many servers
- Tools panel scrolls for long lists
- All servers now visible (no hidden items)

### Expandable Tool Output
- Click output to expand/collapse
- Full output shown (not truncated)
- Works for both success and error states

### Auto-Expanding Input
- Textarea grows with content
- Max height with scroll overflow
- Better for multi-line prompts

### Malformed JSON Recovery
```python
# Handles broken LLM output like:
# {"tool":"ping","arguments":{"}}"

# Extracts tool name even from partial JSON
tool_match = re.search(r'"tool"\s*:\s*"([^"]+)"', content)
```

## 🔒 Core Principles

1. **Tools = Ground Truth** - Only tool output is real
2. **No Pretending** - Cannot claim execution without tool
3. **No Inference** - Cannot describe unseen state
4. **Determinism > Helpfulness** - Refuse rather than hallucinate

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  AutonomousAgent v7.1                        │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────────┐   │
│  │ ToolCache   │   │ Hallucin.   │   │ LoopDiscipline  │   │
│  │ - Lookup    │   │ - Context   │   │ - Max 20 iter   │   │
│  │ - Fuzzy     │   │ - Meta skip │   │ - Confidence    │   │
│  │ - Suggest   │   │ - Patterns  │   │ - Early exit    │   │
│  └─────────────┘   └─────────────┘   └─────────────────┘   │
│                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────────┐   │
│  │ Execution   │   │ Path        │   │ Argument        │   │
│  │ Registry    │   │ Normalizer  │   │ Normalizer      │   │
│  │ - State FSM │   │ - Absolute  │   │ - key→path      │   │
│  │ - Approve   │   │ - Context   │   │ - Aliases       │   │
│  └─────────────┘   └─────────────┘   └─────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

```bash
cd lluna_v7
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

## 📁 Files

```
lluna_v7/
├── agent/
│   ├── __init__.py
│   ├── core.py              # Main agent
│   ├── tool_integrity.py    # State machine + hallucination
│   ├── loop_discipline.py   # Confidence + iteration control
│   ├── argument_normalizer.py
│   ├── filesystem_context.py
│   └── tool_cache.py        # Tool lookup + meta-questions
├── app.py                   # Flask server
├── templates/index.html     # UI with dark mode
├── llm/                     # Ollama/LM Studio clients
├── mcp_client/              # MCP protocol client
├── mcp_servers/             # Tool implementations
├── config.yaml              # Server configuration
├── requirements.txt
├── README.md
└── DOCUMENTATION.md         # Full documentation
```

## 🔍 Issues Fixed in v7.1

| Issue | Solution |
|-------|----------|
| Tool output truncated | Show full output, click to expand |
| Servers hidden | Scrollable panel, all visible |
| Input doesn't expand | Auto-resize textarea |
| No dark mode | Theme toggle with persistence |
| Malformed JSON from LLM | Fallback regex extraction |
| sudo_handler error | Removed broken reference |

## 🎨 UI Theme

**Light Mode** (default for light system)
- White/gray backgrounds
- Violet accents
- Standard shadows

**Dark Mode** (toggle or system dark)
- Deep purple/navy backgrounds
- Muted violet accents
- Subtle borders

## 📝 Example Usage

```
User: list my desktop
Agent: {"tool":"list_directory","arguments":{"path":"/home/user/Desktop"}}
[Tool executes - shows files]
Agent: Your desktop has 3 files: notes.txt, project/, image.png

User: what tools do you have?
Agent: I have 15 tools including list_directory, read_file, write_file, 
       execute_command, ping, and others for filesystem and network tasks.
```

## License

MIT License
