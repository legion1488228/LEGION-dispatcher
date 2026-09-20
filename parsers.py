from __future__ import annotations

import re
from datetime import datetime, timedelta


def _norm_time(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().replace(".", ":")
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", value)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def _date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _minus_30(time_str: str | None) -> str | None:
    if not time_str:
        return None
    t = datetime.strptime(time_str, "%H:%M") - timedelta(minutes=30)
    return t.strftime("%H:%M")


def parse_agent_ready_form(text: str, organization_profile: str = "") -> dict:
    """Heuristic parser for the concrete formats discussed for LEGION.

    Returns a draft. The bot must always show the result to the agent for confirmation.
    """
    raw = text.replace("\r", "")
    low = raw.lower()
    profile = (organization_profile or "").lower().strip()

    # Date: prefer a date after 'выдача'.
    dates = re.findall(r"\b\d{2}\.\d{2}\.\d{2,4}\b", raw)
    work_date = None
    m = re.search(r"выдача\s+(\d{2}\.\d{2}\.\d{2,4})", low, re.I)
    if m:
        work_date = _date(m.group(1))
    elif dates:
        work_date = _date(dates[-1])

    # Deceased full name.
    deceased = ""
    m = re.search(r"(?:ум\.?|умерш(?:ий|ая)?)\s*[:.]?\s*([^\n]+)", raw, re.I)
    if m:
        deceased = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")

    # Issue time and arrival time.
    issue = None
    for pat in [
        r"выдача\s+смэ[^\n]*?(\d{1,2}[:.]\d{2})",
        r"выдача[^\n]*?\sв\s*(\d{1,2}[:.]\d{2})",
        r"выдача[^\n]*?(\d{1,2}[:.]\d{2})",
    ]:
        mm = re.search(pat, low, re.I)
        if mm:
            issue = _norm_time(mm.group(1)); break

    arrival = None
    for pat in [
        r"подача[^\n]*?(\d{1,2}[:.]\d{2})",
        r"доставка\s+к\s*(\d{1,2}[:.]\d{2})",
    ]:
        mm = re.search(pat, low, re.I)
        if mm:
            arrival = _norm_time(mm.group(1)); break
    if not arrival:
        arrival = _minus_30(issue)

    # Team size.
    team_size = None
    mm = re.search(r"(?:бригада\s+сопровождения|грузчики)[^\n]*?(\d+)\s*чел", low, re.I)
    if not mm:
        mm = re.search(r"\b([46])\s*чел", low, re.I)
    if mm:
        team_size = int(mm.group(1))

    # Category.
    category = "standard"
    if re.search(r"\bвип\b|\bvip\b", low, re.I):
        category = "vip"
    elif re.search(r"\bэлит\b|\belite\b", low, re.I):
        category = "elite"
    elif re.search(r"(?:грузчики|бригада)[^\n]*\bст\b", low, re.I):
        category = "standard"

    # Route: explicit Route wins. Otherwise build from meaningful ceremony lines.
    route = ""
    mm = re.search(r"маршрут\s*:\s*([^\n]+)", raw, re.I)
    if mm:
        route = mm.group(1).strip()
    else:
        parts = []
        for line in raw.splitlines():
            l = line.strip()
            ll = l.lower()
            if not l:
                continue
            if any(key in ll for key in ["грузимся", "подача смэ", "выдача смэ", "отпевание", "далее кладбище", "обратка"]):
                parts.append(l)
        route = " → ".join(parts)

    agent_name = ""
    agent_phone = ""
    mm = re.search(r"агент\s+([^\n]+)", raw, re.I)
    if mm:
        agent_name = mm.group(1).strip()
    phones = re.findall(r"(?:\+7|8)[\d\-\s()]{9,20}", raw)
    if phones:
        agent_phone = re.sub(r"\s+", "", phones[-1]).strip()

    organization = organization_profile.strip()
    kickback = 0
    if "марин" in profile:
        organization = organization or "Марина контора"
        if team_size == 4:
            kickback = 1000
        elif team_size == 6:
            kickback = 1500
    elif "рамен" in profile:
        organization = organization or "Раменское"

    return {
        "work_date": work_date,
        "deceased_name": deceased,
        "issue_time": issue,
        "arrival_time": arrival,
        "route": route,
        "category": category,
        "team_size": team_size,
        "organization": organization,
        "agent_name": agent_name,
        "agent_phone": agent_phone,
        "kickback_rub": kickback,
        "raw_text": text,
    }


def parse_gbu_ocr_text(text: str) -> list[dict]:
    """Best-effort GBU OCR parser.

    GBU screenshots vary, so this intentionally returns drafts. A dispatcher reviews
    every draft before saving. Lines with a time and a name-like fragment become drafts.
    """
    drafts = []
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    for line in lines:
        tm = re.search(r"\b(\d{1,2}[:.]\d{2})\b", line)
        dt = re.search(r"\b(\d{2}\.\d{2}\.\d{2,4})\b", line)
        if not tm:
            continue
        # Remove obvious date/time/phone/numeric tokens and keep a candidate title.
        candidate = re.sub(r"\b\d{2}\.\d{2}\.\d{2,4}\b", " ", line)
        candidate = re.sub(r"\b\d{1,2}[:.]\d{2}\b", " ", candidate)
        candidate = re.sub(r"\b\d+\b", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" |-;,")
        if len(candidate) < 3:
            continue
        drafts.append({
            "work_date": _date(dt.group(1)) if dt else None,
            "issue_time": _norm_time(tm.group(1)),
            "deceased_name": candidate[:180],
            "source": "gbu",
            "team_size": 4,
            "category": "standard",
            "route": "",
        })
    return drafts
