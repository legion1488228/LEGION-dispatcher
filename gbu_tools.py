from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO

from PIL import Image, ImageOps
import pytesseract


def clean_cell_text(text: str) -> str:
    text = (text or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip(" |;.,")
    return text


def normalize_person_name(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^а-яa-z0-9]+", " ", value)
    return " ".join(value.split())


def _plus_30(value: str) -> str:
    t = datetime.strptime(value, "%H:%M") + timedelta(minutes=30)
    return t.strftime("%H:%M")




def normalize_gbu_route(value: str) -> str:
    route = clean_cell_text(value)
    if not route:
        return route
    parts = [clean_cell_text(x) for x in re.split(r"[;]+", route) if clean_cell_text(x)]
    if len(parts) >= 2:
        a = normalize_person_name(parts[0])
        b = normalize_person_name(parts[1])
        if a and (a in b or b.startswith(a)):
            parts = parts[1:]
    route = "; ".join(parts)
    route = re.sub(r"\bОТП\b", "отпевание", route, flags=re.I)
    route = re.sub(r"\bХР\b", "храм", route, flags=re.I)
    route = re.sub(r"\s+", " ", route).strip(" ;,.—-")
    if route and not route.casefold().startswith("морг"):
        route = "Морг " + route
    return route

def _ocr(crop: Image.Image, lang: str, psm: int = 6) -> str:
    return clean_cell_text(
        pytesseract.image_to_string(crop, lang=lang, config=f"--psm {psm}")
    )


def parse_gbu_table_image(file_bytes: bytes, work_date: str, lang: str = "rus+eng") -> tuple[str, list[dict]]:
    """Parse the specific GBU table layout used by LEGION.

    The first two columns are deliberately ignored. The useful columns are:
    team size, arrival time, route, deceased, agent.
    Arrival is the source time; issue time is +30 minutes.
    4 and 6 are always Standard in this GBU flow.
    """
    original_rgb = Image.open(BytesIO(file_bytes)).convert("RGB")
    original = original_rgb.convert("L")
    # Upscale + autocontrast improves faint spreadsheet screenshots substantially.
    image = ImageOps.autocontrast(original).resize((original.width * 2, original.height * 2))

    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config="--psm 11",
        output_type=pytesseract.Output.DICT,
    )

    time_rows: list[tuple[str, float]] = []
    raw_words: list[str] = []
    for i, txt in enumerate(data.get("text", [])):
        txt = (txt or "").strip()
        if not txt:
            continue
        raw_words.append(txt)
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", txt)
        if not m:
            continue
        h, minute = int(m.group(1)), int(m.group(2))
        if h > 23 or minute > 59:
            continue
        x_center = data["left"][i] + data["width"][i] / 2
        # Useful time column is approximately 27-35% of this table.
        if not (0.24 * image.width <= x_center <= 0.38 * image.width):
            continue
        y_center = data["top"][i] + data["height"][i] / 2
        time_rows.append((f"{h:02d}:{minute:02d}", y_center))

    # Deduplicate OCR duplicates by nearby y-coordinate.
    time_rows.sort(key=lambda x: x[1])
    deduped: list[tuple[str, float]] = []
    for item in time_rows:
        if deduped and abs(item[1] - deduped[-1][1]) < max(18, image.height * 0.025):
            continue
        deduped.append(item)
    time_rows = deduped

    if not time_rows:
        # Return full OCR text so the dispatcher can still inspect/manual-enter.
        raw = pytesseract.image_to_string(image, lang=lang, config="--psm 6")
        return raw, []

    ys = [y for _, y in time_rows]
    row_bounds: list[tuple[int, int]] = []
    header_floor = int(image.height * 0.18)
    for idx, y in enumerate(ys):
        lo = header_floor if idx == 0 else int((ys[idx - 1] + y) / 2)
        hi = image.height if idx == len(ys) - 1 else int((y + ys[idx + 1]) / 2)
        row_bounds.append((max(0, lo), min(image.height, hi)))

    # Relative columns derived from the stable spreadsheet layout.
    columns = {
        "team": (0.15, 0.27),
        "route": (0.35, 0.64),
        "deceased": (0.64, 0.81),
        "agent": (0.81, 1.00),
    }

    drafts: list[dict] = []
    for (arrival, _), (lo, hi) in zip(time_rows, row_bounds):
        values: dict[str, str] = {}
        for key, (left, right) in columns.items():
            crop = image.crop((int(left * image.width), lo, int(right * image.width), hi))
            values[key] = _ocr(crop, lang, 6)

        # In the GBU spreadsheet the cell colour is the source of truth:
        # white = 4 people, green = 6 people ("колода").
        team_size = 4

        # Colour classification is more reliable than OCR when the digit is faint.
        scale_y = original_rgb.height / image.height
        orig_lo, orig_hi = int(lo * scale_y), int(hi * scale_y)
        team_crop = original_rgb.crop((
            int(0.15 * original_rgb.width), orig_lo,
            int(0.27 * original_rgb.width), orig_hi,
        ))
        pixels = list(team_crop.getdata())
        if pixels:
            greenish = sum(1 for r, g, b in pixels if g > r + 15 and g > b + 10 and g > 80)
            if greenish / len(pixels) > 0.15:
                team_size = 6
        deceased = clean_cell_text(values["deceased"])
        agent = clean_cell_text(values["agent"])
        route = normalize_gbu_route(values["route"])

        # Skip obvious header/garbage rows.
        if not deceased or len(deceased) < 4:
            continue

        drafts.append({
            "work_date": work_date,
            "arrival_time": arrival,
            "issue_time": _plus_30(arrival),
            "deceased_name": deceased,
            "route": route,
            "category": "standard",
            "team_size": team_size,
            "organization": "ГБУ",
            "agent_name": agent,
            "agent_phone": "",
            "notes": "",
        })

    raw = pytesseract.image_to_string(image, lang=lang, config="--psm 6")
    return raw, drafts


def best_agent_match(agent_name: str, contacts: list[tuple[str, str]]) -> tuple[str, str, float] | None:
    target = normalize_person_name(agent_name)
    if not target:
        return None
    best = None
    for full_name, phone in contacts:
        norm = normalize_person_name(full_name)
        score = SequenceMatcher(None, target, norm).ratio()
        # Surname + first-name agreement gets a small boost.
        t_parts, n_parts = target.split(), norm.split()
        if t_parts and n_parts and t_parts[0] == n_parts[0]:
            score += 0.08
        if len(t_parts) > 1 and len(n_parts) > 1 and t_parts[1] == n_parts[1]:
            score += 0.04
        if best is None or score > best[2]:
            best = (full_name, phone, score)
    return best
