#!/usr/bin/env python3
"""Fill null elevations in a Swedish asset from Lantmäteriet's 1 m
Markhöjdmodell (CC BY 4.0).

    python3 scripts/fill_elevation_se.py [--target peaks|viewpoints|forts|caves|huts|dams|waterfalls]

Targets (see TARGETS):

  peaks       assets/peaks_se.json, column 4. Max over a 51x51 px window
              (an OSM peak node is often a few metres off the true top; one
              1 m cell would read the slope). A row whose lookup confirms
              there is no DTM coverage at all (no STAC item, or an all-nodata
              window) is DROPPED and its app id recorded in
              scripts/data/peaks_se_no_coverage.json; a row whose elevation
              (OSM or DTM) is below 25 m is DROPPED too (Norway's peaks
              bottom out at 25 m) and recorded in
              scripts/data/peaks_se_below_floor.json. convert_peaks_se.py
              skips both lists on the next re-export.
  viewpoints  assets/utsiktspunkter_se.json, column 5 (created when the row
              is still the converter's 5-column shape). Max over a 5x5 px
              window: terrain at the node, tolerating a 1-2 m node offset.
              Values below 25 m are stored as null (coastal/over-water DTM
              noise, same floor as the Norwegian fill_elevation.py). No
              coverage -> null, row KEPT.
  forts       assets/festninger_se.json, column 5 (created when the row is
              still the converter's 5-column shape). Same settings as
              viewpoints: 5x5 px window, values below 25 m stored as null
              (Norway's fill_elevation.py uses the same floor for forts),
              no coverage -> null, row KEPT.
  caves       assets/grotter_se.json, column 5 (created when the row is
              still the converter's 5-column shape). Same settings as
              forts: 5x5 px window, values below 25 m stored as null
              (Norway's fill_elevation.py uses the same floor for caves),
              no coverage -> null, row KEPT.
  huts        assets/hytter_se.json, column 5 (created when the row is
              still the converter's 5-column shape). Same settings as
              caves: 5x5 px window, values below 25 m stored as null
              (Norway's fill_elevation.py uses the same floor for huts),
              no coverage -> null, row KEPT.
  dams        assets/dammer_se.json, column 5 (created when the row is
              still the converter's 5-column shape). Same settings as
              huts: 5x5 px window, values below 25 m stored as null
              (Norway's fill_elevation.py uses the same floor for dams),
              no coverage -> null, row KEPT.
  waterfalls  assets/fossefall_se.json, column 5 (created when the row is
              still the converter's 5-column shape). Same settings as
              dams: 5x5 px window, values below 25 m stored as null
              (Norway's fill_elevation.py uses the same floor for
              waterfalls), no coverage -> null, row KEPT.

For each row without an elevation:
  1. STAC search (https://api.lantmateriet.se/stac-hojd/v1/search) for the
     `dtm-cog` item covering the point.
  2. Open the item's cloud-optimized GeoTIFF over HTTP range requests
     (rasterio /vsicurl/), read the window around the point and take the
     maximum, ignoring nodata.
  3. Store it rounded to one decimal (nulled under a "null" floor policy).

Needs:  pip install rasterio pyproj
        ~/.netrc with  machine dl1.lantmateriet.se login <user> password <pw>
        (the tiles need HTTP Basic auth; GDAL reads .netrc by default).

Rows that already have an elevation are never re-fetched (a peak under the
floor is still dropped, whether its height came from OSM or the DTM). Resumable: raw DTM
results (before the floor) are cached per target in /tmp keyed "lat,lng"
(None for no coverage; failures never cached). A row whose fetch merely
failed (network/HTTP error) stays null and is retried on the next run.
Re-run add_points.py (--only <the Swedish files>) and
generate_function_points.py afterwards.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Literal

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")
DATA = os.path.join(os.path.dirname(__file__), "data")
STAC_SEARCH = "https://api.lantmateriet.se/stac-hojd/v1/search"
COLLECTION = "dtm-cog"
NODATA = -9999.0
WORKERS = 4
RETRIES = 3


@dataclass(frozen=True)
class Target:
    file: str
    col: int  # index of the elevation column in a row
    radius_px: int  # window half-size in 1 m pixels
    floor_m: float | None  # below this the row is nulled or dropped (see below_floor)
    below_floor: Literal["null", "drop"]  # store null, or remove the row entirely
    drop_no_coverage: bool  # drop the row when the DTM has nothing here
    no_coverage_file: str | None  # where no-coverage app ids are recorded
    below_floor_file: str | None  # where below-floor app ids are recorded
    cache: str


TARGETS = {
    "peaks": Target(
        file=os.path.join(ASSETS, "peaks_se.json"), col=4, radius_px=25,
        floor_m=25.0, below_floor="drop", drop_no_coverage=True,
        no_coverage_file=os.path.join(DATA, "peaks_se_no_coverage.json"),
        below_floor_file=os.path.join(DATA, "peaks_se_below_floor.json"),
        cache="/tmp/peaks_se_elev_cache.json"),
    "viewpoints": Target(
        file=os.path.join(ASSETS, "utsiktspunkter_se.json"), col=5, radius_px=2,
        floor_m=25.0, below_floor="null", drop_no_coverage=False,
        no_coverage_file=None, below_floor_file=None,
        cache="/tmp/viewpoints_se_elev_cache.json"),
    "forts": Target(
        file=os.path.join(ASSETS, "festninger_se.json"), col=5, radius_px=2,
        floor_m=25.0, below_floor="null", drop_no_coverage=False,
        no_coverage_file=None, below_floor_file=None,
        cache="/tmp/forts_se_elev_cache.json"),
    "caves": Target(
        file=os.path.join(ASSETS, "grotter_se.json"), col=5, radius_px=2,
        floor_m=25.0, below_floor="null", drop_no_coverage=False,
        no_coverage_file=None, below_floor_file=None,
        cache="/tmp/caves_se_elev_cache.json"),
    "huts": Target(
        file=os.path.join(ASSETS, "hytter_se.json"), col=5, radius_px=2,
        floor_m=25.0, below_floor="null", drop_no_coverage=False,
        no_coverage_file=None, below_floor_file=None,
        cache="/tmp/huts_se_elev_cache.json"),
    "dams": Target(
        file=os.path.join(ASSETS, "dammer_se.json"), col=5, radius_px=2,
        floor_m=25.0, below_floor="null", drop_no_coverage=False,
        no_coverage_file=None, below_floor_file=None,
        cache="/tmp/dams_se_elev_cache.json"),
    "waterfalls": Target(
        file=os.path.join(ASSETS, "fossefall_se.json"), col=5, radius_px=2,
        floor_m=25.0, below_floor="null", drop_no_coverage=False,
        no_coverage_file=None, below_floor_file=None,
        cache="/tmp/waterfalls_se_elev_cache.json"),
}


def window_max(values, nodata: float = NODATA) -> float | None:
    best = None
    for row in values:
        for v in row:
            v = float(v)
            if v == nodata or math.isnan(v):
                continue
            if best is None or v > best:
                best = v
    return None if best is None else round(best, 1)


def ensure_elev_slot(row, target: Target = TARGETS["peaks"]) -> None:
    """Column-5 targets (viewpoints, forts, caves, huts, dams, waterfalls): make sure index 5 is the elevation slot.
    len 5 (fresh converter row) -> append None; len 6 with an int at [5]
    (points, no elevation yet) -> insert None before it; otherwise as is.
    Peak rows always carry their elevation column, so nothing to do.
    """
    if target.col != 5:
        return
    if len(row) == 5:
        row.append(None)
    elif len(row) == 6 and isinstance(row[5], int):
        row.insert(5, None)


def apply_floor(value: float | None, target: Target) -> float | None:
    """The "null" policy: a value under the floor is stored as null. Under the
    "drop" policy the real value is kept so is_below_floor() can remove the
    whole row afterwards."""
    if value is None or target.floor_m is None or target.below_floor != "null":
        return value
    return None if value < target.floor_m else value


def is_below_floor(row, target: Target) -> bool:
    """The "drop" policy: whether `row`'s (OSM or DTM) elevation is under the
    floor and the row must be removed from the asset."""
    if target.below_floor != "drop" or target.floor_m is None:
        return False
    value = row[target.col]
    return value is not None and value < target.floor_m


def stac_item_href(lng: float, lat: float) -> str | None:
    body = json.dumps({
        "collections": [COLLECTION],
        "intersects": {"type": "Point", "coordinates": [lng, lat]},
        "limit": 1,
    }).encode()
    req = urllib.request.Request(
        STAC_SEARCH, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        features = json.load(r).get("features") or []
    if not features:
        return None
    return features[0]["assets"]["data"]["href"]


_local = threading.local()


def _transformer():
    import pyproj
    if not hasattr(_local, "tr"):
        _local.tr = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True)
    return _local.tr


def sample_dtm(href: str, lng: float, lat: float, radius_px: int) -> float | None:
    import rasterio
    from rasterio.env import Env
    from rasterio.windows import Window

    x, y = _transformer().transform(lng, lat)
    size = 2 * radius_px + 1
    with Env(GDAL_HTTP_NETRC="YES",
             GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
             CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
             GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2"):
        with rasterio.open("/vsicurl/" + href) as ds:
            row, col = ds.index(x, y)
            nodata = ds.nodata if ds.nodata is not None else NODATA
            window = Window(col - radius_px, row - radius_px, size, size)
            values = ds.read(1, window=window, boundless=True, fill_value=nodata)
    return window_max(values, nodata=nodata)


def fetch(lat: float, lng: float, radius_px: int):
    """Elevation in metres, None when the DTM has nothing here, "ERR" on failure."""
    for attempt in range(RETRIES):
        try:
            href = stac_item_href(lng, lat)
            if href is None:
                return None
            return sample_dtm(href, lng, lat, radius_px=radius_px)
        except Exception:
            # Broad on purpose: this is a retry wrapper that only retries and
            # then returns "ERR", so nothing is masked. GDAL/rasterio raise
            # CPLE_* errors (rasterio._err) that inherit only from Exception,
            # not OSError, so a narrower catch misses transient HTTP failures
            # mid-read and would crash main() before the final rewrite.
            if attempt == RETRIES - 1:
                return "ERR"
            time.sleep(2.0 * (attempt + 1))
    return "ERR"


def keep_row(row, cache, target: Target = TARGETS["peaks"]) -> bool:
    """Whether `row` belongs in the output.

    Targets that keep no-coverage rows always keep. Otherwise: kept when the
    row already has an elevation, or when its cache key is absent (a
    transient fetch failure never gets cached, so the row stays null and is
    retried on the next run). Dropped when the cache holds None for its
    key: a confirmed absence of DTM coverage, not a failure.
    """
    if not target.drop_no_coverage:
        return True
    if row[target.col] is not None:
        return True
    key = f"{row[2]},{row[3]}"
    if key not in cache:
        return True
    return cache[key] is not None


def record_dropped_ids(path: str | None, ids: list) -> None:
    """Union `ids` into the JSON id list at `path` so convert_peaks_se.py can
    skip them on the next re-export. No-op when there is no path or no ids."""
    if not path or not ids:
        return
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = set(json.load(fh))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sorted(existing | set(ids)), fh, indent=0)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", choices=sorted(TARGETS), default="peaks")
    args = parser.parse_args(argv)
    target = TARGETS[args.target]
    col = target.col

    with open(target.file, encoding="utf-8") as fh:
        rows = json.load(fh)
    for row in rows:
        ensure_elev_slot(row, target)
    cache = {}
    if os.path.exists(target.cache):
        with open(target.cache, encoding="utf-8") as fh:
            cache = json.load(fh)

    todo = []  # (row index, cache key, lat, lng)
    for i, row in enumerate(rows):
        if row[col] is None:
            key = f"{row[2]},{row[3]}"
            if key in cache:  # None (no DTM coverage) is cached too; "ERR" never is
                continue
            todo.append((i, key, row[2], row[3]))
    print(f"{args.target}: {sum(1 for r in rows if r[col] is None)} rows without "
          f"elevation, {len(todo)} to fetch ({len(cache)} cached)")

    errors = 0
    done = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch, lat, lng, target.radius_px): key
                   for _, key, lat, lng in todo}
        for fut in as_completed(futures):
            key = futures[fut]
            result = fut.result()
            done += 1
            if result == "ERR":
                errors += 1
            else:
                cache[key] = result
            if done % 100 == 0 or done == len(todo):
                elapsed = time.time() - started
                print(f"  {done}/{len(todo)} ({errors} errors, {elapsed:.0f}s)", flush=True)
                with open(target.cache, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh)

    filled = floored = 0
    for row in rows:
        if row[col] is None:
            raw = cache.get(f"{row[2]},{row[3]}")
            value = apply_floor(raw, target)
            if value is not None:
                row[col] = value
                filled += 1
            elif raw is not None:
                floored += 1

    output = []
    dropped_ids = []
    below_floor_ids = []
    for row in rows:
        if not keep_row(row, cache, target):
            dropped_ids.append(row[0])
            print(f"dropped (no DTM coverage): {row[1]} {row[0]}")
        elif is_below_floor(row, target):
            below_floor_ids.append(row[0])
            print(f"dropped (below {target.floor_m} m): {row[1]} {row[0]} {row[col]} m")
        else:
            output.append(row)

    record_dropped_ids(target.no_coverage_file, dropped_ids)
    record_dropped_ids(target.below_floor_file, below_floor_ids)

    with open(target.file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    still_null = sum(1 for r in output if r[col] is None)
    floor_note = (f"floored (< {target.floor_m} m, stored null) {floored}; "
                  if target.below_floor == "null" and target.floor_m is not None else "")
    drop_note = (f"dropped (below floor) {len(below_floor_ids)}; "
                 if target.below_floor == "drop" else "")
    print(f"filled {filled}; {floor_note}"
          f"dropped (no coverage) {len(dropped_ids)}; {drop_note}"
          f"still null (failed or no coverage) {still_null - floored}; "
          f"failures {errors}; rows {len(output)}")
    if errors:
        print("re-run to retry the failures (cache keeps what succeeded)")


if __name__ == "__main__":
    main()
