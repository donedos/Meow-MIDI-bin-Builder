#!/usr/bin/env python3
"""Build ZIP-based .bin archives from MIDI and karaoke MIDI files."""

from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import shutil
import struct
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


MIDI_EXTENSIONS = {".mid", ".midi", ".kar"}
ARCHIVE_PREFIX = "midi/"
ARCHIVE_NAME = "meow.bin"
ARCHIVE_MAPPING_NAME = "mapping.md"
MAPPING_NAME = "meowmap.md"
FILENAME_CODE_PATTERN = re.compile(r"^\s*(\d+)\s*-\s*(.*)$")
MIDI_TAG_PATTERN = re.compile(r"@([tsga])([^@]*)", re.IGNORECASE)
LABELED_METADATA_PATTERN = re.compile(
    r"(?im)^\s*(title|artist|genre|album)\s*:\s*(.*?)\s*$"
)
MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_USER_AGENT = "SongHubTool/1.0 (MIDI metadata lookup)"
_musicbrainz_request_lock = threading.Lock()
_musicbrainz_last_request = 0.0


@dataclass
class Song:
    path: Path
    relative_path: str
    title: str
    code: str
    size_bytes: int
    artist: str = ""
    genre: str = ""
    album: str = ""

    @property
    def archive_path(self) -> str:
        return ARCHIVE_PREFIX + self.relative_path


@dataclass(frozen=True)
class ArtistMatch:
    artist: str
    recording_title: str
    score: int


