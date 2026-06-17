# Sheet Music Converter

A desktop tool that converts MusicXML files into custom letter-based piano notation, guitar tablature, or a hybrid view that pairs traditional staff notation with custom notation.

## Output Formats

- **Custom Notation** — Renders a horizontal-line format with right hand notes above and left hand notes below, using letter names (C, F#, Bb, etc.). Chords stack vertically; spacing reflects timing. Includes dynamics, wedges (crescendo/decrescendo), volta brackets, and repeat markers.
- **Guitar Tab** — Converts notes to tablature with a position-aware fret assignment algorithm that minimizes hand movement.
- **Hybrid** — Crops each system from the original staff PDF and places it directly above the corresponding custom notation, so you can read both at once. Requires a matching `.pdf` alongside the `.musicxml` file.

## Requirements

- Python 3.11+
- [music21](https://web.mit.edu/music21/) — MusicXML parsing
- [PyMuPDF](https://pymupdf.readthedocs.io/) (fitz) — PDF rendering and cropping (hybrid mode)
- [ReportLab](https://www.reportlab.com/) — PDF generation

Install dependencies:

```
pip install music21 PyMuPDF reportlab
```

## Usage

### GUI (recommended)

```
python app.py
```

1. Click **Add Files** and select one or more `.musicxml` or `.mxl` files.
2. Choose a format: Custom Notation, Guitar Tab, or Hybrid.
3. Optionally change the output folder (defaults to same folder as input).
4. Click **Convert All**.

Output files are named `<original>_custom.pdf`, `<original>_tab.pdf`, or `<original>_hybrid.pdf`.

For hybrid mode, the converter looks for a `.pdf` with the same name as each `.musicxml` file in the same folder (e.g. `Song.musicxml` + `Song.pdf`).

### Command line

```
python converter.py input.musicxml -o output.pdf -t "Song Title"
python tab_converter.py input.musicxml -o output.pdf -t "Song Title"
```

## Building the Standalone Executable

Requires [PyInstaller](https://pyinstaller.org/):

```
pip install pyinstaller
```

Build:

```
python -m PyInstaller --onefile --name SheetMusicConverter ^
    --add-data "C:/Windows/Fonts/arial.ttf;." ^
    --add-data "C:/Windows/Fonts/arialbd.ttf;." ^
    --add-data "C:/Windows/Fonts/ariali.ttf;." ^
    --add-data "C:/Windows/Fonts/seguisym.ttf;." ^
    --windowed ^
    --hidden-import fitz ^
    --hidden-import hybrid_converter ^
    app.py --noconfirm
```

The executable will be in `dist/SheetMusicConverter.exe`. It bundles all dependencies and fonts, so it runs on any Windows machine without Python installed.

## Project Structure

| File | Description |
|------|-------------|
| `app.py` | GUI application (tkinter) with batch mode |
| `converter.py` | MusicXML to custom letter notation |
| `tab_converter.py` | MusicXML to guitar tablature |
| `hybrid_converter.py` | Staff image + custom notation side by side |
