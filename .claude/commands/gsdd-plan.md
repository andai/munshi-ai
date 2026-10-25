---
description: Compatibility alias for the Claude-native `/gsdd-plan` skill
argument-hint: [phase-number]
allowed-tools: Read
---

Read `.claude/skills/gsdd-plan/SKILL.md` and execute that skill as the canonical Claude-native `/gsdd-plan` entry.

Rules:
- Do NOT duplicate orchestration logic here.
- Do NOT fork into a separate planning subagent.
- Preserve the argument value and apply it when resolving the target phase.
