"""Deterministic local Digital FDE rules. No model calls, no Supervity credits.

Structured intake only: regex and keyword matching. It cannot understand free text
the way the Supervity agent can.
"""
from __future__ import annotations

import json
import re
from typing import Any

LOCAL_VERSION = "local-rules-1"

AREAS: list[tuple[str, str]] = [
    ("business_goal", "Business goal"),
    ("current_process", "Current process"),
    ("systems_involved", "Systems involved"),
    ("volume_per_day", "Volume"),
    ("exception_handling", "Exceptions"),
    ("security_compliance", "Security and compliance"),
    ("success_metrics", "Success measures"),
    ("users_and_roles", "Users and roles"),
    ("data_sources_and_formats", "Data and documents"),
    ("integrations_required", "Integrations"),
    ("timeline_constraints", "Timeline and constraints"),
    ("stakeholders_approvers", "Approvers"),
]
LABELS = dict(AREAS)
CRITICAL = [
    "business_goal",
    "current_process",
    "systems_involved",
    "volume_per_day",
    "exception_handling",
    "security_compliance",
    "success_metrics",
    "stakeholders_approvers",
]
QUESTIONS = {
    "business_goal": "What outcome is the client after, in a sentence, and what makes it urgent now?",
    "current_process": "Can you walk me through how this work gets done today, from the moment it arrives to the moment it is finished?",
    "systems_involved": "Which systems or tools does the work touch today?",
    "volume_per_day": "Roughly how many items a day does the team handle, and when are the busy peaks?",
    "exception_handling": "What happens today when something does not fit the normal flow, and who steps in?",
    "security_compliance": "Are there security, privacy or compliance rules to follow, such as where data may live or what needs an audit trail?",
    "success_metrics": "How will the client judge success: time saved, fewer errors, lower cost, or something else?",
    "users_and_roles": "Who will work with this day to day, and what are their roles?",
    "data_sources_and_formats": "What comes in, and in what shape: emails, PDFs, spreadsheets, forms?",
    "integrations_required": "Which systems would this need to read from or write to, and do they have APIs?",
    "timeline_constraints": "Is there a deadline, budget limit or other constraint to plan around?",
    "stakeholders_approvers": "Who signs off on scope, and who approves going live?",
}
RATE = {
    "DISCOVERY_WORKSHOP": 16,
    "PROCESS_MAPPING": 24,
    "INTEGRATION_SIMPLE": 24,
    "INTEGRATION_COMPLEX": 60,
    "DOCUMENT_EXTRACTION_AUTO": 40,
    "WORKFLOW_AUTO_BUILD": 48,
    "DATA_CLEANUP_MIGRATION": 40,
    "POLICY_RULESET": 24,
    "HUMAN_REVIEW_STEP": 16,
    "REPORTING_DASHBOARD": 32,
    "SECURITY_REVIEW": 24,
    "UAT_AND_HYPERCARE": 40,
    "TRAINING_HANDOVER": 16,
}

MAXV, MAXE = 400, 160
PROV = ("FACT", "INFERENCE", "ASSUMPTION")
STRENGTH = {"FACT": 3, "INFERENCE": 2, "ASSUMPTION": 1}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}

