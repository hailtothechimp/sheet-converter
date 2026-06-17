"""Hybrid converter: traditional staff notation + custom letter notation side by side."""

import sys
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics

from converter import (
    parse_music, Measure, ScoreInfo,
    FONT_NAME, FONT_SIZE, MEASURE_PAD, BAR_LINE_WIDTH,
    _visible_events, _unique_offsets, estimate_measure_width,
    _render_hand_events, _draw_mixed_string, _mixed_string_width,
)


PAGE_W, PAGE_H = LETTER
MARGIN_LEFT = 0.5 * inch
MARGIN_RIGHT = 0.5 * inch
MARGIN_TOP = 0.6 * inch
MARGIN_BOTTOM = 0.5 * inch

CUSTOM_SYS_HEIGHT = 1.4 * inch
PAIR_SPACING = 0.2 * inch
IMG_DPI = 150


STAFF_HEIGHT_TENTHS = 40  # 5 lines, 4 spaces of 10 tenths each
CROP_PAD_TENTHS = 45  # padding above/below each system for notes, accidentals


@dataclass
class SystemBreak:
    page_idx: int
    system_idx: int
    total_on_page: int
    measure_numbers: list[int] = field(default_factory=list)
    y_top_tenths: float = 0.0  # top of system in tenths from page top
    y_bottom_tenths: float = 0.0  # bottom of system in tenths from page top


def _parse_xml_root(musicxml_path: str) -> ET.Element:
    """Parse MusicXML, handling .mxl compressed format."""
    p = Path(musicxml_path)
    if p.suffix.lower() == '.mxl':
        with zipfile.ZipFile(p) as zf:
            for name in zf.namelist():
                if name.endswith('.xml') and not name.startswith('META-INF'):
                    with zf.open(name) as f:
                        return ET.parse(f).getroot()
        raise ValueError("No XML found in .mxl archive")
    return ET.parse(musicxml_path).getroot()


def _get_float(el, tag, ns, default=0.0) -> float:
    child = el.find(f'{ns}{tag}')
    return float(child.text) if child is not None else default


@dataclass
class PageLayout:
    page_h_tenths: float
    page_w_tenths: float
    left_margin_tenths: float
    right_margin_tenths: float


