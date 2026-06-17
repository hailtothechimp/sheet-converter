"""
Sheet music to custom notation converter.

Reads MusicXML and produces a PDF where:
- A horizontal line separates right hand (above) from left hand (below)
- Notes are written as letter names with accidentals
- Sequential notes are spaced horizontally within each measure
- Vertical position reflects relative pitch (higher pitch = higher on page)
- Simultaneous notes (chords) share horizontal position, stacked by pitch
- Bar lines separate measures
- Dynamics, tempo, key/time signatures, and volta brackets are shown
"""

import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from music21 import (
    converter as m21_converter, stream, note, chord, harmony,
    key as m21_key, meter, tempo as m21_tempo, dynamics as m21_dynamics,
    spanner, bar, expressions,
)

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


@dataclass
class NoteEvent:
    offset: float
    duration: float
    pitches: list  # list of (name_str, midi_number, tie_type|None)
    is_rest: bool = False


@dataclass
class DynamicMark:
    offset: float
    text: str  # "pp", "p", "mp", "mf", "f", "ff", "fff", etc.


@dataclass
class WedgeMark:
    offset: float
    wedge_type: str  # "cresc" or "dim"
    duration: float  # length in quarter notes


@dataclass
class TempoMark:
    offset: float
    bpm: float
    text: str | None = None  # "rit.", "accel.", etc.


@dataclass
class VoltaBracket:
    number: str  # "1", "2", etc.
    start_measure: int
    end_measure: int


@dataclass
class Measure:
    rh_events: list[NoteEvent] = field(default_factory=list)
    lh_events: list[NoteEvent] = field(default_factory=list)
    number: int = 0
    repeat_start: bool = False
    repeat_end: bool = False
    repeat_times: int | None = None
    dynamics: list[DynamicMark] = field(default_factory=list)
    wedges: list[WedgeMark] = field(default_factory=list)
    tempos: list[TempoMark] = field(default_factory=list)
    volta: str | None = None  # "1", "2", etc. if this measure is under a volta bracket
    volta_start: bool = False  # True if this is the first measure of the volta


@dataclass
class ScoreInfo:
    key_name: str = ""  # e.g. "E♭ major"
    time_sig: str = ""  # e.g. "4/4"
    initial_tempo: float | None = None


def format_pitch_name(p) -> str:
    name = p.name
    name = name.replace("-", "♭").replace("#", "♯")
    return name


def _format_key_name(ks) -> str:
    """Format a key signature for display."""
    try:
        k = ks.asKey()
        name = str(k.tonic.name).replace("-", "♭").replace("#", "♯")
        mode = k.mode
        return f"{name} {mode}"
    except Exception:
        n = ks.sharps
        if n == 0:
            return "C major"
        elif n > 0:
            return f"{n}♯"
        else:
            return f"{abs(n)}♭"


def extract_events_from_part(part) -> list[list[NoteEvent]]:
    measures_out = []
    for m in part.getElementsByClass(stream.Measure):
        events = []
        for el in m.recurse().notesAndRests:
            if isinstance(el, harmony.ChordSymbol):
                continue
            if isinstance(el, chord.Chord):
                pitches = []
                for p in el.pitches:
                    t = None
                    if hasattr(el, 'tie') and el.tie is not None:
                        t = el.tie.type
                    pitches.append((format_pitch_name(p), p.midi, t))
                pitches.sort(key=lambda x: x[1])
                events.append(NoteEvent(
                    offset=el.offset,
                    duration=el.quarterLength,
                    pitches=pitches,
                ))
            elif isinstance(el, note.Note):
                t = None
                if el.tie is not None:
                    t = el.tie.type
                events.append(NoteEvent(
                    offset=el.offset,
                    duration=el.quarterLength,
                    pitches=[(format_pitch_name(el.pitch), el.pitch.midi, t)],
                ))

        events.sort(key=lambda e: (e.offset, -e.pitches[0][1] if e.pitches else 0))

        merged = []
        i = 0
        while i < len(events):
            current = events[i]
            j = i + 1
            while j < len(events) and abs(events[j].offset - current.offset) < 0.01:
                for p in events[j].pitches:
                    if (p[0], p[1]) not in [(x[0], x[1]) for x in current.pitches]:
                        current.pitches.append(p)
                j += 1
            current.pitches.sort(key=lambda x: x[1])
            merged.append(current)
            i = j

        measures_out.append(merged)
    return measures_out