_SYSTEMS = [
    "SAP", "Outlook", "Salesforce", "ServiceNow", "Workday", "NetSuite", "Oracle",
    "SharePoint", "Excel", "Teams", "Slack", "Gmail", "QuickBooks", "Dynamics",
    "Jira", "Confluence", "Zendesk", "HubSpot", "Freshdesk", "Coupa", "Ariba",
]
_COMPLIANCE = [
    "GDPR", "HIPAA", "SOC 2", "SOC2", "ISO 27001", "PCI", "audit trail", "PII",
    "personal data", "data residency", "on prem", "on-prem", "private cloud",
]
_FORMATS = [
    ("PDF", r"\bpdfs?\b"),
    ("Excel", r"\bexcel\b|\bxlsx?\b|\bspreadsheets?\b"),
    ("email", r"\bemails?\b|\boutlook\b|\bmailbox\b"),
    ("Word", r"\bword\b|\bdocx?\b"),
    ("CSV", r"\bcsv\b"),
    ("forms", r"\bforms?\b"),
]
_SUCCESS = [
    (r"time saved|save time|faster|cycle time", "time saved"),
    (r"fewer errors|error rate|accuracy|fewer mistakes", "fewer errors"),
    (r"lower cost|cost sav|cheaper|reduce cost", "lower cost"),
]
_VAGUE = re.compile(
    r"\b(a lot|lots|many|some|soon|quickly|asap|etc\.?|stuff|things|various|several)\b",
    re.I,
)
_GOAL = re.compile(
    r"\b(automat(?:e|ion)|streamline|reduce|cut|speed up|eliminate|digitize|digitalis)\b.{0,80}",
    re.I,
)
_PROCESS = re.compile(
    r"\b(today|currently|manual(?:ly)?|process|workflow|from .{0,40} to .{0,40}|inbox|arrive)\b.{0,100}",
    re.I,
)
_EXCEPTION = re.compile(
    r"\b(exception|escalat|does not fit|doesn't fit|mismatch|wrong|reject|fallback|hand.?off)\b.{0,80}",
    re.I,
)
_APPROVER = re.compile(
    r"\b(approv(?:e|er|al)|sign[- ]?off|sponsor|stakeholder|legal|security team|CFO|finance|team lead)\b.{0,60}",
    re.I,
)
_TIMELINE = re.compile(
    r"\b(by\s+(?:end of\s+)?(?:q[1-4]|january|february|march|april|may|june|july|august|september|october|november|december|\d{4})|deadline|go[- ]live|within\s+\d+\s+(?:days?|weeks?|months?)|budget)\b.{0,40}",
    re.I,
)
_USERS = re.compile(
    r"\b(AP team|accounts payable|analysts?|clerks?|operators?|shared services|end users?|business users?)\b.{0,40}",
    re.I,
)
_NO_API = re.compile(r"\b(no api|without an? api|paper|spreadsheet only|one person|tribal knowledge|in someone'?s head)\b", re.I)
_PAPER = re.compile(r"\b(paper|scanned|fax|handwrit)\b", re.I)


def _words_to_num(text: str) -> float | None:
    t = text.lower().replace(",", "").replace("-", " ")
    t = re.sub(r"(\d)\s*k\b", lambda m: m.group(1) + "000", t)
    parts = re.findall(r"[a-z]+|\d+(?:\.\d+)?", t)
    if not parts:
        return None
    if len(parts) == 1 and re.fullmatch(r"\d+(?:\.\d+)?", parts[0]):
        return float(parts[0])
    total, cur, used = 0, 0, False
    i = 0
    while i < len(parts):
        w = parts[i]
        if re.fullmatch(r"\d+(?:\.\d+)?", w):
            cur += float(w)
            used = True
            i += 1
            continue
        if w == "and":
            i += 1
            continue
        if w in _UNITS:
            cur += _UNITS[w]
            used = True
        elif w in _TENS:
            cur += _TENS[w]
            used = True
        elif w == "hundred":
            cur = max(cur, 1) * 100
            used = True
        elif w == "thousand":
            total += max(cur, 1) * 1000
            cur = 0
            used = True
        else:
            break
        i += 1
    if not used:
        return None
    return float(total + cur)


def _norm_volume(raw_num: float, unit_blob: str) -> tuple[float, str]:
    u = unit_blob.lower()
    if re.search(r"\b(per\s+)?week|weekly|/week\b", u):
        per_day = raw_num / 5.0
        return per_day, f"{raw_num:g}/week (~{per_day:.1f}/day, 5 weekdays)"
    if re.search(r"\b(per\s+)?month|monthly|/month\b", u):
        per_day = raw_num / 22.0
        return per_day, f"{raw_num:g}/month (~{per_day:.1f}/day, 22 days)"
    if re.search(r"\b(per\s+)?year|yearly|annual|/year\b", u):
        per_day = raw_num / (22.0 * 12)
        return per_day, f"{raw_num:g}/year (~{per_day:.1f}/day)"
    return raw_num, f"{raw_num:g}/day"


