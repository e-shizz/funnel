#!/usr/bin/env python3
"""Tiny drag-and-drop GUI for the recovered one-step Funnel packer."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from funnel.pack import pack_path
from funnel.paths import ensure_layout


def parse_uri_list(value: object) -> list[Path]:
    """Decode local file URIs from Dolphin's text/uri-list payload."""
    if isinstance(value, (bytes, bytearray)):
        lines = bytes(value).decode("utf-8").splitlines()
    elif isinstance(value, str):
        lines = value.splitlines()
    else:
        lines = [str(item) for item in value] if value is not None else []
    paths: list[Path] = []
    for line in lines:
        uri = line.strip()
        if not uri or uri.startswith("#"):
            continue
        parsed = urlsplit(uri)
        if parsed.scheme.casefold() != "file":
            raise ValueError("Funnel accepts only local file:// paths; remote URIs are not supported.")
        if parsed.netloc not in ("", "localhost"):
            raise ValueError("Funnel accepts only local file:// paths; remote hosts are not supported.")
        try:
            decoded = unquote_to_bytes(parsed.path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Dropped path is not valid UTF-8: {exc}") from exc
        paths.append(Path(decoded))
    return paths


def single_input_path(paths: list[Path]) -> Path:
    if len(paths) != 1:
        raise ValueError("Drop exactly one local folder, archive, or EXE at a time.")
    return paths[0]


class AddWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Funnel")
        self.set_icon_name("funnel")
        self.set_default_size(440, 260)
        self.set_border_width(14)
        self.set_position(Gtk.WindowPosition.CENTER)
        self._working = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(root)

        self.drop_area = Gtk.EventBox()
        frame = Gtk.Frame()
        self.drop_area.add(frame)
        drop_label = Gtk.Label(label="Drop a Windows folder, ZIP, RAR, 7z, or EXE here")
        drop_label.set_line_wrap(True)
        drop_label.set_justify(Gtk.Justification.CENTER)
        drop_label.set_margin_start(24)
        drop_label.set_margin_end(24)
        drop_label.set_margin_top(55)
        drop_label.set_margin_bottom(55)
        frame.add(drop_label)
        root.pack_start(self.drop_area, True, True, 0)

        self.status = Gtk.Label(label="Ready")
        self.status.set_line_wrap(True)
        self.status.set_selectable(True)
        self.status.set_halign(Gtk.Align.START)
        root.pack_start(self.status, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        row.pack_start(self.spinner, False, False, 0)
        self.choose_button = Gtk.Button(label="Choose…")
        self.choose_button.connect("clicked", self._choose)
        row.pack_end(self.choose_button, False, False, 0)
        root.pack_end(row, False, False, 0)

        targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]
        for widget in (self, self.drop_area):
            widget.drag_dest_set(Gtk.DestDefaults.ALL, targets, Gdk.DragAction.COPY)
            widget.connect("drag-data-received", self._drop)
        self.connect("destroy", Gtk.main_quit)

    def _drop(self, _widget, context, _x, _y, data, _info, event_time) -> None:
        accepted = False
        try:
            raw = data.get_uris() or data.get_data()
            path = single_input_path(parse_uri_list(raw))
            accepted = self._start(path)
        except Exception as exc:
            self.status.set_text(str(exc))
        context.finish(accepted, False, event_time)

    def _choose(self, *_args) -> None:
        if self._working:
            return
        dialog = Gtk.FileChooserDialog(
            title="Choose one Windows folder, archive, or EXE",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            "Choose file", Gtk.ResponseType.OK,
            "Choose folder…", 1001,
        )
        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if response == 1001:
            folder = Gtk.FileChooserDialog(
                title="Choose one Windows application folder", parent=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            folder.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
            if folder.run() == Gtk.ResponseType.OK:
                path = folder.get_filename()
            folder.destroy()
        if path:
            self._start(Path(path))

    def _start(self, path: Path | str) -> bool:
        if self._working:
            self.status.set_text("A conversion is already in progress.")
            return False
        source = Path(path).expanduser()
        if not source.exists():
            self.status.set_text(f"Input does not exist: {source}")
            return False
        self._working = True
        self.choose_button.set_sensitive(False)
        self.spinner.start()
        self.status.set_text(f"Converting…\n{source}")

        def worker() -> None:
            try:
                result = pack_path(
                    source, verbose=True,
                    status_callback=lambda message: GLib.idle_add(self.status.set_text, message),
                )
                GLib.idle_add(self._done, result, None)
            except Exception as exc:
                GLib.idle_add(self._done, None, exc)

        threading.Thread(target=worker, daemon=True, name="funnel-pack").start()
        return True

    def _done(self, result, error) -> bool:
        self._working = False
        self.choose_button.set_sensitive(True)
        self.spinner.stop()
        if error is not None:
            self.status.set_text(f"Failed:\n{error}")
        elif not result.ok:
            self.status.set_text(f"Failed:\n{result.error}")
        else:
            applications = [result] + result.published_apps
            paths = "\n".join(str(application.desktop_copy) for application in applications)
            self.status.set_text(f"Done: {result.display_name}\nDesktop applications created at:\n{paths}")
        return False


def main() -> int:
    ensure_layout()
    window = AddWindow()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["AddWindow", "main", "parse_uri_list", "single_input_path"]
