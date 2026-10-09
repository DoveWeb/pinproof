# pin

**Pre-registered verification for AI-assisted work. State the pass bar before you measure, and make moving it afterwards detectable.**

An AI coding agent says "tests pass", "it's fixed", "the page is live". Someone has to check, and checking is where the time goes. The common failure isn't forgetting to test. It's **deciding what counts as passing *after* seeing the result**, or trusting a check that could never have failed.

`pin` is one Python file with no dependencies. It makes every claim:

1. **Declare its kill condition up front.** That's the result that would prove it false, sealed before any measurement.
2. **Run a control.** A known-good and a known-bad case must read differently, or the instrument is dead.
3. **Record before and after,** with evidence strings.
4. **Get a verdict decided by the pre-registered rule,** not by whoever wants it to pass.

Every pin is an append-only event log, hash-chained like a tiny ledger. Edit the kill condition after the fact and the chain breaks, and `verdict` refuses to render.

## Quick start

```bash
python pin.py open fix-login-timeout \
  --claim "the login no longer times out under 50 concurrent users" \
  --kill  "any timeout in a 50-user load run kills the claim" \
  --rule  "after==0" --unit "timeouts in a 50-user load run" \
  --single "one load run, one count" \
  --degenerate "a run that never reaches 50 users also shows 0 timeouts; the log must show 50 sessions"

python pin.py before  fix-login-timeout --value 7 --evidence "run #1 before the fix: 7 timeouts of 50"
python pin.py after   fix-login-timeout --value 0 --evidence "run #2 after the fix: 0 timeouts, 50 sessions in the log"
python pin.py verdict fix-login-timeout
```
```
CLAIM UNDER TEST : the login no longer times out under 50 concurrent users
RULE   : after==0
COUNTING: timeouts in a 50-user load run
EVAL   : after(0) == 0(0) -> PASS
>>> PASS <<<
```
Pins are stored in `~/.claude/pins/` (override with `PIN_DIR`).

## What it refuses (and why each refusal exists)

Every gate was added after a real mistake got through. Prose rules were followed rarely; a refusal is followed every time.

| Refusal | The mistake it stops |
|---|---|
| a rule with no `--unit` | `after>=150` ... of WHAT? A claim with four parts passing on one counted number |
| no declared split (`--single` or `--steps`) | a claim joining two outcomes with "and": half true, half false, verdict unreadable |
| `--control` declared but never run, or both readings identical | an instrument that returns the same answer on a known-false case measures nothing |
| a named `--sources` document without a read receipt (with a character count) | "naming a source is not reading it" |
| an edited earlier event | moving the pass bar after the result |
| a parent claim whose steps aren't each discharged by a measured child pin | a four-step ladder passing on four checkboxes and one real number |

Two limits, stated rather than hidden:
- **A pin can PASS and the conclusion can still be wrong,** if the kill condition asked the wrong question. No code catches a well-formed wrong question.
- **It does not judge prose.** Without a machine-checkable rule, it prints the original condition beside your stated verdict, so any gap is visible.

## Commands

```
open <slug>      claim, kill condition, rule, unit, split; optional --record --control --scope --sources --parent
source <slug>    file a read receipt for a named source (--chars)
control <slug>   --positive / --negative observations; refuses identical readings
before / after   --value N --evidence "..."
verdict <slug>   auto-decided from the rule; one per pin, final
ladder <slug>    the claim tree and the smallest next action (deepest unresolved step)
verify <slug>    walk the hash chain
list             your pins (filtered by lane, if lanes are configured)
abandon <slug>   retire a pin with a reason (e.g. superseded by a v2)
selftest         positive + negative controls on the tool itself
```

## Optional: lanes, for keeping several projects apart
If one person or team runs agents for several clients, pins can be kept apart by "lane". Copy `examples/_lanes_config.json` into your pin folder and edit it. Without that file, lanes stay dormant. The author is taken from `CLAUDE_CODE_SESSION_ID` (or `PIN_AUTHOR`), and `_lanes.json` maps sessions to lanes.

## Use with Claude Code
See [docs/CLAUDE_CODE.md](docs/CLAUDE_CODE.md).

## Testing
```bash
PIN_DIR=$(mktemp -d) python pin.py selftest   # exits 1 if any control fails
```
The selftest includes negative controls: it proves an edited kill condition IS detected, and that a rule which should fail DOES fail.

## Origin
Designed and battle-tested by Robert Dove over about 2,800 real verifications across several client projects; built with AI-assisted development. The comments in `pin.py` keep the measured reason behind each feature, with client details removed.

MIT License.