def detect_systems(musicxml_path: str) -> tuple[list[SystemBreak], PageLayout]:
    """Detect system/page breaks and exact vertical positions from MusicXML."""
    root = _parse_xml_root(musicxml_path)

    ns = ''
    if root.tag.startswith('{'):
        ns = root.tag.split('}')[0] + '}'

    # Page layout
    page_layout = root.find(f'.//{ns}page-layout')
    page_h_tenths = _get_float(page_layout, 'page-height', ns, 1596.57)
    page_w_tenths = _get_float(page_layout, 'page-width', ns, 1233.71)
    top_margin_tenths = 85.7143
    left_margin_tenths = 85.7143
    right_margin_tenths = 85.7143
    for pm in root.iter(f'{ns}page-margins'):
        tm = pm.find(f'{ns}top-margin')
        if tm is not None:
            top_margin_tenths = float(tm.text)
        lm = pm.find(f'{ns}left-margin')
        if lm is not None:
            left_margin_tenths = float(lm.text)
        rm = pm.find(f'{ns}right-margin')
        if rm is not None:
            right_margin_tenths = float(rm.text)
        break

    # Walk first part's measures to collect system/page breaks
    parts = list(root.iter(f'{ns}part'))
    systems_data = []  # (page_idx, measure_numbers, top_sys_dist, sys_dist, staff_dist)
    cur_page = 0
    cur_measures = []
    cur_top_sys_dist = None
    cur_sys_dist = None
    cur_staff_dist = 65.0

    if parts:
        for measure in parts[0].iter(f'{ns}measure'):
            num = int(measure.get('number', '0'))
            new_sys = new_page = False
            top_sys_d = sys_d = staff_d = None

            for pr in measure.iter(f'{ns}print'):
                if pr.get('new-system') == 'yes':
                    new_sys = True
                if pr.get('new-page') == 'yes':
                    new_page = True
                sl = pr.find(f'{ns}system-layout')
                if sl is not None:
                    tsd = sl.find(f'{ns}top-system-distance')
                    if tsd is not None:
                        top_sys_d = float(tsd.text)
                    sd = sl.find(f'{ns}system-distance')
                    if sd is not None:
                        sys_d = float(sd.text)
                sfl = pr.find(f'{ns}staff-layout')
                if sfl is not None:
                    sdf = sfl.find(f'{ns}staff-distance')
                    if sdf is not None:
                        staff_d = float(sdf.text)

            if not cur_measures:
                cur_measures = [num]
                cur_top_sys_dist = top_sys_d
                cur_sys_dist = sys_d
                if staff_d is not None:
                    cur_staff_dist = staff_d
                continue

            if new_page:
                if cur_measures:
                    systems_data.append((cur_page, cur_measures,
                                        cur_top_sys_dist, cur_sys_dist, cur_staff_dist))
                cur_page += 1
                cur_measures = [num]
                cur_top_sys_dist = top_sys_d
                cur_sys_dist = sys_d
                if staff_d is not None:
                    cur_staff_dist = staff_d
            elif new_sys:
                if cur_measures:
                    systems_data.append((cur_page, cur_measures,
                                        cur_top_sys_dist, cur_sys_dist, cur_staff_dist))
                cur_measures = [num]
                cur_top_sys_dist = top_sys_d
                cur_sys_dist = sys_d
                if staff_d is not None:
                    cur_staff_dist = staff_d
            else:
                cur_measures.append(num)

    if cur_measures:
        systems_data.append((cur_page, cur_measures,
                             cur_top_sys_dist, cur_sys_dist, cur_staff_dist))

    # Collect extra part staves (parts beyond the first) per system-start measure
    extra_part_info = {}  # measure_number -> list of (num_staves, staff_distance)
    for part in parts[1:]:
        part_staves = 1
        for m in part.iter(f'{ns}measure'):
            for attr in m.iter(f'{ns}attributes'):
                s = attr.find(f'{ns}staves')
                if s is not None:
                    part_staves = int(s.text)
            break
        for m in part.iter(f'{ns}measure'):
            mnum = int(m.get('number', '0'))
            for pr in m.iter(f'{ns}print'):
                for sl in pr.findall(f'{ns}staff-layout'):
                    sd = sl.find(f'{ns}staff-distance')
                    if sd is not None:
                        dist = float(sd.text)
                        extra_part_info.setdefault(mnum, []).append(
                            (part_staves, dist))

    # Calculate exact y positions in tenths
    page_counts = {}
    for pi, *_ in systems_data:
        page_counts[pi] = page_counts.get(pi, 0) + 1

    result = []
    prev_bottom = 0.0
    prev_page = -1
    page_sys_idx = {}

    for pi, mnums, top_sys_d, sys_d, staff_d in systems_data:
        si = page_sys_idx.get(pi, 0)
        page_sys_idx[pi] = si + 1

        if pi != prev_page:
            tsd = top_sys_d if top_sys_d is not None else 170.0
            y_top = top_margin_tenths + tsd
            prev_page = pi
        else:
            sd = sys_d if sys_d is not None else 120.0
            y_top = prev_bottom + sd

        # Base height: first part's staves (e.g. piano grand staff)
        sys_height = STAFF_HEIGHT_TENTHS + staff_d + STAFF_HEIGHT_TENTHS

        # Add height for extra parts visible in this system
        first_measure = mnums[0]
        if first_measure in extra_part_info:
            for num_staves, dist in extra_part_info[first_measure]:
                if dist > 0:
                    sys_height += dist + STAFF_HEIGHT_TENTHS * num_staves

        y_bottom = y_top + sys_height
        prev_bottom = y_bottom

        result.append(SystemBreak(
            page_idx=pi, system_idx=si,
            total_on_page=page_counts[pi],
            measure_numbers=mnums,
            y_top_tenths=y_top,
            y_bottom_tenths=y_bottom,
        ))

    layout = PageLayout(page_h_tenths, page_w_tenths,
                        left_margin_tenths, right_margin_tenths)
    return result, layout


