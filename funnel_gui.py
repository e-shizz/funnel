#!/usr/bin/env python3
"""Funnel window: drop a game in at the top, manage your converted games below."""

from __future__ import annotations

import subprocess
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
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from funnel import library
from funnel.pack import pack_path
from funnel.paths import LOGS, ensure_layout

ICON_SIZE = 40


def parse_uri_list(value: object) -> list[Path]:
    """Decode local file URIs from a file manager's text/uri-list payload."""
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


def _open_location(path: Path | None) -> None:
    if path is None:
        return
    target = path if path.is_dir() else path.parent
    if target.is_dir():
        Gio.AppInfo.launch_default_for_uri(target.as_uri(), None)


def _icon_widget(record: library.Record) -> Gtk.Image:
    icon = record.path("icon")
    if icon and icon.is_file():
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(icon), ICON_SIZE, ICON_SIZE, True)
            return Gtk.Image.new_from_pixbuf(pixbuf)
        except GLib.Error:
            pass
    image = Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)
    image.set_pixel_size(ICON_SIZE)
    return image


class GameRow(Gtk.ListBoxRow):
    def __init__(self, window: "FunnelWindow", record: library.Record) -> None:
        super().__init__()
        self.record = record
        self.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        self.add(box)

        box.pack_start(_icon_widget(record), False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(record.display_name)}</b>")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        runtime = str(record.data.get("runtime_kind") or "proton").upper()
        detail = f"{record.slug} · {runtime}"
        if not record.installed():
            detail += " · launcher missing"
        subtitle = Gtk.Label(label=detail, xalign=0)
        subtitle.get_style_context().add_class("dim-label")
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        text.pack_start(title, False, False, 0)
        text.pack_start(subtitle, False, False, 0)
        box.pack_start(text, True, True, 0)

        play = Gtk.Button(label="Play")
        play.get_style_context().add_class("suggested-action")
        play.set_valign(Gtk.Align.CENTER)
        play.set_sensitive(record.installed())
        play.set_tooltip_text(f"Launch {record.display_name}")
        play.connect("clicked", lambda *_: window.play(record))
        box.pack_start(play, False, False, 0)

        menu = Gtk.Menu()
        for label, action in (
            ("Open game folder", lambda *_: _open_location(record.path("app_dir") or record.path("exe"))),
            ("Open logs", lambda *_: _open_location(LOGS)),
            (None, None),
            ("Remove…", lambda *_: window.confirm_remove(record)),
        ):
            item = Gtk.SeparatorMenuItem() if label is None else Gtk.MenuItem(label=label)
            if action is not None:
                item.connect("activate", action)
            menu.append(item)
        menu.show_all()
        more = Gtk.MenuButton(popup=menu)
        more.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        more.set_valign(Gtk.Align.CENTER)
        more.set_tooltip_text("More actions")
        box.pack_start(more, False, False, 0)


class FunnelWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Funnel")
        self.set_icon_name("funnel")
        self.set_default_size(520, 600)
        self.set_border_width(14)
        self.set_position(Gtk.WindowPosition.CENTER)
        self._working = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(root)

        # Drop area
        self.drop_area = Gtk.EventBox()
        frame = Gtk.Frame()
        self.drop_area.add(frame)
        drop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        drop_box.set_margin_top(22)
        drop_box.set_margin_bottom(22)
        drop_box.set_margin_start(18)
        drop_box.set_margin_end(18)
        drop_label = Gtk.Label(label="Drop a Windows game folder, ZIP, RAR, 7z, EXE, or installer here")
        drop_label.set_line_wrap(True)
        drop_label.set_justify(Gtk.Justification.CENTER)
        drop_box.pack_start(drop_label, False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.CENTER)
        self.file_button = Gtk.Button(label="Choose file…")
        self.file_button.connect("clicked", lambda *_: self._choose(Gtk.FileChooserAction.OPEN))
        self.folder_button = Gtk.Button(label="Choose folder…")
        self.folder_button.connect("clicked", lambda *_: self._choose(Gtk.FileChooserAction.SELECT_FOLDER))
        buttons.pack_start(self.file_button, False, False, 0)
        buttons.pack_start(self.folder_button, False, False, 0)
        drop_box.pack_start(buttons, False, False, 0)
        frame.add(drop_box)
        root.pack_start(self.drop_area, False, False, 0)

        # Status
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.spinner = Gtk.Spinner()
        status_row.pack_start(self.spinner, False, False, 0)
        self.status = Gtk.Label(label="Ready", xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_selectable(True)
        status_row.pack_start(self.status, True, True, 0)
        root.pack_start(status_row, False, False, 0)

        # Library
        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Your games</b>")
        heading.set_margin_top(6)
        root.pack_start(heading, False, False, 0)

        self.games = Gtk.ListBox()
        self.games.set_selection_mode(Gtk.SelectionMode.NONE)
        placeholder = Gtk.Label(label="No games yet. Drop one above to get started.")
        placeholder.get_style_context().add_class("dim-label")
        placeholder.set_margin_top(30)
        placeholder.set_margin_bottom(30)
        placeholder.show()
        self.games.set_placeholder(placeholder)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.games)
        root.pack_start(scroller, True, True, 0)

        targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0)]
        for widget in (self, self.drop_area):
            widget.drag_dest_set(Gtk.DestDefaults.ALL, targets, Gdk.DragAction.COPY)
            widget.connect("drag-data-received", self._drop)
        self.connect("destroy", Gtk.main_quit)
        self.refresh()

    # Library -----------------------------------------------------------------

    def refresh(self) -> None:
        for row in self.games.get_children():
            self.games.remove(row)
        records = sorted(library.launchers(), key=lambda record: record.display_name.casefold())
        for record in records:
            self.games.add(GameRow(self, record))
        self.games.show_all()

    def play(self, record: library.Record) -> None:
        launch = record.path("launch")
        if not launch or not launch.is_file():
            self.status.set_text(f"Launcher missing for {record.display_name}. Remove it and drop the game again.")
            return
        try:
            subprocess.Popen([str(launch)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.status.set_text(f"Could not start {record.display_name}: {exc}")
            return
        self.status.set_text(f"Starting {record.display_name}…")

    def confirm_remove(self, record: library.Record) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text=f"Remove {record.display_name}?",
        )
        dialog.format_secondary_text(
            "This removes its launcher and menu entries. Your original game files are not touched."
        )
        purge = Gtk.CheckButton(label="Also delete saves and settings (Wine prefix) and any extracted copy")
        purge.set_margin_start(12)
        purge.set_margin_end(12)
        dialog.get_message_area().pack_start(purge, False, False, 0)
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        remove_button = dialog.add_button("_Remove", Gtk.ResponseType.OK)
        remove_button.get_style_context().add_class("destructive-action")
        dialog.show_all()
        response = dialog.run()
        wants_purge = purge.get_active()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        try:
            result = library.remove(record, purge=wants_purge)
        except OSError as exc:
            self.status.set_text(f"Could not remove {record.display_name}: {exc}")
            return
        kept = [note for note in result.kept if note.startswith("kept prefix")]
        self.status.set_text(f"Removed {record.display_name}." + (f"\n{kept[0]}" if kept else ""))
        self.refresh()

    # Adding ------------------------------------------------------------------

    def _drop(self, _widget, context, _x, _y, data, _info, event_time) -> None:
        accepted = False
        try:
            raw = data.get_uris() or data.get_data()
            path = single_input_path(parse_uri_list(raw))
            accepted = self._start(path)
        except Exception as exc:
            self.status.set_text(str(exc))
        context.finish(accepted, False, event_time)

    def _choose(self, action: Gtk.FileChooserAction) -> None:
        if self._working:
            return
        folder = action is Gtk.FileChooserAction.SELECT_FOLDER
        dialog = Gtk.FileChooserNative(
            title="Choose a Windows game folder" if folder else "Choose a Windows archive, EXE, or installer",
            transient_for=self, action=action, accept_label="_Convert",
        )
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            path = dialog.get_filename()
            if path:
                self._start(Path(path))
        dialog.destroy()

    def _set_busy(self, busy: bool) -> None:
        self._working = busy
        self.file_button.set_sensitive(not busy)
        self.folder_button.set_sensitive(not busy)
        (self.spinner.start if busy else self.spinner.stop)()

    def _start(self, path: Path | str) -> bool:
        if self._working:
            self.status.set_text("A conversion is already in progress.")
            return False
        source = Path(path).expanduser()
        if not source.exists():
            self.status.set_text(f"Input does not exist: {source}")
            return False
        self._set_busy(True)
        self.status.set_text(f"Converting {source.name}…")

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
        self._set_busy(False)
        if error is not None:
            self.status.set_text(f"Failed: {error}")
        elif not result.ok:
            self.status.set_text(f"Failed: {result.error}")
        else:
            names = ", ".join(app.display_name for app in [result] + result.published_apps)
            if result.already_existed:
                self.status.set_text(f"{names} is already in your games. Press Play below.")
            else:
                self.status.set_text(f"Done: {names} is in your application menu and on your Desktop.")
        self.refresh()
        return False


# Older name, kept for anything importing it.
AddWindow = FunnelWindow


def main() -> int:
    ensure_layout()
    # GTK3's Wayland backend derives xdg_toplevel.app_id from GLib's program
    # name. It must match funnel.desktop so Plasma resolves the branded icon.
    GLib.set_prgname("funnel")
    window = FunnelWindow()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FunnelWindow", "AddWindow", "main", "parse_uri_list", "single_input_path"]