def search_musicbrainz_artists(title: str) -> list[ArtistMatch]:
    """Search MusicBrainz recordings by title and return credited artists."""
    global _musicbrainz_last_request

    title = title.strip()
    if not title:
        raise ValueError("Song title cannot be blank.")

    query = urllib.parse.urlencode(
        {"query": f'recording:"{title}"', "fmt": "json", "limit": 10}
    )
    request = urllib.request.Request(
        f"{MUSICBRAINZ_SEARCH_URL}?{query}",
        headers={"User-Agent": MUSICBRAINZ_USER_AGENT},
    )
    try:
        with _musicbrainz_request_lock:
            wait = 1.0 - (time.monotonic() - _musicbrainz_last_request)
            if wait > 0:
                time.sleep(wait)
            _musicbrainz_last_request = time.monotonic()
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError(f"MusicBrainz lookup failed: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("MusicBrainz returned an unexpected response.")
    if data.get("error"):
        raise RuntimeError(f"MusicBrainz returned an error: {data['error']}")
    recordings = data.get("recordings", [])
    if not isinstance(recordings, list):
        raise RuntimeError("MusicBrainz returned an invalid recordings list.")

    matches: list[ArtistMatch] = []
    seen_artists: set[str] = set()
    for recording in recordings:
        if not isinstance(recording, dict):
            continue
        artist_credit = recording.get("artist-credit", [])
        if not isinstance(artist_credit, list):
            continue
        credited_artists = []
        for credit in artist_credit:
            if isinstance(credit, str):
                credited_artists.append(credit)
            elif isinstance(credit, dict):
                artist = credit.get("artist", {})
                if not isinstance(artist, dict):
                    artist = {}
                name = artist.get("name") or credit.get("name", "")
                if isinstance(name, str) and name:
                    credited_artists.append(name)
                joinphrase = credit.get("joinphrase", "")
                if isinstance(joinphrase, str):
                    credited_artists.append(joinphrase)
        artist_name = "".join(credited_artists).strip()
        if not artist_name or artist_name.casefold() in seen_artists:
            continue
        seen_artists.add(artist_name.casefold())
        try:
            score = int(recording.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        recording_title = recording.get("title", "")
        matches.append(
            ArtistMatch(
                artist=artist_name,
                recording_title=(
                    recording_title if isinstance(recording_title, str) else ""
                ),
                score=score,
            )
        )
    return matches


def scan_midi_files(folder: Path, progress_callback=None) -> list[Song]:
    """Find supported files recursively, skipping symbolic links."""
    root = folder.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Not a folder: {folder}")

    songs: list[Song] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories
            if not (current_path / name).is_symlink()
        )
        for filename in filenames:
            path = current_path / filename
            if path.is_symlink() or path.suffix.casefold() not in MIDI_EXTENSIONS:
                continue
            relative_path = path.relative_to(root).as_posix()
            filename_match = FILENAME_CODE_PATTERN.match(path.stem)
            filename_title = (
                filename_match.group(2).strip() if filename_match else path.stem
            )
            midi_metadata = _read_midi_metadata(path)
            songs.append(
                Song(
                    path=path,
                    relative_path=relative_path,
                    title=midi_metadata.get("title") or filename_title,
                    code=(
                        f"{int(filename_match.group(1)):06d}"
                        if filename_match
                        else ""
                    ),
                    size_bytes=path.stat().st_size,
                    artist=midi_metadata.get("artist", ""),
                    genre=midi_metadata.get("genre", ""),
                    album=midi_metadata.get("album", ""),
                )
            )
            if progress_callback:
                progress_callback(len(songs), relative_path)

    songs.sort(key=lambda song: (song.relative_path.casefold(), song.relative_path))
    return songs


def _read_variable_length(data: bytes, offset: int, limit: int) -> tuple[int, int] | None:
    value = 0
    for _ in range(4):
        if offset >= limit:
            return None
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, offset
    return None


def _decode_midi_text(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        text = value.decode("latin-1")
    return text.strip("\x00 \t\r\n")


def _read_midi_metadata(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    if len(data) < 14 or data[:4] != b"MThd":
        return {}

    header_length = struct.unpack_from(">I", data, 4)[0]
    if header_length < 6 or 8 + header_length > len(data):
        return {}
    track_count = struct.unpack_from(">H", data, 10)[0]
    offset = 8 + header_length
    texts: list[str] = []

    for _ in range(track_count):
        if offset + 8 > len(data) or data[offset:offset + 4] != b"MTrk":
            break
        track_length = struct.unpack_from(">I", data, offset + 4)[0]
        track_start = offset + 8
        track_end = track_start + track_length
        if track_end > len(data):
            break
        offset = track_start
        running_status = 0

        while offset < track_end:
            delta = _read_variable_length(data, offset, track_end)
            if delta is None:
                break
            _, offset = delta
            if offset >= track_end:
                break

            status = data[offset]
            if status < 0x80:
                if running_status == 0:
                    break
                status = running_status
            else:
                offset += 1

            if status == 0xFF:
                if offset >= track_end:
                    break
                meta_type = data[offset]
                offset += 1
                length = _read_variable_length(data, offset, track_end)
                if length is None:
                    break
                payload_length, offset = length
                payload_end = offset + payload_length
                if payload_end > track_end:
                    break
                if meta_type in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07):
                    texts.append(_decode_midi_text(data[offset:payload_end]))
                offset = payload_end
                if meta_type == 0x2F:
                    break
            elif status in (0xF0, 0xF7):
                length = _read_variable_length(data, offset, track_end)
                if length is None:
                    break
                payload_length, offset = length
                if offset + payload_length > track_end:
                    break
                offset += payload_length
                running_status = 0
            elif 0x80 <= status <= 0xEF:
                running_status = status
                data_length = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
                if offset + data_length > track_end:
                    break
                offset += data_length
            else:
                system_lengths = {0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF6: 0}
                data_length = system_lengths.get(status)
                if data_length is None or offset + data_length > track_end:
                    break
                offset += data_length
                running_status = 0
        offset = track_end

    metadata: dict[str, str] = {}
    tag_names = {"t": "title", "s": "artist", "g": "genre", "a": "album"}
    for text in texts:
        for key, value in MIDI_TAG_PATTERN.findall(text):
            field = tag_names[key.casefold()]
            if value.strip():
                metadata.setdefault(field, value.strip())
        for field, value in LABELED_METADATA_PATTERN.findall(text):
            if value.strip():
                metadata.setdefault(field.casefold(), value.strip())
    return metadata


def _validate_songs(songs: list[Song], root: Path) -> None:
    if not songs:
        raise ValueError("No MIDI files found. Scan a folder containing .mid, .midi, or .kar files.")

    seen_codes: set[str] = set()
    seen_paths: set[str] = set()
    for song in songs:
        if not song.title.strip():
            raise ValueError(f"Song title cannot be blank: {song.relative_path}")
        code = song.code.strip()
        if not code:
            raise ValueError(f"Song code cannot be blank: {song.relative_path}")
        try:
            numeric_code = int(code)
        except ValueError:
            numeric_code = None
        normalized_code = (
            f"numeric:{numeric_code}"
            if numeric_code is not None and numeric_code > 0
            else f"text:{code.casefold()}"
        )
        if normalized_code in seen_codes:
            raise ValueError(f"Song code must be unique: {code}")
        seen_codes.add(normalized_code)

        source = song.path
        if source.is_symlink():
            raise ValueError(f"Symbolic links are not supported: {source}")
        resolved_source = source.resolve(strict=True)
        try:
            resolved_source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"File is outside the selected folder: {source}") from exc
        if not resolved_source.is_file():
            raise ValueError(f"Not a file: {source}")

        relative = Path(song.relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid relative file path: {song.relative_path}")
        archive_path = (ARCHIVE_PREFIX + relative.as_posix()).casefold()
        if archive_path in seen_paths:
            raise ValueError(f"Duplicate archive path: {song.relative_path}")
        seen_paths.add(archive_path)


def _markdown_cell(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def _write_mapping_markdown(file_object, songs: list[Song]) -> None:
    text = io.TextIOWrapper(file_object, encoding="utf-8", newline="")
    text.write(
        "| Song code | Title | Artist | Genre | Album | Archive path | Size (bytes) |\n"
    )
    text.write("| --- | --- | --- | --- | --- | --- | ---: |\n")
    for song in songs:
        cells = (
            song.code.strip(),
            song.title.strip(),
            song.artist.strip(),
            song.genre.strip(),
            song.album.strip(),
            song.archive_path,
            str(song.size_bytes),
        )
        text.write("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |\n")
    text.flush()
    text.detach()


def _write_songs_text(file_object, songs: list[Song]) -> None:
    text = io.TextIOWrapper(file_object, encoding="utf-8", newline="")
    writer = csv.writer(text, delimiter="\t", lineterminator="\n")
    writer.writerow(("Song code", "Song title", "Archive path"))
    for song in songs:
        writer.writerow((song.code.strip(), song.title.strip(), song.archive_path))
    text.flush()
    text.detach()


def create_bin(
    source_folder: Path,
    output_file: Path,
    songs: list[Song],
    progress_callback=None,
) -> Path:
    """Create the fixed-name compressed archive and matching Markdown mapping."""
    root = source_folder.expanduser().resolve(strict=True)
    output = output_file.expanduser().absolute()
    if output.name.casefold() != ARCHIVE_NAME:
        raise ValueError(f"Output filename must be {ARCHIVE_NAME}")
    if not output.parent.is_dir():
        raise ValueError(f"Output folder does not exist: {output.parent}")

    _validate_songs(songs, root)
    for song in songs:
        if song.path.stat().st_size != song.size_bytes:
            raise ValueError(f"File changed since scanning; scan again: {song.relative_path}")

    mapping_file = output.with_name(MAPPING_NAME)
    archive_fd, archive_temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    mapping_fd, mapping_temp_name = tempfile.mkstemp(
        prefix=f".{mapping_file.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(archive_fd)
    os.close(mapping_fd)
    archive_temp = Path(archive_temp_name)
    mapping_temp = Path(mapping_temp_name)

    try:
        with mapping_temp.open("wb") as mapping_stream:
            _write_mapping_markdown(mapping_stream, songs)

        with zipfile.ZipFile(
            archive_temp, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for index, song in enumerate(songs, start=1):
                info = zipfile.ZipInfo.from_file(song.path, arcname=song.archive_path)
                info.compress_type = zipfile.ZIP_DEFLATED
                with song.path.open("rb") as source, archive.open(
                    info, mode="w", force_zip64=True
                ) as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                if progress_callback:
                    progress_callback(index, len(songs), song.relative_path)

            with archive.open(ARCHIVE_MAPPING_NAME, mode="w") as member:
                _write_mapping_markdown(member, songs)
            with archive.open("songs.txt", mode="w") as member:
                _write_songs_text(member, songs)

        os.replace(archive_temp, output)
        os.replace(mapping_temp, mapping_file)
    finally:
        archive_temp.unlink(missing_ok=True)
        mapping_temp.unlink(missing_ok=True)

    return mapping_file


class MidiBinBuilder:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Meow MIDI .bin Builder")
        self.root.geometry("1050x680")
        self.root.minsize(780, 500)

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a folder containing MIDI files.")
        self.songs: list[Song] = []
        self.events: queue.Queue = queue.Queue()
        self.busy = False

        self._configure_button_style()
        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._populate_table())
        self.root.after(100, self._poll_events)

    def _configure_button_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "Blue.TButton",
            background="#1976D2",
            foreground="#FFFFFF",
            bordercolor="#1565C0",
            focuscolor="#90CAF9",
            padding=(10, 5),
        )
        style.map(
            "Blue.TButton",
            background=[
                ("disabled", "#BDBDBD"),
                ("pressed", "#0D47A1"),
                ("active", "#1565C0"),
            ],
            foreground=[("disabled", "#666666"), ("!disabled", "#FFFFFF")],
            bordercolor=[("disabled", "#BDBDBD")],
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(3, weight=1)

        ttk.Label(outer, text="MIDI folder").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
        self.source_entry = ttk.Entry(outer, textvariable=self.source_var)
        self.source_entry.grid(row=0, column=1, sticky="ew", pady=5)
        self.browse_source_button = ttk.Button(
            outer, text="Browse…", command=self._browse_source, style="Blue.TButton"
        )
        self.browse_source_button.grid(row=0, column=2, padx=(8, 0), pady=5)
        self.scan_button = ttk.Button(
            outer, text="Scan folder", command=self._scan, style="Blue.TButton"
        )
        self.scan_button.grid(row=0, column=3, padx=(8, 0), pady=5)

        ttk.Label(outer, text="Output .bin").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=5)
        ttk.Entry(outer, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", pady=5)
        self.browse_output_button = ttk.Button(
            outer, text="Save as…", command=self._browse_output, style="Blue.TButton"
        )
        self.browse_output_button.grid(row=1, column=2, columnspan=2, sticky="e", padx=(8, 0), pady=5)

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 6))
        ttk.Label(toolbar, text="Songs").pack(side="left")
        ttk.Label(toolbar, text="Search").pack(side="left", padx=(20, 6))
        search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=30)
        search_entry.pack(side="left")
        ttk.Label(
            toolbar,
            text="Double-click a song to edit its information.",
        ).pack(side="right")

        table_frame = ttk.Frame(outer)
        table_frame.grid(row=3, column=0, columnspan=4, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(
            table_frame,
            columns=("title", "code", "artist", "file", "size"),
            show="headings",
            selectmode="browse",
        )
        self.table.heading("title", text="Song title")
        self.table.heading("code", text="Song code")
        self.table.heading("artist", text="Artist / Singer")
        self.table.heading("file", text="MIDI file")
        self.table.heading("size", text="Size")
        self.table.column("title", width=220, minwidth=120)
        self.table.column("code", width=100, minwidth=90)
        self.table.column("artist", width=180, minwidth=100)
        self.table.column("file", width=360, minwidth=160)
        self.table.column("size", width=90, minwidth=75, anchor="e")
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<Double-1>", self._edit_selected)

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(footer, mode="determinate", length=180)
        self.progress.grid(row=0, column=1, padx=10)
        self.build_button = ttk.Button(
            footer, text="Create .bin", command=self._build_archive, style="Blue.TButton"
        )
        self.build_button.grid(row=0, column=2)

    def _browse_source(self) -> None:
        folder = filedialog.askdirectory(title="Choose MIDI folder")
        if folder:
            self.source_var.set(folder)
            self.output_var.set(str(Path(folder) / ARCHIVE_NAME))
            self._scan()

    def _browse_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Save MIDI archive",
            initialfile=ARCHIVE_NAME,
            defaultextension=".bin",
            filetypes=(("MIDI archive", "*.bin"), ("All files", "*")),
        )
        if filename:
            if Path(filename).name.casefold() != ARCHIVE_NAME:
                messagebox.showwarning("Use the required filename", f"The archive must be named {ARCHIVE_NAME}.")
                return
            self.output_var.set(filename)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.browse_source_button.configure(state=state)
        self.scan_button.configure(state=state)
        self.browse_output_button.configure(state=state)
        self.build_button.configure(state=state)
        self.source_entry.configure(state=state)

    def _scan(self) -> None:
        if self.busy:
            return
        folder = Path(self.source_var.get()).expanduser()
        if not self.source_var.get().strip():
            messagebox.showwarning("Choose a folder", "Select the folder containing your MIDI files.")
            return

        self._set_busy(True)
        self.status_var.set("Scanning folder…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        def report_progress(count: int, relative_path: str) -> None:
            if count == 1 or count % 25 == 0:
                self.events.put(("scan_progress", count, relative_path))

        def worker() -> None:
            try:
                result = scan_midi_files(folder, report_progress)
                self.events.put(("scan_done", result))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _populate_table(self) -> None:
        query = self.search_var.get().strip().casefold()
        self.table.delete(*self.table.get_children())
        visible = 0
        for index, song in enumerate(self.songs):
            if query and not any(
                query in value.casefold()
                for value in (
                    song.title,
                    song.code,
                    song.artist,
                    song.genre,
                    song.album,
                    song.relative_path,
                )
            ):
                continue
            self.table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    song.title,
                    song.code,
                    song.artist,
                    song.relative_path,
                    f"{song.size_bytes:,} B",
                ),
            )
            visible += 1
        if self.songs:
            self.status_var.set(f"{len(self.songs)} MIDI file(s) found; showing {visible}.")

    def _edit_selected(self, _event) -> None:
        if self.busy:
            return
        selected = self.table.selection()
        if not selected:
            return
        song = self.songs[int(selected[0])]

        dialog = tk.Toplevel(self.root)
        dialog.title("Edit song")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        fields = (
            ("Song title", "title", song.title),
            ("Song code", "code", song.code),
            ("Artist / Singer", "artist", song.artist),
            ("Genre", "genre", song.genre),
            ("Album", "album", song.album),
        )
        field_vars = {}
        first_entry = None
        for row, (label, key, value) in enumerate(fields):
            variable = tk.StringVar(value=value)
            field_vars[key] = variable
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(frame, textvariable=variable, width=42)
            entry.grid(row=row, column=1, pady=5)
            if key == "artist":
                lookup_button = ttk.Button(
                    frame,
                    text="Search MusicBrainz",
                    style="Blue.TButton",
                    command=lambda: self._lookup_artist(
                        dialog,
                        field_vars["title"],
                        field_vars["artist"],
                        lookup_button,
                    ),
                )
                lookup_button.grid(row=row, column=2, padx=(8, 0), pady=5)
            if first_entry is None:
                first_entry = entry

        def save() -> None:
            song.title = field_vars["title"].get().strip()
            song.code = field_vars["code"].get().strip()
            song.artist = field_vars["artist"].get().strip()
            song.genre = field_vars["genre"].get().strip()
            song.album = field_vars["album"].get().strip()
            dialog.destroy()
            self._populate_table()

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(
            buttons, text="Cancel", command=dialog.destroy, style="Blue.TButton"
        ).pack(side="right")
        ttk.Button(buttons, text="Save", command=save, style="Blue.TButton").pack(
            side="right", padx=(0, 8)
        )
        if first_entry:
            first_entry.focus_set()

    def _lookup_artist(
        self,
        dialog: tk.Toplevel,
        title_var: tk.StringVar,
        artist_var: tk.StringVar,
        button: ttk.Button,
    ) -> None:
        title = title_var.get().strip()
        if not title:
            messagebox.showwarning(
                "Missing song title",
                "Enter a song title before searching MusicBrainz.",
                parent=dialog,
            )
            return
        if artist_var.get().strip() and not messagebox.askyesno(
            "Replace artist?",
            "An artist is already entered. Search for a replacement?",
            parent=dialog,
        ):
            return

        button.configure(state="disabled")
        self.status_var.set(f"Searching MusicBrainz for “{title}”…")

        def worker() -> None:
            try:
                matches = search_musicbrainz_artists(title)
                self.events.put(
                    (
                        "artist_lookup_done",
                        dialog,
                        title,
                        artist_var,
                        button,
                        matches,
                        "",
                    )
                )
            except (RuntimeError, ValueError) as exc:
                self.events.put(
                    (
                        "artist_lookup_done",
                        dialog,
                        title,
                        artist_var,
                        button,
                        [],
                        str(exc),
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def _choose_artist_match(
        self,
        parent: tk.Toplevel,
        artist_var: tk.StringVar,
        matches: list[ArtistMatch],
    ) -> None:
        chooser = tk.Toplevel(parent)
        chooser.title("Choose artist")
        chooser.transient(parent)
        chooser.resizable(True, False)
        frame = ttk.Frame(chooser, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Select the correct recording. MusicBrainz results may include covers.",
        ).pack(anchor="w", pady=(0, 8))

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(
            list_frame,
            height=min(8, len(matches)),
            width=88,
            exportselection=False,
        )
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for match in matches:
            listbox.insert(
                "end",
                f'{match.artist} — {match.recording_title} (match score {match.score}%)',
            )
        listbox.selection_set(0)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))

        def use_selected() -> None:
            selection = listbox.curselection()
            if not selection:
                messagebox.showwarning(
                    "Choose a result",
                    "Select a MusicBrainz result first.",
                    parent=chooser,
                )
                return
            artist_var.set(matches[selection[0]].artist)
            chooser.destroy()

        ttk.Button(
            buttons,
            text="Cancel",
            command=chooser.destroy,
            style="Blue.TButton",
        ).pack(side="right")
        ttk.Button(
            buttons,
            text="Use selected artist",
            command=use_selected,
            style="Blue.TButton",
        ).pack(side="right", padx=(0, 8))
        listbox.bind("<Double-1>", lambda _event: use_selected())
        chooser.grab_set()
        listbox.focus_set()
        chooser.wait_window()

    def _build_archive(self) -> None:
        if self.busy:
            return
        if not self.source_var.get().strip() or not self.output_var.get().strip():
            messagebox.showwarning("Missing path", "Choose both a MIDI folder and an output .bin file.")
            return
        songs = [
            Song(
                song.path,
                song.relative_path,
                song.title,
                song.code,
                song.size_bytes,
                song.artist,
                song.genre,
                song.album,
            )
            for song in self.songs
        ]
        if not songs:
            messagebox.showwarning("No MIDI files", "Scan a folder before creating an archive.")
            return

        source_folder = Path(self.source_var.get()).expanduser()
        output_file = Path(self.output_var.get()).expanduser()
        mapping_file = output_file.with_name(MAPPING_NAME)
        existing = [path for path in (output_file, mapping_file) if path.exists()]
        if existing and not messagebox.askyesno(
            "Replace existing files?",
            "The following output file(s) already exist:\n\n"
            + "\n".join(str(path) for path in existing)
            + "\n\nReplace them?",
        ):
            return
        self._set_busy(True)
        self.progress.configure(mode="determinate", maximum=len(songs), value=0)
        self.status_var.set("Creating compressed archive…")

        def report_progress(done: int, total: int, filename: str) -> None:
            self.events.put(("progress", done, total, filename))

        def worker() -> None:
            try:
                mapping = create_bin(source_folder, output_file, songs, report_progress)
                self.events.put(("build_done", output_file, mapping))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, done, total, filename = event
                    self.progress.configure(maximum=total, value=done)
                    self.status_var.set(f"Compressing {done}/{total}: {filename}")
                elif event[0] == "scan_progress":
                    _, count, filename = event
                    self.status_var.set(f"Scanning MIDI files… {count} found: {filename}")
                elif event[0] == "scan_done":
                    self.songs = event[1]
                    self.progress.stop()
                    self.progress.configure(
                        mode="determinate",
                        maximum=max(1, len(self.songs)),
                        value=0,
                    )
                    self._populate_table()
                    if not self.songs:
                        self.status_var.set("No .mid, .midi, or .kar files found.")
                    self._set_busy(False)
                elif event[0] == "artist_lookup_done":
                    _, dialog, title, artist_var, button, matches, error = event
                    if not dialog.winfo_exists():
                        continue
                    button.configure(state="normal")
                    if error:
                        self.status_var.set("MusicBrainz artist lookup failed.")
                        messagebox.showerror(
                            "Artist lookup failed",
                            error,
                            parent=dialog,
                        )
                    elif not matches:
                        self.status_var.set("No MusicBrainz artist matches found.")
                        messagebox.showinfo(
                            "No matches",
                            "MusicBrainz returned no artist matches for this title.",
                            parent=dialog,
                        )
                    else:
                        self.status_var.set(
                            f"MusicBrainz found {len(matches)} candidate(s) for "
                            f"{title}."
                        )
                        self._choose_artist_match(dialog, artist_var, matches)
                elif event[0] == "build_done":
                    _, output, mapping = event
                    self.progress.configure(value=self.progress["maximum"])
                    self.status_var.set(f"Created {output.name} and {mapping.name}")
                    self._set_busy(False)
                    messagebox.showinfo(
                        "Archive created",
                        f"Archive:\n{output}\n\nMapping:\n{mapping}",
                    )
                elif event[0] == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.status_var.set("Operation failed.")
                    self._set_busy(False)
                    messagebox.showerror("Could not complete operation", event[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)


def main() -> None:
    root = tk.Tk()
    MidiBinBuilder(root)
    root.mainloop()


if __name__ == "__main__":
    main()
