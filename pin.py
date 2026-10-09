#!/usr/bin/env python3
"""pin.py - a frozen benchmark with a PRE-REGISTERED kill condition.

WHY THIS EXISTS
    "A probe without a control is a rumour." The failure this prevents is not
    forgetting to test - it is quietly softening the pass bar AFTER seeing the
    result. Prose at rest cannot stop that. A hash chain can.

THE MECHANISM
    Every pin is an append-only event log. Each event carries prev_hash, so
    editing any earlier event (especially the kill condition) breaks the chain
    and `verdict` REFUSES to render. The original condition is re-printed at
    judgment time, next to the result, so rationalisation is visible.

    This tool does NOT pretend to judge prose. It does two things honestly:
      1. makes tampering detectable
      2. auto-decides when, and only when, a machine-checkable rule was given

USAGE
    pin.py open  <slug> --claim "..." --kill "..." [--rule "after>before"]
    pin.py before <slug> --value N --evidence "..."
    pin.py after  <slug> --value N --evidence "..."
    pin.py verdict <slug> [--result PASS|FAIL --rationale "..."]
    pin.py verify <slug>          # chain integrity only
    pin.py list
    pin.py selftest               # positive + negative controls

RULES (machine-checkable, optional)
    after>before  after>=before  after<before  after==0  after>=N  after>N
    Anything else must be judged by a human and stated with --result.
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

# Windows cp1252 stdout cannot encode emoji. On 2026-08-16 an evidence string
# containing an emoji recorded fine and then CRASHED while printing, taking the
# verdict with it. A tool that fails in its reporting path is the same class as
# the watchdog BOM crash. Report in utf-8 or degrade, never die.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PIN_DIR = os.environ.get(
    "PIN_DIR", os.path.join(os.path.expanduser("~"), ".claude", "pins")
)

# --------------------------------------------------------------------------
# chain primitives
# --------------------------------------------------------------------------

GENESIS = "0" * 64


def _canon(obj):
    """Deterministic serialisation. Key order must never vary or hashes drift."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(prev_hash, seq, ts, action, data):
    payload = _canon({"seq": seq, "ts": ts, "action": action, "data": data})
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _author():
    """The AUTHOR of this pin: the session that opened it. Sealed at open.

    Why it exists: once pins from several projects share one folder, nothing records
    WHO wrote each one, so every ladder and verdict reads every project's pins.

    Three attribution schemes were measured and rejected before this one:
      * cwd            sessions often run from the home directory, so the working
                       directory names no project.
      * slug prefix    only a minority of pins carry one, and it names the
                       SUBJECT, not the author.
      * pin ts         one timestamp maps to several overlapping sessions when
                       sessions stay open for days.

    CLAUDE_CODE_SESSION_ID is exported into every subprocess by Claude Code and is
    the transcript's own filename, so it is provenance rather than inference.
    PIN_AUTHOR overrides it for use outside Claude Code.

    Absent both, the author is "unknown" DELIBERATELY: an unattributable pin is
    excluded from "current work" filters rather than left universally readable,
    so "unknown" is a real first-class value and never defaults to a lane.
    """
    return os.environ.get("PIN_AUTHOR") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"


# --------------------------------------------------------------------------
# LANES (optional) - keeping several projects' or clients' pins apart
# --------------------------------------------------------------------------
# A "lane" is one project or client. The author field says WHO wrote a pin; it does
# not say WHICH project that session served, and a session can span two. So the lane
# is resolved separately, in a fixed order of decreasing evidence, and NEVER guessed:
#   sidecar   _lanes.json, an explicit session->lane declaration. Strongest: a
#             session naming its own lane is testimony, not inference.
#   prefix    the slug prefix ("acme-..."). It names the SUBJECT rather than the
#             author; the two disagree on a few percent of pins, and each of those
#             is a real cross-project pin.
#   unknown   excluded from "current work" filters. A wrong lane is WORSE than no
#             lane, because it lands confidently and nobody re-examines it.
#
# Everything project-specific lives in an OPTIONAL file, PIN_DIR/_lanes_config.json:
#   {
#     "lanes":            ["acme", "globex", "myco"],
#     "operator_lanes":   ["myco"],        # your own company: not a confidentiality
#                                          # counterparty, so never "cross-client"
#     "subject_patterns": {"acme": "acme|roadrunner", "globex": "globex"},
#     "scrub_patterns":   ["me@myco\\.com"] # text that appears in every project's
#                                          # records (your own mailbox) and would
#                                          # otherwise drag pins toward one lane
#   }
# With no file, lanes are dormant: every pin is "unknown" and listings are not filtered.


