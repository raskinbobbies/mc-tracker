from __future__ import annotations

import argparse
import logging
import sys
import time

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
    assets = AssetStore()
    overlay = Overlay(tracker, assets)
    w = MainWindow(tracker, overlay, assets, args.hotkey)
    from .items_dialog import ItemsDialog

    items_dlg = app.items_dialog = ItemsDialog(tracker, None if args.demo else _resolve_item, w)
    w.menuBar().addAction("Items && prices").triggered.connect(items_dlg.show)
    problems = [f.problem for f in (tracker.prices, tracker.slot_items, tracker.settings) if f.problem]
    if problems:  # damaged settings file: it was backed up, say so
        w.statusBar().showMessage(" ".join(problems))
    if args.demo:
        from .demo import DemoFeed

        app.feed = DemoFeed(tracker)  # keep a reference
    else:
        from . import gamedata
        from .capture import CaptureWorker

        tracker.item_resolver = gamedata.item_id
        cap = app.feed = CaptureWorker(args.window)
        cap.status_read.connect(tracker.on_status)
        cap.map_read.connect(tracker.on_map)
        cap.channel_read.connect(tracker.on_channel)
        cap.messages_read.connect(tracker.on_message_lines)
        cap.party_read.connect(tracker.on_party)
        cap.chat_read.connect(tracker.on_chat_lines)
        cap.quickslots_read.connect(tracker.on_quickslots)
        cap.info.connect(lambda msg: w.statusBar().showMessage(msg))
        wire_minimap_prompt(cap, tracker, [w, overlay])
        app.aboutToQuit.connect(cap.stop)
        app.aboutToQuit.connect(tracker.end_session)  # after capture stops: catch late updates
        cap.price_items.connect(w.show_price)
        cap.start()

        from .hotkey import GlobalHotkey

        hk = app.hotkey = GlobalHotkey(args.hotkey)
        hk.pressed.connect(cap.request_price_check)
        hk.failed.connect(lambda msg: w.statusBar().showMessage(msg))
        app.aboutToQuit.connect(hk.stop)
        hk.start()
    if not args.demo:
        _wire_follow_game(app, w, overlay, tracker, args.window, instance)
    w.show()
    overlay.show()
    if args.crash_test:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, lambda: 1 / 0)
    return app.exec()


def _wire_follow_game(app, w, overlay, tracker, window_title, instance) -> None:
    """The "Start and stop with MapleStory" menu item, and the follower that acts on it."""
    from . import follow

    def raise_windows() -> None:
        w.showNormal()
        w.raise_()
        w.activateWindow()
        overlay.show()

    instance.message.connect(lambda _text: raise_windows())

    follower = follow.GameFollower(follow.game_probe(window_title), app)

    def game_started() -> None:  # the game opened after the app: begin reading it
        cap = getattr(app, "feed", None)
        if cap is not None and cap.hwnd is None:
            cap.start()

    def game_stopped() -> None:  # same save as a normal quit, then exit
        w.close()
        app.quit()

    follower.started.connect(game_started)
    follower.stopped.connect(game_stopped)

    action = w.menuBar().addMenu("Settings").addAction("Start and stop with MapleStory")
    action.setCheckable(True)
    action.setToolTip("Opens mc-tracker when MapleStory starts and closes it when MapleStory closes")
    action.setChecked(tracker.settings.follow_game)

    def apply(on: bool) -> None:
        if on:
            try:
                follow.enable_login_entry()
            except OSError as e:
                action.blockSignals(True)
                action.setChecked(False)
                action.blockSignals(False)
                w.statusBar().showMessage(f"Couldn't set up start with MapleStory: {e}")
                return
            follow.spawn_waiter()  # so the next game start works without a re-login
            follower.start()
        else:
            follower.stop()
            try:
                follow.disable_login_entry()
            except OSError as e:
                w.statusBar().showMessage(f"Couldn't remove the login entry: {e}")
            follow.ask_waiter_to_quit()
        tracker.settings.follow_game = on
        tracker.settings.save()

    action.toggled.connect(apply)
    if tracker.settings.follow_game:
        follower.start()


def _resolve_item(name: str) -> int | None:
    from . import gamedata

    return gamedata.item_id(name)
