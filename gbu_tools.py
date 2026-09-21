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


def _clean_multiline(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        value = re.sub(r"\s+", " ", raw).strip(" |;,.—-:")
        if value:
            lines.append(value)
    return lines


def normalize_person_name(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^а-яa-z0-9]+", " ", value)
    return " ".join(value.split())


def _plus_30(value: str) -> str:
    t = datetime.strptime(value, "%H:%M") + timedelta(minutes=30)
    return t.strftime("%H:%M")


def _fix_common_ocr(value: str) -> str:
    """Conservative corrections for recurring GBU ritual-table OCR errors."""
    value = value or ""
    replacements = [
        (r"\b(?:КЛАЛБИШЕ|КЛАЛБИЩЕ|КПАЛБИШЕ|КПАПБИНЕ|КПАПЕИШЕ)\b", "кладбище"),
        (r"\bкладби(?:ш|щ)е\b", "кладбище"),
        (r"\bкрематори[йи]\b", "крематорий"),
        (r"\bкоематорий\b", "крематорий"),
        (r"\bНикопо-Архангельск", "Николо-Архангельск"),
        (r"\b(?:ЛОМОЛЕЛОВСКОЕ|ПОМОЛЕЛОВСКОЕ)\b", "ДОМОДЕДОВСКОЕ"),
        (r"\bКапитниковск", "Калитниковск"),
        (r"СУДЕБНЫИ", "СУДЕБНЫЙ"),
        (r"\bOTN\b", "ОТП"),
        (r"\bOTT\]?\s*XP\b", "ОТП ХР"),
    ]
    for pattern, repl in replacements:
        value = re.sub(pattern, repl, value, flags=re.I)
    # OТП is sometimes recognized with Latin O/P.
    value = re.sub(r"\b[ОO0]Т[ПP]\b", "ОТП", value, flags=re.I)
    return value


def normalize_gbu_route(value: str) -> str:
    raw_lines = [x for x in (value or "").replace("\r", "").split("\n") if x.strip()]
    if not raw_lines:
        return ""

    # Tesseract often wraps one long route step into two visual lines. The
    # semicolon printed in the source table marks the real end of a step, so
    # rebuild steps using that punctuation instead of treating every OCR line
    # as a separate route point.
    steps: list[str] = []
    pending = ""
    for raw in raw_lines:
        fixed = _fix_common_ocr(re.sub(r"\s+", " ", raw).strip())
        ends_step = bool(re.search(r"[;:]\s*$", fixed))
        fixed = fixed.strip(" |;,.—-:")
        if not fixed:
            continue
        pending = (pending + " " + fixed).strip() if pending else fixed
        if ends_step:
            steps.append(pending)
            pending = ""
    if pending:
        steps.append(pending)

    if len(steps) >= 2:
        first = normalize_person_name(steps[0])
        second = normalize_person_name(steps[1])
        if first and (first == second or second.startswith(first + " ")):
            steps = steps[1:]

    return "; ".join(steps).strip(" ;,.—-:")


def normalize_gbu_person(value: str) -> str:
    lines = [_fix_common_ocr(x) for x in _clean_multiline(value)]
    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip(" |;,.—-:")
    return text


def _cluster_positions(values) -> list[int]:
    clusters: list[list[int]] = []
    for raw in values:
        value = int(raw)
        if not clusters or value > clusters[-1][-1] + 1:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    return [int(round(sum(c) / len(c))) for c in clusters]


def _detect_table_grid(image: Image.Image) -> tuple[list[int], list[int]] | None:
    """Detect the actual spreadsheet grid instead of relying on percentages.

    The GBU screenshots have long black vertical/horizontal separators. Reading
    those separators makes OCR resilient to image width, cropping and the
    optional red service-code column on the right.
    """
    gray = image.convert("L")
    width, height = gray.size
    pixels = gray.load()

    # Count dark pixels for every x/y. Pure Python keeps the Railway image small
    # (no OpenCV/numpy dependency is required).
    vertical_counts = []
    for x in range(width):
        count = 0
        for y in range(height):
            if pixels[x, y] < 170:
                count += 1
        vertical_counts.append(count)

    horizontal_counts = []
    for y in range(height):
        count = 0
        for x in range(width):
            if pixels[x, y] < 170:
                count += 1
        horizontal_counts.append(count)

    # Vertical borders normally cover the table body (well over half the image
    # height in supplied GBU screenshots); horizontal borders span almost the
    # full image width.
    vx = [x for x, count in enumerate(vertical_counts) if count >= max(20, int(height * 0.42))]
    hy = [y for y, count in enumerate(horizontal_counts) if count >= max(50, int(width * 0.55))]

    xs = _cluster_positions(vx)
    ys = _cluster_positions(hy)

    # The left outer border in these screenshots can sit exactly on x=0 and be
    # clipped/anti-aliased. Reconstruct it when the first detected separator is
    # the end of the first service column.
    if len(xs) >= 2 and xs[0] > width * 0.025:
        first_gap = xs[1] - xs[0]
        # In the standard export the clipped left border would sit roughly one
        # first-column width before the first detected separator. Only rebuild
        # x=0 when that geometry matches; this avoids mistaking an outer margin
        # in a photographed table for the first service column.
        if first_gap > 0 and abs(xs[0] - first_gap) <= first_gap * 0.22:
            xs.insert(0, 0)
    elif xs and xs[0] <= width * 0.02:
        xs[0] = 0

    # Need: service1 | service2 | team | time | route | deceased | agent.
    # That is at least 8 x-boundaries (the right service-code column is optional).
    if len(xs) < 8 or len(ys) < 2:
        return None

    return xs, ys


def _prepare_cell(crop: Image.Image, scale: int = 4) -> Image.Image:
    width, height = crop.size
    # Trim grid borders so Tesseract does not mistake them for letters/digits.
    # Three pixels proved more stable than a proportional crop on the actual
    # GBU exports: it removes the grid while keeping the first Cyrillic glyph.
    pad_x = 3 if width > 10 else 1
    pad_y = 3 if height > 10 else 1
    if width > pad_x * 2 + 2 and height > pad_y * 2 + 2:
        crop = crop.crop((pad_x, pad_y, width - pad_x, height - pad_y))
    crop = ImageOps.autocontrast(crop.convert("L"))
    return crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)


