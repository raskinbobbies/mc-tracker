from __future__ import annotations

import argparse
import logging
import sys
import time

from PySide6.QtCore import QEvent, QObject, Slot
from PySide6.QtWidgets import QApplication

from .assets import AssetStore
from .gui import MainWindow
from .minimap_prompt import MinimapPromptRelay
from .overlay import Overlay
from .session import Tracker

DEFAULT_HOTKEY = "alt+a"


def _app_icon():
    from pathlib import Path

    from PySide6.QtGui import QIcon

    return QIcon(str(Path(__file__).parent / "res" / "icon.png"))


def wire_minimap_prompt(cap, tracker, windows, clock=time.monotonic) -> MinimapPromptRelay:
    """Show the minimap prompt in every window while the capture's status calls for it.

    A saved session (map change, restart, quit) also clears it; it comes back after the usual hold
    if the status still asks for it.
    """
    from PySide6.QtCore import QTimer

    relay = MinimapPromptRelay([win.set_minimap_prompt for win in windows], clock)
    cap.minimap_changed.connect(relay.on_status)
    tracker.on_saved.append(lambda _p: QTimer.singleShot(0, lambda: relay.on_status("unknown")))
    return relay


class _DetachRelay(QObject):
    """Receives ``attached_changed`` in the GUI thread (``on_closed`` fires on windows-capture's thread)."""

    def __init__(self, tracker, parent):
        super().__init__(parent)
        self.tracker = tracker

    @Slot(bool)
    def on_attached(self, attached: bool) -> None:
        if not attached:
            self.tracker.game_closed()


def wire_game_detach(cap, tracker) -> QObject:
    """The game window closed (or the capture stopped): end the session there, saved like any other end,
    so a reattach starts a fresh one (the session clock is wall time; the next player may be someone else).
    The receiver lives in ``cap``'s thread (the GUI's), so a close reported from another thread is queued there."""
    relay = _DetachRelay(tracker, cap)
    cap.attached_changed.connect(relay.on_attached)
    return relay


def main() -> int:
    from . import crash

    crash.ensure_std_streams()  # windowed exe: stdout/stderr are None
    crash.setup_logging()
    parser = argparse.ArgumentParser(prog="mc-tracker")
    parser.add_argument("--demo", action="store_true", help="feed fake session data")
    parser.add_argument("--window", default="MapleStory", help="exact game window title, used only if MapleStory.exe isn't found")
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help="price-check hotkey, e.g. alt+a")
    parser.add_argument("--follow-game", action="store_true",
                        help="no windows: wait for MapleStory and start the app when it opens (the login entry runs this)")
    parser.add_argument("--crash-test", action="store_true", help=argparse.SUPPRESS)  # raise once, to check the error box
    args, qt_args = parser.parse_known_args()

    logging.getLogger("RapidOCR").setLevel(logging.ERROR)

    if sys.platform == "win32":  # own taskbar identity (and icon) instead of python's
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("raskinbobbies.mc-tracker")
        except (AttributeError, OSError):
            pass
    app = QApplication([sys.argv[0], *qt_args])
    app.setWindowIcon(_app_icon())
    crash.install_excepthook(app)
    from . import follow

    if args.follow_game:
        return follow.run_waiter(app, _app_icon(), args.window, ("--window", args.window, "--hotkey", args.hotkey))
    instance = None
    if not args.demo and not args.crash_test:  # one app per data folder: a second start raises the first
        instance = follow.SingleInstance(follow.instance_name("app"))
        if not instance.acquire("raise"):
            return 0
    if args.demo:
        import tempfile
        from pathlib import Path

        from .items import AppSettings

        demo_settings = AppSettings()
        tracker = Tracker(save_dir=Path(tempfile.mkdtemp(prefix="mc-tracker-demo-")), settings=demo_settings)
    else:
        from .items import AppSettings, PriceList, SlotItems, data_path

        tracker = Tracker(prices=PriceList(data_path("prices.json")),
                          slot_items=SlotItems(data_path("slot_items.json")),
                          settings=AppSettings(data_path("app.json")))
    # A session the last run didn't end (crash, kill, power cut) is saved now, before the history loads.
    recovered = None if args.demo else tracker.recover()
    assets = AssetStore()
    overlay = Overlay(tracker, assets)
    w = MainWindow(tracker, overlay, assets, args.hotkey)
    from .items_dialog import ItemsDialog

    items_dlg = app.items_dialog = ItemsDialog(tracker, None if args.demo else _resolve_item, w)
    w.menuBar().addAction("Items && prices").triggered.connect(items_dlg.show)
    problems = [f.problem for f in (tracker.prices, tracker.slot_items, tracker.settings) if f.problem]
    if problems:  # damaged settings file: it was backed up, say so
        w.show_notice(" ".join(problems))
    if recovered:
        w.show_notice(recovered, timeout_s=30)
    from PySide6.QtCore import QTimer

    tracker.on_notice.append(lambda msg: QTimer.singleShot(0, lambda: w.show_notice(msg)))  # a failed save
    if args.demo:
        from .demo import DemoFeed

        app.feed = DemoFeed(tracker)  # keep a reference
    else:
        from . import gamedata
        from .capture import CaptureWatcher, CaptureWorker

        tracker.item_resolver = gamedata.item_id
        cap = app.feed = CaptureWorker(args.window)
        cap.status_read.connect(tracker.on_status)
        cap.map_read.connect(tracker.on_map)
        cap.channel_read.connect(tracker.on_channel)
        cap.messages_read.connect(tracker.on_message_lines)
        cap.party_read.connect(tracker.on_party)
        cap.chat_read.connect(tracker.on_chat_lines)
        cap.quickslots_read.connect(tracker.on_quickslots)
        cap.info.connect(lambda msg: w.show_notice(msg))
        cap.attached_changed.connect(w.set_game_attached)
        w.set_game_attached(False)  # until the capture attaches
        wire_game_detach(cap, tracker)
        wire_minimap_prompt(cap, tracker, [w, overlay])
        watcher = app.capture_watcher = CaptureWatcher(cap)  # attaches when the game starts, and again after a restart
        app.aboutToQuit.connect(watcher.stop)
        app.aboutToQuit.connect(cap.stop)
        app.aboutToQuit.connect(tracker.end_session)  # after capture stops: catch late updates
        cap.price_items.connect(w.show_price)
        watcher.start()  # attaches now if the game is up

        from .hotkey import GlobalHotkey

        hk = app.hotkey = GlobalHotkey(args.hotkey)
        hk.pressed.connect(cap.request_price_check)
        hk.failed.connect(lambda msg: w.show_notice(msg))
        app.aboutToQuit.connect(hk.stop)
        hk.start()
    from .windowstate import WindowMemory

    for widget, key, keep_size in ((w, "main", True), (overlay, "overlay", False)):
        mem = WindowMemory(widget, tracker.settings, key, keep_size=keep_size)
        mem.restore()
        app.aboutToQuit.connect(mem.save)
    if not args.demo:
        extra = (("--hotkey", args.hotkey) if args.hotkey != DEFAULT_HOTKEY else ())
        if args.window != "MapleStory":
            extra += ("--window", args.window)
        _wire_follow_game(app, w, overlay, tracker, args.window, instance, extra)
    w.show()
    overlay.show()
    if args.crash_test:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, lambda: 1 / 0)
    return app.exec()


