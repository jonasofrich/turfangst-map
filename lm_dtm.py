#!/usr/bin/env python3
"""Lantmäteriet 1 m DTM window reader with tile mosaic.

`LantmaterietDtm.window(e, n)` returns a square window of the DTM centred on
a SWEREF 99 TM (EPSG:3006) point, stitched from every `dtm-cog` tile that
intersects it, so a window crossing a tile border or corner is complete.
Feeds summit_snap.judge(). Pure stitching lives in `paste_tiles` (testable
without network).

Needs rasterio + pyproj and ~/.netrc (machine dl1.lantmateriet.se).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Callable, Protocol

import numpy as np

from fill_elevation_se import COLLECTION, NODATA, STAC_SEARCH

Bounds = tuple[float, float, float, float]
Reader = Callable[[float, float, float, float, int], np.ndarray]

RETRIES = 6
# Bump whenever the window/summit_height reading changes what it returns:
# convert_peaks_lm_se folds it into its verdict-cache file name.
READER_VERSION = 1


class WindowSource(Protocol):
    def window(self, e: float, n: float, radius_m: float,
               step_m: float) -> np.ndarray | None: ...


def paste_tiles(tiles: list[tuple[Bounds, Reader]], e: float, n: float,
                radius_m: float, step_m: float) -> np.ndarray:
    """Window of (2r/step+1)^2 cells centred on (e, n), row 0 = north, NaN
    where no tile covers. Cell (row, col) sits at x = e + (col-R)*step,
    y = n - (row-R)*step. Tile edges are half-open ([minx, maxx), (miny, maxy])
    so neighbours partition the grid: no gap and no double cover at a seam.
    Each reader is asked for the cells' rectangle, snapped to the grid."""
    r = int(round(radius_m / step_m))
    size = 2 * r + 1
    out = np.full((size, size), np.nan)
    idx = np.arange(size)
    xs = e + (idx - r) * step_m
    ys = n - (idx - r) * step_m
    for (minx, miny, maxx, maxy), reader in tiles:
        cols = idx[(xs >= minx) & (xs < maxx)]
        rows = idx[(ys > miny) & (ys <= maxy)]
        if cols.size == 0 or rows.size == 0:
            continue
        c0, c1, r0, r1 = cols[0], cols[-1], rows[0], rows[-1]
        h = step_m / 2
        block = reader(xs[c0] - h, ys[r1] - h, xs[c1] + h, ys[r0] + h, step_m)
        out[r0:r1 + 1, c0:c1 + 1] = block
    return out


_local = threading.local()


def _transformers():
    import pyproj
    if not hasattr(_local, "to_wgs"):
        _local.to_wgs = pyproj.Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True)
    return _local.to_wgs


class LantmaterietDtm:
    """Thread-safe (per-thread pyproj transformer; no shared mutable state)."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep):
        self._sleep = sleep

    def _retry(self, fn):
        for attempt in range(RETRIES):
            try:
                return fn()
            except Exception:
                # GDAL/rasterio CPLE_* errors inherit only from Exception.
                if attempt == RETRIES - 1:
                    raise
                self._sleep(2 * 2 ** attempt)

    def _hrefs(self, minx: float, miny: float, maxx: float, maxy: float) -> list[str]:
        tr = _transformers()
        pts = [tr.transform(x, y) for x in (minx, maxx) for y in (miny, maxy)]
        bbox = [min(p[0] for p in pts), min(p[1] for p in pts),
                max(p[0] for p in pts), max(p[1] for p in pts)]
        body = json.dumps({"collections": [COLLECTION], "bbox": bbox,
                           "limit": 20}).encode()

        def search():
            req = urllib.request.Request(
                STAC_SEARCH, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp).get("features") or []

        features = self._retry(search)
        return sorted({f["assets"]["data"]["href"] for f in features})

    def _tile_reader(self, href: str) -> tuple[Bounds, Reader]:
        import rasterio
        from rasterio.env import Env

        env = dict(GDAL_HTTP_NETRC="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                   CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                   GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2")

        def open_bounds():
            with Env(**env), rasterio.open("/vsicurl/" + href) as ds:
                return tuple(ds.bounds)

        l, b, r, t = self._retry(open_bounds)

        def read(minx, miny, maxx, maxy, step):
            from rasterio.enums import Resampling
            w = int(round((maxx - minx) / step))
            h = int(round((maxy - miny) / step))

            def go():
                with Env(**env), rasterio.open("/vsicurl/" + href) as ds:
                    nodata = ds.nodata if ds.nodata is not None else NODATA
                    win = ds.window(minx, miny, maxx, maxy)
                    a = ds.read(1, window=win, out_shape=(h, w), boundless=True,
                                fill_value=nodata, resampling=Resampling.nearest)
                    a = a.astype(float)
                    a[a == nodata] = np.nan
                    return a

            return self._retry(go)

        return (l, b, r, t), read

    def window(self, e: float, n: float, radius_m: float = 600,
               step_m: float = 2) -> np.ndarray | None:
        pad = radius_m + step_m
        hrefs = self._hrefs(e - pad, n - pad, e + pad, n + pad)
        if not hrefs:
            # Also what a transient empty STAC answer looks like: callers
            # must not cache None as a lasting "no data" verdict.
            return None
        tiles = [self._tile_reader(h) for h in hrefs]
        return paste_tiles(tiles, e, n, radius_m, step_m)

    def summit_height(self, e: float, n: float) -> float | None:
        """Max of a 5x5 px window at native 1 m resolution (like
        fill_elevation_se.sample_dtm radius_px=2); never decimated, so an
        averaged overview cannot read a summit low."""
        w = self.window(e, n, 2, 1)
        if w is None or np.isnan(w).all():
            return None
        return round(float(np.nanmax(w)), 1)