def _snippet(msg: str, start: int, end: int, limit: int = 25) -> str:
    words = msg[max(0, start) : min(len(msg), end)].split()
    return " ".join(words[:limit])[:MAXE]


def _empty_state() -> dict[str, Any]:
    return {"client_name": "", "fields": {}, "wbs": [], "risks": [], "turn": 0}


def _load_state(state: dict[str, Any] | None) -> dict[str, Any]:
    st = _empty_state()
    if not isinstance(state, dict):
        return st
    st["client_name"] = str(state.get("client_name") or "")[:200]
    fields = state.get("fields") if isinstance(state.get("fields"), dict) else {}
    for k, _ in AREAS:
        it = fields.get(k)
        if isinstance(it, dict):
            v = str(it.get("v") or "").strip()[:MAXV]
            p = it.get("p")
            if v and p in PROV:
                st["fields"][k] = {"v": v, "p": p, "e": str(it.get("e") or "")[:MAXE]}
    wbs = state.get("wbs") if isinstance(state.get("wbs"), list) else []
    for w in wbs[:30]:
        if isinstance(w, dict) and w.get("unit") in RATE:
            try:
                q = int(w.get("quantity"))
            except (TypeError, ValueError):
                continue
            st["wbs"].append(
                {"unit": w["unit"], "quantity": min(max(q, 1), 50), "reason": str(w.get("reason") or "")[:200]}
            )
    risks = state.get("risks") if isinstance(state.get("risks"), list) else []
    st["risks"] = [str(r)[:200] for r in risks[:6]]
    try:
        st["turn"] = min(max(int(state.get("turn") or 0), 0), 500)
    except (TypeError, ValueError):
        st["turn"] = 0
    return st


def _set_field(fields: dict[str, Any], key: str, value: str, prov: str, evidence: str) -> bool:
    """Update field if stronger or new. Returns True if written."""
    value = value.strip()[:MAXV]
    evidence = evidence.strip()[:MAXE]
    if not value or key not in LABELS or prov not in PROV:
        return False
    old = fields.get(key)
    if old and STRENGTH[old["p"]] > STRENGTH[prov]:
        return False
    if old and old["p"] == prov and old["v"] == value:
        return False
    fields[key] = {"v": value, "p": prov, "e": evidence}
    return True


def _ok(fields: dict[str, Any], key: str) -> bool:
    f = fields.get(key)
    return bool(f and f["p"] in ("FACT", "INFERENCE"))


def _first_open_critical(fields: dict[str, Any]) -> str | None:
    for k in CRITICAL:
        if not _ok(fields, k):
            return k
    for k, _ in AREAS:
        if not _ok(fields, k):
            return k
    return None


def _extract_volume(msg: str) -> tuple[str, str, float] | None:
    # number / number-words + period
    patterns = [
        r"((?:\d+(?:\.\d+)?|\d+\s*k|[a-z]+(?:[\s-](?:hundred|thousand|[a-z]+)){0,6}))\s*"
        r"(?:invoices?|items?|cases?|tickets?|docs?|documents?|records?)?\s*"
        r"(?:a|/|\s+per\s+)(day|daily|week|weekly|month|monthly|year|yearly|annually)",
        r"((?:\d+(?:\.\d+)?|\d+\s*k))\s*(?:/\s*)(day|week|month|year)",
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.I)
        if not m:
            continue
        num = _words_to_num(m.group(1))
        if num is None or num <= 0:
            continue
        per_day, label = _norm_volume(num, m.group(2))
        return label, _snippet(msg, m.start(), m.end()), per_day
    return None