def extract_measure_metadata(part) -> dict[int, dict]:
    """Extract dynamics, wedges, and tempos per measure."""
    info: dict[int, dict] = {}
    for m_obj in part.getElementsByClass(stream.Measure):
        mnum = m_obj.number
        data: dict = {"dynamics": [], "wedges": [], "tempos": []}

        for d in m_obj.recurse().getElementsByClass(m21_dynamics.Dynamic):
            data["dynamics"].append(DynamicMark(offset=d.offset, text=d.value))

        for w in m_obj.recurse().getElementsByClass(m21_dynamics.DynamicWedge):
            wtype = "cresc" if "Crescendo" in type(w).__name__ else "dim"
            dur = w.quarterLength if hasattr(w, 'quarterLength') else 2.0
            data["wedges"].append(WedgeMark(offset=w.offset, wedge_type=wtype, duration=dur))

        for t in m_obj.recurse().getElementsByClass(m21_tempo.MetronomeMark):
            bpm = t.getQuarterBPM()
            text = t.text if t.text else None
            if bpm:
                data["tempos"].append(TempoMark(offset=t.offset, bpm=bpm, text=text))

        # repeat barlines
        rep = {}
        if m_obj.leftBarline and 'repeat' in str(getattr(m_obj.leftBarline, 'type', '')).lower():
            rep['repeat_start'] = True
        if m_obj.rightBarline and 'repeat' in str(getattr(m_obj.rightBarline, 'type', '')).lower():
            rep['repeat_end'] = True
        rb = m_obj.rightBarline
        if rb and hasattr(rb, 'times') and rb.times:
            rep['repeat_times'] = rb.times
        data.update(rep)

        if any(data[k] for k in ("dynamics", "wedges", "tempos")) or rep:
            info[mnum] = data

    return info


def extract_voltas(score) -> list[VoltaBracket]:
    """Extract volta (repeat ending) brackets."""
    voltas = []
    seen = set()
    for v in score.recurse().getElementsByClass(spanner.RepeatBracket):
        spanned = v.getSpannedElements()
        if not spanned:
            continue
        measures = []
        for el in spanned:
            m = el.getContextByClass(stream.Measure)
            if m:
                measures.append(m.number)
        if not measures:
            continue
        key = (v.number, min(measures), max(measures))
        if key in seen:
            continue
        seen.add(key)
        voltas.append(VoltaBracket(
            number=str(v.number) if v.number else "1",
            start_measure=min(measures),
            end_measure=max(measures),
        ))
    return voltas


def parse_music(filepath: str) -> tuple[list[Measure], ScoreInfo]:
    score = m21_converter.parse(filepath)
    parts = list(score.parts)

    if len(parts) >= 2:
        rh_part, lh_part = parts[0], parts[1]
    elif len(parts) == 1:
        rh_part, lh_part = parts[0], None
    else:
        raise ValueError("No parts found in the score.")

    rh_measures = extract_events_from_part(rh_part)
    lh_measures = extract_events_from_part(lh_part) if lh_part else []

    metadata = extract_measure_metadata(rh_part)
    voltas = extract_voltas(score)

    # build volta lookup: measure_number -> (volta_number, is_first_measure)
    volta_lookup: dict[int, tuple[str, bool]] = {}
    for v in voltas:
        for mn in range(v.start_measure, v.end_measure + 1):
            volta_lookup[mn] = (v.number, mn == v.start_measure)

    # score-level info
    info = ScoreInfo()
    ks_list = list(rh_part.recurse().getElementsByClass(m21_key.KeySignature))
    if ks_list:
        info.key_name = _format_key_name(ks_list[0])
    ts_list = list(rh_part.recurse().getElementsByClass(meter.TimeSignature))
    if ts_list:
        info.time_sig = ts_list[0].ratioString

    num_measures = max(len(rh_measures), len(lh_measures))
    measures = []
    for i in range(num_measures):
        m = Measure(number=i + 1)
        if i < len(rh_measures):
            m.rh_events = rh_measures[i]
        if i < len(lh_measures):
            m.lh_events = lh_measures[i]

        md = metadata.get(i + 1, {})
        m.dynamics = md.get("dynamics", [])
        m.wedges = md.get("wedges", [])
        m.tempos = md.get("tempos", [])
        m.repeat_start = md.get("repeat_start", False)
        m.repeat_end = md.get("repeat_end", False)
        m.repeat_times = md.get("repeat_times", None)
        vinfo = volta_lookup.get(i + 1)
        m.volta = vinfo[0] if vinfo else None
        m.volta_start = vinfo[1] if vinfo else False

        if i == 0 and m.tempos:
            info.initial_tempo = m.tempos[0].bpm

        measures.append(m)

    return measures, info


