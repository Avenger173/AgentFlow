# AgentFlow 情景记忆只读回顾验收

> Status: automated acceptance passed on 2026-09-11; no Qt UI change in this addition.

## Purpose

The conversation archive already preserves sanitized, complete sessions while short-term context uses only the active session's summary and bounded recent turns. This addition makes prior sessions useful only when the user explicitly asks to review them.

## Contract

- Detect explicit retrospective questions such as `我叫你生成过什么内容的 PPT？` before Commander planning and before any model call.
- Read at most five sanitized user requests from the same `project_scope`; never scan another scope and never inject this archive into ordinary chat prompts.
- Treat recall as `history_recall`: no workflow plan, dry-run, tool execution, handoff, file creation, or long-term-memory usage update.
- Keep an explicit creation request such as `帮我生成一份球星职业生涯 PPT。` on the existing presentation route.
- Do not convert archived requests into semantic memory, profile fields, or inferred preferences. The confirmed global `user_preference` records remain the transparent, user-managed profile source.

## Automated Verification

```powershell
cd D:\project\AgentFlow\AgentFlow\backend
.\.venv\Scripts\python.exe scripts\verify_conversation_history_recall.py
.\.venv\Scripts\python.exe scripts\verify_commander_intent_routing.py
```

The verifier uses a temporary SQLite database and mock chat mode. It covers two saved presentation requests, repeated recall without echoing a prior recall question, cross-project isolation, an action-free recall response in both mock and direct LLM service paths, and the unchanged presentation creation route.