def _extract(msg: str) -> tuple[list[tuple[str, str, str, str]], list[str], bool]:
    """Returns (updates as key,value,prov,evidence), kill/ask hints, matched_any."""
    updates: list[tuple[str, str, str, str]] = []
    hints: list[str] = []
    matched = False
    low = msg.lower()

    vol = _extract_volume(msg)
    if vol:
        label, ev, _per = vol
        updates.append(("volume_per_day", label, "FACT", ev))
        matched = True

    found_sys = []
    for name in _SYSTEMS:
        if re.search(r"\b" + re.escape(name) + r"\b", msg, re.I):
            found_sys.append(name)
    if found_sys:
        ev = ", ".join(found_sys[:6])
        updates.append(("systems_involved", ", ".join(found_sys), "FACT", ev[:MAXE]))
        matched = True
        if any(s in ("SAP", "Salesforce", "Workday", "NetSuite", "Oracle", "Dynamics") for s in found_sys):
            hints.append("integrations")

    comps = [c for c in _COMPLIANCE if c.lower() in low]
    if comps:
        updates.append(("security_compliance", ", ".join(comps), "FACT", comps[0][:MAXE]))
        matched = True
        hints.append("security")

    formats = []
    for label, pat in _FORMATS:
        m = re.search(pat, msg, re.I)
        if m:
            formats.append(label)
            matched = True
    if formats:
        updates.append(("data_sources_and_formats", ", ".join(formats), "FACT", formats[0]))

    for pat, label in _SUCCESS:
        m = re.search(pat, msg, re.I)
        if m:
            updates.append(("success_metrics", label, "FACT", _snippet(msg, m.start(), m.end())))
            matched = True
            break

    m = _APPROVER.search(msg)
    if m:
        updates.append(("stakeholders_approvers", m.group(0).strip()[:MAXV], "FACT", _snippet(msg, m.start(), m.end())))
        matched = True
        hints.append("approver")

    m = _EXCEPTION.search(msg)
    if m:
        updates.append(("exception_handling", m.group(0).strip()[:MAXV], "INFERENCE", _snippet(msg, m.start(), m.end())))
        matched = True

    m = _TIMELINE.search(msg)
    if m:
        updates.append(("timeline_constraints", m.group(0).strip()[:MAXV], "FACT", _snippet(msg, m.start(), m.end())))
        matched = True

    m = _USERS.search(msg)
    if m:
        updates.append(("users_and_roles", m.group(0).strip()[:MAXV], "FACT", _snippet(msg, m.start(), m.end())))
        matched = True

    m = _GOAL.search(msg)
    if m:
        updates.append(("business_goal", m.group(0).strip()[:MAXV], "INFERENCE", _snippet(msg, m.start(), m.end())))
        matched = True

    m = _PROCESS.search(msg)
    if m and ("process" in low or "manual" in low or "today" in low or "inbox" in low):
        updates.append(("current_process", m.group(0).strip()[:MAXV], "INFERENCE", _snippet(msg, m.start(), m.end())))
        matched = True

    if re.search(r"\bapi\b", msg, re.I):
        updates.append(("integrations_required", "APIs mentioned", "INFERENCE", "API"))
        matched = True

    return updates, hints, matched


def _apply_rate_card(rate_card_json: str) -> tuple[dict[str, int], str, bool]:
    rate = dict(RATE)
    source = "starter"
    bad = False
    raw = (rate_card_json or "").strip()
    if not raw:
        return rate, source, bad
    try:
        obj = json.loads(raw)
        new = {k: int(v) for k, v in obj.items() if k in rate and 0 < int(v) <= 1000}
        if new:
            rate.update(new)
            source = "supplied" if len(new) == len(RATE) else "partly supplied"
        else:
            bad = True
    except Exception:
        bad = True
    return rate, source, bad


