# 🧩 MCP Servers (External Tooling)

⚠️ **IMPORTANT NOTICE**

This directory does **NOT** contain any active MCP server implementations.

LLuna is designed to work with **external, user-provided MCP servers** that expose system, network, or security-related capabilities.  
For safety, legal, and security reasons, these servers are **NOT shipped** with the public repository.

---

## 🔒 Why MCP servers are not included

- MCP servers may:
  - execute system commands
  - access the filesystem
  - perform network scanning or security testing
- Shipping them publicly could:
  - violate security best practices
  - cause accidental misuse
  - create legal liability

Therefore:
> **All MCP servers must be installed, audited, and maintained by the user.**

---

## 🧠 How LLuna uses MCP servers

LLuna **does not assume** tool availability.

Each MCP server:
- registers its own capabilities
- exposes tools explicitly
- is sandboxed by LLuna’s approval & validation layers

If a requested tool is:
- ❌ missing → LLuna should report *Tool not found*
- 🔁 similar tool exists → LLuna may suggest an alternative
- 🔐 requires elevated privileges → explicit user approval is required

---

## 🛠️ Typical MCP Server Categories (examples)

These are **examples only** — not included in this repo:

- `sysops-mcp` – filesystem, process, system tools
- `netsec-mcp` – nmap, ping, traceroute
- `webapp-mcp` – dirb, nikto, wpscan
- `wireless-mcp` – aircrack-ng, airodump-ng
- `crypto-mcp` – hash tools, wordlists
- `docker-mcp` – container operations
- `browser-automator-mcp` – controlled browser automation

---

## ⚠️ Security Warning

🚨 **DO NOT run untrusted MCP servers**

- Always review code before use
- Never expose MCP servers publicly
- Avoid running MCP servers as root unless strictly necessary
- Enable logging and auditing

You are responsible for everything an MCP server can execute.

---

## 🧪 Development Status

- MCP integration: **active development**
- Conflict detection: **planned**
- Capability negotiation: **planned**
- Tool fallback logic: **planned**

This is a **garage project**, not an enterprise security framework.

---

## 📎 Summary

- LLuna core is safe and minimal
- MCP servers are **external**
- You control what runs
- You accept responsibility

If you know what you're doing — welcome.  
If not — **do not plug in MCP servers.**

