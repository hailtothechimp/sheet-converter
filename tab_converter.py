"""
MusicXML to guitar tablature converter with smart fret assignment.

Uses a position-aware algorithm that minimizes hand movement and
considers chord shapes when assigning notes to strings/frets.
"""

import sys
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from itertools import product
from music21 import converter as m21_converter, stream, note, chord, harmony

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Guitar Model ──

STANDARD_TUNING = [40, 45, 50, 55, 59, 64]  # E2 A2 D3 G3 B3 E4 (MIDI)
STRING_NAMES = ["E", "A", "D", "G", "B", "e"]
MAX_FRET = 22
MAX_HAND_SPAN = 5  # max fret spread within one chord position


@dataclass
class FretNote:
    string_idx: int  # 0=low E, 5=high e
    fret: int
    midi: int


@dataclass
class TabEvent:
    offset: float
    duration: float
    fret_notes: list[FretNote]
    is_rest: bool = False


@dataclass
class TabMeasure:
    events: list[TabEvent] = field(default_factory=list)
    number: int = 0


def possible_positions(midi_num: int, tuning: list[int] = STANDARD_TUNING) -> list[tuple[int, int]]:
    """Return all (string_idx, fret) pairs that can produce this MIDI pitch."""
    positions = []
    for s, open_pitch in enumerate(tuning):
        fret = midi_num - open_pitch
        if 0 <= fret <= MAX_FRET:
            positions.append((s, fret))
    return positions


def assign_single_note(midi_num: int, prev_position: float | None,
                       tuning: list[int] = STANDARD_TUNING) -> FretNote | None:
    """Assign a single note to the best string/fret."""
    positions = possible_positions(midi_num, tuning)
    if not positions:
        return None

    def cost(s, f):
        c = f * 0.1  # slight preference for lower frets
        if prev_position is not None:
            c += abs(f - prev_position) * 2  # penalize jumps from current position
        return c

    best = min(positions, key=lambda p: cost(p[0], p[1]))
    return FretNote(string_idx=best[0], fret=best[1], midi=midi_num)


def assign_chord(midi_nums: list[int], prev_position: float | None,
                 tuning: list[int] = STANDARD_TUNING) -> list[FretNote] | None:
    """Assign a chord to strings/frets minimizing hand span and position jumps."""
    if not midi_nums:
        return []

    if len(midi_nums) == 1:
        result = assign_single_note(midi_nums[0], prev_position, tuning)
        return [result] if result else None

    per_note_options = []
    for midi in midi_nums:
        opts = possible_positions(midi, tuning)
        if not opts:
            return None
        per_note_options.append([(s, f, midi) for s, f in opts])

    best_combo = None
    best_cost = float('inf')

    for combo in product(*per_note_options):
        strings_used = [c[0] for c in combo]
        if len(set(strings_used)) != len(strings_used):
            continue  # each note must be on a different string

        frets = [c[1] for c in combo]
        fretted = [f for f in frets if f > 0]

        if fretted:
            span = max(fretted) - min(fretted)
            if span > MAX_HAND_SPAN:
                continue

        # cost function
        cost = 0.0

        # hand span cost
        if fretted:
            cost += (max(fretted) - min(fretted)) * 1.5

        # position jump cost
        if prev_position is not None and fretted:
            avg_fret = sum(fretted) / len(fretted)
            cost += abs(avg_fret - prev_position) * 2.0

        # prefer lower frets slightly
        cost += sum(frets) * 0.05

        # prefer middle strings for single notes (ergonomic)
        cost += sum(abs(s - 2.5) * 0.1 for s, _, _ in combo)

        if cost < best_cost:
            best_cost = cost
            best_combo = combo

    if best_combo is None:
        # fallback: relax hand span constraint
        best_combo = _fallback_assign(midi_nums, prev_position, tuning)
        if best_combo is None:
            return None

    return [FretNote(string_idx=s, fret=f, midi=m) for s, f, m in best_combo]


def _fallback_assign(midi_nums: list[int], prev_position: float | None,
                     tuning: list[int]) -> tuple | None:
    """Relaxed assignment when strict hand span fails."""
    per_note_options = []
    for midi in midi_nums:
        opts = possible_positions(midi, tuning)
        if not opts:
            return None
        per_note_options.append([(s, f, midi) for s, f in opts])

    best_combo = None
    best_cost = float('inf')

    for combo in product(*per_note_options):
        strings_used = [c[0] for c in combo]
        if len(set(strings_used)) != len(strings_used):
            continue

        frets = [c[1] for c in combo]
        fretted = [f for f in frets if f > 0]
        cost = 0.0
        if fretted:
            cost += (max(fretted) - min(fretted)) * 3.0
        if prev_position is not None and fretted:
            avg_fret = sum(fretted) / len(fretted)
            cost += abs(avg_fret - prev_position) * 2.0
        cost += sum(frets) * 0.1

        if cost < best_cost:
            best_cost = cost
            best_combo = combo

    return best_combo