def _choose_wbs(fields: dict[str, Any]) -> list[dict[str, Any]]:
    wbs: list[dict[str, Any]] = [
        {"unit": "DISCOVERY_WORKSHOP", "quantity": 1, "reason": "scope workshop"},
        {"unit": "PROCESS_MAPPING", "quantity": 1, "reason": "map current process"},
        {"unit": "WORKFLOW_AUTO_BUILD", "quantity": 1, "reason": "build the Auto"},
        {"unit": "UAT_AND_HYPERCARE", "quantity": 1, "reason": "test and hypercare"},
        {"unit": "TRAINING_HANDOVER", "quantity": 1, "reason": "handover"},
    ]
    sys_v = (fields.get("systems_involved") or {}).get("v", "").lower()
    if any(x in sys_v for x in ("sap", "salesforce", "workday", "netsuite", "oracle", "dynamics")):
        wbs.append({"unit": "INTEGRATION_COMPLEX", "quantity": 1, "reason": "complex system integration"})
    elif sys_v:
        wbs.append({"unit": "INTEGRATION_SIMPLE", "quantity": 1, "reason": "simple integration"})
    fmt = (fields.get("data_sources_and_formats") or {}).get("v", "").lower()
    if any(x in fmt for x in ("pdf", "email", "excel", "csv", "word")):
        wbs.append({"unit": "DOCUMENT_EXTRACTION_AUTO", "quantity": 1, "reason": "document extraction"})
    if _ok(fields, "security_compliance"):
        wbs.append({"unit": "SECURITY_REVIEW", "quantity": 1, "reason": "security review"})
    if _ok(fields, "exception_handling"):
        wbs.append({"unit": "HUMAN_REVIEW_STEP", "quantity": 1, "reason": "exception review step"})
    if _ok(fields, "success_metrics"):
        wbs.append({"unit": "REPORTING_DASHBOARD", "quantity": 1, "reason": "success reporting"})
    # dedupe by unit
    seen: dict[str, dict[str, Any]] = {}
    for w in wbs:
        seen[w["unit"]] = w
    return list(seen.values())


def _volume_per_day(fields: dict[str, Any]) -> float | None:
    f = fields.get("volume_per_day")
    if not f:
        return None
    m = re.search(r"~([\d.]+)/day", f["v"])
    if m:
        return float(m.group(1))
    m = re.search(r"([\d.]+)/day", f["v"])
    if m:
        return float(m.group(1))
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", f["v"])]
    return max(nums) if nums else None


def _verdict(fields: dict[str, Any], msg: str) -> dict[str, Any]:
    reasons: list[str] = []
    kill: list[str] = []
    see: list[str] = []
    low = msg.lower()

    if not (_ok(fields, "business_goal") and _ok(fields, "current_process") and _ok(fields, "volume_per_day")):
        return {"verdict": "UNSURE", "reasons": reasons, "kill_risks": kill, "ask_to_see": see}

    vol = _volume_per_day(fields)
    if vol is not None and vol < 5:
        return {
            "verdict": "DONT_BUILD",
            "reasons": ["Volume is under 5 items a day, so automation is unlikely to pay back."],
            "kill_risks": kill,
            "ask_to_see": see,
        }
    if vol is not None and vol < 20:
        reasons.append("Volume is under 20 items a day, so the build may not pay back.")
        verdict = "UNSURE"
    else:
        verdict = "BUILD"

    if _NO_API.search(msg) or _PAPER.search((fields.get("current_process") or {}).get("v", "")) or _PAPER.search(
        (fields.get("data_sources_and_formats") or {}).get("v", "")
    ):
        verdict = "NOT_YET"
        reasons = (["Process or data looks hard to reach (paper, no API, or knowledge held tightly)."] + reasons)[:3]

    if "security" in low or _ok(fields, "security_compliance"):
        kill.append("IT security: compliance or hosting rules could block it")
    if re.search(r"\blegal\b", low) or "gdpr" in low or "hipaa" in low:
        kill.append("Legal: privacy or regulatory review could stop the project")
    if re.search(r"\bfinance\b|\bcfo\b", low):
        kill.append("Finance: budget or ROI sign-off could stop the project")
    if re.search(r"\bteam lead\b|\bmanager\b", low):
        kill.append("Team lead: day-to-day ownership change could block go-live")

    if "invoice" in low or "pdf" in (fields.get("data_sources_and_formats") or {}).get("v", "").lower():
        see.append("10 real invoices from last week")
    if "email" in low or "mailbox" in low or "outlook" in (fields.get("systems_involved") or {}).get("v", "").lower():
        see.append("the shared mailbox")
    if not see:
        see.append("a sample of the real work items from last week")

    if verdict == "BUILD" and not all(_ok(fields, k) for k in CRITICAL):
        verdict = "UNSURE"
        reasons = (["Not all key areas are confirmed yet."] + reasons)[:3]
    elif verdict == "BUILD":
        reasons = (["Volume, process and goal support a build."] + reasons)[:3]

    return {"verdict": verdict, "reasons": reasons[:3], "kill_risks": kill[:3], "ask_to_see": see[:3]}