def _crop_system(pdf_doc, sb: SystemBreak,
                 layout: PageLayout) -> tuple[io.BytesIO, int, int]:
    """Crop one system from a PDF page using exact layout positions."""
    if sb.page_idx >= len(pdf_doc):
        raise ValueError(f"PDF has {len(pdf_doc)} pages but system references page {sb.page_idx + 1}")

    page = pdf_doc[sb.page_idx]
    pw, ph = page.rect.width, page.rect.height

    # Convert tenths to PDF points
    sy = ph / layout.page_h_tenths
    sx = pw / layout.page_w_tenths

    y0 = (sb.y_top_tenths - CROP_PAD_TENTHS) * sy
    y1 = (sb.y_bottom_tenths + CROP_PAD_TENTHS) * sy
    x0 = (layout.left_margin_tenths - 15) * sx  # small inset to keep clefs
    x1 = pw - (layout.right_margin_tenths - 40) * sx
    y0 = max(0, y0)
    y1 = min(ph, y1)
    x0 = max(0, x0)
    x1 = min(pw, x1)

    clip = fitz.Rect(x0, y0, x1, y1)
    mat = fitz.Matrix(IMG_DPI / 72, IMG_DPI / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip)

    buf = io.BytesIO(pix.tobytes("png"))
    return buf, pix.width, pix.height


def _render_custom_system(c: canvas.Canvas, measures: list[Measure],
                          sys_top: float, usable_w: float):
    """Render one line of custom notation for the given measures."""
    rh_zone_h = CUSTOM_SYS_HEIGHT / 2
    lh_zone_h = CUSTOM_SYS_HEIGHT / 2

    center_y = sys_top - rh_zone_h
    bar_top = center_y + rh_zone_h * 0.85
    bar_bottom = center_y - lh_zone_h * 0.85

    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(1)
    c.line(MARGIN_LEFT, center_y, MARGIN_LEFT + usable_w, center_y)

    raw_widths = [estimate_measure_width(m) for m in measures]
    total_raw = sum(raw_widths)
    scale = usable_w / total_raw if total_raw > 0 else 1
    widths = [w * scale for w in raw_widths]

    x = MARGIN_LEFT

    for j, m in enumerate(measures):
        mw = widths[j]

        if j > 0:
            c.setStrokeColorRGB(0, 0, 0)
            c.setLineWidth(BAR_LINE_WIDTH)
            c.line(x, bar_bottom, x, bar_top)

        if m.repeat_start:
            c.setFillColorRGB(0, 0, 0)
            c.circle(x + 6, center_y + 6, 2, fill=1)
            c.circle(x + 6, center_y - 6, 2, fill=1)

        if m.volta:
            volta_y = bar_top + 12
            c.setStrokeColorRGB(0, 0, 0)
            c.setLineWidth(1)
            c.line(x, volta_y, x + mw, volta_y)
            if m.volta_start:
                c.line(x, bar_top + 2, x, volta_y)
                c.setFont(FONT_NAME, 7)
                c.setFillColorRGB(0, 0, 0)
                c.drawString(x + 4, volta_y + 2, f"{m.volta}.")
                c.setFont(FONT_NAME, FONT_SIZE)

        inner_left = x + MEASURE_PAD
        inner_w = mw - MEASURE_PAD * 2

        offsets = _unique_offsets(m)
        if len(offsets) <= 1:
            offset_to_x = {o: inner_left + inner_w * 0.3 for o in offsets}
        else:
            offset_to_x = {}
            for oi, o in enumerate(offsets):
                offset_to_x[o] = inner_left + (oi / (len(offsets) - 1)) * inner_w * 0.85

        gap = FONT_SIZE * 0.4
        rh_bottom = center_y + gap
        rh_top = bar_top - FONT_SIZE
        lh_top = center_y - gap - FONT_SIZE
        lh_bottom = bar_bottom

        rh_visible = _visible_events(m.rh_events)
        lh_visible = _visible_events(m.lh_events)
        rh_midis = sorted(set(p[1] for ev in rh_visible for p in ev.pitches))
        lh_midis = sorted(set(p[1] for ev in lh_visible for p in ev.pitches))

        _render_hand_events(c, rh_visible, offset_to_x,
                            all_midis=rh_midis, zone_bottom=rh_bottom, zone_top=rh_top)
        _render_hand_events(c, lh_visible, offset_to_x,
                            all_midis=lh_midis, zone_bottom=lh_bottom, zone_top=lh_top)

        # dynamics
        dyn_y = bar_bottom - 10
        for dm in m.dynamics:
            dm_x = offset_to_x.get(dm.offset, inner_left)
            c.setFont("Arial-Italic", 8)
            c.setFillColorRGB(0.15, 0.15, 0.6)
            c.drawString(dm_x, dyn_y, dm.text)
            c.setFillColorRGB(0, 0, 0)

        for wm in m.wedges:
            wm_x = offset_to_x.get(wm.offset, inner_left)
            label = "cresc." if wm.wedge_type == "cresc" else "dim."
            c.setFont("Arial-Italic", 7)
            c.setFillColorRGB(0.15, 0.15, 0.6)
            c.drawString(wm_x, dyn_y - 9, label)
            c.setFillColorRGB(0, 0, 0)

        if m.repeat_end:
            end_x = x + mw
            c.setStrokeColorRGB(0, 0, 0)
            c.setLineWidth(2)
            c.line(end_x - 2, bar_bottom, end_x - 2, bar_top)
            c.setLineWidth(BAR_LINE_WIDTH)
            c.line(end_x - 6, bar_bottom, end_x - 6, bar_top)
            c.setFillColorRGB(0, 0, 0)
            c.circle(end_x - 10, center_y + 6, 2, fill=1)
            c.circle(end_x - 10, center_y - 6, 2, fill=1)

        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.setFont(FONT_NAME, 6)
        c.drawString(x + 2, bar_top + 4, str(m.number))
        c.setFont(FONT_NAME, FONT_SIZE)
        c.setFillColorRGB(0, 0, 0)

        x += mw

    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(BAR_LINE_WIDTH)
    c.line(x, bar_bottom, x, bar_top)


