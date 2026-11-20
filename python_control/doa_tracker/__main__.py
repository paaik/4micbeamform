"""Run the tracker: poll the XVF3800, drive the pan/tilt servos, camera and LCD, and serve the dashboard.

    python -m doa_tracker                      # everything in doa_tracker.toml
    python -m doa_tracker --no-servo --no-tilt --no-camera --no-lcd   # dashboard only (e.g. on a PC)
    python -m doa_tracker --check              # report what is reachable
    python -m doa_tracker --servo-test         # sweep the pan servo through 0/90/180
    python -m doa_tracker --servo-test tilt    # sweep the tilt servo through its elevation range
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import webbrowser

from . import config
from .poller import DoaPoller, SharedState

log = logging.getLogger("doa_tracker")


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="doa_tracker", description="reSpeaker XVF3800 direction tracker")
    parser.add_argument("--config", help="TOML settings file (default: doa_tracker.toml next to this package)")
    parser.add_argument("--no-servo", action="store_true", help="do not drive the pan servo")
    parser.add_argument("--no-tilt", action="store_true", help="do not drive the tilt servo")
    parser.add_argument("--no-camera", action="store_true", help="do not stream the camera")
    parser.add_argument("--no-lcd", action="store_true", help="do not use the LCD")
    parser.add_argument("--no-dashboard", action="store_true", help="do not serve the web dashboard")
    parser.add_argument("--host", help="dashboard bind address")
    parser.add_argument("--port", type=int, help="dashboard port")
    parser.add_argument("--poll-hz", type=float, help="control poll rate")
    parser.add_argument("--open-browser", action="store_true", help="open the dashboard in a browser")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report device, LCD, camera and servo readiness, then exit")
    mode.add_argument("--servo-test", nargs="?", const="pan", choices=("pan", "tilt"),
                      help="sweep the pan (default) or tilt servo to check pulse limits, then exit")
    mode.add_argument("--prepare-hardware", action="store_true",
                      help="(root) export the servo PWM channels for --group, then exit")
    parser.add_argument("--group", default="gpio", help="group given access by --prepare-hardware")
    return parser.parse_args(argv)


def load_settings(args) -> dict:
    path = args.config
    if path is None and config.DEFAULT_PATH.is_file():
        path = config.DEFAULT_PATH
    settings = config.load(path)
    if args.no_servo:
        settings["servo"]["enabled"] = False
    if args.no_tilt:
        settings["tilt"]["enabled"] = False
    if args.no_camera:
        settings["camera"]["enabled"] = False
    if args.no_lcd:
        settings["lcd"]["enabled"] = False
    if args.no_dashboard:
        settings["dashboard"]["enabled"] = False
    if args.host:
        settings["dashboard"]["host"] = args.host
    if args.port:
        settings["dashboard"]["port"] = args.port
    if args.poll_hz:
        settings["device"]["poll_hz"] = args.poll_hz
    if args.open_browser:
        settings["dashboard"]["open_browser"] = True
    config.validate(settings)
    return settings


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        settings = load_settings(args)
    except (OSError, ValueError) as exc:
        log.error("Bad settings: %s", exc)
        return 2

    if args.prepare_hardware:
        from .servo import prepare_hardware
        return prepare_hardware([settings["servo"], settings["tilt"]], args.group)
    if args.check:
        from .check import run
        return run(settings)
    if args.servo_test:
        from .servo import ServoError, pan_positions, sweep
        from .tilt import sweep_positions
        cfg = settings["servo"] if args.servo_test == "pan" else settings["tilt"]
        positions = pan_positions(cfg) if args.servo_test == "pan" else sweep_positions(cfg)
        try:
            return sweep(cfg, positions)
        except (ServoError, OSError) as exc:
            log.error("Servo test failed: %s", exc)
            return 1

    state = SharedState()
    stop = threading.Event()
    workers = [threading.Thread(target=DoaPoller(settings["device"], settings["tracking"], state).run,
                                args=(stop,), name="poller")]

    if settings["servo"]["enabled"]:
        from .servo import ServoController
        workers.append(threading.Thread(target=ServoController(settings["servo"], state).run,
                                        args=(stop,), name="servo"))
    else:
        state.set_module("servo", {"enabled": False})

    hub = None
    if settings["camera"]["enabled"]:
        from .camera import CameraModule
        from .vision import FrameHub
        hub = FrameHub(settings["camera"], state)
        workers.append(threading.Thread(target=CameraModule(settings["camera"], state, hub).run,
                                        args=(stop,), name="camera"))
    else:
        state.set_module("camera", {"enabled": False})

    if settings["tilt"]["enabled"]:
        from .tilt import TiltController
        workers.append(threading.Thread(target=TiltController(settings["tilt"], state, hub).run,
                                        args=(stop,), name="tilt"))
    else:
        state.set_module("tilt", {"enabled": False})

    if settings["lcd"]["enabled"]:
        from .lcd import LcdModule
        workers.append(threading.Thread(target=LcdModule(settings["lcd"], state).run, args=(stop,), name="lcd"))
    else:
        state.set_module("lcd", {"enabled": False})

    server = None
    if settings["dashboard"]["enabled"]:
        from .web import DashboardServer
        try:
            server = DashboardServer(settings["dashboard"], state, stop, hub)
        except OSError as exc:
            log.error("Cannot serve the dashboard on port %s: %s", settings["dashboard"]["port"], exc)
            return 1

    def request_stop(signum, _frame):
        log.info("Stopping (signal %d)", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    for worker in workers:
        worker.start()
    if server is not None:
        server.start()
        if settings["dashboard"]["open_browser"]:
            webbrowser.open(server.urls()[0])

    try:
        while not stop.wait(0.5):
            pass
    finally:
        stop.set()
        if server is not None:
            server.close()
        for worker in workers:
            worker.join(timeout=3.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
