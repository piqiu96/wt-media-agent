---
name: agent-platform-adapter-change
description: Add or modify Agent platform adapters for Douyin, Bilibili, or Baijiahao.
---
<!-- GENERATED FILE - DO NOT EDIT DIRECTLY -->
<!-- Source: skills/agent/agent-platform-adapter-change/SKILL.md -->


# Agent Platform Adapter Change

Use this skill when changing platform-specific execution behavior.

## Check

- Platform differences stay under `src/wt_media_agent/clients/<platform>/`; the shared
  cookie-based path is `src/wt_media_agent/clients/platform_identity.py`, and
  `src/wt_media_agent/clients/bilibili/identity.py` is what a platform with its own
  in-page or server-side rules looks like.
- High-risk actions report uncertain results when confirmation fails.
- Runtime adapters do not contain platform business rules.
- Agent does not decide formal Cloud business status.
- External e2e actions require explicit opt-in.

## Output

Summarize platform, action type, risk handling, and test isolation.