def _lanes_config():
    """Resolved at CALL time, never at import, so the selftest's temp PIN_DIR is honoured.

    A module constant here was once a real bug: the selftest swapped PIN_DIR, but a
    path captured at import still pointed at the REAL store, so running the tests
    overwrote live data. A test that mutates production is the same family as a probe
    that writes the string it then searches for.
    """
    try:
        with open(os.path.join(PIN_DIR, "_lanes_config.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
    except Exception:
        cfg = {}
    return {
        "lanes": tuple(cfg.get("lanes", ())),
        "operator_lanes": tuple(cfg.get("operator_lanes", ())),
        "subject_patterns": dict(cfg.get("subject_patterns", {})),
        "scrub_patterns": [re.compile(p, re.I) for p in cfg.get("scrub_patterns", ())],
    }


def is_cross_client(author_lane, subject_lane):
    """True only when two distinct CLIENT lanes are involved.

    An operator lane (your own company, or the directory a session starts from) is
    not a counterparty, so a disagreement involving it is not cross-client mixing.
    """
    if author_lane == subject_lane:
        return False
    if "unknown" in (author_lane, subject_lane):
        return False
    return not ({author_lane, subject_lane} & set(_lanes_config()["operator_lanes"]))


def _lane_sidecar():
    """session -> lane declarations. Resolved at call time (see _lanes_config)."""
    return os.path.join(PIN_DIR, "_lanes.json")


def _lane_map():
    """session_id -> lane. Declared, never inferred. Missing file is not an error."""
    try:
        with open(_lane_sidecar(), "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _author_backfill():
    """slug -> authoring session, for pins that predate the sealed author field.

    Optional file PIN_DIR/_authors.json ({"authors": {slug: session}}). Build it from
    CREATION-COMMAND provenance: the literal `pin.py open <slug>` found in exactly ONE
    transcript. A bare slug is NOT safe evidence: slugs leak between transcripts
    whenever one session lists them to another. The command form stays unique.
    """
    try:
        with open(os.path.join(PIN_DIR, "_authors.json"), "r", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("authors", {})
    except Exception:
        return {}


def _prefix_lane(slug):
    for lane in _lanes_config()["lanes"]:
        if slug.startswith(lane + "-"):
            return lane
    return None


def _lane_of(pin):
    """Which lane AUTHORED a pin: sealed author, then backfill, then slug prefix."""
    d = _open_data(pin)
    author = d.get("author_session") or _author_backfill().get(pin.get("slug", ""), "")
    declared = _lane_map().get(author or "")
    if declared:
        return declared
    return _prefix_lane(pin.get("slug", "")) or "unknown"


def _current_lane():
    return _lane_map().get(_author(), "unknown")


# SUBJECT INDEX. Author and subject are different questions that disagree on a few
# percent of pins, and each is right about a different thing, so both are kept and
# neither is reconciled into the other.
#
# It reads --record and --scope ONLY. Not claim, not kill, not the evidence prose,
# which quotes other projects freely and would inflate every count.
#
# NEGATION. --scope's job is to state a BOUNDARY, so its text is disproportionately
# EXCLUSIONS: "says NOTHING about the live site", "NOT the acme index". A plain marker
# test reads an exclusion as an attribution and inverts the meaning, on exactly the
# pins that state their boundaries most carefully.
_NEGATION = re.compile(
    r"(?:\bnot\b|\bnever\b|\bexcluding\b|\brather than\b|nothing about|says nothing"
    r"|\bno\b|\bexcept\b|\bbeyond\b)[^.;]{0,40}$", re.I)


def _subject_of(pin):
    """Which lane a pin is ABOUT, from its own sealed fields. 'unknown' when unsure.

    HIGH PRECISION BY DESIGN, and low recall is the accepted cost: an unattributed pin
    is merely excluded, while a WRONGLY attributed one lands confidently in a lane and
    nobody re-examines it. So a lane is returned only when exactly one survives.

    scrub_patterns run first. Text that appears in every project's records (your own
    mailbox, say) otherwise drags pins toward one lane. 🔑 A KNOWN-HARMLESS FALSE
    POSITIVE IS A DORMANT DEFECT, NOT A CLOSED ONE: it stays harmless exactly as long
    as nothing downstream starts trusting it, and the --subject filter would.
    """
    cfg = _lanes_config()
    d = _open_data(pin)
    text = " ".join(filter(None, [d.get("record") or "", d.get("scope") or ""]))
    for pat in cfg["scrub_patterns"]:
        text = pat.sub(" ", text)
    if not text.strip():
        return "unknown"
    hits = set()
    for lane, pat in cfg["subject_patterns"].items():
        for m in re.finditer(pat, text, re.I):
            if not _NEGATION.search(text[max(0, m.start() - 60):m.start()]):
                hits.add(lane)
                break
    return hits.pop() if len(hits) == 1 else "unknown"


def _subject_lane_of(pin):
    """Which lane a pin is ABOUT. The read-side counterpart to _lane_of's authorship.

    The order is the opposite of _lane_of's because the QUESTION is the opposite. A
    slug prefix is a human explicitly naming the subject at creation time, so here it
    is the STRONGEST signal - the same fact that makes it the weakest for authorship.

    🛑 A lesson recorded so nobody repeats it: judging the author resolver only on
    pins SELECTED for author/subject disagreement makes it look wrong every time
    (sampling on the dependent variable). Against an independent ground truth it was
    right about 98% of the time. The gap was never that the author answer is
    unreliable; it was that nothing answered the SUBJECT question.
    """
    return _prefix_lane(pin.get("slug", "")) or _subject_of(pin)


def _path(slug):
    return os.path.join(PIN_DIR, "%s.json" % _slugify(slug))


def _slugify(s):
    out = re.sub(r"[^a-z0-9._-]+", "-", str(s).strip().lower()).strip("-")
    if not out:
        raise SystemExit("ERROR: slug is empty after normalising")
    return out


def _load(slug):
    p = _path(slug)
    if not os.path.exists(p):
        raise SystemExit("ERROR: no pin '%s'. Open one first." % slug)
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(slug, pin):
    os.makedirs(PIN_DIR, exist_ok=True)
    p = _path(slug)
    tmp = p + ".tmp"
    # temp-then-rename: a truncating write that dies mid-encode leaves 0 bytes.
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pin, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def _append(pin, action, data):
    events = pin["events"]
    prev = events[-1]["row_hash"] if events else GENESIS
    seq = len(events)
    ts = _now()
    events.append(
        {
            "seq": seq,
            "ts": ts,
            "action": action,
            "data": data,
            "prev_hash": prev,
            "row_hash": _row_hash(prev, seq, ts, action, data),
        }
    )


def verify_chain(pin):
    """Return (ok, [problems]). Recomputes every link from genesis."""
    problems = []
    prev = GENESIS
    for i, ev in enumerate(pin.get("events", [])):
        if ev.get("seq") != i:
            problems.append("event %d: seq is %r, expected %d" % (i, ev.get("seq"), i))
        if ev.get("prev_hash") != prev:
            problems.append("event %d (%s): prev_hash does not match the previous row"
                            % (i, ev.get("action")))
        recomputed = _row_hash(prev, ev.get("seq"), ev.get("ts"),
                               ev.get("action"), ev.get("data"))
        if recomputed != ev.get("row_hash"):
            problems.append(
                "event %d (%s): CONTENT WAS EDITED AFTER SEALING" % (i, ev.get("action"))
            )
        prev = ev.get("row_hash")
    return (not problems), problems


def _first(pin, action):
    for ev in pin["events"]:
        if ev["action"] == action:
            return ev
    return None


# --------------------------------------------------------------------------
# rule evaluation
# --------------------------------------------------------------------------

_RULE_RE = re.compile(r"^after\s*(>=|<=|==|>|<)\s*(before|-?\d+(?:\.\d+)?)$")


def parse_rule(rule):
    if not rule:
        return None
    m = _RULE_RE.match(rule.strip().replace(" ", "").replace("after", "after ", 1))
    if not m:
        m = _RULE_RE.match(rule.strip())
    if not m:
        raise SystemExit(
            "ERROR: unsupported --rule %r.\n"
            "Supported: after>before, after>=before, after<before, after==0,\n"
            "           after>=N, after>N, after<N, after<=N\n"
            "Anything else must be judged with --result." % rule
        )
    return m.group(1), m.group(2)


def eval_rule(rule, before_v, after_v):
    """Returns (passed, explanation). PASS means the change EARNED its place."""
    op, rhs = parse_rule(rule)
    rhs_v = before_v if rhs == "before" else float(rhs)
    if before_v is None or after_v is None:
        raise SystemExit("ERROR: need both a before and an after value.")
    ops = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
    }
    ok = ops[op](float(after_v), float(rhs_v))
    return ok, "after(%g) %s %s(%g) -> %s" % (
        after_v, op, rhs, rhs_v, "PASS" if ok else "FAIL")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_open(a):
    slug = _slugify(a.slug)
    if os.path.exists(_path(slug)):
        raise SystemExit(
            "ERROR: pin '%s' already exists. A pin is never reopened - that is "
            "the whole point. Use a new slug." % slug)
    if a.rule:
        parse_rule(a.rule)  # fail now, not at verdict

    # ---- V7 SUB-PIN BINDING (2026-08-26) --------------------------------------------
    # Validated BEFORE anything reaches disk. A refused bind writes nothing, same
    # contract as the V5/V6 open gates (selftest asserts it).
    _praw = (getattr(a, "parent", None) or "").strip()
    parent_slug = _slugify(_praw) if _praw else None
    parent_step = (getattr(a, "step", None) or "").strip() or None
    if parent_slug or parent_step:
        def _refuse(headline, *lines):
            print("=" * 68)
            print("OPEN REFUSED - %s" % headline)
            print("=" * 68)
            for ln in lines:
                print(ln)
            return 2
        if not (parent_slug and parent_step):
            return _refuse(
                "--parent AND --step GO TOGETHER",
                "  A child pin discharges exactly ONE declared step of ONE parent.",
                "  --parent <parent-slug> --step '<one of its declared steps>'")
        if not os.path.exists(_path(parent_slug)):
            return _refuse("NO SUCH PARENT PIN: %s" % parent_slug,
                           "  Open the parent first, with its --steps declared.")
        parent = _load(parent_slug)
        pdecl = _open_data(parent).get("steps") or []
        if not pdecl:
            return _refuse(
                "PARENT '%s' DECLARED NO STEPS" % parent_slug,
                "  It was opened --single, so it asserts ONE thing and has no rungs.",
                "  A child can only discharge a step the parent actually declared.")
        if parent_step not in pdecl:
            return _refuse(
                "'%s' IS NOT A DECLARED STEP OF %s" % (parent_step, parent_slug),
                "  declared: %s" % ", ".join(pdecl))
        if _first(parent, "verdict"):
            return _refuse(
                "PARENT '%s' HAS ALREADY RENDERED ITS VERDICT" % parent_slug,
                "  Splitting after judgment does not un-judge it. New parent pin.")
        if parent_step in _step_outcomes(parent):
            return _refuse(
                "STEP '%s' ALREADY HAS AN OUTCOME" % parent_step,
                "  A step is discharged once. Re-testing means a new parent pin.")
        for _cs, _cstep, _cv in _children_of(parent_slug):
            if _cstep == parent_step:
                return _refuse(
                    "STEP '%s' IS ALREADY BOUND TO CHILD '%s'" % (parent_step, _cs),
                    "  Two children on one rung means the rung has two answers.")

    srcs = [s.strip() for s in (getattr(a, "sources", None) or "").split(",") if s.strip()]
    steps = [s.strip() for s in (getattr(a, "steps", None) or "").split(",") if s.strip()]

    # ---- V6 DECOMPOSE + UNIT GATE (2026-08-25) ---------------------------------------
    # MEASURED, n=557 sealed pins. The four hardening flags, by adoption:
    #   --control 94.4%   --scope 84.0%   --sources 64.3%   --steps 2.9%
    # The first three have a REFUSAL behind them. --steps was advisory. That is the whole
    # difference, and it is why a DECOMPOSE rule stated in prose was restated six times while
    # the documentation for it sat unread.
    #
    # The v5 attempt gated this on is_compound(), a 23-word list. Measured against the same
    # corpus it has 31% RECALL: 4 of 13 hand-adjudicated compound claims, and 9 of those 13
    # had ALREADY rendered a PASS verdict. A structural rewrite fired on 70.7% of the corpus
    # (noise). Both are lexical matchers standing in for a semantic judgement - the class the
    # register names five times as "a mention is not a call".
    #
    # So there is NO DETECTOR IN THIS PATH. --control and --sources never judge whether you
    # need one either; they refuse if you DECLARED one and did not run it. A declaration
    # cannot false-negative. One word satisfies it. But it must be answered every time, the
    # way --claim and --kill are answered, and nobody argues with those.
    single_reason = getattr(a, "single", None)
    if not steps and not single_reason:
        print("=" * 68)
        print("OPEN REFUSED - YOU HAVE NOT DECLARED THE SPLIT")
        print("=" * 68)
        print("  claim : %s" % a.claim)
        hinted = sorted(compound_outcomes(a.claim))
        if hinted:
            print("  hint  : outcome words present -> %s" % ", ".join(hinted))
            print("          (a 31%-recall hint, never the gate. Judge it yourself.)")
        print("")
        print("  How many INDEPENDENTLY FALSIFIABLE assertions does this claim carry?")
        print("  Never A to B. A to A1 to A2 to A3, then B.")
        print("")
        print("    --steps 'first,second'   two or more. Each needs a recorded")
        print("                               outcome before the verdict renders.")
        print("    --single '<why it is ONE>'  exactly one. Sealed in the chain")
        print("                               and reprinted at verdict.")
        print("")
        print("  If you cannot write a kill condition that ONE measurement would")
        print("  settle, the problem is not the kill condition. It is too big. Split it.")
        return 2

    # ---- THE UNIT GATE --------------------------------------------------------------
    # A real overnight-run pin claimed FOUR things (survives 12h, >=150 recordings,
    # audio retained, 95% coverage) under rule "after>=150", which measures ONE of them. It
    # would PASS with the audio discarded and coverage at 40%. The register already held
    # that shape from 2026-08-18: a claim about X with a kill condition that only asked Y,
    # where "only --scope saved the verdict".
    #
    # A rule is a bare number. Naming what it COUNTS costs one argument and makes the
    # claim-vs-rule mismatch legible at verdict - the one moment anyone rereads both. This
    # is the standing prescription: make the success message carry its own
    # evidence, and refuse to print without a measured value beside it.
    unit = getattr(a, "unit", None)
    if a.rule and not unit:
        print("=" * 68)
        print("OPEN REFUSED - THE RULE IS A BARE NUMBER WITH NO UNIT")
        print("=" * 68)
        print("  rule  : %s" % a.rule)
        print("  claim : %s" % a.claim)
        print("")
        print("  %s ... of WHAT?" % a.rule)
        print("  Name what the number counts, so that at verdict the rule prints")
        print("  beside the claim and any mismatch between them is visible:")
        print("")
        print("    --unit 'recordings completed end to end'")
        print("")
        print("  If the unit does not cover the whole claim, the rule is measuring")
        print("  one part of it and the verdict will certify all of it. Split it.")
        return 2

    # ---- V8 THE DEGENERATE GATE (2026-09-12) ----------------------------------------
    # Chosen as Option A of four, 2026-09-13. The other three (ban the bare
    # floor on count rules / an all-empty-ETL canary / print rows_written beside
    # rows_filtered) were each a point-patch for ONE bug shape. This is the structural one.
    #
    # THE HOLE IT CLOSES. pin.py already REFUSES six things: a missing control, a
    # rubber-stamp control that does not discriminate, a bare number with no --unit, an
    # undeclared split, an unresolved child rung, and a blocked rung. It had NO gate on
    # A THRESHOLD THAT GARBAGE SATISFIES. Two pins fell through it in 25 minutes on
    # 2026-09-08, the second immediately after the first was named out loud - which is the
    # evidence that awareness is not the mechanism:
    #   "sheets_to_pg fetches >0 rows"  -> SATISFIED by 261 fetched and 0 WRITTEN
    #   "form_responses >56 AND today"  -> SATISFIED by 158 rows of which 100 were BLANK
    # Neither was a bad probe. Both controls discriminated correctly. What failed is the
    # KILL CONDITION SHAPE: each was written around the failure just seen rather than
    # around what working means. A mirror of the last symptom is not a definition of health.
    #
    # WHY A DECLARATION AND NOT A DETECTOR. Same argument that settled --unit and --steps:
    # a detector can false-negative and a required declaration cannot, because no detector
    # sits in the path. n=557 sealed pins in the author's use: refusal-backed flags reach 64-94% adoption,
    # advisory-backed flags reach 2.9%.
    #
    # TRIGGER: the presence of --rule, exactly like --unit. Deliberately NOT a guess about
    # which rule shapes are degenerately satisfiable - nearly all of them are, and a gate
    # that tries to tell them apart is the weak detector this option was chosen over.
    degenerate = getattr(a, "degenerate", None)
    if a.rule and not degenerate:
        print("=" * 68)
        print("OPEN REFUSED - YOU HAVE NOT SAID WHAT A DEGENERATE PASS LOOKS LIKE")
        print("=" * 68)
        print("  rule  : %s" % a.rule)
        print("  counts: %s" % (unit or "?"))
        print("  claim : %s" % a.claim)
        print("")
        print("  Name the result that would SATISFY that rule while the thing")
        print("  under test is still broken. Every threshold has one:")
        print("")
        print("    --degenerate 'fetches 261 rows and writes 0 of them'")
        print("    --degenerate 'returns 158 rows of which 100 are blank'")
        print("    --degenerate 'the probe finds nothing because it never ran'")
        print("")
        print("  If you cannot name one, the rule was written around the failure")
        print("  you just saw rather than around what WORKING means, and a mirror")
        print("  of a symptom is not a definition of health.")
        print("")
        print("  It is REPRINTED at verdict beside the rule, so whoever reads the")
        print("  PASS can check that this is not what happened.")
        return 2

    pin = {"slug": slug, "created": _now(), "events": []}
    # --record and --control are the CLAIM-MODE additions (2026-08-13). Both default to None,
    # so every pin written before this date behaves exactly as it did.
    # --scope and --sources are the V3 HARDENING additions (2026-08-14). Both default to None,
    # so every pin written before this date behaves exactly as it did.
    #   --scope   the BOUNDARY of the claim. Three times on 2026-08-13/14 a claim about one
    #             surface (the public REST API) was stated as a claim about the platform.
    #             Writing the boundary down at open makes the overreach visible at verdict.
    #   --sources the named sources that must be READ before a verdict renders (named-source-skip)
    #             named-source-skip fired FOUR times in one session, twice on documents that
    #             answered the question directly. Naming a source is not reading it.
    _append(pin, "open", {"claim": a.claim, "kill": a.kill, "rule": a.rule or None,
                          "record": getattr(a, "record", None) or None,
                          "control": getattr(a, "control", None) or None,
                          "scope": getattr(a, "scope", None) or None,
                          "sources": srcs or None,
                          "steps": steps or None,
                          "single": single_reason or None,
                          "unit": unit or None,
                          "degenerate": degenerate or None,
                          "parent": parent_slug,
                          "parent_step": parent_step,
                          # AUTHOR INDEX (2026-09-01). Sealed here, never typed, so it
                          # cannot be forgotten the way --steps was (2.9% adoption as an
                          # advisory). SUBJECT stays where it already lives, in --record
                          # and --scope, which carry 92.9% / 85.8% because both are
                          # verdict-gated. Two indexes on purpose: they disagree on ~4%
                          # of pins and each is right about a different question, so a
                          # design that reconciles them destroys one.
                          "author_session": _author()})
    _save(slug, pin)
    print("PIN OPENED: %s" % slug)
    print("  claim : %s" % a.claim)
    print("  KILL  : %s" % a.kill)
    print("  rule  : %s" % (a.rule or "(prose - verdict must be stated by hand)"))
    if getattr(a, "record", None):
        print("  RECORD: %s" % a.record)
    if getattr(a, "control", None):
        print("  CONTROL (must FAIL, or the instrument proves nothing):")
        print("          %s" % a.control)
    if getattr(a, "scope", None):
        print("  SCOPE : %s" % a.scope)
        print("          A claim may not travel wider than this. State the surface, not the world.")
    if srcs:
        print("  SOURCES THAT MUST BE READ (%d) - the verdict refuses until each has a receipt:"
              % len(srcs))
        for s in srcs:
            print("          - %s" % s)
    if steps:
        print("  SUB-STEPS (%d), each needs an outcome before the verdict renders:" % len(steps))
        for s in steps:
            print("          - %s" % s)
    if single_reason:
        print("  !! COMPOUND CLAIM, OVERRIDDEN ON THE RECORD:")
        print("     %s" % single_reason)
        print("     outcomes detected: %s" % ", ".join(sorted(compound_outcomes(a.claim))))
        print("     Sealed in the chain and reprinted at verdict.")
    if parent_slug:
        print("  SUB-PIN OF   : %s" % parent_slug)
        print("  DISCHARGES   : %s" % parent_step)
        print("          That parent step is now MEASURED, not asserted. Its outcome is")
        print("          written by THIS pin's verdict, and the parent refuses to render")
        print("          until this one has.")
    print("\nSealed. Editing the kill condition from here on breaks the chain.")
    return 0


_OUTCOME_WORDS = (r"built|created|creating|exists|publish|published|publishing|deployed|"
                  r"installed|shipped|live|working|fixed|closed|verified|readable|purchasable|"
                  r"attached|enabled|granted|deleted|renamed|wired")
# 2026-08-25 (pin pin-compound-detector-fires-aug25, FAIL 0/3): these two lines held a
# literal 0x08 BACKSPACE byte where the two characters 0x5C 0x62 - a regex word boundary -
# belong. Both patterns therefore demanded a backspace character either side of every word
# and matched NOTHING, EVER, from the day they were written. The r-prefix gave false
# confidence: it stops PYTHON interpreting the escape, but the escape had already been
# interpreted before the bytes reached disk. Built from chr(92) so no editor, shell or
# JSON layer can eat it again, and asserted at import so a dead detector cannot stay quiet.
_WB = chr(92) + "b"
_OUTCOME_RE = re.compile(_WB + "(?:" + _OUTCOME_WORDS + ")" + _WB, re.I)
_JOIN_RE = re.compile(_WB + "(?:and|plus|as well as|then|also)" + _WB, re.I)

# IMPORT-TIME GUARD. A detector returning False on everything is indistinguishable from
# one that simply never saw a compound claim - which is exactly how this survived from
# birth. These two asserts make the dead state LOUD at import, not silent at open.
assert _OUTCOME_RE.search("published"), "pin.py: _OUTCOME_RE is DEAD (word boundary corrupted)"
assert _JOIN_RE.search("a and b"), "pin.py: _JOIN_RE is DEAD (word boundary corrupted)"


def compound_outcomes(claim):
    """The DISTINCT outcome words found. Printed at refusal so the split is obvious."""
    if not claim:
        return set()
    return {m.group(0).lower() for m in _OUTCOME_RE.finditer(claim)}


def is_compound(claim):
    """True when a claim asserts two or more independent OUTCOMES.

    Counts DISTINCT outcome words and requires a conjunction. An earlier version used a
    proximity regex with a 120 character window and went SILENT on the real compound claim
    from 2026-08-14, because the gap between "EXISTS" and "and" in that claim was about 150
    characters. The positive control caught it. Proximity was the wrong measure; distinct
    outcomes is the thing that actually matters.

    Deliberately still narrow: a conjunction is required, so a single-outcome sentence that
    merely repeats a word does not trip it. A detector that fires on everything is noise.
    """
    if not claim:
        return False
    found = {m.group(0).lower() for m in _OUTCOME_RE.finditer(claim)}
    return len(found) >= 2 and bool(_JOIN_RE.search(claim))


# --------------------------------------------------------------------------
# V7 - SUB-PINS - THE LADDER IS WALKED, NOT DECLARED (2026-08-26)
# --------------------------------------------------------------------------
# V6 made the SPLIT a required declaration, and the n=557 natural experiment says a
# refusal gets 64-94% adoption where an advisory got 2.9%. That fixed WHETHER you split.
# It did not fix whether the split was ever MEASURED.
#
# `pin.py step --state done --evidence '...'` is a SELF-ASSERTED CHECKBOX. No kill
# condition, no rule, no control, no before/after. It is the identical shape to the
# rubber-stamp `--discriminated` flag that V3 killed, and to the `ok:true` that
# is the classic failure mode. So a four-step ladder could render PASS on
# four assertions and exactly ONE measured number - which is the
# overnight-run disease (four claims, one rule) one level down, inside the very mechanism
# built to prevent it.
#
# A step may now be discharged by a CHILD PIN. A child carries every gate a pin carries
# (kill condition, rule, unit, control, sources, scope, its own required split), and a
# child may itself declare --steps and have children. That is what makes "keep splitting
# until every piece is solvable" RECURSIVE instead of advisory, and it is what turns
# "combine them at the end" into a refusal: the parent cannot render while a child is
# unresolved.
#
# `pin.py step` still works. Some sub-steps genuinely are one grep. But the verdict now
# prints which steps were MEASURED by a child pin and which were merely ASSERTED, in the
# same three-line block as the unit, for the same reason: the gap is legible there or
# nowhere.


def _all_pins():
    if not os.path.isdir(PIN_DIR):
        return []
    # Underscore-prefixed files are STORE METADATA, not pins (_lanes.json). Without
    # this the sidecar would be loaded as a pin, _open_data would return {}, and it
    # would silently appear in every listing as a chainless row.
    return sorted(f[:-5] for f in os.listdir(PIN_DIR)
                  if f.endswith(".json") and not f.startswith("_"))


def _open_data(pin):
    ev = _first(pin, "open")
    return (ev or {}).get("data", {}) or {}


def _verdict_of(pin):
    v = _first(pin, "verdict")
    return (v or {}).get("data", {}).get("result")


def _children_of(slug):
    """[(child_slug, step_name, verdict_or_None)] for every pin bound to this parent."""
    out = []
    for s in _all_pins():
        if s == slug:
            continue
        try:
            child = _load(s)
        except SystemExit:
            continue
        d = _open_data(child)
        if d.get("parent") == slug:
            out.append((s, d.get("parent_step"), _verdict_of(child)))
    return sorted(out)


def _step_outcomes(pin):
    return {e["data"]["name"]: e["data"] for e in pin["events"] if e["action"] == "step"}


def _next_action(slug, _seen=None):
    """The SMALLEST next action: the deepest unresolved leaf, depth-first.

    V4 said "hand over the SMALLEST next action rather than the whole ladder" and left it
    to prose. This computes it. Returns a one-line string, or None when the pin is done.
    """
    _seen = _seen or set()
    if slug in _seen:
        return None
    _seen.add(slug)
    try:
        pin = _load(slug)
    except SystemExit:
        return None
    if _verdict_of(pin):
        return None
    kids = _children_of(slug)
    for cs, _cstep, cv in kids:
        if not cv:
            deeper = _next_action(cs, _seen)
            if deeper:
                return deeper
    d = _open_data(pin)
    outcomes = _step_outcomes(pin)
    bound = {st for _cs, st, _cv in kids}
    for st in (d.get("steps") or []):
        if st in outcomes:
            continue
        if st in bound:
            continue
        return ("pin.py open <child-slug> --parent %s --step '%s' ...   "
                "(or: pin.py step %s --name '%s' --state done|blocked --evidence '...')"
                % (slug, st, slug, st))
    if not _first(pin, "before"):
        return "pin.py before %s --value N --evidence '...'" % slug
    if not _first(pin, "after"):
        return "pin.py after %s --value N --evidence '...'" % slug
    return "pin.py verdict %s" % slug


def cmd_ladder(a):
    """Print the whole ladder under a pin, plus the SMALLEST next action."""
    root = _slugify(a.slug)
    _load(root)  # existence check

    def walk(slug, indent, seen):
        if slug in seen:
            print("%s(cycle: %s)" % (indent, slug))
            return
        seen.add(slug)
        pin = _load(slug)
        d = _open_data(pin)
        v = _verdict_of(pin) or "open"
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]"}.get(v, "[open]")
        print("%s%s %s" % (indent, mark, slug))
        print("%s        claim: %s" % (indent, d.get("claim", "")))
        outcomes = _step_outcomes(pin)
        kids = {st: cs for cs, st, _cv in _children_of(slug)}
        for st in (d.get("steps") or []):
            if st in kids:
                print("%s   |- %s   -> MEASURED by child pin" % (indent, st))
                walk(kids[st], indent + "   |     ", seen)
            elif st in outcomes:
                print("%s   |- %s   -> %s (ASSERTED, no kill condition)"
                      % (indent, st, outcomes[st]["state"]))
            else:
                print("%s   |- %s   -> unresolved" % (indent, st))

    print("=" * 68)
    print("PIN LADDER: %s" % root)
    print("=" * 68)
    walk(root, "", set())
    print("-" * 68)
    nxt = _next_action(root)
    if nxt:
        print("SMALLEST NEXT ACTION:")
        print("  %s" % nxt)
        print("")
        print("Do THIS one. Not the ladder. Splitting is sequencing, never an excuse")
        print("to quietly deliver less.")
    else:
        print("Nothing unresolved under this pin.")
    return 0


def cmd_step(a):
    """Record the outcome of one declared sub-step."""
    slug = _slugify(a.slug)
    pin = _load(slug)
    declared = (_first(pin, "open")["data"].get("steps") or [])
    if not declared:
        raise SystemExit("ERROR: pin '%s' was opened without --steps." % slug)
    if a.name not in declared:
        raise SystemExit("ERROR: '%s' is not a declared step. Declared: %s"
                         % (a.name, ", ".join(declared)))
    for ev in pin["events"]:
        if ev["action"] == "step" and ev["data"].get("name") == a.name:
            raise SystemExit("ERROR: step '%s' already recorded." % a.name)
    _append(pin, "step", {"name": a.name, "state": a.state, "evidence": a.evidence})
    _save(slug, pin)
    done = {e["data"]["name"] for e in pin["events"] if e["action"] == "step"}
    print("STEP RECORDED: %s -> %s" % (a.name, a.state))
    print("  evidence : %s" % a.evidence)
    print("  progress : %d of %d recorded" % (len(done), len(declared)))
    return 0


def cmd_source(a):
    """Record a READ RECEIPT for a source named at open.

    Naming a source is not reading it. On 2026-08-13/14, the named-source-skip failure fired four
    times, twice on documents sitting in the repo that answered the question outright. The
    receipt carries a CHARACTER COUNT because a count cannot be satisfied by claiming
    completeness, which is precisely the property an assertion lacks.
    """
    slug = _slugify(a.slug)
    pin = _load(slug)
    declared = (_first(pin, "open")["data"].get("sources") or [])
    if not declared:
        raise SystemExit(
            "ERROR: pin '%s' was opened without --sources, so there is nothing to receipt." % slug)
    if a.name not in declared:
        raise SystemExit(
            "ERROR: '%s' is not one of the sources named at open. Declared: %s"
            % (a.name, ", ".join(declared)))
    if a.chars <= 0:
        raise SystemExit(
            "ERROR: a read receipt of %d characters is not a read." % a.chars)
    for ev in pin["events"]:
        if ev["action"] == "source" and ev["data"].get("name") == a.name:
            raise SystemExit("ERROR: '%s' already has a read receipt." % a.name)
    _append(pin, "source", {"name": a.name, "chars": int(a.chars), "evidence": a.evidence})
    _save(slug, pin)
    read = {e["data"]["name"] for e in pin["events"] if e["action"] == "source"}
    print("READ RECEIPT: %s" % a.name)
    print("  chars    : %d" % a.chars)
    print("  evidence : %s" % a.evidence)
    print("  progress : %d of %d sources read" % (len(read), len(declared)))
    return 0


def _record(a, action):
    slug = _slugify(a.slug)
    pin = _load(slug)
    ok, problems = verify_chain(pin)
    if not ok:
        print("REFUSING: chain is broken.")
        for p in problems:
            print("  - %s" % p)
        return 2
    if _first(pin, action):
        raise SystemExit("ERROR: '%s' already recorded for '%s'." % (action, slug))
    if action == "after" and not _first(pin, "before"):
        raise SystemExit("ERROR: record 'before' first, or there is no baseline.")
    _append(pin, action, {"value": a.value, "evidence": a.evidence})
    _save(slug, pin)
    print("%s recorded for %s: value=%g" % (action.upper(), slug, a.value))
    print("  evidence: %s" % a.evidence)
    return 0


def cmd_before(a):
    return _record(a, "before")


def cmd_after(a):
    return _record(a, "after")


def cmd_control(a):
    """Record the NEGATIVE CONTROL. Claim mode only.

    The control is a deliberately-wrong version of the same measurement. It MUST come back
    negative. If the instrument reports success on a case you know is false, the instrument is
    not measuring what you think, and the real result is worthless no matter how good it looks.

    This is the executable form of `a_probe_without_a_control_is_a_rumour`, and of the six
    artifact-read-as-live-state failures on 2026-08-12 where an artifact was read as live state and every answer was
    internally consistent.
    """
    slug = _slugify(a.slug)
    pin = _load(slug)
    opened = _first(pin, "open")
    if not opened["data"].get("control"):
        raise SystemExit(
            "ERROR: pin '%s' was opened without --control, so there is no registered control "
            "to satisfy. Open a new pin in claim mode." % slug)
    if _first(pin, "control"):
        raise SystemExit("ERROR: control already recorded for '%s'." % slug)

    # V3 HARDENING (2026-08-14): --discriminated used to be an ASSERTION. On 2026-08-14 a
    # control in this very session was the expression `done != False`, which is just `done`,
    # a rubber stamp that would have passed anything, and the tool accepted it. So when the
    # two observations are supplied, the tool now checks they actually DIFFER. An instrument
    # that returns the same reading on a known-good and a known-bad case is not an instrument.
    pos = getattr(a, "positive", None)
    neg = getattr(a, "negative", None)
    if pos and neg:
        norm = lambda s: " ".join(s.lower().split())  # noqa: E731
        if norm(pos) == norm(neg):
            raise SystemExit(
                "ERROR: the control did not discriminate. The known-good and known-bad "
                "observations are identical:\n  %s\nThat is a rubber stamp, not a control. "
                "An instrument returning the same reading on both cases measures nothing."
                % pos)
    _append(pin, "control", {"discriminated": bool(a.discriminated),
                             "evidence": a.evidence,
                             "positive": pos or None, "negative": neg or None})
    _save(slug, pin)
    print("CONTROL RECORDED: %s" % slug)
    print("  registered : %s" % opened["data"]["control"])
    print("  outcome    : %s" % ("DISCRIMINATED (control correctly came back negative)"
                                 if a.discriminated else
                                 "DID NOT DISCRIMINATE - the instrument is not trustworthy"))
    print("  evidence   : %s" % a.evidence)
    return 0


def cmd_verify(a):
    pin = _load(a.slug)
    ok, problems = verify_chain(pin)
    print("CHAIN %s  (%d events)" % ("OK" if ok else "BROKEN", len(pin["events"])))
    for p in problems:
        print("  - %s" % p)
    return 0 if ok else 2


def cmd_abandon(a):
    """Retire a pin that will never be measured, WITHOUT faking a verdict.

    THIS EXISTS BECAUSE (2026-08-17) 14 pins sat open. Eleven were opened and never
    measured - the moment they were measuring has passed - and two had before/after but
    controls recorded as a bare --discriminated assertion, which the V3 hardening
    correctly stopped accepting. There was no honest exit: the only way to close a pin
    was a verdict, and a verdict needs a real measurement. So the only options were to
    fabricate one or leave them open forever, and an ever-growing pile of "unfinished
    measurements" trains everyone to ignore the number entirely.

    Abandon is NOT a pass and NOT a fail. It records in the same hash chain that the
    measurement was never taken, and why. The claim stays visibly unanswered.
    Re-testing still means a NEW pin with a new slug; abandoning reopens nothing.
    """
    slug = _slugify(a.slug)
    pin = _load(slug)
    ok, problems = verify_chain(pin)
    if not ok:
        print("ABANDON REFUSED - chain is broken: " + "; ".join(problems))
        return 2
    acts = [e.get("action") for e in pin.get("events", [])]
    if "verdict" in acts:
        print("This pin already has a VERDICT. Abandon is only for unmeasured pins.")
        return 2
    if "abandoned" in acts:
        print("Already abandoned.")
        return 0
    _append(pin, "abandoned", {"reason": a.reason})
    # _append MUTATES IN MEMORY ONLY. The first version of this function omitted _save and
    # printed "PIN ABANDONED" for 13 pins while writing nothing to disk - a brand new tool
    # reporting success while doing nothing, which is this system's most expensive class.
    # Caught by reading a pin's events back rather than trusting the printout.
    _save(slug, pin)
    bar = "=" * 68
    print(bar)
    print("PIN ABANDONED: " + slug)
    print(bar)
    print("CLAIM  : " + str(pin.get("claim", "")))
    print("KILL   : " + str(pin.get("kill", "")))
    print("REASON : " + str(a.reason))
    print("")
    print("This is NOT a pass and NOT a fail. The claim above remains UNANSWERED.")
    print("Re-testing it means a new pin with a new slug.")
    return 0


def cmd_verdict(a):
    slug = _slugify(a.slug)
    pin = _load(slug)
    ok, problems = verify_chain(pin)
    if not ok:
        print("=" * 68)
        print("VERDICT REFUSED - THE PIN WAS TAMPERED WITH")
        print("=" * 68)
        for p in problems:
            print("  - %s" % p)
        print("\nA kill condition edited after the measurement is not a kill")
        print("condition. Re-run the experiment with a fresh pin.")
        return 2

    opened = _first(pin, "open")
    b = _first(pin, "before")
    aft = _first(pin, "after")
    if not b or not aft:
        raise SystemExit("ERROR: need both before and after before a verdict.")
    if _first(pin, "verdict"):
        raise SystemExit("ERROR: verdict already rendered for '%s'. It is final." % slug)

    # ---- CLAIM MODE GATE (2026-08-13) -------------------------------------------------
    # If a control was registered at open, it must have been RUN and it must have
    # DISCRIMINATED. An instrument that returns the same answer on a case you know is false
    # is not measuring the thing. Refusing here is the whole point: on 2026-08-12 six
    # confident wrong answers were all internally consistent, which is exactly what an
    # uncontrolled probe produces.
    ctl_declared = opened["data"].get("control")
    if ctl_declared:
        ctl = _first(pin, "control")
        if not ctl:
            print("=" * 68)
            print("VERDICT REFUSED - THE REGISTERED CONTROL WAS NEVER RUN")
            print("=" * 68)
            print("  registered control : %s" % ctl_declared)
            print("\nA probe without a control is a rumour. Run:")
            print("  pin.py control %s --discriminated|--no-discriminated --evidence '...'"
                  % slug)
            return 2
        if not ctl["data"].get("discriminated"):
            print("=" * 68)
            print("VERDICT REFUSED - THE CONTROL DID NOT DISCRIMINATE")
            print("=" * 68)
            print("  registered control : %s" % ctl_declared)
            print("  control evidence   : %s" % ctl["data"].get("evidence"))
            print("\nThe instrument returned the same answer on a case known to be false,")
            print("so the real measurement proves nothing. Fix the instrument, new pin.")
            return 2

    # ---- V3 HARDENING GATE (2026-08-14): READ RECEIPTS -------------------------------
    # Naming a source is not reading it. the named-source-skip failure fired FOUR times on
    # 2026-08-13/14, twice on documents already in the repo that answered the question
    # outright, and once inside the very message criticising the omission. Registering a
    # gap is not closing it, so the verdict now refuses while a declared source is unread.
    declared_sources = opened["data"].get("sources") or []
    if declared_sources:
        receipted = {e["data"]["name"]: e["data"]["chars"]
                     for e in pin["events"] if e["action"] == "source"}
        missing = [s for s in declared_sources if s not in receipted]
        if missing:
            print("=" * 68)
            print("VERDICT REFUSED - A NAMED SOURCE WAS NEVER READ")
            print("=" * 68)
            for s in missing:
                print("  UNREAD : %s" % s)
            for s, n in receipted.items():
                print("  read   : %s (%d chars)" % (s, n))
            print("\nNaming a source is not reading it. Record each one:")
            print("  pin.py source %s --name '<source>' --chars N --evidence '...'" % slug)
            return 2

    # ---- V7 LADDER GATES (2026-08-26) -----------------------------------------------
    # "Complete them one by one, combine them at the end" is a REFUSAL here, not advice.
    _kids = _children_of(slug)
    _pending = [(cs, st) for cs, st, cv in _kids if not cv]
    if _pending:
        print("=" * 68)
        print("VERDICT REFUSED - A CHILD PIN HAS NOT RENDERED")
        print("=" * 68)
        for cs, st in _pending:
            print("  UNRESOLVED : %-28s discharges step '%s'" % (cs, st))
        print("")
        print("You combine at the END. A parent that renders over an open child")
        print("certifies a rung nobody measured.")
        print("  next: %s" % (_next_action(slug) or "resolve the child pins above"))
        return 2

    declared_steps = opened["data"].get("steps") or []
    if declared_steps:
        _out = _step_outcomes(pin)
        missing = [s for s in declared_steps if s not in _out]
        if missing:
            print("=" * 68)
            print("VERDICT REFUSED - A DECLARED SUB-STEP HAS NO RECORDED OUTCOME")
            print("=" * 68)
            for s in missing:
                print("  UNRECORDED : %s" % s)
            print("")
            print("A verdict with unmeasured steps hides WHICH part failed. Either")
            print("MEASURE it with a child pin (preferred - it carries a kill condition):")
            print("  pin.py open <child> --parent %s --step '<step>' --claim ... --kill ..." % slug)
            print("or assert it, on the record, and accept that it prints as ASSERTED:")
            print("  pin.py step %s --name '<step>' --state done|blocked --evidence '...'" % slug)
            return 2

        # A BLOCKED rung used to count as "recorded" and let the verdict through, so a
        # ladder could render PASS with a rung nobody could climb. V4 said "re-split what
        # fails"; this is that sentence with a refusal behind it.
        _blocked = [s for s in declared_steps if _out.get(s, {}).get("state") == "blocked"]
        if _blocked:
            print("=" * 68)
            print("VERDICT REFUSED - A SUB-STEP IS BLOCKED")
            print("=" * 68)
            for s in _blocked:
                print("  BLOCKED : %-28s %s" % (s, _out[s].get("evidence", "")))
            print("")
            print("A blocked rung is not a result, it is a rung that is still two")
            print("problems. Re-split it: open a child pin per half, or abandon this")
            print("pin honestly:")
            print("  pin.py open <child> --parent %s --step '<step>' ..." % slug)
            print("  pin.py abandon %s --reason '...'" % slug)
            return 2

    claim = opened["data"]["claim"]
    kill = opened["data"]["kill"]
    rule = opened["data"].get("rule")
    bv, av = b["data"]["value"], aft["data"]["value"]

    print("=" * 68)
    print("PIN VERDICT: %s" % slug)
    print("=" * 68)
    print("CLAIM UNDER TEST : %s" % claim)
    print("KILL CONDITION   : %s" % kill)
    if opened["data"].get("scope"):
        # Printed next to the claim on purpose. A claim that reads wider than its scope is
        # visible here, at judgment time, which is the only moment anyone re-reads either.
        print("EVIDENCE SCOPE   : %s" % opened["data"]["scope"])
    _rs = [e["data"] for e in pin["events"] if e["action"] == "source"]
    if _rs:
        print("SOURCES READ     : %s"
              % "; ".join("%s (%d chars)" % (s["name"], s["chars"]) for s in _rs))
    if opened["data"].get("single"):
        # Reprinted beside the claim on purpose: the override was an argument made
        # BEFORE the result was known, and this is the one moment anyone rereads it.
        print("DECOMPOSE OVERRIDE: %s" % opened["data"]["single"])
    _st = [e["data"] for e in pin["events"] if e["action"] == "step"]
    if _st:
        print("SUB-STEPS        : " + "; ".join("%s=%s" % (s["name"], s["state"]) for s in _st))
        # Same technique as COUNTING above: the gap between what was MEASURED and what was
        # merely ASSERTED is legible in this block or nowhere.
        _bound = {st for _cs, st, _cv in _children_of(slug)}
        _asserted = [s["name"] for s in _st if s["name"] not in _bound]
        _measured = [s["name"] for s in _st if s["name"] in _bound]
        print("  MEASURED by a child pin (%d): %s"
              % (len(_measured), ", ".join(_measured) or "none"))
        print("  ASSERTED, no kill condition (%d): %s"
              % (len(_asserted), ", ".join(_asserted) or "none"))
        if _asserted:
            print("           ^ these rungs carry NO kill condition. If the claim above")
            print("             depends on them, this verdict certifies an assertion.")
    print("   (sealed %s, unmodified since)" % opened["ts"][:19])
    print("-" * 68)
    print("BEFORE : %-10g  %s" % (bv, b["data"]["evidence"]))
    print("AFTER  : %-10g  %s" % (av, aft["data"]["evidence"]))
    print("-" * 68)

    if rule:
        passed, why = eval_rule(rule, bv, av)
        result = "PASS" if passed else "FAIL"
        print("RULE   : %s" % rule)
        # The unit prints HERE, directly under the claim and the kill condition, because
        # this is the only moment anyone rereads both. A claim asserting four things under
        # a rule counting one of them is visible in this three-line block or nowhere.
        if opened["data"].get("unit"):
            print("COUNTING: %s" % opened["data"]["unit"])
            print("           ^ if this does not cover the WHOLE claim above, the rule")
            print("             measured one part and the verdict certifies all of it.")
        # V8 (2026-09-12). Printed HERE, in the same block as the rule and the unit, for
        # the same reason both of those print here: this is the one moment anyone rereads
        # the rule beside the result. A PASS whose degenerate shape is sitting unread in
        # the chain file has not been checked. On a PASS it is a challenge; on a FAIL it
        # is context. Either way the reader is handed the thing to rule out.
        if opened["data"].get("degenerate"):
            print("DEGENERATE PASS WOULD LOOK LIKE: %s" % opened["data"]["degenerate"])
            print("           ^ declared BEFORE the measurement. If the result above")
            print("             is indistinguishable from this, the rule was satisfied")
            print("             by garbage and the verdict means nothing.")
        print("EVAL   : %s" % why)
        if a.result and a.result != result:
            print("\nREFUSING: you stated %s but the pre-registered rule says %s."
                  % (a.result, result))
            print("The rule was registered before the measurement. It wins.")
            return 2
        rationale = a.rationale or "auto-evaluated from the pre-registered rule"
    else:
        if not a.result:
            raise SystemExit(
                "ERROR: this pin has no machine-checkable rule, so --result "
                "PASS|FAIL is required. Read the KILL CONDITION above first.")
        result = a.result
        rationale = a.rationale or ""

    _append(pin, "verdict", {"result": result, "rationale": rationale})
    _save(slug, pin)

    # V7: a child's verdict WRITES the parent's step outcome. The checkbox becomes a
    # measurement receipt - it cites a sealed chain with its own kill condition, which is
    # exactly what a hand-typed `--state done` cannot do.
    _pslug = opened["data"].get("parent")
    _pstep = opened["data"].get("parent_step")
    if _pslug and _pstep and os.path.exists(_path(_pslug)):
        _parent = _load(_pslug)
        if _pstep not in _step_outcomes(_parent):
            _append(_parent, "step", {
                "name": _pstep,
                "state": "done" if result == "PASS" else "blocked",
                "evidence": "child pin '%s' rendered %s (before=%g after=%g) - MEASURED "
                            "against its own kill condition, not asserted"
                            % (slug, result, bv, av),
                "child": slug})
            _save(_pslug, _parent)
            print("\n  ^ parent '%s' step '%s' -> %s"
                  % (_pslug, _pstep, "done" if result == "PASS" else "blocked"))
            if result == "FAIL":
                print("    A FAILED child BLOCKS the parent verdict. Re-split that rung.")
            _nx = _next_action(_pslug)
            if _nx:
                print("    next: %s" % _nx)

    print("\n>>> %s <<<" % result)
    if rationale:
        print("    %s" % rationale)
    if result == "FAIL":
        print("\nFAIL is a real answer. Do not adopt the change.")
    return 0


def cmd_list(a):
    if not os.path.isdir(PIN_DIR):
        print("no pins yet (%s)" % PIN_DIR)
        return 0
    # LANE FILTER (2026-09-01). Rule: "any tool reading pins for 'current work'
    # filters to the current project's tag first." This is that reader. It is the
    # ONLY filtered one on purpose: _children_of stays wide, because splitting the
    # ladder lookup by lane was MEASURED at the time to deadlock all 94 parent-bound
    # pins - the parent stops seeing its child and can never render.
    want = getattr(a, "lane", None) or (None if getattr(a, "all", False)
                                        else _current_lane())
    # SUBJECT_FILTER_V1, 2026-09-02. ADDITIVE. --subject swaps only WHICH RESOLVER
    # THIS LISTING CALLS. _lane_of is untouched, so foreign-pin detection keeps the
    # AUTHOR answer the both-indexes rule protects. Nothing is reconciled; a second
    # question is simply askable.
    #
    # WHY IT EXISTS, measured: pins about one project resolved to other lanes
    # because the work was done inside sessions declared to those lanes. A lane
    # FILTER asks "is this pin about my work", which is the SUBJECT question;
    # _lane_of answers "who typed it".
    #
    # 🛑 THE REORDER I TRIED FIRST WAS WRONG AND IS RECORDED SO NOBODY RETRIES IT:
    # moving the prefix above author INSIDE _lane_of trips the L1320 canary by
    # design, silently redefines two other callers, and destroys the author index.
    # That attempt was sealed as a FAIL.
    subject_mode = bool(getattr(a, "subject", False))
    resolver = _subject_lane_of if subject_mode else _lane_of
    rows = []
    hidden = 0
    for fn in sorted(os.listdir(PIN_DIR)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        pin = _load(fn[:-5])
        if want is not None and resolver(pin) != want:
            hidden += 1
            continue
        ok, _ = verify_chain(pin)
        v = _first(pin, "verdict")
        # ABANDONED is a THIRD terminal state (2026-08-17). Without it, a retired pin
        # still reads as "open" and keeps inflating the unfinished-measurement count,
        # which is the thing that makes people ignore the number.
        state = v["data"]["result"] if v else (
            "ABANDONED" if _first(pin, "abandoned") else
            "measured" if _first(pin, "after") else
            "baselined" if _first(pin, "before") else "open")
        # DUAL_LANE_COLUMN_V1, 2026-09-02.
        # Computed HERE, BELOW the filter, on purpose: the `continue` above has
        # already decided membership, so nothing added at this point can change
        # WHICH pins are shown. That is structural, not a promise, and it was
        # measured as slug SETS rather than counts, because a count can hold while
        # membership moves.
        #
        # WHY BOTH: the both-indexes rule keeps two indexes because author and
        # subject are different questions and reconciling them destroys one. A FLAG
        # makes the reader pick one and hides the other, which nobody can do
        # correctly without already knowing the answer they are asking for. This
        # reconciles nothing; it DISPLAYS both and marks the ~4% where they differ.
        _al, _sl = _lane_of(pin), _subject_lane_of(pin)
        if _al == _sl:
            _lane_cell = "%s (both)" % _al if _al != "unknown" else "unknown (neither)"
        elif _al == "unknown":
            _lane_cell = "%s (subject)" % _sl
        elif _sl == "unknown":
            _lane_cell = "%s (author)" % _al
        else:
            _lane_cell = "%s/%s (SPLIT)" % (_al, _sl)
        rows.append((pin["slug"], _lane_cell, state, "OK" if ok else "TAMPERED",
                     _first(pin, "open")["data"]["claim"][:44]))
    # The hidden count is printed ALWAYS, including when it is the only thing to
    # say. A filter that silently shows nothing is indistinguishable from an empty
    # store, and "an EMPTY result must prove it is really empty before you call it
    # clear" is this house's rule. Naming the lane and the count is that proof.
    def _banner():
        if want is None:
            print("lane: ALL (%d pins)" % len(rows))
        else:
            print("lane: %s  ·  %d shown, %d hidden from other lanes  ·  --all to widen"
                  % (want, len(rows), hidden))
            if want == "unknown":
                _foreign = sum(1 for _r in rows
                               if _subject_of(_load(_r[0])) not in (None, "", "unknown"))
                print("  ^ this session has no declared lane, so it sees only "
                      "unattributed pins.\n"
                      "    THAT IS NOT THE SAME AS SAFE: %d of these %d carry an "
                      "identifiable\n    OTHER-LANE SUBJECT. Declare it in %s"
                      % (_foreign, len(rows), _lane_sidecar()))
    if not rows:
        _banner()
        print("no pins in this lane")
        return 0
    _banner()
    print("%-30s %-17s %-10s %-9s %s"
          % ("SLUG", "LANE", "STATE", "CHAIN", "CLAIM"))
    for r in rows:
        print("%-30s %-17s %-10s %-9s %s" % r)
    # A SPLIT is the ~4% author/subject disagreement made visible at READ time,
    # which is the only moment it can change what anyone does. Counted rather than
    # merely marked, so it cannot be scrolled past.
    _splits = sum(1 for r in rows if "(SPLIT)" in r[1])
    if _splits:
        print("")
        print("  %d of %d rows SPLIT: the author index and the subject index "
              "disagree." % (_splits, len(rows)))
        print("  Neither is wrong. They answer different questions.")
    return 0


# --------------------------------------------------------------------------
# selftest - positive AND negative controls
# --------------------------------------------------------------------------

def cmd_canary(a):
    """Assert the lane invariant against the LIVE store. Exit 0 clear, 1 FAIL.

    Rule: a safety mechanism whose invariant is not asserted anywhere is
    the next silently-dead system. The selftest proves the filter works in a temp
    directory; this proves it is working HERE, now, on the real store.

    It deliberately does NOT live in the VM invariant canary. That runs on the VM
    and the pin store is a local Windows path, so a VM check would read zero by
    construction and report clear - one path, two places, which is how a notifier
    once reported "0 FAILING" while six were red.
    """
    problems = []
    lane = _current_lane()
    try:
        pins = _all_pins()
    except Exception as e:                                   # pragma: no cover
        print("PIN LANE CANARY: FAIL - store unreadable: %s" % e)
        return 1

    # Counted the SAME WAY whatever the lane is. The first cut skipped this when the
    # lane was unknown, so it printed "1012 total, 1012 in lane, 0 other lanes" -
    # a number that was not measured, in the exact place a reader would trust it.
    foreign = [s for s in pins if _lane_of(_load(s)) != lane]
    total = len(pins)
    mine = total - len(foreign)

    if lane == "unknown":
        # "unattributed" is not "neutral". Measured 2026-09-02: 218 of 608 unattributed
        # pins carry an identifiable other-lane subject. Counted live so it cannot
        # go stale the way the undated 39% figure did.
        # SCOPED TO THE UNATTRIBUTED BUCKET, not the whole store. The first cut
        # counted across all 1165 pins and printed "433 of those 1165" under a
        # sentence whose "those" meant the 608 unattributed - a wrong denominator
        # inside the fix for a misleading message.
        _unattributed = [_s for _s in pins if _lane_of(_load(_s)) == "unknown"]
        _foreign_subj = sum(1 for _s in _unattributed
                            if _subject_of(_load(_s)) not in (None, "", "unknown"))
        problems.append("this session has no declared lane in %s, so every listing "
                        "is restricted to unattributed pins - and %d of those %d "
                        "carry an identifiable OTHER-LANE SUBJECT, so unattributed "
                        "is not neutral"
                        % (_lane_sidecar(), _foreign_subj, len(_unattributed)))
    # THE WIDE ARM, in the canary itself. A filter that hides everything also
    # reports zero foreign pins, so "0 foreign" alone is not evidence.
    if lane != "unknown" and mine == 0 and total > 0:
        problems.append("the filter surfaced NOTHING out of %d pins - that is "
                        "blanking, not filtering" % total)
    if lane != "unknown" and mine == total and total > 0:
        problems.append("the filter hid NOTHING out of %d pins - it is not "
                        "filtering at all" % total)

    # THE CHECK THAT ACTUALLY DISCRIMINATES, and the first cut did not have it.
    # Blanking and not-filtering are the easy failures. The dangerous one is PARTIAL
    # mis-classification: a resolver that quietly pulls other lanes' pins into mine.
    # Measured against a build with _lane_of broken, the two checks above BOTH passed
    # while 728 of 1012 pins had been mis-mapped into this lane. A guard that misses
    # the failure it exists to catch is decorative.
    #
    # So cross-check the two INDEPENDENT signals. A slug prefix naming lane X while
    # the resolver returns lane Y is a disagreement. Measured at the time, prefix and
    # subject agree 96.1% of the time, so a handful is normal and expected - the
    # threshold is deliberately loose. A broken resolver pushes it to hundreds.
    # This is the THIRD version of this check. The first two passed on a build with
    # _lane_of deliberately broken, which is the guard failing to catch the only
    # thing it exists for. Recorded rather than quietly replaced:
    #   v1  flagged blanking (mine==0) and no-filtering (mine==total). A build that
    #       mis-mapped 728 of 1012 pins sat between those two and passed.
    #   v2  cross-checked the slug prefix against the resolver. Blind by
    #       construction: the break only moved UNPREFIXED pins, which have no
    #       prefix to disagree with.
    #   v3  below. An INDEPENDENT reimplementation of the resolution rule, compared
    #       against the real resolver on every pin. It duplicates the logic on
    #       purpose - a control that calls the thing it is checking is not a control.
    lm = _lane_map()
    bf = _author_backfill()
    known = _lanes_config()["lanes"]
    mismatch = []
    for s in pins:
        pin = _load(s)
        # Independent reimplementation of the SAME precedence: sealed author, then
        # backfilled author, then prefix, then unknown. When this drifted out of
        # sync with _lane_of it FIRED on the very next run, which is the point.
        author = _open_data(pin).get("author_session") or bf.get(s, "")
        declared = lm.get(author or "")
        pre = next((L for L in known if s.startswith(L + "-")), None)
        expected = declared or pre or "unknown"
        if _lane_of(pin) != expected:
            mismatch.append((s, expected, _lane_of(pin)))
    if mismatch:
        problems.append(
            "%d of %d pins resolve to a lane the declaration rules do not produce "
            "(e.g. %s: expected %s, got %s). The resolver is inventing attribution, "
            "and a wrongly-attributed pin is worse than an unattributed one because "
            "it lands confidently and nobody re-examines it"
            % (len(mismatch), total, mismatch[0][0], mismatch[0][1], mismatch[0][2]))

    print("PIN LANE CANARY")
    print("  store        : %s" % PIN_DIR)
    print("  this session : %s -> lane %s" % (_author()[:8], lane))
    print("  pins         : %d total · %d in lane · %d other lanes" % (total, mine, len(foreign)))
    if problems:
        print("  STATUS       : FAIL")
        for p in problems:
            print("    - %s" % p)
        return 1
    print("  STATUS       : OK  (default reads are lane-scoped)")
    return 0


def cmd_selftest(a):
    import shutil
    import tempfile

    global PIN_DIR
    saved = PIN_DIR
    PIN_DIR = tempfile.mkdtemp(prefix="pin_selftest_")
    failures = []

    def check(name, got, want):
        ok = got == want
        print("  [%s] %s (got %r, want %r)" % ("PASS" if ok else "FAIL", name, got, want))
        if not ok:
            failures.append(name)

    try:
        print("pin.py selftest - 2 of these MUST come back negative\n")

        # 1 POSITIVE CONTROL: a clean chain verifies
        pin = {"slug": "t1", "created": _now(), "events": []}
        _append(pin, "open", {"claim": "c", "kill": "k", "rule": "after>before"})
        _append(pin, "before", {"value": 1, "evidence": "e"})
        _append(pin, "after", {"value": 5, "evidence": "e"})
        check("clean chain verifies", verify_chain(pin)[0], True)

        # 2 NEGATIVE CONTROL: softening the kill condition is DETECTED
        tampered = json.loads(json.dumps(pin))
        tampered["events"][0]["data"]["kill"] = "k (but actually anything is fine)"
        check("edited kill condition is caught", verify_chain(tampered)[0], False)

        # 3 NEGATIVE CONTROL: editing a measured VALUE is DETECTED
        t2 = json.loads(json.dumps(pin))
        t2["events"][2]["data"]["value"] = 999
        check("edited after-value is caught", verify_chain(t2)[0], False)

        # 4 ANTI-RATIONALISATION: a rule that should fail, fails
        check("failing rule returns FAIL", eval_rule("after>before", 10, 3)[0], False)
        check("passing rule returns PASS", eval_rule("after>before", 3, 10)[0], True)
        check("threshold rule evaluates", eval_rule("after>=5", 0, 5)[0], True)

        # 5 the real shape of today's pyright test: 0 true positives must FAIL
        check("pyright case (0 true positives) FAILS",
              eval_rule("after>=1", 0, 0)[0], False)

        # ---- V3 HARDENING (2026-08-14), each fed the EXACT bad input from that session ----
        import argparse as _ap

        # 8 NEGATIVE CONTROL: a rubber-stamp control is REFUSED.
        # This is my real 2026-08-14 mistake: the control expression was `done != False`,
        # which is just `done`. Identical observations on both arms = no discrimination.
        _p = {"slug": "t8", "created": _now(), "events": []}
        _append(_p, "open", {"claim": "c", "kill": "k", "control": "a control"})
        _save("t8", _p)
        rubber = _ap.Namespace(slug="t8", discriminated=True, evidence="e",
                               positive="returned True", negative="returned True")
        try:
            cmd_control(rubber); got = "accepted"
        except SystemExit:
            got = "refused"
        check("rubber-stamp control REFUSED", got, "refused")

        # 9 POSITIVE CONTROL: genuinely different observations are ACCEPTED
        _p9 = {"slug": "t9", "created": _now(), "events": []}
        _append(_p9, "open", {"claim": "c", "kill": "k", "control": "a control"})
        _save("t9", _p9)
        real = _ap.Namespace(slug="t9", discriminated=True, evidence="e",
                             positive="known-good returned 200 with data",
                             negative="known-bad returned 404")
        try:
            cmd_control(real); got = "accepted"
        except SystemExit:
            got = "refused"
        check("discriminating control ACCEPTED", got, "accepted")

        # 10 NEGATIVE CONTROL: verdict REFUSES while a named source is unread.
        # The real case: the Premium Advisory doc was named, logged as skipped, and skipped again.
        _p10 = {"slug": "t10", "created": _now(), "events": []}
        _append(_p10, "open", {"claim": "c", "kill": "k", "rule": "after==0",
                               "sources": ["doc_a", "doc_b"]})
        _append(_p10, "before", {"value": 1, "evidence": "e"})
        _append(_p10, "after", {"value": 0, "evidence": "e"})
        _save("t10", _p10)
        rc = cmd_verdict(_ap.Namespace(slug="t10", result=None, rationale=None))
        check("verdict REFUSED while a source is unread", rc, 2)

        # 11 a zero-character read is not a read
        _p11 = {"slug": "t11", "created": _now(), "events": []}
        _append(_p11, "open", {"claim": "c", "kill": "k", "sources": ["doc_a"]})
        _save("t11", _p11)
        try:
            cmd_source(_ap.Namespace(slug="t11", name="doc_a", chars=0, evidence="e"))
            got = "accepted"
        except SystemExit:
            got = "refused"
        check("zero-char read receipt REFUSED", got, "refused")

        # 12 BACKWARD COMPAT: a legacy pin with none of the v3 fields still verdicts
        _p12 = {"slug": "t12", "created": _now(), "events": []}
        _append(_p12, "open", {"claim": "c", "kill": "k", "rule": "after==0"})
        _append(_p12, "before", {"value": 3, "evidence": "e"})
        _append(_p12, "after", {"value": 0, "evidence": "e"})
        _save("t12", _p12)
        rc12 = cmd_verdict(_ap.Namespace(slug="t12", result=None, rationale=None))
        check("legacy pin still verdicts (backward compat)", rc12, 0)

        # ---- V6 GATE CONTROLS (2026-08-25). 17-22. The v5 checks are replaced, not kept:
        # they asserted the word-list gated the open, and it no longer does.
        # V8: `degenerate` joins the base so existing cases keep testing what they were
        # written to test. The cases that specifically exercise the NEW gate override it
        # to None deliberately (see 37/38 below).
        _base = dict(kill="k", record=None, control=None, scope=None,
                     sources=None, steps=None, single=None, unit=None,
                     degenerate="the probe returns a number while writing nothing")

        # 17 NEGATIVE CONTROL: no split declared -> REFUSED, even with an ATOMIC claim.
        # This is the point of dropping the detector: it refuses on a claim carrying NONE
        # of the 23 outcome words, which v5 sailed straight through.
        _n17 = _ap.Namespace(slug="t17", claim="the bus check is complete", rule=None,
                             **{k: v for k, v in _base.items() if k != "rule"})
        check("no split declared REFUSED (atomic-looking claim)", cmd_open(_n17), 2)
        check("...and nothing was written to disk", os.path.exists(_path("t17")), False)

        # 18 POSITIVE CONTROL: --single satisfies it, one word, no rule so no unit needed.
        _n18 = _ap.Namespace(slug="t18", claim="the bus check is complete",
                             **{k: v for k, v in _base.items() if k not in ("single",)},
                             single="one surface, one measurement", rule=None)
        check("--single ACCEPTED", cmd_open(_n18), 0)

        # 19 NEGATIVE CONTROL: a rule with no unit is REFUSED. Fed the exact shape of
        # the overnight-run pin - four assertions under rule after>=150.
        _n19 = _ap.Namespace(
            slug="t19",
            claim="the runner survives 12 hours and completes 150 recordings and retains audio",
            **{k: v for k, v in _base.items() if k not in ("steps", "rule", "unit")},
            steps="survives,completes,retains", rule="after>=150", unit=None)
        check("rule without a unit REFUSED", cmd_open(_n19), 2)
        check("...and nothing was written to disk", os.path.exists(_path("t19")), False)

        # 20 POSITIVE CONTROL: the same pin opens once the unit is named.
        _n20 = _ap.Namespace(
            slug="t20",
            claim="the runner survives 12 hours and completes 150 recordings and retains audio",
            **{k: v for k, v in _base.items() if k not in ("steps", "rule", "unit")},
            steps="survives,completes,retains", rule="after>=150",
            unit="recordings completed end to end")
        check("same pin ACCEPTED once --unit is named", cmd_open(_n20), 0)

        # 21 the word list is a HINT now and must never gate. A compound claim WITH a
        # declared split opens cleanly even though is_compound() fires on it.
        _n21 = _ap.Namespace(
            slug="t21", claim="the card is published and the splice is deployed",
            **{k: v for k, v in _base.items() if k not in ("steps",)},
            steps="card,splice", rule=None)
        check("compound claim opens WITH a declared split", cmd_open(_n21), 0)

        # 22 BACKWARD COMPAT, the one that protects 557 sealed pins. None of them carry
        # steps/single/unit. The gate is at OPEN only, so they must still verdict.
        _p22 = {"slug": "t22", "created": _now(), "events": []}
        _append(_p22, "open", {"claim": "c", "kill": "k", "rule": "after==0"})
        _append(_p22, "before", {"value": 3, "evidence": "e"})
        _append(_p22, "after", {"value": 0, "evidence": "e"})
        _save("t22", _p22)
        check("legacy pin with NO unit still verdicts",
              cmd_verdict(_ap.Namespace(slug="t22", result=None, rationale=None)), 0)

        # ---- V8 DEGENERATE-GATE CONTROLS (2026-09-12) ------------------------------
        # Two negative, two positive. Each asserts the REFUSAL HEADLINE and not merely
        # exit 2, because the unit gate and the split gate also return 2 from the same
        # function - and an exit code cannot tell you WHICH guard fired. That lesson is
        # V7's own: two controls once passed on a neutered build for the wrong reason.

        # 37 NEGATIVE: the exact shape that fell through on 2026-09-08. A floor rule,
        # a named unit, and no declaration of what garbage satisfying it looks like.
        _n37 = _ap.Namespace(
            slug="t37", claim="the sheets_to_pg sync moves rows into postgres",
            **{k: v for k, v in _base.items() if k not in ("rule", "unit", "degenerate", "single")},
            single="one pipeline, one direction", rule="after>0",
            unit="rows present in the destination table", degenerate=None)
        _cap = io.StringIO()
        with contextlib.redirect_stdout(_cap):
            _rc37 = cmd_open(_n37)
        check("threshold rule with NO --degenerate REFUSED", _rc37, 2)
        check("...and it is the DEGENERATE gate that fired, not the unit gate",
              "YOU HAVE NOT SAID WHAT A DEGENERATE PASS LOOKS LIKE" in _cap.getvalue(), True)
        check("...and nothing was written to disk", os.path.exists(_path("t37")), False)

        # 38 POSITIVE: the same pin opens once the degenerate shape is named.
        _n38 = _ap.Namespace(
            slug="t38", claim="the sheets_to_pg sync moves rows into postgres",
            **{k: v for k, v in _base.items() if k not in ("rule", "unit", "degenerate", "single")},
            single="one pipeline, one direction", rule="after>0",
            unit="rows present in the destination table",
            degenerate="fetches 261 rows from the source and writes 0 of them")
        check("same pin ACCEPTED once --degenerate is named", cmd_open(_n38), 0)

        # 39 NEGATIVE CONTROL ON THE TRIGGER: a pin with NO rule at all must still open
        # without the flag. The gate keys on the presence of --rule, exactly like --unit,
        # so it must not fire on a pin that never registered a threshold.
        _n39 = _ap.Namespace(
            slug="t39", claim="the bus check is complete",
            **{k: v for k, v in _base.items() if k not in ("rule", "unit", "degenerate", "single")},
            single="one check", rule=None, unit=None, degenerate=None)
        check("pin with NO rule opens WITHOUT --degenerate", cmd_open(_n39), 0)

        # 40 the declaration must REACH THE VERDICT. A declaration sitting unread in the
        # chain file is the advisory this option was chosen over.
        _append(_load("t38"), "x", {})  # touch-load to prove it round-trips
        _p40 = _load("t38")
        _append(_p40, "before", {"value": 0, "evidence": "e"})
        _append(_p40, "after", {"value": 5, "evidence": "e"})
        _save("t38", _p40)
        _cap40 = io.StringIO()
        with contextlib.redirect_stdout(_cap40):
            cmd_verdict(_ap.Namespace(slug="t38", result=None, rationale=None))
        check("the degenerate shape is REPRINTED at verdict",
              "fetches 261 rows from the source and writes 0 of them" in _cap40.getvalue(), True)

        # ---- V7 SUB-PIN CONTROLS (2026-08-26) -------------------------------------
        # Half positive, half negative. A guard that cannot fail is not a guard, so each
        # refusal is paired with the same call succeeding once its reason is removed.

        def _mk(slug_, **kw):
            base = dict(claim="c", kill="k", rule=None, record=None, control=None,
                        scope=None, sources=None, steps=None, single="one thing",
                        unit=None, parent=None, step=None)
            base.update(kw)
            return _ap.Namespace(slug=slug_, **base)

        # 23 POSITIVE: a parent with a declared split, then a child bound to a real rung.
        check("parent with declared split opens",
              cmd_open(_mk("t23p", steps="a,b", single=None)), 0)
        check("child pin binds to a declared step",
              cmd_open(_mk("t23c", parent="t23p", step="a")), 0)

        # 24 NEGATIVE: an UNDECLARED step is refused, and NOTHING is written to disk.
        check("child on an undeclared step is REFUSED",
              cmd_open(_mk("t24", parent="t23p", step="not-a-real-step")), 2)
        check("  ...and the refused child wrote nothing",
              os.path.exists(_path("t24")), False)

        # 25 NEGATIVE: two children cannot share one rung.
        check("second child on the same rung is REFUSED",
              cmd_open(_mk("t25", parent="t23p", step="a")), 2)

        # 26 NEGATIVE: parent REFUSES to render while its child is unresolved.
        #
        # This control asserts the refusal HEADLINE, not just exit 2, and the reason is
        # a defect found in this very control on 2026-08-26. With _children_of() neutered
        # the check still passed, because an unresolved child-bound rung ALSO has no step
        # outcome, so the older missing-outcome gate returned 2 as well. Same exit code,
        # different gate: the control was measuring "something refused", which is the
        # rubber-stamp shape V3 killed on --discriminated. An exit code is not proof of
        # WHICH guard fired.
        import contextlib as _ctx

        def _refusal_headline(fn, *fa, **fkw):
            _buf = io.StringIO()
            with _ctx.redirect_stdout(_buf):
                _rc = fn(*fa, **fkw)
            _txt = _buf.getvalue()
            for _ln in _txt.splitlines():
                if _ln.startswith("VERDICT REFUSED") or _ln.startswith("OPEN REFUSED"):
                    return _rc, _ln.strip()
            return _rc, "(no refusal)"

        _p26 = _load("t23p")
        _append(_p26, "before", {"value": 0, "evidence": "e"})
        _append(_p26, "after", {"value": 1, "evidence": "e"})
        _save("t23p", _p26)
        _rc26, _hl26 = _refusal_headline(
            cmd_verdict, _ap.Namespace(slug="t23p", result="PASS", rationale=None))
        check("parent REFUSES while a child is unresolved", _rc26, 2)
        check("  ...and it refuses for THAT reason, not another gate",
              _hl26, "VERDICT REFUSED - A CHILD PIN HAS NOT RENDERED")

        # 27 POSITIVE: the child's verdict WRITES the parent's step outcome.
        _c27 = _load("t23c")
        _append(_c27, "before", {"value": 0, "evidence": "e"})
        _append(_c27, "after", {"value": 1, "evidence": "e"})
        _save("t23c", _c27)
        check("child renders", cmd_verdict(
            _ap.Namespace(slug="t23c", result="PASS", rationale=None)), 0)
        check("  ...and it discharged the parent rung as done",
              _step_outcomes(_load("t23p")).get("a", {}).get("state"), "done")
        check("  ...citing the child pin, not a hand-typed assertion",
              "child" in _step_outcomes(_load("t23p")).get("a", {}), True)

        # 28 NEGATIVE: a BLOCKED rung REFUSES the parent verdict. Before V7 this
        # rendered, so a ladder could PASS with a rung nobody could climb.
        cmd_step(_ap.Namespace(slug="t23p", name="b", state="blocked",
                               evidence="could not climb it"))
        _rc28, _hl28 = _refusal_headline(
            cmd_verdict, _ap.Namespace(slug="t23p", result="PASS", rationale=None))
        check("a BLOCKED rung REFUSES the parent verdict", _rc28, 2)
        check("  ...and it refuses for THAT reason, not another gate",
              _hl28, "VERDICT REFUSED - A SUB-STEP IS BLOCKED")

        # 29 POSITIVE: same parent, same call, renders once the rung is not blocked.
        # This is the control ON control 28 - it proves 28 refused for the stated
        # reason and not because the pin was malformed.
        check("parent with a declared split opens (2)",
              cmd_open(_mk("t29p", steps="a,b", single=None)), 0)
        _p29 = _load("t29p")
        _append(_p29, "before", {"value": 0, "evidence": "e"})
        _append(_p29, "after", {"value": 1, "evidence": "e"})
        _save("t29p", _p29)
        cmd_step(_ap.Namespace(slug="t29p", name="a", state="done", evidence="e"))
        cmd_step(_ap.Namespace(slug="t29p", name="b", state="done", evidence="e"))
        check("...same call RENDERS when no rung is blocked",
              cmd_verdict(_ap.Namespace(slug="t29p", result="PASS", rationale=None)), 0)

        # 30 BACKWARD COMPAT: a legacy pin has no parent and no children, so none of
        # the V7 gates may touch it. This protects 588 sealed chains.
        _p30 = {"slug": "t30", "created": _now(), "events": []}
        _append(_p30, "open", {"claim": "c", "kill": "k", "rule": "after==0"})
        _append(_p30, "before", {"value": 3, "evidence": "e"})
        _append(_p30, "after", {"value": 0, "evidence": "e"})
        _save("t30", _p30)
        check("legacy pin with no ladder still verdicts",
              cmd_verdict(_ap.Namespace(slug="t30", result=None, rationale=None)), 0)
        check("_children_of on a childless pin is empty", _children_of("t30"), [])

        # ---- AUTHOR INDEX CONTROLS (2026-09-01) -----------------------------------
        # Rule: a new safety mechanism without its own invariant in the SAME
        # pass is the next silently-dead system. Half positive, half negative, because
        # a field that records the same value in every case indexes nothing - the exact
        # rubber-stamp shape V3 killed on --discriminated.
        _saved_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        try:
            # 31 POSITIVE: the author is captured from the environment and sealed.
            os.environ["CLAUDE_CODE_SESSION_ID"] = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
            check("author is sealed into a new pin", cmd_open(_mk("t31")), 0)
            check("  ...and it is the REAL session id, not a placeholder",
                  _open_data(_load("t31")).get("author_session"),
                  "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb")

            # 32 NEGATIVE: with no session in the environment it must record "unknown".
            # It must NOT inherit the previous value, invent one, or silently omit the
            # key - any of those would make an unattributable pin look attributed,
            # which is the confidentiality failure this whole change exists to close.
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            check("no session in env still opens", cmd_open(_mk("t32")), 0)
            check("  ...and records unknown, never inventing or inheriting an id",
                  _open_data(_load("t32")).get("author_session"), "unknown")
            check("  ...and the two differ, so the field discriminates",
                  _open_data(_load("t31")).get("author_session")
                  != _open_data(_load("t32")).get("author_session"), True)

            # 33 BACKWARD COMPAT, the one protecting 1003 sealed chains. None of them
            # carry author_session. The field is written only at open, so a legacy pin
            # must still verify and still render a verdict.
            _p33 = {"slug": "t33", "created": _now(), "events": []}
            _append(_p33, "open", {"claim": "c", "kill": "k", "rule": "after==0"})
            _append(_p33, "before", {"value": 3, "evidence": "e"})
            _append(_p33, "after", {"value": 0, "evidence": "e"})
            _save("t33", _p33)
            check("legacy pin with NO author field still verifies",
                  verify_chain(_load("t33"))[0], True)
            check("  ...and still renders a verdict", cmd_verdict(
                _ap.Namespace(slug="t33", result=None, rationale=None)), 0)
            # ---- LANE FILTER CONTROLS (2026-09-01) ---------------------------
            # 34 POSITIVE + NEGATIVE on the same store. The wide arm is the half
            # people skip: a filter that hides EVERYTHING also scores zero foreign
            # pins, so "0 foreign" alone cannot tell filtering from blanking.
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-alpha"
            with open(_lane_sidecar(), "w", encoding="utf-8") as _fh:
                json.dump({"sess-alpha": "acme", "sess-beta": "globex"}, _fh)
            with open(os.path.join(PIN_DIR, "_lanes_config.json"), "w", encoding="utf-8") as _fh:
                json.dump({"lanes": ["acme", "globex", "myco"], "operator_lanes": ["myco"],
                           "subject_patterns": {"acme": "acme|roadrunner", "globex": "globex"},
                           "scrub_patterns": ["me@myco\\.com"]}, _fh)
            cmd_open(_mk("t34-mine"))
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-beta"
            cmd_open(_mk("t34-theirs"))
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-alpha"

            def _listed(**kw):
                _b = io.StringIO()
                with _ctx.redirect_stdout(_b):
                    cmd_list(_ap.Namespace(**kw))
                return _b.getvalue()

            _default = _listed(lane=None, all=False)
            check("default listing SHOWS my own lane's pin",
                  "t34-mine" in _default, True)
            check("default listing HIDES the other lane's pin",
                  "t34-theirs" in _default, False)
            check("  ...and says so rather than looking like an empty store",
                  "hidden from other lanes" in _default, True)
            # ---- SUBJECT RESOLVER CONTROLS (2026-09-01) ----------------------
            # 35. Precision is the property that matters, so the negative arms are
            # the point: a lane named inside a NEGATION must not be attributed, and
            # two lanes named at once must return unknown rather than a winner.
            def _subj(**kw):
                _p = {"slug": "x", "created": _now(), "events": []}
                _append(_p, "open", dict(claim="c", kill="k", **kw))
                return _subject_of(_p)
            check("plain marker in record resolves",
                  _subj(record="the Roadrunner quote pipeline", scope=None), "acme")
            check("marker inside a NEGATION is NOT attributed",
                  _subj(record="globex_login.py only",
                        scope="says NOTHING about acme"), "globex")
            check("a pure exclusion resolves to unknown, not to the excluded lane",
                  _subj(record=None, scope="not the per-client index for globex"), "unknown")
            check("TWO lanes named -> unknown, never a winner",
                  _subj(record="roadrunner and globex both", scope=None), "unknown")
            check("empty record+scope -> unknown",
                  _subj(record=None, scope=None), "unknown")

            _wide = _listed(lane=None, all=True)
            check("--all surfaces BOTH, so the filter filters and does not blank",
                  ("t34-mine" in _wide and "t34-theirs" in _wide), True)
            check("the lane sidecar is not itself listed as a pin",
                  "_lanes" in _wide, False)
        finally:
            if _saved_sid is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = _saved_sid

        print()
        if failures:
            print("SELFTEST BROKEN: %s" % ", ".join(failures))
            return 1
        print("SELFTEST PASS - the tamper check discriminates, and the rule")
        print("evaluator returns FAIL when it should.")
        return 0
    finally:
        shutil.rmtree(PIN_DIR, ignore_errors=True)
        PIN_DIR = saved


def main():
    p = argparse.ArgumentParser(description="frozen benchmark, pre-registered kill condition")
    sub = p.add_subparsers(dest="cmd")

    o = sub.add_parser("open"); o.add_argument("slug")
    o.add_argument("--claim", required=True); o.add_argument("--kill", required=True)
    o.add_argument("--rule")
    # claim mode
    o.add_argument("--record", help="the SYSTEM OF RECORD this claim must be measured against, "
                                    "never a doc, a memory, a local copy or one vendor's list")
    o.add_argument("--control", help="the negative control that MUST come back negative. "
                                     "Registering one makes the verdict refuse until it is run")
    # v3 hardening
    o.add_argument("--scope", help="the BOUNDARY of the claim. A claim may not travel wider "
                                   "than its evidence: name the surface, not the world")
    o.add_argument("--sources", help="comma-separated named sources that MUST be read before "
                                     "a verdict renders. Naming a source is not reading it")
    o.add_argument("--steps", help="comma-separated SUB-OUTCOMES. Each needs a recorded "
                                   "outcome before the verdict renders, so one pass/fail bit "
                                   "cannot cover several independent things")
    o.add_argument("--single", help="override the DECOMPOSE gate: state why this "
                                    "compound-looking claim really is ONE measurable "
                                    "thing. Sealed in the chain and reprinted at verdict, "
                                    "so it is an argument on the record, never a silent bypass")
    o.add_argument("--unit", help="what the rule's number COUNTS. Required whenever "
                                  "--rule is given. Printed beside the claim at verdict so "
                                  "a 4-part claim under a 1-part rule is visible there")
    o.add_argument("--degenerate", help="V8: the result that would SATISFY the rule while "
                                        "the thing under test is still broken (e.g. "
                                        "'fetches 261 rows and writes 0 of them'). Required "
                                        "whenever --rule is given, and reprinted at verdict "
                                        "so a PASS can be checked against it")
    o.set_defaults(fn=cmd_open)

    o.add_argument("--parent", help="V7: this pin DISCHARGES one declared sub-step of "
                                    "that parent pin. Use with --step. The parent's step "
                                    "outcome is then written by THIS pin's verdict, so the "
                                    "rung is measured against a kill condition instead of "
                                    "asserted with a checkbox.")
    o.add_argument("--step", help="V7: which declared step of --parent this pin discharges. "
                                  "Must be one of the parent's --steps, unbound, and "
                                  "without an outcome already.")

    c = sub.add_parser("control"); c.add_argument("slug")
    c.add_argument("--discriminated", dest="discriminated", action="store_true")
    c.add_argument("--no-discriminated", dest="discriminated", action="store_false")
    c.set_defaults(discriminated=None)
    c.add_argument("--positive", help="what the instrument returned on a KNOWN-GOOD case")
    c.add_argument("--negative", help="what it returned on a KNOWN-BAD case. If these two "
                                      "are identical the control is a rubber stamp and is refused")
    c.add_argument("--evidence", required=True); c.set_defaults(fn=cmd_control)

    sp = sub.add_parser("step"); sp.add_argument("slug")
    sp.add_argument("--name", required=True)
    sp.add_argument("--state", required=True, choices=["done", "blocked"])
    sp.add_argument("--evidence", required=True); sp.set_defaults(fn=cmd_step)

    sc = sub.add_parser("source"); sc.add_argument("slug")
    sc.add_argument("--name", required=True, help="must match a name given to --sources at open")
    sc.add_argument("--chars", type=int, required=True,
                    help="characters actually read. A count cannot be satisfied by claiming "
                         "completeness, which is why it is the receipt")
    sc.add_argument("--evidence", required=True); sc.set_defaults(fn=cmd_source)

    for name, fn in (("before", cmd_before), ("after", cmd_after)):
        s = sub.add_parser(name); s.add_argument("slug")
        s.add_argument("--value", type=float, required=True)
        s.add_argument("--evidence", required=True); s.set_defaults(fn=fn)

    v = sub.add_parser("verdict"); v.add_argument("slug")
    v.add_argument("--result", choices=["PASS", "FAIL"]); v.add_argument("--rationale")
    v.set_defaults(fn=cmd_verdict)

    ab = sub.add_parser("abandon", help="retire an unmeasured pin without faking a verdict")
    ab.add_argument("slug")
    ab.add_argument("--reason", required=True)
    ab.set_defaults(fn=cmd_abandon)

    ld = sub.add_parser("ladder", help="print the sub-pin tree under a pin plus the "
                                       "SMALLEST next action")
    ld.add_argument("slug")
    ld.set_defaults(fn=cmd_ladder)

    for name, fn in (("verify", cmd_verify), ("list", cmd_list),
                     ("selftest", cmd_selftest), ("canary", cmd_canary)):
        s = sub.add_parser(name)
        if name in ("verify",):
            s.add_argument("slug")
        if name == "list":
            s.add_argument("--lane", help="show only this lane. Default is the lane "
                                          "declared for THIS session in _lanes.json")
            s.add_argument("--all", action="store_true",
                           help="every lane. Use deliberately: reading another "
                                "client's pins as 'current work' context is the "
                                "exposure the lane filter exists to close")
            s.add_argument("--subject", action="store_true",
                           help="filter by what each pin is ABOUT rather than who "
                                "authored it. Answers 'is this pin about my work'; "
                                "the default answers 'who typed it'. Both indexes "
                                "stay - this picks which one THIS listing reads")
        s.set_defaults(fn=fn)

    a = p.parse_args()
    if not getattr(a, "cmd", None):
        p.print_help()
        return 1
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