def render_hybrid_pdf(musicxml_path: str, staff_pdf_path: str,
                      output_path: str, title: str = ""):
    """Render hybrid PDF: each staff system image followed by custom notation."""
    measures, score_info = parse_music(musicxml_path)
    systems, layout = detect_systems(musicxml_path)

    if not systems:
        raise ValueError("No system layout info found in MusicXML")

    measure_lookup = {m.number: m for m in measures}
    pdf_doc = fitz.open(staff_pdf_path)

    c = canvas.Canvas(output_path, pagesize=LETTER)
    usable_w = PAGE_W - MARGIN_LEFT - MARGIN_RIGHT

    pairs = []
    for sb in systems:
        buf, img_w, img_h = _crop_system(pdf_doc, sb, layout)
        scale = usable_w / img_w
        display_h = img_h * scale
        sys_measures = [measure_lookup[n] for n in sb.measure_numbers if n in measure_lookup]
        pairs.append((buf, display_h, sys_measures))

    page_num = 0
    y = PAGE_H - MARGIN_TOP
    first_on_page = True

    for buf, img_display_h, sys_measures in pairs:
        needed = img_display_h + CUSTOM_SYS_HEIGHT + 12 + PAIR_SPACING

        if not first_on_page and (y - needed) < MARGIN_BOTTOM:
            c.showPage()
            page_num += 1
            y = PAGE_H - MARGIN_TOP
            first_on_page = True

        if first_on_page and page_num == 0 and title:
            c.setFont("Arial-Bold", 14)
            c.drawCentredString(PAGE_W / 2, y, title)
            y -= 18
            if score_info:
                parts = []
                if score_info.key_name:
                    parts.append(score_info.key_name)
                if score_info.time_sig:
                    parts.append(score_info.time_sig)
                if score_info.initial_tempo:
                    parts.append(f"♩ = {int(score_info.initial_tempo)}")
                if parts:
                    sub = "   ".join(parts)
                    sw = _mixed_string_width(sub, FONT_NAME, 10)
                    c.setFillColorRGB(0.3, 0.3, 0.3)
                    _draw_mixed_string(c, PAGE_W / 2 - sw / 2, y, sub, FONT_NAME, 10)
                    c.setFillColorRGB(0, 0, 0)
                    y -= 14
            first_on_page = False

        if first_on_page:
            first_on_page = False

        # staff system image
        buf.seek(0)
        img = ImageReader(buf)
        c.drawImage(img, MARGIN_LEFT, y - img_display_h,
                    width=usable_w, height=img_display_h)
        y -= img_display_h

        # separator
        y -= 3
        c.setStrokeColorRGB(0.75, 0.75, 0.75)
        c.setLineWidth(0.5)
        c.line(MARGIN_LEFT, y, PAGE_W - MARGIN_RIGHT, y)
        y -= 3

        # custom notation
        if sys_measures:
            _render_custom_system(c, sys_measures, y, usable_w)
        y -= CUSTOM_SYS_HEIGHT
        y -= PAIR_SPACING

    pdf_doc.close()
    c.save()