def _wire_follow_game(app, w, overlay, tracker, window_title, instance, extra: tuple[str, ...] = ()) -> None:
    """The "Start and stop with MapleStory" menu item, and the follower that acts on it.

    ``instance``: the single-instance guard (None when there is none, e.g. ``--crash-test``).
    ``extra``: options to keep when the waiter starts the app (a custom ``--hotkey``).
    The login entry in the registry is the truth about whether the setting is on: the settings
    file follows it, so a damaged or reset ``app.json`` can't leave an entry the menu doesn't show.
    """
    from . import follow

    settings = tracker.settings

    def raise_windows() -> None:
        w.showNormal()
        w.raise_()
        w.activateWindow()
        overlay.show()

    if instance is not None:  # only an explicit "raise" brings the window forward, not a "ping"
        instance.message.connect(lambda text: raise_windows() if text == "raise" else None)

    follower = follow.GameFollower(follow.game_probe(window_title), w)

    closing_for_game = []

    def game_stopped() -> None:  # same save as a normal quit, then exit
        closing_for_game.append(True)
        w.close()
        app.quit()

    pause_sent = []

    def user_closed() -> None:
        """Closed by the user (not because the game ended) while the game may still be up: tell the
        waiter not to bring the app straight back. Sent once, and as early as possible."""
        if not closing_for_game and not pause_sent and tracker.settings.follow_game:
            pause_sent.append(True)
            follow.ask_waiter_to_pause()

    class CloseWatcher(QObject):
        def eventFilter(self, _obj, event):  # noqa: N802 - Qt API
            if event.type() == QEvent.Type.Close:
                user_closed()  # before the window's own close handling saves the session and stops the capture
            return False

    closer = CloseWatcher(w)
    w.installEventFilter(closer)
    app.aboutToQuit.connect(user_closed)  # quits that don't close the window

    follower.stopped.connect(game_stopped)

    action = w.menuBar().addMenu("Settings").addAction("Start and stop with MapleStory")
    action.setCheckable(True)
    action.setToolTip("Opens mc-tracker when MapleStory starts and closes it when MapleStory closes")

    def set_checked(on: bool) -> None:
        action.blockSignals(True)
        action.setChecked(on)
        action.blockSignals(False)

    def apply(on: bool) -> None:
        if on:
            try:
                follow.enable_login_entry(follow.run_command(extra))
            except OSError as e:
                set_checked(False)
                w.show_notice(f"Couldn't set up start with MapleStory: {e}", timeout_s=15)
                return
            follow.spawn_waiter(extra)  # so the next game start works without a re-login
            follower.start()
        else:
            try:
                follow.disable_login_entry()
            except OSError as e:
                set_checked(True)  # still installed: don't pretend it is off
                w.show_notice(f"Couldn't remove the login entry: {e}", timeout_s=15)
                return
            follower.stop()
            follow.ask_waiter_to_quit()
        settings.follow_game = on
        settings.save()

    other_install = None
    try:
        current = follow.login_entry_value()
        entry = current is not None
        if entry:
            # the dev checkout and the exe share one Run value: repair it when its program is ours or gone
            # (a moved exe, a new hotkey), but never take it from another install that still exists
            state = follow.check_login_entry(current, follow.run_command(extra))
            if state == "rewrite":
                follow.enable_login_entry(follow.run_command(extra))
            elif state == "foreign":
                other_install = follow.command_target(current) or current
    except OSError:
        entry = settings.follow_game  # can't tell: keep what the settings say
    if entry != settings.follow_game:
        settings.follow_game = entry
        settings.save()
    set_checked(entry)
    action.toggled.connect(apply)
    if other_install is not None:
        w.show_notice(f"Start with MapleStory is set up by another mc-tracker install ({other_install}), "
                      "so it is left as is. Turn it off and on here to use this install instead.", timeout_s=20)
    if entry:
        follower.start()


def _resolve_item(name: str) -> int | None:
    from . import gamedata

    return gamedata.item_id(name)