# ── MusicXML Parsing ──

def parse_for_tab(filepath: str, tuning: list[int] = STANDARD_TUNING) -> list[TabMeasure]:
    """Parse MusicXML and assign fret positions."""
    score = m21_converter.parse(filepath)

    # for guitar tab, merge all parts or use the first one
    parts = list(score.parts)
    if not parts:
        raise ValueError("No parts found.")

    # use first part (or could merge)
    part = parts[0]

    prev_position: float | None = None
    tab_measures = []

    for m in part.getElementsByClass(stream.Measure):
        tab_m = TabMeasure(number=m.number)

        for el in m.recurse().notesAndRests:
            if isinstance(el, (note.Rest, harmony.ChordSymbol)):
                continue

            if isinstance(el, chord.Chord):
                midis = sorted(set(p.midi for p in el.pitches))
            elif isinstance(el, note.Note):
                midis = [el.pitch.midi]
            else:
                continue

            # skip tied continuations
            if hasattr(el, 'tie') and el.tie and el.tie.type in ('continue', 'stop'):
                continue

            fret_notes = assign_chord(midis, prev_position, tuning)
            if fret_notes:
                tab_m.events.append(TabEvent(
                    offset=el.offset,
                    duration=el.quarterLength,
                    fret_notes=fret_notes,
                ))
                fretted = [fn.fret for fn in fret_notes if fn.fret > 0]
                if fretted:
                    prev_position = sum(fretted) / len(fretted)

        tab_measures.append(tab_m)

    return tab_measures


# ── PDF Rendering ──

PAGE_W, PAGE_H = LETTER
MARGIN_LEFT = 0.5 * inch
MARGIN_RIGHT = 0.5 * inch
MARGIN_TOP = 0.6 * inch
MARGIN_BOTTOM = 0.6 * inch

STRING_SPACING = 12  # pixels between tab lines
SYSTEM_HEIGHT = STRING_SPACING * 5 + 20  # 6 strings + padding
SYSTEM_GAP = 28  # gap between systems
MEASURE_PAD = 10

def _font_path(filename: str) -> str:
    import os
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, filename)
    return f"C:/Windows/Fonts/{filename}"

pdfmetrics.registerFont(TTFont("Arial", _font_path("arial.ttf")))
FONT_NAME = "Arial"
FONT_SIZE = 8
FRET_FONT_SIZE = 9


def _unique_offsets_tab(measure: TabMeasure) -> list[float]:
    offsets = set()
    for ev in measure.events:
        offsets.add(ev.offset)
    return sorted(offsets)


def estimate_tab_measure_width(measure: TabMeasure) -> float:
    n = max(len(_unique_offsets_tab(measure)), 1)
    return n * FRET_FONT_SIZE * 2.5 + MEASURE_PAD * 2