def _scrub(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("\u2014", ", ").replace("\u2013", "-")).strip()


def respond(
    message: str,
    state: dict[str, Any] | None,
    rate_card_json: str = "",
    force: bool = False,
    notice: str = "",
) -> dict[str, Any] | None:
    """Answer one chat turn with rules. Returns None when the turn should go to the agent."""
    msg = (message or "").strip()
    if not msg and not force:
        return None

    st = _load_state(state)
    fields = dict(st["fields"])
    notices: list[str] = []
    if notice:
        notices.append(notice[:240])

    rate, rate_source, rate_bad = _apply_rate_card(rate_card_json)
    if rate_bad:
        notices.append("The rate card could not be read, so the starter hours were used.")

    asked = _first_open_critical(fields)
    updates, _hints, matched = _extract(msg) if msg else ([], [], False)
    vague_hit = _VAGUE.search(msg) if msg else None
    # Vague-only answers for the open area
    if vague_hit and asked and len(msg.split()) <= 12:
        quote = vague_hit.group(1)
        _set_field(fields, asked, msg[:MAXV], "ASSUMPTION", "vague")
        matched = True
        reply_q = f'You said "{quote}", which is too vague to build from. {QUESTIONS[asked]}'
        # fall through to build full response with this assumption
        wrote_vague = True
    else:
        wrote_vague = False
        reply_q = ""

    wrote = False
    for key, value, prov, evidence in updates:
        if _set_field(fields, key, value, prov, evidence):
            wrote = True
            matched = True

    if not wrote and not wrote_vague and asked and msg and not matched:
        # Assign the answer to the area we were asking about
        if _set_field(fields, asked, msg[:MAXV], "FACT", _snippet(msg, 0, len(msg))):
            wrote = True
            matched = True

    if not matched and not force and len(msg) >= 60:
        return None

    if not matched and not force and not msg:
        return None

    # Recompute open critical after updates
    asked_next = _first_open_critical(fields)
    fit = _verdict(fields, msg)
    vol = _volume_per_day(fields)
    if vol is not None and vol < 20 and fit["verdict"] == "BUILD":
        fit["verdict"] = "UNSURE"
        fit["reasons"] = (["Volume is under 20 items a day, so the build may not pay back."] + fit["reasons"])[:3]

    total_w = sum(2 if k in CRITICAL else 1 for k, _ in AREAS)
    ok_w = sum(2 if k in CRITICAL else 1 for k, _ in AREAS if _ok(fields, k))
    completeness = round(ok_w / total_w, 2)
    key_confirmed = sum(1 for k in CRITICAL if _ok(fields, k))
    key_total = len(CRITICAL)

    gaps = []
    for k, label in AREAS:
        if _ok(fields, k):
            continue
        status = "MISSING"
        if k in fields:
            status = "VAGUE" if fields[k].get("e") == "vague" else "ASSUMED"
        if status == "VAGUE":
            q = f'You said "{fields[k]["v"][:120]}", which is too vague to build from. {QUESTIONS[k]}'
        elif status == "ASSUMED":
            q = f'Please confirm this assumption: {fields[k]["v"][:150]} {QUESTIONS[k]}'
        else:
            q = QUESTIONS[k]
        gaps.append({"key": k, "label": label, "status": status, "critical": k in CRITICAL, "question": q})

    critical_open = [g for g in gaps if g["critical"]]
    advise = fit["verdict"] in ("NOT_YET", "DONT_BUILD")
    if advise and not critical_open:
        phase = "advise"
    elif critical_open:
        phase = "discover"
    else:
        phase = "propose"

    wbs = _choose_wbs(fields) if _ok(fields, "business_goal") else []
    base = sum(rate[w["unit"]] * w["quantity"] for w in wbs)
    high = round(base * (1.25 + 0.5 * (1 - completeness))) if wbs else 0
    estimate = None
    if wbs:
        estimate = {
            "low_hours": base,
            "high_hours": high,
            "calibrated": rate_source == "supplied",
            "basis": (
                "YOUR RATE CARD"
                if rate_source == "supplied"
                else "STARTER RATE CARD, not calibrated to delivery history"
            ),
        }

    ack_bits = []
    if wrote or wrote_vague:
        for key, value, prov, _ev in updates[:2]:
            ack_bits.append(value[:80])
        if wrote_vague:
            ack_bits.append("that answer was too vague")
    ack = ("Got it: " + "; ".join(ack_bits) + ".") if ack_bits else "Got it."

    if wrote_vague and reply_q:
        reply = _scrub(ack + " " + reply_q)
    elif phase == "discover" and gaps:
        g = gaps[0]
        q = g["question"]
        reply = _scrub(ack + " " + q)
    elif phase == "advise":
        head = "I would not build this at all." if fit["verdict"] == "DONT_BUILD" else "I would not start building yet."
        why = " ".join(r.rstrip(".") + "." for r in fit["reasons"])
        reply = _scrub(f"{ack} To be straight with you: {head} {why} Tell me if I have misread anything.")
    elif phase == "propose":
        goal = fields.get("business_goal", {}).get("v", "this process")
        reply = _scrub(
            f"{ack} Here is what I would propose. Automate {goal} as an Auto with people reviewing exceptions. "
            "Say create the manual when you are ready, or tell me what to change."
        )
    else:
        target = asked_next or "business_goal"
        reply = _scrub(ack + " " + QUESTIONS[target])

    if "\u2014" in reply or "—" in reply:
        reply = reply.replace("\u2014", ", ").replace("—", ", ")

    areas_out = []
    for k, label in AREAS:
        f = fields.get(k)
        areas_out.append(
            {
                "key": k,
                "label": label,
                "status": f["p"] if f else "UNKNOWN",
                "value": f["v"] if f else "",
                "critical": k in CRITICAL,
            }
        )

    new_state = {
        "client_name": st["client_name"],
        "fields": fields,
        "wbs": wbs or st["wbs"],
        "risks": st["risks"],
        "turn": st["turn"] + 1,
    }

    return {
        "reply": reply,
        "phase": phase,
        "agent_version": LOCAL_VERSION,
        "engine": "local",
        "key_confirmed": key_confirmed,
        "key_total": key_total,
        "fit": fit,
        "completeness": completeness,
        "readiness": "NEEDS_CLARIFICATION" if critical_open else "READY_FOR_REVIEW",
        "areas": areas_out,
        "open_questions": [
            {"label": g["label"], "question": g["question"], "critical": g["critical"]} for g in gaps
        ][:6],
        "estimate": estimate,
        "manual": None,
        "notices": notices,
        "state": new_state,
    }
