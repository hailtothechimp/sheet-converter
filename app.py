"""GUI for the sheet music converter (custom notation, guitar tab, hybrid). Supports batch mode."""

import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
import threading

from converter import parse_music, render_pdf
from tab_converter import parse_for_tab, render_tab_pdf
from hybrid_converter import render_hybrid_pdf


class ConverterApp:
    def __init__(self, root):
        root.title("Sheet Music Converter")
        root.geometry("580x470")
        root.resizable(False, False)

        self.files: list[str] = []

        # ── File list ──
        file_frame = tk.LabelFrame(root, text="MusicXML Files", padx=10, pady=5)
        file_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=(10, 5))

        list_frame = tk.Frame(file_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.file_listbox = tk.Listbox(list_frame, height=8, selectmode=tk.EXTENDED)
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(list_frame, command=self.file_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_listbox.config(yscrollcommand=scrollbar.set)

        file_btn_frame = tk.Frame(file_frame)
        file_btn_frame.pack(fill=tk.X, pady=(5, 0))

        tk.Button(file_btn_frame, text="Add Files...", command=self._add_files).pack(side=tk.LEFT)
        tk.Button(file_btn_frame, text="Remove Selected", command=self._remove_selected).pack(
            side=tk.LEFT, padx=(8, 0))
        tk.Button(file_btn_frame, text="Clear All", command=self._clear).pack(
            side=tk.LEFT, padx=(8, 0))

        # ── Options ──
        opt_frame = tk.Frame(root, padx=15)
        opt_frame.pack(fill=tk.X, pady=(5, 0))

        tk.Label(opt_frame, text="Format:").pack(side=tk.LEFT)
        self.format_var = tk.StringVar(value="custom")
        self.format_var.trace_add("write", self._on_format_change)
        tk.Radiobutton(opt_frame, text="Custom Notation", variable=self.format_var,
                       value="custom").pack(side=tk.LEFT, padx=(4, 0))
        tk.Radiobutton(opt_frame, text="Guitar Tab", variable=self.format_var,
                       value="tab").pack(side=tk.LEFT, padx=(8, 0))
        tk.Radiobutton(opt_frame, text="Hybrid", variable=self.format_var,
                       value="hybrid").pack(side=tk.LEFT, padx=(8, 0))

        # ── Hybrid: staff PDF note ──
        self.hybrid_note = tk.Label(
            root,
            text="Hybrid mode looks for a matching .pdf next to each .musicxml file",
            fg="gray", font=("Arial", 8),
        )

        # ── Output folder ──
        out_frame = tk.Frame(root, padx=15)
        out_frame.pack(fill=tk.X, pady=(8, 0))

        tk.Label(out_frame, text="Output folder:").pack(side=tk.LEFT)
        self.out_var = tk.StringVar(value="(same as input)")
        tk.Entry(out_frame, textvariable=self.out_var, width=35, state="readonly").pack(
            side=tk.LEFT, padx=(6, 6))
        tk.Button(out_frame, text="Change...", command=self._pick_output_dir).pack(side=tk.LEFT)

        # ── Convert button + status ──
        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=12)

        self.convert_btn = tk.Button(btn_frame, text="Convert All", command=self._convert,
                                     width=20, height=2)
        self.convert_btn.pack()

        self.status = tk.Label(root, text="", fg="gray")
        self.status.pack(pady=(0, 8))

    def _on_format_change(self, *_args):
        if self.format_var.get() == "hybrid":
            self.hybrid_note.pack(fill=tk.X, padx=20, pady=(2, 0),
                                  before=self.convert_btn.master)
        else:
            self.hybrid_note.pack_forget()

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select MusicXML files",
            filetypes=[
                ("MusicXML files", "*.musicxml *.xml *.mxl"),
                ("All files", "*.*"),
            ],
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_listbox.insert(tk.END, Path(p).name)

    def _remove_selected(self):
        selected = list(self.file_listbox.curselection())
        for idx in reversed(selected):
            self.file_listbox.delete(idx)
            del self.files[idx]

    def _clear(self):
        self.files.clear()
        self.file_listbox.delete(0, tk.END)
        self.status.config(text="", fg="gray")

    def _pick_output_dir(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.out_var.set(d)

    def _convert(self):
        if not self.files:
            messagebox.showwarning("No files", "Add at least one MusicXML file first.")
            return

        self.convert_btn.config(state=tk.DISABLED)
        self.status.config(text="Converting...", fg="gray")
        threading.Thread(target=self._run_batch, daemon=True).start()

    def _run_batch(self):
        fmt = self.format_var.get()
        suffix_map = {"custom": "_custom.pdf", "tab": "_tab.pdf", "hybrid": "_hybrid.pdf"}
        suffix = suffix_map[fmt]
        out_dir = self.out_var.get()

        done = 0
        errors = []

        for i, src in enumerate(self.files):
            src_path = Path(src)
            self.status.after(0, lambda i=i: self.status.config(
                text=f"Converting {i + 1} of {len(self.files)}...", fg="gray"))

            if out_dir == "(same as input)":
                dest = str(src_path.with_name(src_path.stem + suffix))
            else:
                dest = str(Path(out_dir) / (src_path.stem + suffix))

            try:
                title = src_path.stem.replace("_", " ").replace("-", " ").title()
                if fmt == "custom":
                    measures, score_info = parse_music(src)
                    render_pdf(measures, dest, title=title, score_info=score_info)
                elif fmt == "tab":
                    measures = parse_for_tab(src)
                    render_tab_pdf(measures, dest, title=title)
                elif fmt == "hybrid":
                    staff_pdf = src_path.with_suffix(".pdf")
                    if not staff_pdf.exists():
                        raise FileNotFoundError(
                            f"No matching PDF found: {staff_pdf.name}")
                    render_hybrid_pdf(src, str(staff_pdf), dest, title=title)
                done += 1
            except Exception as e:
                errors.append(f"{src_path.name}: {e}")

        self.status.after(0, lambda: self._batch_done(done, errors))

    def _batch_done(self, done, errors):
        self.convert_btn.config(state=tk.NORMAL)
        if errors:
            self.status.config(text=f"Done: {done} converted, {len(errors)} failed", fg="orange")
            messagebox.showwarning("Some files failed",
                                   "\n\n".join(errors[:10]))
        else:
            self.status.config(text=f"Done! {done} file{'s' if done != 1 else ''} converted.", fg="green")


if __name__ == "__main__":
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()
