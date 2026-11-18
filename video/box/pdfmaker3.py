import base64
import html
import os
import random
import re
import string
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

from PIL import Image, ImageTk
import fitz
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter


APP_TITLE = "Center PDF Builder"
A4_RATIO = 210 / 297  # width / height
SCRIPT_DIR = Path(__file__).resolve().parent


def clean_unicode_filename(text: str) -> str:
    """Keep Unicode letters/numbers. Replace everything else with one hyphen."""
    text = text.strip().lower()
    out = []
    last_dash = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        else:
            if out and not last_dash:
                out.append("-")
                last_dash = True
    return "".join(out).strip("-")


def clean_english_filename(text: str) -> str:
    """Keep only English letters and digits."""
    parts = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "-".join(parts)


def random_string(length: int) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(max(1, length)))


def clean_affix(text: str) -> str:
    """Prefix/suffix supports Unicode, with safe separators."""
    return clean_unicode_filename(text)


def valid_http_url(url: str) -> bool:
    if not url.strip():
        return True
    p = urlparse(url.strip())
    return p.scheme in ("http", "https") and bool(p.netloc)


def file_to_data_uri(path: str) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


class PDFBuilderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1380x860")
        self.minsize(1120, 720)

        self.txt_file = ""
        self.template_pdf = ""
        self.output_dir = ""
        self.output_dir_selected = False
        self.content_top = tk.IntVar(value=20)

        self.blocks = [
            {
                "id": "title",
                "type": "title",
                "name": "Title",
                "font_size": 28,
                "align": "center",
                "gap_before": 0,
                "gap": 10,
            },
            {
                "id": "image",
                "type": "image",
                "name": "Image",
                "path": "",
                "link": "",
                "width": 70,
                "gap_before": 0,
            "gap": 10,
            },
            {
                "id": "description",
                "type": "text",
                "name": "Description",
                "text": "",
                "font_size": 14,
                "align": "center",
                "gap_before": 0,
                "gap": 10,
            },
        ]

        self.drag_index = None
        self.preview_image_refs = []
        self.template_preview_ref = None

        self._build_ui()
        self._refresh_block_list()
        self.block_list.selection_set(0)
        self._show_selected_block_editor()
        self._schedule_preview()

    # ---------------- UI ----------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        root = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        root.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        left_outer = ttk.Frame(root)
        right = ttk.Frame(root, padding=10)
        root.add(left_outer, weight=3)
        root.add(right, weight=2)

        left_outer.columnconfigure(0, weight=1)
        left_outer.rowconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, highlightthickness=0)
        left_scrollbar = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)

        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scrollbar.grid(row=0, column=1, sticky="ns")

        left = ttk.Frame(left_canvas, padding=10)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")

        def _sync_scrollregion(_event=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def _sync_inner_width(event):
            left_canvas.itemconfigure(left_window, width=event.width)

        left.bind("<Configure>", _sync_scrollregion)
        left_canvas.bind("<Configure>", _sync_inner_width)

        def _on_mousewheel(event):
            if event.delta:
                left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_linux_scroll_up(_event):
            left_canvas.yview_scroll(-3, "units")

        def _on_linux_scroll_down(_event):
            left_canvas.yview_scroll(3, "units")

        # Scroll only while pointer is over the left settings area.
        def _bind_scroll(_event):
            left_canvas.bind_all("<MouseWheel>", _on_mousewheel)
            left_canvas.bind_all("<Button-4>", _on_linux_scroll_up)
            left_canvas.bind_all("<Button-5>", _on_linux_scroll_down)

        def _unbind_scroll(_event):
            left_canvas.unbind_all("<MouseWheel>")
            left_canvas.unbind_all("<Button-4>")
            left_canvas.unbind_all("<Button-5>")

        left_canvas.bind("<Enter>", _bind_scroll)
        left_canvas.bind("<Leave>", _unbind_scroll)
        left.bind("<Enter>", _bind_scroll)
        left.bind("<Leave>", _unbind_scroll)

        left.columnconfigure(0, weight=1)

        # Titles
        title_box = ttk.LabelFrame(left, text="Titles", padding=10)
        title_box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        title_box.columnconfigure(1, weight=1)

        ttk.Label(title_box, text="Manual Title *").grid(row=0, column=0, sticky="w")
        self.manual_title = tk.StringVar()
        e = ttk.Entry(title_box, textvariable=self.manual_title)
        e.grid(row=0, column=1, sticky="ew", padx=8)
        e.bind("<KeyRelease>", lambda _e: self._schedule_preview())

        ttk.Button(title_box, text="Select TXT Titles", command=self.select_txt).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.txt_label = ttk.Label(title_box, text="No TXT selected")
        self.txt_label.grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0))

        ttk.Label(
            title_box,
            text="TXT mode: each non-empty line = one PDF. TXT titles override Manual Title.",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))

        ttk.Button(title_box, text="Select Template PDF", command=self.select_template_pdf).grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )
        self.template_label = ttk.Label(title_box, text="No template selected (blank PDF)")
        self.template_label.grid(row=3, column=1, sticky="w", padx=8, pady=(10, 0))

        ttk.Label(title_box, text="Content Top Position (px)").grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )
        top_spin = ttk.Spinbox(
            title_box, from_=0, to=1000, textvariable=self.content_top, width=10
        )
        top_spin.grid(row=4, column=1, sticky="w", padx=8, pady=(8, 0))
        top_spin.bind("<KeyRelease>", lambda _e: self._schedule_preview())

        # Block ordering
        layout_box = ttk.LabelFrame(left, text="PDF Blocks — drag to reorder", padding=10)
        layout_box.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        layout_box.columnconfigure(0, weight=1)

        self.block_list = tk.Listbox(layout_box, height=6, exportselection=False)
        self.block_list.grid(row=0, column=0, rowspan=3, sticky="ew")
        self.block_list.bind("<<ListboxSelect>>", lambda _e: self._show_selected_block_editor())
        self.block_list.bind("<Button-1>", self._drag_start)
        self.block_list.bind("<B1-Motion>", self._drag_motion)

        ttk.Button(layout_box, text="+ Add Text Field", command=self.add_text_block).grid(
            row=0, column=1, padx=(8, 0), sticky="ew"
        )
        ttk.Button(layout_box, text="+ Add Image Field", command=self.add_image_block).grid(
            row=1, column=1, padx=(8, 0), pady=5, sticky="ew"
        )
        ttk.Button(layout_box, text="+ Add Title Field", command=self.add_title_block).grid(
            row=2, column=1, padx=(8, 0), pady=5, sticky="ew"
        )
        ttk.Button(layout_box, text="Delete Selected", command=self.delete_block).grid(
            row=3, column=1, padx=(8, 0), pady=5, sticky="ew"
        )
        ttk.Label(layout_box, text="Main Title is locked at #1.").grid(
            row=4, column=1, padx=(8, 0), sticky="w"
        )

        # Selected block editor
        self.editor_box = ttk.LabelFrame(left, text="Selected Block Settings", padding=10)
        self.editor_box.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        self.editor_box.columnconfigure(1, weight=1)

        # Filename
        file_box = ttk.LabelFrame(left, text="Output File Name", padding=10)
        file_box.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        file_box.columnconfigure(1, weight=1)

        self.filename_mode = tk.StringVar(value="default")
        ttk.Label(file_box, text="File Name Mode").grid(row=0, column=0, sticky="w")
        modes = ttk.Frame(file_box)
        modes.grid(row=0, column=1, sticky="w")
        for text, val in [
            ("Default", "default"),
            ("English Only", "english"),
            ("Random String", "random"),
        ]:
            ttk.Radiobutton(
                modes,
                text=text,
                variable=self.filename_mode,
                value=val,
            ).pack(side=tk.LEFT, padx=(0, 12))

        self.filename_length = tk.IntVar(value=60)
        ttk.Label(file_box, text="File Name Length").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(file_box, from_=1, to=200, textvariable=self.filename_length, width=10).grid(
            row=1, column=1, sticky="w", pady=5
        )

        self.prefix = tk.StringVar()
        self.suffix = tk.StringVar()
        ttk.Label(file_box, text="Prefix (optional)").grid(row=2, column=0, sticky="w")
        ttk.Entry(file_box, textvariable=self.prefix).grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(file_box, text="Suffix (optional)").grid(row=3, column=0, sticky="w")
        ttk.Entry(file_box, textvariable=self.suffix).grid(row=3, column=1, sticky="ew", pady=3)

        serial_frame = ttk.Frame(file_box)
        serial_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self.serial_enabled = tk.BooleanVar(value=False)
        self.serial_start = tk.IntVar(value=1)
        self.serial_padding = tk.IntVar(value=3)

        ttk.Checkbutton(serial_frame, text="Serial Number", variable=self.serial_enabled).pack(
            side=tk.LEFT
        )
        ttk.Label(serial_frame, text="Start").pack(side=tk.LEFT, padx=(15, 4))
        ttk.Spinbox(serial_frame, from_=0, to=999999999, textvariable=self.serial_start, width=8).pack(
            side=tk.LEFT
        )
        ttk.Label(serial_frame, text="Digits").pack(side=tk.LEFT, padx=(15, 4))
        ttk.Spinbox(serial_frame, from_=1, to=12, textvariable=self.serial_padding, width=5).pack(
            side=tk.LEFT
        )
        ttk.Label(serial_frame, text="Serial is always last.").pack(side=tk.LEFT, padx=(15, 0))

        # Output/actions
        action_box = ttk.Frame(left)
        action_box.grid(row=4, column=0, sticky="ew")
        action_box.columnconfigure(1, weight=1)

        ttk.Button(action_box, text="Output Folder", command=self.select_output_dir).grid(
            row=0, column=0, sticky="w"
        )
        self.output_label = ttk.Label(action_box, text="Auto: output / output-2 / output-3 beside Python file")
        self.output_label.grid(row=0, column=1, sticky="w", padx=8)

        ttk.Button(action_box, text="Generate PDF(s)", command=self.generate_pdfs).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0)
        )

        self.status = ttk.Label(action_box, text="Ready")
        self.status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            action_box,
            variable=self.progress_var,
            maximum=100,
            mode="determinate"
        )
        self.progress_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self.progress_text = ttk.Label(action_box, text="0% • 0 / 0 completed")
        self.progress_text.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Preview
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        ttk.Label(right, text="Live PDF Preview", font=("", 14, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.preview = tk.Canvas(right, bg="#d9d9d9", highlightthickness=0)
        self.preview.grid(row=1, column=0, sticky="nsew")
        self.preview.bind("<Configure>", lambda _e: self._schedule_preview())

    # ---------------- Block functions ----------------

    def _refresh_block_list(self):
        current = self.block_list.curselection()
        selected_id = None
        if current:
            idx = current[0]
            if idx < len(self.blocks):
                selected_id = self.blocks[idx]["id"]

        self.block_list.delete(0, tk.END)
        for i, block in enumerate(self.blocks, start=1):
            optional = "" if block["type"] == "title" else " (optional)"
            self.block_list.insert(tk.END, f"{i}. {block['name']}{optional}")

        if selected_id:
            for i, block in enumerate(self.blocks):
                if block["id"] == selected_id:
                    self.block_list.selection_set(i)
                    break

    def _selected_index(self):
        sel = self.block_list.curselection()
        return sel[0] if sel else None

    def _drag_start(self, event):
        idx = self.block_list.nearest(event.y)
        self.drag_index = idx if idx > 0 else None  # Title cannot move

    def _drag_motion(self, event):
        if self.drag_index is None:
            return
        target = self.block_list.nearest(event.y)
        if target <= 0 or target == self.drag_index:
            return
        block = self.blocks.pop(self.drag_index)
        self.blocks.insert(target, block)
        self.drag_index = target
        self._refresh_block_list()
        self.block_list.selection_clear(0, tk.END)
        self.block_list.selection_set(target)
        self._show_selected_block_editor()
        self._schedule_preview()

    def add_text_block(self):
        n = 1 + sum(1 for b in self.blocks if b["id"].startswith("extra_"))
        block = {
            "id": f"extra_{n}_{random.randint(1000,9999)}",
            "type": "text",
            "name": f"Extra Text {n}",
            "text": "",
            "font_size": 14,
            "align": "center",
            "gap_before": 0,
                "gap": 10,
        }
        self.blocks.append(block)
        self._refresh_block_list()
        idx = len(self.blocks) - 1
        self.block_list.selection_clear(0, tk.END)
        self.block_list.selection_set(idx)
        self._show_selected_block_editor()
        self._schedule_preview()

    def add_title_block(self):
        n = 1 + sum(1 for b in self.blocks if b["id"].startswith("title_extra_"))
        block = {
            "id": f"title_extra_{n}_{random.randint(1000,9999)}",
            "type": "extra_title",
            "name": f"Extra Title {n}",
            "font_size": 22,
            "align": "center",
            "gap_before": 0,
                "gap": 10,
        }
        self.blocks.append(block)
        self._refresh_block_list()
        idx = len(self.blocks) - 1
        self.block_list.selection_clear(0, tk.END)
        self.block_list.selection_set(idx)
        self._show_selected_block_editor()
        self._schedule_preview()

    def add_image_block(self):
        n = 1 + sum(1 for b in self.blocks if b["id"].startswith("image_extra_"))
        block = {
            "id": f"image_extra_{n}_{random.randint(1000,9999)}",
            "type": "image",
            "name": f"Image {n + 1}",
            "path": "",
            "link": "",
            "width": 70,
            "gap_before": 0,
            "gap": 10,
        }
        self.blocks.append(block)
        self._refresh_block_list()
        idx = len(self.blocks) - 1
        self.block_list.selection_clear(0, tk.END)
        self.block_list.selection_set(idx)
        self._show_selected_block_editor()
        self._schedule_preview()

    def delete_block(self):
        idx = self._selected_index()
        if idx is None:
            return
        if idx == 0 or self.blocks[idx]["type"] == "title":
            messagebox.showinfo(APP_TITLE, "Title is required and cannot be deleted.")
            return
        del self.blocks[idx]
        self._refresh_block_list()
        new_idx = min(idx, len(self.blocks) - 1)
        self.block_list.selection_set(new_idx)
        self._show_selected_block_editor()
        self._schedule_preview()

    def _clear_editor(self):
        for w in self.editor_box.winfo_children():
            w.destroy()

    def _show_selected_block_editor(self):
        self._clear_editor()
        idx = self._selected_index()
        if idx is None or idx >= len(self.blocks):
            return

        block = self.blocks[idx]
        row = 0

        ttk.Label(self.editor_box, text=block["name"], font=("", 12, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        row += 1

        if block["type"] == "title":
            ttk.Label(
                self.editor_box,
                text="Title text comes from Manual Title or the selected TXT file.",
            ).grid(row=row, column=0, columnspan=3, sticky="w")
            row += 1

        elif block["type"] == "image":
            ttk.Label(self.editor_box, text="Image").grid(row=row, column=0, sticky="w")
            path_var = tk.StringVar(value=block.get("path", ""))
            ent = ttk.Entry(self.editor_box, textvariable=path_var)
            ent.grid(row=row, column=1, sticky="ew", padx=8)

            def choose_image():
                path = filedialog.askopenfilename(
                    initialdir=str(SCRIPT_DIR),
                    title="Select Image",
                    filetypes=[
                        ("Images", "*.png *.jpg *.jpeg *.webp *.gif"),
                        ("All Files", "*.*"),
                    ],
                )
                if path:
                    block["path"] = path
                    path_var.set(path)
                    self._schedule_preview()

            ttk.Button(self.editor_box, text="Browse", command=choose_image).grid(row=row, column=2)
            row += 1

            ttk.Label(self.editor_box, text="Clickable URL").grid(row=row, column=0, sticky="w")
            link_var = tk.StringVar(value=block.get("link", ""))

            def link_changed(*_):
                block["link"] = link_var.get()

            link_var.trace_add("write", link_changed)
            ttk.Entry(self.editor_box, textvariable=link_var).grid(
                row=row, column=1, columnspan=2, sticky="ew", padx=8, pady=4
            )
            row += 1

            ttk.Label(self.editor_box, text="Image Width %").grid(row=row, column=0, sticky="w")
            width_var = tk.IntVar(value=block.get("width", 70))

            def width_changed(*_):
                try:
                    block["width"] = max(5, min(100, int(width_var.get())))
                except Exception:
                    pass
                self._schedule_preview()

            width_var.trace_add("write", width_changed)
            ttk.Spinbox(self.editor_box, from_=5, to=100, textvariable=width_var, width=8).grid(
                row=row, column=1, sticky="w", padx=8
            )
            row += 1

        elif block["type"] == "extra_title":
            ttk.Label(
                self.editor_box,
                text="Uses the same title as Main Title / current TXT line."
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row += 1

        elif block["type"] == "text":
            ttk.Label(self.editor_box, text="Field Name").grid(row=row, column=0, sticky="w")
            name_var = tk.StringVar(value=block["name"])

            def name_changed(*_):
                block["name"] = name_var.get().strip() or "Text"
                self._refresh_block_list()

            name_var.trace_add("write", name_changed)
            ttk.Entry(self.editor_box, textvariable=name_var).grid(
                row=row, column=1, columnspan=2, sticky="ew", padx=8
            )
            row += 1

            ttk.Label(self.editor_box, text="Text").grid(row=row, column=0, sticky="nw", pady=5)
            txt = tk.Text(self.editor_box, height=7, wrap="word")
            txt.grid(row=row, column=1, columnspan=2, sticky="nsew", padx=8, pady=5)
            txt.insert("1.0", block.get("text", ""))

            def text_changed(_e=None):
                block["text"] = txt.get("1.0", "end-1c")
                self._schedule_preview()

            txt.bind("<KeyRelease>", text_changed)
            row += 1

        if block["type"] in ("title", "text", "extra_title"):
            ttk.Label(self.editor_box, text="Font Size").grid(row=row, column=0, sticky="w")
            fs_var = tk.IntVar(value=block.get("font_size", 14))

            def fs_changed(*_):
                try:
                    block["font_size"] = max(6, min(120, int(fs_var.get())))
                except Exception:
                    pass
                self._schedule_preview()

            fs_var.trace_add("write", fs_changed)
            ttk.Spinbox(self.editor_box, from_=6, to=120, textvariable=fs_var, width=8).grid(
                row=row, column=1, sticky="w", padx=8
            )
            row += 1

            ttk.Label(self.editor_box, text="Alignment").grid(row=row, column=0, sticky="w", pady=7)
            align_var = tk.StringVar(value=block.get("align", "center"))

            align_frame = ttk.Frame(self.editor_box)
            align_frame.grid(row=row, column=1, columnspan=2, sticky="w", padx=8, pady=7)

            def set_alignment(value):
                block["align"] = value
                align_var.set(value)
                self._schedule_preview()

            ttk.Radiobutton(
                align_frame, text="Left",
                variable=align_var, value="left",
                command=lambda: set_alignment("left")
            ).pack(side=tk.LEFT, padx=(0, 12))

            ttk.Radiobutton(
                align_frame, text="Center",
                variable=align_var, value="center",
                command=lambda: set_alignment("center")
            ).pack(side=tk.LEFT, padx=(0, 12))

            ttk.Radiobutton(
                align_frame, text="Right",
                variable=align_var, value="right",
                command=lambda: set_alignment("right")
            ).pack(side=tk.LEFT)

            row += 1

        ttk.Label(self.editor_box, text="Gap Before (px)").grid(row=row, column=0, sticky="w", pady=5)
        gap_before_var = tk.IntVar(value=block.get("gap_before", 0))

        def gap_before_changed(*_):
            try:
                block["gap_before"] = max(0, min(300, int(gap_before_var.get())))
            except Exception:
                pass
            self._schedule_preview()

        gap_before_var.trace_add("write", gap_before_changed)
        ttk.Spinbox(
            self.editor_box, from_=0, to=300,
            textvariable=gap_before_var, width=8
        ).grid(row=row, column=1, sticky="w", padx=8, pady=5)
        row += 1

        ttk.Label(self.editor_box, text="Gap After (px)").grid(row=row, column=0, sticky="w", pady=5)
        gap_var = tk.IntVar(value=block.get("gap", 10))

        def gap_changed(*_):
            try:
                block["gap"] = max(0, min(300, int(gap_var.get())))
            except Exception:
                pass
            self._schedule_preview()

        gap_var.trace_add("write", gap_changed)
        ttk.Spinbox(self.editor_box, from_=0, to=300, textvariable=gap_var, width=8).grid(
            row=row, column=1, sticky="w", padx=8, pady=5
        )

    # ---------------- Selection ----------------

    def select_txt(self):
        path = filedialog.askopenfilename(
            initialdir=str(SCRIPT_DIR),
            title="Select TXT File",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if path:
            self.txt_file = path
            self.txt_label.config(text=Path(path).name)
            self._schedule_preview()

    def select_template_pdf(self):
        path = filedialog.askopenfilename(
            initialdir=str(SCRIPT_DIR),
            title="Select Template PDF",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")]
        )
        if path:
            self.template_pdf = path
            self.template_label.config(text=Path(path).name)
            self._schedule_preview()

    def select_output_dir(self):
        path = filedialog.askdirectory(initialdir=str(SCRIPT_DIR))
        if path:
            self.output_dir = path
            self.output_dir_selected = True
            self.output_label.config(text=path)

    def get_auto_output_dir(self) -> str:
        """Create a new unique output folder beside this Python file."""
        base = SCRIPT_DIR / "output"
        if not base.exists():
            base.mkdir(parents=True)
            return str(base)

        n = 2
        while True:
            candidate = SCRIPT_DIR / f"output-{n}"
            if not candidate.exists():
                candidate.mkdir(parents=True)
                return str(candidate)
            n += 1

    def get_titles(self):
        if self.txt_file:
            try:
                text = Path(self.txt_file).read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = Path(self.txt_file).read_text(encoding="utf-8")
            titles = [line.strip() for line in text.splitlines() if line.strip()]
            return titles

        title = self.manual_title.get().strip()
        return [title] if title else []

    # ---------------- Filename logic ----------------

    def build_filename(self, title: str, index: int) -> str:
        try:
            length = max(1, min(200, int(self.filename_length.get())))
        except Exception:
            length = 60

        mode = self.filename_mode.get()

        if mode == "random":
            core = random_string(length)
        elif mode == "english":
            core = clean_english_filename(title)[:length].strip("-")
            if not core:
                core = random_string(min(length, 12))
        else:
            core = clean_unicode_filename(title)[:length].strip("-")
            if not core:
                core = random_string(min(length, 12))

        pieces = []
        pfx = clean_affix(self.prefix.get())
        sfx = clean_affix(self.suffix.get())

        if pfx:
            pieces.append(pfx)
        pieces.append(core)
        if sfx:
            pieces.append(sfx)

        if self.serial_enabled.get():
            try:
                start = int(self.serial_start.get())
            except Exception:
                start = 1
            try:
                padding = max(1, min(12, int(self.serial_padding.get())))
            except Exception:
                padding = 3
            serial = str(start + index).zfill(padding)
            pieces.append(serial)  # ALWAYS LAST

        stem = "-".join(x.strip("-") for x in pieces if x.strip("-"))
        return stem + ".pdf"

    def unique_output_path(self, filename: str) -> Path:
        out = Path(self.output_dir) / filename
        if not out.exists():
            return out

        stem = out.stem
        suffix = out.suffix
        n = 2
        while True:
            candidate = out.with_name(f"{stem}-{n}{suffix}")
            if not candidate.exists():
                return candidate
            n += 1

    def get_page_size_inches(self):
        """Use template first-page size; otherwise A4."""
        if self.template_pdf and Path(self.template_pdf).is_file():
            reader = PdfReader(self.template_pdf)
            page = reader.pages[0]
            return float(page.mediabox.width) / 72.0, float(page.mediabox.height) / 72.0
        return 8.27, 11.69

    def merge_overlay_with_template(self, overlay_pdf: Path, output_pdf: Path):
        """Overlay generated page 1 on template page 1; preserve other template pages."""
        template_reader = PdfReader(self.template_pdf)
        overlay_reader = PdfReader(str(overlay_pdf))
        writer = PdfWriter()

        if not template_reader.pages:
            raise ValueError("Template PDF has no pages.")

        first = template_reader.pages[0]
        if overlay_reader.pages:
            first.merge_page(overlay_reader.pages[0])
        writer.add_page(first)

        for page in template_reader.pages[1:]:
            writer.add_page(page)

        for page in overlay_reader.pages[1:]:
            writer.add_page(page)

        with open(output_pdf, "wb") as f:
            writer.write(f)

    # ---------------- HTML / PDF ----------------

    def build_html(self, title: str, page_width_in=8.27, page_height_in=11.69) -> str:
        body_parts = []

        for block in self.blocks:
            gap_before = int(block.get("gap_before", 0))
            gap = int(block.get("gap", 10))

            if block["type"] == "title":
                size = int(block.get("font_size", 28))
                align = block.get("align", "center")
                body_parts.append(
                    f'<div class="block title" style="font-size:{size}px;'
                    f'text-align:{align};margin-top:{gap_before}px;margin-bottom:{gap}px;">{html.escape(title)}</div>'
                )

            elif block["type"] == "image":
                path = block.get("path", "").strip()
                if not path or not Path(path).is_file():
                    continue

                width = max(5, min(100, int(block.get("width", 70))))
                data_uri = file_to_data_uri(path)
                img = (
                    f'<img src="{data_uri}" alt="" '
                    f'style="width:{width}%;height:auto;display:block;margin:0 auto;">'
                )

                link = block.get("link", "").strip()
                if link and valid_http_url(link):
                    img = (
                        f'<a href="{html.escape(link, quote=True)}" '
                        f'style="display:block;text-decoration:none;">{img}</a>'
                    )

                body_parts.append(
                    f'<div class="block image-block" style="margin-top:{gap_before}px;margin-bottom:{gap}px;">{img}</div>'
                )

            elif block["type"] == "extra_title":
                text = title
                if not text.strip():
                    continue

                size = int(block.get("font_size", 22))
                align = block.get("align", "center")
                safe = html.escape(text).replace("\n", "<br>")
                body_parts.append(
                    f'<div class="block title" style="font-size:{size}px;'
                    f'text-align:{align};margin-top:{gap_before}px;margin-bottom:{gap}px;">{safe}</div>'
                )

            elif block["type"] == "text":
                text = block.get("text", "")
                if not text.strip():
                    continue

                size = int(block.get("font_size", 14))
                align = block.get("align", "center")
                safe = html.escape(text).replace("\n", "<br>")
                body_parts.append(
                    f'<div class="block text" style="font-size:{size}px;'
                    f'text-align:{align};margin-top:{gap_before}px;margin-bottom:{gap}px;">{safe}</div>'
                )

        title_meta = html.escape(title)
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title_meta}</title>
<style>
@page {{
    size: {page_width_in:.4f}in {page_height_in:.4f}in;
    margin: 0;
}}
html, body {{
    margin: 0;
    padding: 0;
}}
body {{
    box-sizing: border-box;
    padding: {int(self.content_top.get())}px 18mm 18mm 18mm;
    font-family:
        Arial,
        "Noto Sans",
        "Noto Sans Bengali",
        "Noto Sans Arabic",
        "Noto Sans Devanagari",
        "Noto Sans Thai",
        "Noto Sans CJK SC",
        "Noto Sans CJK JP",
        "Noto Sans CJK KR",
        sans-serif;
    text-align: center;
    color: #111;
    line-height: 1.45;
    word-wrap: break-word;
}}
.block {{
    width: 100%;
    box-sizing: border-box;
    break-inside: avoid;
}}
.title {{
    font-weight: 700;
    line-height: 1.2;
}}
.text {{
    white-space: normal;
}}
img {{
    max-width: 100%;
}}
a {{
    color: inherit;
}}
</style>
</head>
<body>
{''.join(body_parts)}
</body>
</html>
"""

    def apply_pdf_metadata(self, pdf_path: Path, title: str):
        """Set PDF /Title metadata while keeping the generated pages and links."""
        reader = PdfReader(str(pdf_path))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        old = reader.metadata or {}
        metadata = {}
        for k, v in old.items():
            if isinstance(k, str) and isinstance(v, str):
                metadata[k] = v

        metadata["/Title"] = title
        metadata.setdefault("/Creator", APP_TITLE)
        metadata.setdefault("/Producer", "Chromium + pypdf")
        writer.add_metadata(metadata)

        temp = pdf_path.with_suffix(".metadata.tmp.pdf")
        with open(temp, "wb") as f:
            writer.write(f)

        os.replace(temp, pdf_path)

    def generate_pdfs(self):
        titles = self.get_titles()
        if not titles:
            messagebox.showerror(APP_TITLE, "Title is required. Enter a title or select a TXT file.")
            return

        # Validate optional image link(s)
        for block in self.blocks:
            if block["type"] == "image":
                link = block.get("link", "").strip()
                if link and not valid_http_url(link):
                    messagebox.showerror(
                        APP_TITLE,
                        f"Invalid image URL:\n{link}\n\nUse http:// or https://",
                    )
                    return

        if not self.output_dir_selected:
            self.output_dir = self.get_auto_output_dir()
            self.output_label.config(text=self.output_dir)
        else:
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        total = len(titles)
        self.progress_var.set(0)
        self.progress_text.config(text=f"0% • 0 / {total} completed")
        self.status.config(text=f"Generating 0/{total} PDF(s)...")
        self.update_idletasks()

        generated = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()

                page_width_in, page_height_in = self.get_page_size_inches()

                for i, title in enumerate(titles):
                    filename = self.build_filename(title, i)
                    out_path = self.unique_output_path(filename)

                    self.status.config(
                        text=f"Generating {i + 1}/{total}: {out_path.name}"
                    )
                    self.update_idletasks()

                    html_text = self.build_html(
                        title,
                        page_width_in=page_width_in,
                        page_height_in=page_height_in
                    )

                    page.set_content(html_text, wait_until="load")
                    page.emulate_media(media="print")

                    if self.template_pdf and Path(self.template_pdf).is_file():
                        with tempfile.NamedTemporaryFile(
                            suffix=".pdf", delete=False, dir=self.output_dir
                        ) as tmp:
                            overlay_path = Path(tmp.name)

                        try:
                            page.pdf(
                                path=str(overlay_path),
                                width=f"{page_width_in}in",
                                height=f"{page_height_in}in",
                                print_background=True,
                                prefer_css_page_size=True,
                                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                            )
                            self.merge_overlay_with_template(overlay_path, out_path)
                        finally:
                            try:
                                overlay_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                    else:
                        page.pdf(
                            path=str(out_path),
                            width=f"{page_width_in}in",
                            height=f"{page_height_in}in",
                            print_background=True,
                            prefer_css_page_size=True,
                            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                        )

                    self.apply_pdf_metadata(out_path, title)
                    generated.append(out_path)

                    completed = i + 1
                    percent = (completed / total) * 100
                    self.progress_var.set(percent)
                    self.progress_text.config(
                        text=f"{percent:.0f}% • {completed} / {total} completed"
                    )
                    self.status.config(
                        text=f"Generated {completed}/{total}: {out_path.name}"
                    )
                    self.update_idletasks()

                browser.close()

        except Exception as e:
            messagebox.showerror(
                APP_TITLE,
                "PDF generation failed.\n\n"
                f"{e}\n\n"
                "If Chromium is not installed, run:\n"
                "python -m playwright install chromium",
            )
            self.status.config(text="Generation failed")
            self.progress_text.config(text="Generation failed")
            return

        self.progress_var.set(100)
        self.progress_text.config(
            text=f"100% • {len(generated)} / {len(generated)} completed"
        )
        self.status.config(text=f"Done — {len(generated)} PDF(s) created")
        messagebox.showinfo(
            APP_TITLE,
            f"Completed.\n\nCreated: {len(generated)} PDF(s)\nFolder:\n{self.output_dir}",
        )

    # ---------------- Preview ----------------

    def _schedule_preview(self):
        if hasattr(self, "_preview_job"):
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.after(120, self.draw_preview)

    def draw_preview(self):
        c = self.preview
        c.delete("all")
        self.preview_image_refs = []

        cw = max(300, c.winfo_width())
        ch = max(400, c.winfo_height())

        max_w = cw - 50
        max_h = ch - 50

        preview_ratio = A4_RATIO
        if self.template_pdf and Path(self.template_pdf).is_file():
            try:
                pdf_doc = fitz.open(self.template_pdf)
                if len(pdf_doc) > 0 and pdf_doc[0].rect.height:
                    preview_ratio = pdf_doc[0].rect.width / pdf_doc[0].rect.height
                pdf_doc.close()
            except Exception:
                preview_ratio = A4_RATIO

        page_w = min(max_w, max_h * preview_ratio)
        page_h = page_w / preview_ratio
        if page_h > max_h:
            page_h = max_h
            page_w = page_h * preview_ratio

        x0 = (cw - page_w) / 2
        y0 = (ch - page_h) / 2
        x1 = x0 + page_w
        y1 = y0 + page_h

        # Page background: render selected template first page, otherwise white.
        template_drawn = False
        if self.template_pdf and Path(self.template_pdf).is_file():
            try:
                pdf_doc = fitz.open(self.template_pdf)
                pdf_page = pdf_doc[0]

                # Render first page at a practical preview resolution.
                target_px_w = max(300, int(page_w * 2))
                zoom = target_px_w / max(1, pdf_page.rect.width)
                pix = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)

                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                img = img.resize((max(1, int(page_w)), max(1, int(page_h))), Image.LANCZOS)

                self.template_preview_ref = ImageTk.PhotoImage(img)
                c.create_image(x0, y0, image=self.template_preview_ref, anchor="nw")
                c.create_rectangle(x0, y0, x1, y1, outline="#999")
                template_drawn = True

                pdf_doc.close()
            except Exception:
                self.template_preview_ref = None

        if not template_drawn:
            c.create_rectangle(x0, y0, x1, y1, fill="white", outline="#999")

        titles = self.get_titles()
        title = titles[0] if titles else "TITLE REQUIRED"

        content_x = (x0 + x1) / 2
        content_w = page_w * 0.82
        # Scale CSS-ish pixels to preview pixels
        scale = page_w / 794.0  # approx A4 @ 96dpi
        try:
            top_px = max(0, int(self.content_top.get()))
        except Exception:
            top_px = 20
        y = y0 + (top_px * scale)

        for block in self.blocks:
            gap_before = block.get("gap_before", 0) * scale
            gap = block.get("gap", 10) * scale
            y += gap_before

            if block["type"] == "title":
                fs = max(7, int(block.get("font_size", 28) * scale))
                align = block.get("align", "center")
                anchor_x = content_x
                anchor = "n"
                if align == "left":
                    anchor_x = content_x - content_w / 2
                    anchor = "nw"
                elif align == "right":
                    anchor_x = content_x + content_w / 2
                    anchor = "ne"

                item = c.create_text(
                    anchor_x,
                    y,
                    text=title,
                    width=content_w,
                    justify=align,
                    anchor=anchor,
                    font=("Arial", fs, "bold"),
                    fill="#111",
                )
                bbox = c.bbox(item)
                y = bbox[3] + gap if bbox else y + fs * 1.5 + gap

            elif block["type"] == "extra_title":
                text = title
                if not text.strip():
                    continue
                fs = max(7, int(block.get("font_size", 22) * scale))
                align = block.get("align", "center")
                anchor_x = content_x
                anchor = "n"
                if align == "left":
                    anchor_x = content_x - content_w / 2
                    anchor = "nw"
                elif align == "right":
                    anchor_x = content_x + content_w / 2
                    anchor = "ne"

                item = c.create_text(
                    anchor_x,
                    y,
                    text=text,
                    width=content_w,
                    justify=align,
                    anchor=anchor,
                    font=("Arial", fs, "bold"),
                    fill="#111",
                )
                bbox = c.bbox(item)
                y = bbox[3] + gap if bbox else y + fs * 1.5 + gap

            elif block["type"] == "text":
                text = block.get("text", "")
                if not text.strip():
                    continue
                fs = max(6, int(block.get("font_size", 14) * scale))
                align = block.get("align", "center")
                anchor_x = content_x
                anchor = "n"
                if align == "left":
                    anchor_x = content_x - content_w / 2
                    anchor = "nw"
                elif align == "right":
                    anchor_x = content_x + content_w / 2
                    anchor = "ne"

                item = c.create_text(
                    anchor_x,
                    y,
                    text=text,
                    width=content_w,
                    justify=align,
                    anchor=anchor,
                    font=("Arial", fs),
                    fill="#111",
                )
                bbox = c.bbox(item)
                y = bbox[3] + gap if bbox else y + fs * 1.5 + gap

            elif block["type"] == "image":
                path = block.get("path", "")
                if not path or not Path(path).is_file():
                    continue

                try:
                    img = Image.open(path)
                    target_w = max(20, int(content_w * block.get("width", 70) / 100))
                    ratio = target_w / img.width
                    target_h = max(1, int(img.height * ratio))

                    # Keep preview from overflowing too aggressively
                    max_img_h = int(page_h * 0.45)
                    if target_h > max_img_h:
                        ratio2 = max_img_h / target_h
                        target_h = max_img_h
                        target_w = max(1, int(target_w * ratio2))

                    img = img.convert("RGB")
                    img.thumbnail((target_w, target_h), Image.LANCZOS)
                    preview_ref = ImageTk.PhotoImage(img)
                    self.preview_image_refs.append(preview_ref)
                    c.create_image(content_x, y, image=preview_ref, anchor="n")
                    y += img.height + gap
                except Exception:
                    c.create_text(
                        content_x,
                        y,
                        text="[Image preview unavailable]",
                        anchor="n",
                        fill="#777",
                    )
                    y += 30 + gap

        c.create_text(
            content_x,
            y1 - 12,
            text="Preview • actual PDF uses Chromium rendering",
            anchor="s",
            fill="#888",
            font=("Arial", 8),
        )


if __name__ == "__main__":
    app = PDFBuilderApp()
    app.mainloop()