def _ocr_cell(crop: Image.Image, lang: str, psm: int = 6) -> str:
    return pytesseract.image_to_string(
        _prepare_cell(crop),
        lang=lang,
        config=f"--psm {psm}",
    ).strip()


def _parse_time_cell(value: str) -> str | None:
    value = (value or "").replace(".", ":")
    match = re.search(r"(?<!\d)(\d{1,2})\s*[:\-]\s*(\d{2})(?::\d{2})?", value)
    if not match:
        # Tesseract occasionally drops the separator but still gives HHMM.
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 4:
            match_digits = digits[:4]
            hour, minute = int(match_digits[:-2]), int(match_digits[-2:])
        else:
            return None
    else:
        hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _team_size_from_cell(crop: Image.Image, lang: str) -> int:
    # Source of truth: green fill = 6 people, white/pale fill = 4 people.
    rgb = crop.convert("RGB")
    total = max(1, rgb.width * rgb.height)
    greenish = 0
    for r, g, b in rgb.getdata():
        if g > r + 15 and g > b + 10 and g > 80:
            greenish += 1
    if greenish / total > 0.12:
        return 6

    # OCR is only a fallback/sanity check for monochrome/recompressed images.
    team_text = clean_cell_text(_ocr_cell(crop, lang, 6))
    if re.search(r"\b6\b", team_text):
        return 6
    return 4


def _parse_gbu_by_grid(original_rgb: Image.Image, work_date: str, lang: str) -> list[dict]:
    grid = _detect_table_grid(original_rgb)
    if grid is None:
        return []
    xs, ys = grid

    # After reconstructing optional x=0, fixed logical column indices are exact:
    # 0 service, 1 service, 2 team, 3 time, 4 route, 5 deceased, 6 agent,
    # optional 7 right-side service code. We deliberately ignore service columns.
    if len(xs) < 8:
        return []

    drafts: list[dict] = []
    for top, bottom in zip(ys[:-1], ys[1:]):
        if bottom - top < 22:
            continue

        def cell(index: int) -> Image.Image:
            return original_rgb.crop((xs[index], top, xs[index + 1], bottom))

        arrival = _parse_time_cell(_ocr_cell(cell(3), lang, 7))
        if not arrival:
            # A row without a valid time is not an order row.
            continue

        route_raw = _ocr_cell(cell(4), lang, 6)
        deceased_raw = _ocr_cell(cell(5), lang, 6)
        agent_raw = _ocr_cell(cell(6), lang, 6)

        deceased = normalize_gbu_person(deceased_raw)
        agent = normalize_gbu_person(agent_raw)
        route = normalize_gbu_route(route_raw)
        team_size = _team_size_from_cell(cell(2), lang)

        if len(deceased) < 4:
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

    return drafts


