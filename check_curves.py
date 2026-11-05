#!/usr/bin/env python3
"""Checks that contours.gpkg is the result of a finished build_elevation.py run.

    check_curves.py CONTOURS.gpkg

Exit 0 when good, 1 otherwise. The sample tiles in check_tiles.py can't see a file that
covers those few places and lacks the rest, and curves don't change tile counts, so this
looks at the build itself, from the folder the file sits in:

- manifest.json has no bbox, so it's both countries and not a trial area. That file is
  written when a build starts, so it says nothing about the build having finished.
- build.log ends in the "SUMMARY ok" line, printed only after every stage has passed,
  land check included. A build that failed or was interrupted leaves no such last line.
- That line's curve count is the file's real count, so it describes this very file.
- The count is at least MIN_CURVES.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

# 1,420,786 on 2026-11-03. New source data or settings can move this a little, and more
# than about 5 % means look before shipping.
MIN_CURVES = 1_350_000

RUN_AGAIN = (
    "Run build_elevation.py as infra/map/README.md shows, redirecting to build.log. "
    "With both final files in place it only prints the SUMMARY again, in seconds."
)


def problems(contours: Path) -> tuple[list[str], int | None]:
    """What's wrong, and the curve count if it could be read."""
    if not contours.is_file():
        return [f"no curves at {contours}, build them first with build_elevation.py"], None
    folder = contours.parent
    errors = []

    manifest = folder / "manifest.json"
    try:
        if json.loads(manifest.read_text()).get("bbox", "absent") is not None:
            errors.append(f"{manifest} is from a trial area, or has no bbox field. {RUN_AGAIN}")
    except (OSError, ValueError, AttributeError) as e:
        errors.append(f"no readable {manifest}: {e}. {RUN_AGAIN}")

    try:
        count = sqlite3.connect(f"file:{contours}?mode=ro&immutable=1", uri=True).execute(
            "SELECT count(*) FROM contour"
        ).fetchone()[0]
    except sqlite3.Error as e:
        return errors + [f"{contours} is unreadable: {e}"], None

    log = folder / "build.log"
    try:
        lines = [line for line in log.read_text(errors="replace").splitlines() if line.strip()]
    except OSError as e:
        return errors + [f"no readable {log}: {e}. {RUN_AGAIN}"], count
    last = lines[-1] if lines else ""
    if not last.startswith("SUMMARY ok "):
        errors.append(f"{log} doesn't end in SUMMARY ok, so that build didn't finish. {RUN_AGAIN}")
    else:
        match = re.search(r"\bcurves=(\d+)\b", last)
        if not match:
            errors.append(f"{log} has no curve count in its SUMMARY. {RUN_AGAIN}")
        elif int(match.group(1)) != count:
            errors.append(f"{log} reports {match.group(1)} curves, {contours} holds {count}. {RUN_AGAIN}")

    if count < MIN_CURVES:
        errors.append(f"{contours} holds {count} curves, the floor is {MIN_CURVES}")
    return errors, count


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    errors, count = problems(Path(args[0]).expanduser())
    for error in errors:
        print(f"FAIL {error}")
    if errors:
        return 1
    print(f"ok   {args[0]}: {count} curves from a finished build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
