# 🧪 Security Policy

## ⚠️ Important Notice

LLuna can:
- Execute system commands
- Modify files
- Scan networks
- Interact with real services

**This is not a sandbox.**

---

## Threat Model

LLuna assumes:
- Trusted operator
- Local or lab environment
- Explicit user approval for high-risk actions

LLuna does NOT protect against:
- Malicious users
- Compromised LLMs
- Hostile environments

---

## Safety Mechanisms

### ✅ Approval Layer
- Destructive actions require confirmation
- Privileged commands require explicit consent

### ✅ Command Validation
- Input validation before execution
- Deny-list for known dangerous patterns

### ✅ Output Sanitization
- Prevents hallucinated success
- Detects missing or invalid tool output

### ✅ Audit Logging
- All actions can be logged and reviewed

---

## Reporting Security Issues

If you discover:
- Privilege escalation bugs
- Approval bypass
- MCP isolation failures

Please **do not open a public issue**.

Contact the maintainer directly or open a private report.

---

## Disclaimer

This project is intended for:
- Research
- Education
- Controlled environments

**You are responsible for how you use it.**