def _fallback_parse_gbu(file_bytes: bytes, work_date: str, lang: str) -> tuple[str, list[dict]]:
    """Legacy fallback for a photo where spreadsheet grid lines are not detectable."""
    original_rgb = Image.open(BytesIO(file_bytes)).convert("RGB")
    original = original_rgb.convert("L")
    image = ImageOps.autocontrast(original).resize((original.width * 2, original.height * 2))

    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config="--psm 11",
        output_type=pytesseract.Output.DICT,
    )

    time_rows: list[tuple[str, float]] = []
    for i, txt in enumerate(data.get("text", [])):
        txt = (txt or "").strip()
        if not txt:
            continue
        time_value = _parse_time_cell(txt)
        if not time_value:
            continue
        x_center = data["left"][i] + data["width"][i] / 2
        if not (0.24 * image.width <= x_center <= 0.38 * image.width):
            continue
        y_center = data["top"][i] + data["height"][i] / 2
        time_rows.append((time_value, y_center))

    time_rows.sort(key=lambda x: x[1])
    deduped: list[tuple[str, float]] = []
    for item in time_rows:
        if deduped and abs(item[1] - deduped[-1][1]) < max(18, image.height * 0.025):
            continue
        deduped.append(item)
    time_rows = deduped

    if not time_rows:
        raw = pytesseract.image_to_string(image, lang=lang, config="--psm 6")
        return raw, []

    ys = [y for _, y in time_rows]
    row_bounds: list[tuple[int, int]] = []
    header_floor = int(image.height * 0.18)
    for idx, y in enumerate(ys):
        lo = header_floor if idx == 0 else int((ys[idx - 1] + y) / 2)
        hi = image.height if idx == len(ys) - 1 else int((y + ys[idx + 1]) / 2)
        row_bounds.append((max(0, lo), min(image.height, hi)))

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
            values[key] = _ocr_cell(crop, lang, 6)

        team_size = 4
        scale_y = original_rgb.height / image.height
        orig_lo, orig_hi = int(lo * scale_y), int(hi * scale_y)
        team_crop = original_rgb.crop((
            int(0.15 * original_rgb.width), orig_lo,
            int(0.27 * original_rgb.width), orig_hi,
        ))
        team_size = _team_size_from_cell(team_crop, lang)

        deceased = normalize_gbu_person(values["deceased"])
        agent = normalize_gbu_person(values["agent"])
        route = normalize_gbu_route(values["route"])
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


def parse_gbu_table_image(file_bytes: bytes, work_date: str, lang: str = "rus+eng") -> tuple[str, list[dict]]:
    """Parse the GBU spreadsheet by its real cells, like a human reads it.

    Rules agreed with LEGION:
    - first two columns: ignore;
    - team column: white = 4 Standard, green = 6 Standard;
    - time column = arrival / «Быть»;
    - issue time = +30 minutes;
    - then read Route, Deceased, Agent from their own cells;
    - right-side red service-code column: ignore.
    """
    original_rgb = Image.open(BytesIO(file_bytes)).convert("RGB")
    drafts = _parse_gbu_by_grid(original_rgb, work_date, lang)

    # Keep raw OCR for the review accordion in Mini App.
    preview = ImageOps.autocontrast(original_rgb.convert("L")).resize(
        (original_rgb.width * 2, original_rgb.height * 2),
        Image.Resampling.LANCZOS,
    )
    raw = pytesseract.image_to_string(preview, lang=lang, config="--psm 6")

    if drafts:
        return raw, drafts

    return _fallback_parse_gbu(file_bytes, work_date, lang)


def best_agent_match(agent_name: str, contacts: list[tuple[str, str]]) -> tuple[str, str, float] | None:
    target = normalize_person_name(agent_name)
    if not target:
        return None
    best = None
    best_quality = -10**9
    for full_name, phone in contacts:
        norm = normalize_person_name(full_name)
        score = SequenceMatcher(None, target, norm).ratio()
        t_parts, n_parts = target.split(), norm.split()
        if t_parts and n_parts and t_parts[0] == n_parts[0]:
            score += 0.08
        if len(t_parts) > 1 and len(n_parts) > 1 and t_parts[1] == n_parts[1]:
            score += 0.04

        # If two directory rows normalize to the same person (e.g. an old OCR
        # row with an accidental dot and a corrected row), prefer the cleaner
        # human-readable spelling.
        punctuation = len(re.findall(r"[^А-Яа-яЁёA-Za-z0-9\s-]", str(full_name)))
        quality = -punctuation
        if best is None or score > best[2] + 1e-9 or (abs(score - best[2]) <= 1e-9 and quality > best_quality):
            best = (full_name, phone, score)
            best_quality = quality
    return best
