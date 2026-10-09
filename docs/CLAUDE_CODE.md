# Using pin with Claude Code

Install it as a skill, so Claude can use it whenever it is about to claim something works:

```
~/.claude/skills/pin/
  pin.py
  SKILL.md
```

A minimal `SKILL.md`:

```markdown
---
name: pin
description: Pre-registered verification. Use before claiming a change works, before adopting a tool or refactor, and before asserting anything about a live system. Open the pin BEFORE measuring.
---
Run `python ~/.claude/skills/pin/pin.py`. The five steps, in order:
1 OPEN with --claim, --kill, --rule, --unit and --single or --steps (add --control for any probe of a live system)
2 SOURCE: read each named source end to end and file a receipt with its character count
3 CONTROL: --positive and --negative; identical readings = a dead instrument
4 BEFORE (what was asserted) and AFTER (what was measured), with evidence
5 VERDICT, decided by the pre-registered rule. FAIL is a result, not a setback.
```

Why it helps with agents: an agent's "done" is an assertion. A pin turns it into a measurement with a bar set in advance, which is exactly the review step that otherwise lands on a human.

**Lanes:** Claude Code exports `CLAUDE_CODE_SESSION_ID` to subprocesses, so each pin records the session that opened it. To keep clients apart, declare each session's lane in `PIN_DIR/_lanes.json` and configure `_lanes_config.json` (see `examples/`).