# ── PDF Rendering ──

PAGE_W, PAGE_H = LETTER
MARGIN_LEFT = 0.6 * inch
MARGIN_RIGHT = 0.5 * inch
MARGIN_TOP = 0.6 * inch
MARGIN_BOTTOM = 0.6 * inch

SYSTEM_HEIGHT = 1.6 * inch
SYSTEM_SPACING = 0.25 * inch

def _font_path(filename: str) -> str:
    """Resolve font path — works both in dev and PyInstaller bundle."""
    import os
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, filename)
    return f"C:/Windows/Fonts/{filename}"

pdfmetrics.registerFont(TTFont("Arial", _font_path("arial.ttf")))
pdfmetrics.registerFont(TTFont("Arial-Bold", _font_path("arialbd.ttf")))
pdfmetrics.registerFont(TTFont("Arial-Italic", _font_path("ariali.ttf")))
pdfmetrics.registerFont(TTFont("SegoeSymbol", _font_path("seguisym.ttf")))
FONT_NAME = "Arial"
FONT_SIZE = 9
MEASURE_PAD = 8
BAR_LINE_WIDTH = 1


def _visible_events(events: list[NoteEvent]) -> list[NoteEvent]:
    result = []
    for ev in events:
        visible_pitches = [p for p in ev.pitches if p[2] not in ('continue', 'stop')]
        if visible_pitches:
            result.append(NoteEvent(
                offset=ev.offset, duration=ev.duration, pitches=visible_pitches,
            ))
    return result


def _unique_offsets(measure: Measure) -> list[float]:
    offsets = set()
    for ev in _visible_events(measure.rh_events):
        offsets.add(ev.offset)
    for ev in _visible_events(measure.lh_events):
        offsets.add(ev.offset)
    return sorted(offsets)


def estimate_measure_width(measure: Measure) -> float:
    num_positions = max(len(_unique_offsets(measure)), 1)
    event_width = FONT_SIZE * 2.2
    return num_positions * event_width + MEASURE_PAD * 2


def pitch_to_zone_y(midi_num: int, all_midis: list[int],
                    zone_bottom: float, zone_top: float) -> float:
    if not all_midis or len(set(all_midis)) <= 1:
        return (zone_bottom + zone_top) / 2
    lo, hi = min(all_midis), max(all_midis)
    if hi == lo:
        return (zone_bottom + zone_top) / 2
    t = (midi_num - lo) / (hi - lo)
    return zone_bottom + t * (zone_top - zone_bottom)


SYMBOL_CHARS = set("♭♯♩♪♫♬")


def _draw_mixed_string(c: canvas.Canvas, x: float, y: float, text: str,
                       font: str, size: float):
    """Draw a string using `font` for normal chars and SegoeSymbol for music symbols."""
    cx = x
    for ch in text:
        if ch in SYMBOL_CHARS:
            c.setFont("SegoeSymbol", size)
        else:
            c.setFont(font, size)
        c.drawString(cx, y, ch)
        cx += pdfmetrics.stringWidth(ch, "SegoeSymbol" if ch in SYMBOL_CHARS else font, size)


def _mixed_string_width(text: str, font: str, size: float) -> float:
    w = 0.0
    for ch in text:
        f = "SegoeSymbol" if ch in SYMBOL_CHARS else font
        w += pdfmetrics.stringWidth(ch, f, size)
    return w


