"""Dashboard-only launcher for a desktop PC (no servo or LCD).

    python doa_dashboard.py
    python doa_dashboard.py --port 8765 --hz 20 --no-browser

The full tracker is `python -m doa_tracker`; see doa_tracker/README.md.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from doa_tracker.__main__ import main  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live 3D reSpeaker DOA dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--hz", type=float, default=20.0, help="control poll rate")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    argv = ["--no-servo", "--no-tilt", "--no-camera", "--no-lcd", "--host", args.host, "--port", str(args.port), "--poll-hz", str(args.hz)]
    if not args.no_browser:
        argv.append("--open-browser")
    sys.exit(main(argv))