def render_tab_pdf(measures: list[TabMeasure], output_path: str, title: str = ""):
    c = canvas.Canvas(output_path, pagesize=LETTER)

    usable_w = PAGE_W - MARGIN_LEFT - MARGIN_RIGHT
    usable_h = PAGE_H - MARGIN_TOP - MARGIN_BOTTOM

    # break into lines
    lines: list[list[int]] = []
    current_line: list[int] = []
    current_w = 0.0
    for i, m in enumerate(measures):
        mw = estimate_tab_measure_width(m)
        if current_w + mw > usable_w and current_line:
            lines.append(current_line)
            current_line = [i]
            current_w = mw
        else:
            current_line.append(i)
            current_w += mw
    if current_line:
        lines.append(current_line)

    systems_per_page = max(1, int(usable_h // (SYSTEM_HEIGHT + SYSTEM_GAP)))

    page_num = 0
    line_idx = 0

    while line_idx < len(lines):
        if page_num > 0:
            c.showPage()

        if page_num == 0 and title:
            c.setFont(FONT_NAME, 14)
            c.drawCentredString(PAGE_W / 2, PAGE_H - MARGIN_TOP + 10, title)

        for sys_idx in range(systems_per_page):
            if line_idx >= len(lines):
                break

            measure_indices = lines[line_idx]
            line_idx += 1

            # top of this system's tab staff
            sys_top = PAGE_H - MARGIN_TOP - sys_idx * (SYSTEM_HEIGHT + SYSTEM_GAP) - 30

            # draw 6 tab lines
            for s in range(6):
                line_y = sys_top - s * STRING_SPACING
                c.setStrokeColorRGB(0.6, 0.6, 0.6)
                c.setLineWidth(0.5)
                c.line(MARGIN_LEFT, line_y, PAGE_W - MARGIN_RIGHT, line_y)

            # string labels on the left
            c.setFont(FONT_NAME, 7)
            c.setFillColorRGB(0.4, 0.4, 0.4)
            for s in range(6):
                line_y = sys_top - s * STRING_SPACING
                # strings: 0=low E (bottom), 5=high e (top) → display reversed
                display_s = 5 - s
                c.drawRightString(MARGIN_LEFT - 3, line_y - 3, STRING_NAMES[display_s])

            # compute measure widths
            raw_widths = [estimate_tab_measure_width(measures[mi]) for mi in measure_indices]
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
                    c.setLineWidth(1)
                    c.line(x, sys_top - 5 * STRING_SPACING, x, sys_top)

                inner_left = x + MEASURE_PAD
                inner_w = mw - MEASURE_PAD * 2

                # time-to-x mapping
                offsets = _unique_offsets_tab(m)
                if len(offsets) <= 1:
                    offset_to_x = {o: inner_left + inner_w * 0.4 for o in offsets}
                else:
                    offset_to_x = {}
                    for oi, o in enumerate(offsets):
                        offset_to_x[o] = inner_left + (oi / (len(offsets) - 1)) * inner_w * 0.85

                # measure number
                c.setFillColorRGB(0.5, 0.5, 0.5)
                c.setFont(FONT_NAME, 6)
                c.drawString(x + 2, sys_top + 6, str(m.number))

                # render fret numbers
                c.setFont(FONT_NAME, FRET_FONT_SIZE)
                c.setFillColorRGB(0, 0, 0)

                for ev in m.events:
                    event_x = offset_to_x.get(ev.offset)
                    if event_x is None:
                        continue

                    for fn in ev.fret_notes:
                        # string 0 (low E) is at bottom, string 5 (high e) at top
                        display_row = 5 - fn.string_idx
                        line_y = sys_top - display_row * STRING_SPACING

                        fret_str = str(fn.fret)
                        fw = pdfmetrics.stringWidth(fret_str, FONT_NAME, FRET_FONT_SIZE)

                        # white background to clear the tab line
                        c.setFillColorRGB(1, 1, 1)
                        c.rect(event_x - 1, line_y - FRET_FONT_SIZE * 0.35,
                               fw + 2, FRET_FONT_SIZE * 0.85, fill=1, stroke=0)

                        c.setFillColorRGB(0, 0, 0)
                        c.drawString(event_x, line_y - FRET_FONT_SIZE * 0.3, fret_str)

                x += mw

            # closing bar line
            c.setStrokeColorRGB(0, 0, 0)
            c.setLineWidth(1)
            c.line(x, sys_top - 5 * STRING_SPACING, x, sys_top)

        page_num += 1

    c.save()


def main():
    parser = argparse.ArgumentParser(
        description="Convert MusicXML to guitar tablature PDF with smart fret assignment."
    )
    parser.add_argument("input", help="Path to MusicXML file (.xml, .mxl, .musicxml)")
    parser.add_argument("-o", "--output", help="Output PDF path")
    parser.add_argument("-t", "--title", default="", help="Title on first page")
    parser.add_argument("--tuning", default=None,
                        help="Custom tuning as comma-separated MIDI numbers (default: standard E A D G B E)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found.")
        sys.exit(1)

    tuning = STANDARD_TUNING
    if args.tuning:
        tuning = [int(x.strip()) for x in args.tuning.split(",")]
        if len(tuning) != 6:
            print("Error: tuning must have exactly 6 values.")
            sys.exit(1)

    output_path = args.output or str(input_path.with_name(input_path.stem + "_tab.pdf"))

    print(f"Parsing {input_path}...")
    measures = parse_for_tab(str(input_path), tuning)
    print(f"Found {len(measures)} measures.")

    print(f"Rendering to {output_path}...")
    render_tab_pdf(measures, output_path, title=args.title)
    print("Done!")


if __name__ == "__main__":
    main()