def render_pdf(measures: list[Measure], output_path: str, title: str = "",
               score_info: ScoreInfo | None = None):
    c = canvas.Canvas(output_path, pagesize=LETTER)
    c.setFont(FONT_NAME, FONT_SIZE)

    usable_w = PAGE_W - MARGIN_LEFT - MARGIN_RIGHT
    usable_h = PAGE_H - MARGIN_TOP - MARGIN_BOTTOM

    rh_zone_h = SYSTEM_HEIGHT / 2
    lh_zone_h = SYSTEM_HEIGHT / 2

    # break measures into lines
    lines: list[list[int]] = []
    current_line: list[int] = []
    current_w = 0.0
    for i, m in enumerate(measures):
        mw = estimate_measure_width(m)
        if current_w + mw > usable_w and current_line:
            lines.append(current_line)
            current_line = [i]
            current_w = mw
        else:
            current_line.append(i)
            current_w += mw
    if current_line:
        lines.append(current_line)

    systems_per_page = max(1, int(usable_h // (SYSTEM_HEIGHT + SYSTEM_SPACING)))

    page_num = 0
    line_idx = 0

    while line_idx < len(lines):
        if page_num > 0:
            c.showPage()
            c.setFont(FONT_NAME, FONT_SIZE)

        if page_num == 0:
            header_y = PAGE_H - MARGIN_TOP + 10
            if title:
                c.setFont("Arial-Bold", 14)
                c.drawCentredString(PAGE_W / 2, header_y, title)

            # key sig, time sig, tempo subtitle
            subtitle_parts = []
            if score_info:
                if score_info.key_name:
                    subtitle_parts.append(score_info.key_name)
                if score_info.time_sig:
                    subtitle_parts.append(score_info.time_sig)
                if score_info.initial_tempo:
                    subtitle_parts.append(f"♩ = {int(score_info.initial_tempo)}")
            if subtitle_parts:
                subtitle = "   ".join(subtitle_parts)
                sw = _mixed_string_width(subtitle, FONT_NAME, 10)
                c.setFillColorRGB(0.3, 0.3, 0.3)
                _draw_mixed_string(c, PAGE_W / 2 - sw / 2, header_y - 16,
                                   subtitle, FONT_NAME, 10)
                c.setFillColorRGB(0, 0, 0)

            c.setFont(FONT_NAME, FONT_SIZE)

        for sys_idx in range(systems_per_page):
            if line_idx >= len(lines):
                break

            measure_indices = lines[line_idx]
            line_idx += 1

            sys_top = PAGE_H - MARGIN_TOP - sys_idx * (SYSTEM_HEIGHT + SYSTEM_SPACING) - 35
            center_y = sys_top - rh_zone_h
            bar_top = center_y + rh_zone_h * 0.85
            bar_bottom = center_y - lh_zone_h * 0.85

            # center line
            c.setStrokeColorRGB(0, 0, 0)
            c.setLineWidth(1)
            c.line(MARGIN_LEFT, center_y, PAGE_W - MARGIN_RIGHT, center_y)

            raw_widths = [estimate_measure_width(measures[mi]) for mi in measure_indices]
            total_raw = sum(raw_widths)
            scale = usable_w / total_raw if total_raw > 0 else 1
            widths = [w * scale for w in raw_widths]

            x = MARGIN_LEFT

            for j, mi in enumerate(measure_indices):
                m = measures[mi]
                mw = widths[j]

                # bar line
                if j > 0 or sys_idx > 0 or page_num > 0 or mi > 0:
                    c.setStrokeColorRGB(0, 0, 0)
                    c.setLineWidth(BAR_LINE_WIDTH)
                    c.line(x, bar_bottom, x, bar_top)

                # repeat start dots
                if m.repeat_start:
                    c.setFillColorRGB(0, 0, 0)
                    c.circle(x + 6, center_y + 6, 2, fill=1)
                    c.circle(x + 6, center_y - 6, 2, fill=1)

                # volta bracket
                if m.volta:
                    volta_y = bar_top + 12
                    c.setStrokeColorRGB(0, 0, 0)
                    c.setLineWidth(1)
                    end_x = x + mw
                    c.line(x, volta_y, end_x, volta_y)
                    if m.volta_start:
                        c.line(x, bar_top + 2, x, volta_y)
                        c.setFont(FONT_NAME, 7)
                        c.setFillColorRGB(0, 0, 0)
                        c.drawString(x + 4, volta_y + 2, f"{m.volta}.")
                        c.setFont(FONT_NAME, FONT_SIZE)

                inner_left = x + MEASURE_PAD
                inner_w = mw - MEASURE_PAD * 2

                # time-to-x mapping
                offsets = _unique_offsets(m)
                if len(offsets) <= 1:
                    offset_to_x = {o: inner_left + inner_w * 0.3 for o in offsets}
                else:
                    offset_to_x = {}
                    for oi, o in enumerate(offsets):
                        offset_to_x[o] = inner_left + (oi / (len(offsets) - 1)) * inner_w * 0.85

                # note zones
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

                # dynamics — render below the LH zone
                dyn_y = bar_bottom - 10
                for dm in m.dynamics:
                    dm_x = offset_to_x.get(dm.offset, inner_left)
                    c.setFont("Arial-Italic", 8)
                    c.setFillColorRGB(0.15, 0.15, 0.6)
                    c.drawString(dm_x, dyn_y, dm.text)
                    c.setFillColorRGB(0, 0, 0)

                # wedges (cresc/dim) — render as text below dynamics
                for wm in m.wedges:
                    wm_x = offset_to_x.get(wm.offset, inner_left)
                    label = "cresc." if wm.wedge_type == "cresc" else "dim."
                    c.setFont("Arial-Italic", 7)
                    c.setFillColorRGB(0.15, 0.15, 0.6)
                    c.drawString(wm_x, dyn_y - 9, label)
                    c.setFillColorRGB(0, 0, 0)

                # tempo changes (not the initial one) — above the system
                for tm in m.tempos:
                    if mi == 0 and tm.offset == 0:
                        continue  # already shown in header
                    tm_x = offset_to_x.get(tm.offset, inner_left)
                    label = tm.text if tm.text else f"♩={int(tm.bpm)}"
                    c.setFont(FONT_NAME, 7)
                    c.setFillColorRGB(0.4, 0.1, 0.1)
                    c.drawString(tm_x, bar_top + 4, label)
                    c.setFillColorRGB(0, 0, 0)

                # repeat end
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
                    if m.repeat_times:
                        c.setFont(FONT_NAME, 7)
                        c.drawString(end_x - 20, bar_top + 4, f"x{m.repeat_times}")
                        c.setFont(FONT_NAME, FONT_SIZE)

                # measure number
                c.setFillColorRGB(0.5, 0.5, 0.5)
                c.setFont(FONT_NAME, 6)
                c.drawString(x + 2, bar_top + 4, str(m.number))
                c.setFont(FONT_NAME, FONT_SIZE)
                c.setFillColorRGB(0, 0, 0)

                x += mw

            # closing barline
            c.setStrokeColorRGB(0, 0, 0)
            c.setLineWidth(BAR_LINE_WIDTH)
            c.line(x, bar_bottom, x, bar_top)

        page_num += 1

    c.save()


def _draw_note_name(c: canvas.Canvas, x: float, y: float, pitch_name: str):
    letter = pitch_name[0]
    accidental = pitch_name[1:] if len(pitch_name) > 1 else ""
    c.setFont(FONT_NAME, FONT_SIZE)
    c.drawString(x, y, letter)
    if accidental:
        letter_w = pdfmetrics.stringWidth(letter, FONT_NAME, FONT_SIZE)
        c.setFont("SegoeSymbol", FONT_SIZE)
        c.drawString(x + letter_w, y, accidental)


def _render_hand_events(
    c: canvas.Canvas,
    events: list[NoteEvent],
    offset_to_x: dict[float, float],
    all_midis: list[int],
    zone_bottom: float,
    zone_top: float,
):
    if not events:
        return

    c.setFillColorRGB(0, 0, 0)
    line_h = FONT_SIZE * 1.15

    event_midis = []
    for ev in events:
        if ev.pitches:
            avg = sum(p[1] for p in ev.pitches) / len(ev.pitches)
            event_midis.append(avg)

    for ev in events:
        if not ev.pitches:
            continue

        event_x = offset_to_x.get(ev.offset)
        if event_x is None:
            continue

        avg_midi = sum(p[1] for p in ev.pitches) / len(ev.pitches)
        center_y = pitch_to_zone_y(avg_midi, event_midis, zone_bottom, zone_top)

        n = len(ev.pitches)
        stack_top = center_y + (n - 1) * line_h / 2
        stack_bottom = center_y - (n - 1) * line_h / 2

        if stack_top > zone_top:
            shift = stack_top - zone_top
            stack_top -= shift
            stack_bottom -= shift
        if stack_bottom < zone_bottom:
            shift = zone_bottom - stack_bottom
            stack_top += shift
            stack_bottom += shift

        for i, (pitch_name, midi_num, tie) in enumerate(ev.pitches):
            note_y = stack_bottom + i * line_h
            _draw_note_name(c, event_x, note_y, pitch_name)


def main():
    parser = argparse.ArgumentParser(
        description="Convert sheet music (MusicXML) to custom letter notation PDF."
    )
    parser.add_argument("input", help="Path to MusicXML file (.xml, .mxl, .musicxml)")
    parser.add_argument("-o", "--output", help="Output PDF path (default: input name + .pdf)")
    parser.add_argument("-t", "--title", default="", help="Title to display on the first page")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found.")
        sys.exit(1)

    output_path = args.output or str(input_path.with_suffix(".pdf"))

    print(f"Parsing {input_path}...")
    measures, score_info = parse_music(str(input_path))
    print(f"Found {len(measures)} measures.")

    print(f"Rendering to {output_path}...")
    render_pdf(measures, output_path, title=args.title, score_info=score_info)
    print("Done!")


if __name__ == "__main__":
    main()
