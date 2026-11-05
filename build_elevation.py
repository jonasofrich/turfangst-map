#!/usr/bin/env python3
"""Elevation build for Turfangst's own outdoor map of Norway and Sweden.

    python3 infra/map/build_elevation.py --no-dtm <dir> --out <dir> [--bbox w,s,e,n] [--force]

Produces two files in --out:

    contours.gpkg      layer contour, lines in EPSG:4326, fields ele:int, index:int, minzoom:int
    hillshade.mbtiles  PNG with alpha, zooms 8 to 13

Stages, each resumable, because a full build can run for hours:

    1. sweden    one 10 m GeoTIFF per Lantmäteriet source tile, cached for good
    2. surface   Kartverket in Norway, Lantmäteriet in Sweden, blended across the land
                 border, nodata outside both, smoothed, in 50 km chunks
    3. coverage  land cells with no height, per country, against set limits
    4. contours  gdal_contour per chunk, cut at the chunk edge so curves join exactly
    5. shading   gdaldem per z8 map tile, warped to z13 pixels, tiled with overviews
    6. merge     the two final files, written under a temporary name and then renamed

Re-running continues where a run stopped. A failed stage exits non-zero and says
what failed. See infra/map/README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import warnings
from functools import lru_cache
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
import shapely
from pyproj import Transformer
from rasterio.env import Env
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning
from rasterio.features import rasterize
from rasterio.transform import Affine, from_origin
from rasterio.warp import reproject
from rasterio.windows import Window
from scipy.ndimage import distance_transform_edt, gaussian_filter, label
from shapely.geometry import shape

# rasterio's in-memory warp dataset trips this. The tests pin the warped positions.
warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)

HERE = Path(__file__).resolve().parent
# Beside this file in the published copy, in scripts/ where they are kept.
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "scripts")]
from fill_elevation_se import COLLECTION, STAC_SEARCH  # noqa: E402
from lm_dtm import LantmaterietDtm  # noqa: E402

UTM33 = "EPSG:25833"
NODATA = -9999.0
CELL_M = 10
# Upper left corner of Kartverket's DTM10 mosaic. Every chunk and every Swedish
# tile sits on this grid, so all of them line up cell for cell without resampling.
GRID_X0 = -100255
GRID_Y0 = 7950255

# Chosen by eye on real terrain, so changing any of these needs a look at the map.
SIGMA = 1.3
CONTOUR_INTERVAL_M = 20
SIMPLIFY_M = 1.5
HILLSHADE_Z = "1.6"
SHADE_TURN = 0.72
SHADE_DARK_RGB = (28, 38, 56)
SHADE_LIGHT_RGB = (255, 255, 255)
SHADE_DARK_ALPHA = 150
SHADE_LIGHT_ALPHA = 90

CHUNK_CELLS = 5000
SMOOTH_PAD = 8  # more than the Gaussian's reach of 6 cells at sigma 1.3
CONTOUR_PAD = 2  # marching squares needs the cells past the cut to agree on both sides
SHADE_PAD_M = 500  # past the map tile, so warping and gdaldem have no edge inside it
CHUNK_ZOOM = 8
MAX_ZOOM = 13
Z8_TILE_PX = 256 * 2 ** (MAX_ZOOM - CHUNK_ZOOM)
MERCATOR_ORIGIN = 20037508.342789244

SE_WORKERS = 4  # same as fill_elevation_se.py, this is a public service
SE_GIVE_UP_AFTER = 20  # failures in a row: the service is down, stop asking it
SE_GDAL_ENV = dict(GDAL_HTTP_NETRC="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                   CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                   GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2",
                   GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")
GDAL_CACHE_MB = "512"
# Lantmäteriet's search reports no total, so a shorter catalog than this can't be
# shown to be whole. Counted on 2026-11-03. Raise it if the collection grows.
SE_KNOWN_TILES = 5004

OUTSIDE, NORWAY, SWEDEN = 0, 1, 2
COUNTRY_CODES = {NORWAY: "NO", SWEDEN: "SE"}
BORDERS = HERE / "data" / "national_borders.geojson"  # see build_borders.py
OCEAN = Path("~/map-build/ocean_no_se.gpkg")  # see build_borders.py
BLEND_CELLS = 25  # 250 m on each side of the land border
# Limits for the coverage stage, set from a measurement of both countries on 2026-11-03:
# Norway 99.951 %, Sweden 99.986 %, largest hole 9.03 km2. A whole 10 km source tile is
# 100 km2, and the smallest that can go unseen is 2.5 x 5 km.
COVERAGE_MIN_PERCENT = 99.5
COVERAGE_MIN_LAND_KM2 = 100.0  # a sliver of a country in a test box proves nothing
HOLE_MAX_KM2 = 12.0

BUILD_SETTINGS = {"sigma": SIGMA, "interval": CONTOUR_INTERVAL_M, "simplify": SIMPLIFY_M,
                  "z": HILLSHADE_Z, "turn": SHADE_TURN, "chunk": CHUNK_CELLS, "blend": BLEND_CELLS, "version": 2}


class BuildError(Exception):
    """A stage failed. The message says what to do next."""


# ---------------------------------------------------------------- pure parts

def land_mask(a: np.ndarray) -> np.ndarray:
    """True where a cell holds real ground. Sea and missing data are often 0.0 in
    Kartverket's tiles, -32767 is its nodata, -9999 is Lantmäteriet's and ours.
    Ground below sea level (Kristianstad) counts as missing too: it has no curve
    to draw, and the shading is flat there, so nothing visible is lost."""
    return (a > 0.0) & (a < 9000.0)


def se_weight(countries: np.ndarray, blend_cells: int = BLEND_CELLS) -> np.ndarray:
    """How much of each cell's height comes from Sweden: 0 in Norway, 1 in Sweden, 0.5
    on the land border, with a smooth ramp `blend_cells` wide on each side, so the
    surface has no crease where the ramp starts."""
    weight = (countries == SWEDEN).astype("float32")
    if not ((countries == NORWAY).any() and (countries == SWEDEN).any()):
        return weight
    # Half a cell off: the border itself lies between a cell and its neighbour.
    to_se = distance_transform_edt(countries != SWEDEN).astype("float32") - 0.5
    to_no = distance_transform_edt(countries != NORWAY).astype("float32") - 0.5
    side = np.where(countries == NORWAY, -to_se, to_no)
    t = np.clip((side + blend_cells) / (2 * blend_cells), 0, 1)
    ramp = t * t * (3 - 2 * t)
    return np.where(countries == OUTSIDE, weight, ramp).astype("float32")


def mosaic(no: np.ndarray, se: np.ndarray, countries: np.ndarray, weight: np.ndarray | None = None) -> np.ndarray:
    """One surface. Each country uses its own source, blended across the land border.
    Where only one source has ground, that source is used, even in the other country,
    because a hole is worse. Outside both countries the result is NODATA, whatever the
    sources hold."""
    if weight is None:
        weight = se_weight(countries)
    no_ok, se_ok = land_mask(no), land_mask(se)
    out = np.full(no.shape, NODATA, dtype="float32")
    out[se_ok] = se[se_ok]
    out[no_ok] = no[no_ok]
    both = no_ok & se_ok
    out[both] = (no[both] * (1 - weight[both]) + se[both] * weight[both]).astype("float32")
    out[countries == OUTSIDE] = NODATA
    return out


def smooth(a: np.ndarray, sigma: float = SIGMA) -> np.ndarray:
    """Gaussian over the land only, so coast cells aren't pulled toward the sea's 0
    and nodata stays nodata."""
    land = land_mask(a)
    weights = gaussian_filter(land.astype("float32"), sigma)
    out = gaussian_filter(np.where(land, a, 0).astype("float32"), sigma) / np.maximum(weights, 1e-3)
    out[~land] = NODATA
    return out


def contour_props(ele: int) -> dict | None:
    """Fields of one curve, or None when the curve isn't drawn (sea level)."""
    if ele <= 0:
        return None
    if ele % 100 == 0:
        return {"ele": ele, "index": 1, "minzoom": 9}
    if ele % 50 == 0:
        return {"ele": ele, "index": 0, "minzoom": 11}
    return {"ele": ele, "index": 0, "minzoom": 12}


def _shade_luts() -> np.ndarray:
    v = (np.arange(256, dtype="float32") / 255.0)
    dark = np.clip((SHADE_TURN - v) / SHADE_TURN, 0, 1) ** 1.2
    light = np.clip((v - SHADE_TURN) / (1 - SHADE_TURN), 0, 1)
    is_dark = v < SHADE_TURN
    luts = np.zeros((4, 256), dtype="uint8")
    for band in range(3):
        luts[band] = np.where(is_dark, SHADE_DARK_RGB[band], SHADE_LIGHT_RGB[band])
    luts[3] = np.where(is_dark, dark * SHADE_DARK_ALPHA, light * SHADE_LIGHT_ALPHA).astype("uint8")
    luts[3, 0] = 0  # gdaldem's nodata
    return luts


_SHADE_LUTS = _shade_luts()


def shade_rgba(hillshade: np.ndarray) -> np.ndarray:
    """gdaldem's byte shading to four bands: dark blue shadow, white highlight, and
    flat ground nearly transparent. Shape (4, h, w)."""
    return _SHADE_LUTS[:, hillshade]


# ---------------------------------------------------------------- grid

@dataclass(frozen=True)
class Win:
    """A rectangle of cells on the global grid."""
    col: int
    row: int
    w: int
    h: int

    def bounds(self) -> tuple[float, float, float, float]:
        left, top = cell_edge(self.col, self.row)
        return left, top - self.h * CELL_M, left + self.w * CELL_M, top

    def intersect(self, o: "Win") -> "Win | None":
        c0, r0 = max(self.col, o.col), max(self.row, o.row)
        c1, r1 = min(self.col + self.w, o.col + o.w), min(self.row + self.h, o.row + o.h)
        return Win(c0, r0, c1 - c0, r1 - r0) if c1 > c0 and r1 > r0 else None

    def grown(self, cells: int) -> "Win":
        return Win(self.col - cells, self.row - cells, self.w + 2 * cells, self.h + 2 * cells)

    def union(self, o: "Win") -> "Win":
        c0, r0 = min(self.col, o.col), min(self.row, o.row)
        c1, r1 = max(self.col + self.w, o.col + o.w), max(self.row + self.h, o.row + o.h)
        return Win(c0, r0, c1 - c0, r1 - r0)

    def transform(self) -> Affine:
        left, top = cell_edge(self.col, self.row)
        return from_origin(left, top, CELL_M, CELL_M)

    def name(self) -> str:
        return f"c{self.col}_{self.row}"


def cell_edge(col: int, row: int) -> tuple[float, float]:
    """Upper left corner of a cell, in EPSG:25833."""
    return GRID_X0 + col * CELL_M, GRID_Y0 - row * CELL_M


def cell_centre(col: int, row: int) -> tuple[float, float]:
    x, y = cell_edge(col, row)
    return x + CELL_M / 2, y - CELL_M / 2


def chunks_of(area: Win) -> list[Win]:
    """The area cut on a fixed lattice, so chunks are the same whatever the area."""
    out = []
    for r in range(area.row // CHUNK_CELLS, (area.row + area.h - 1) // CHUNK_CELLS + 1):
        for c in range(area.col // CHUNK_CELLS, (area.col + area.w - 1) // CHUNK_CELLS + 1):
            piece = Win(c * CHUNK_CELLS, r * CHUNK_CELLS, CHUNK_CELLS, CHUNK_CELLS).intersect(area)
            if piece:
                out.append(piece)
    return out


def utm_window(left: float, bottom: float, right: float, top: float) -> Win:
    """The smallest window of whole cells covering a rectangle."""
    c0 = math.floor((left - GRID_X0) / CELL_M)
    c1 = math.ceil((right - GRID_X0) / CELL_M)
    r0 = math.floor((GRID_Y0 - top) / CELL_M)
    r1 = math.ceil((GRID_Y0 - bottom) / CELL_M)
    return Win(c0, r0, c1 - c0, r1 - r0)


def bbox_window(bbox: tuple[float, float, float, float]) -> Win:
    w, s, e, n = bbox
    tr = Transformer.from_crs("EPSG:4326", UTM33, always_xy=True)
    xs, ys = tr.transform([w, w, e, e], [s, n, s, n])
    return utm_window(min(xs), min(ys), max(xs), max(ys))


def utm_bounds_to_wgs84(left: float, bottom: float, right: float, top: float) -> tuple[float, float, float, float]:
    tr = Transformer.from_crs(UTM33, "EPSG:4326", always_xy=True)
    return tr.transform_bounds(left, bottom, right, top, densify_pts=21)


def footprint_window(left: float, bottom: float, right: float, top: float) -> Win:
    """Cells whose centres fall in a Swedish tile: x in [left, right), y in (bottom,
    top], so neighbouring tiles partition the grid with no gap and no double cover."""
    c0 = math.ceil((left - GRID_X0 - CELL_M / 2) / CELL_M)
    c1 = math.ceil((right - GRID_X0 - CELL_M / 2) / CELL_M)
    r0 = math.ceil((GRID_Y0 - CELL_M / 2 - top) / CELL_M)
    r1 = math.ceil((GRID_Y0 - CELL_M / 2 - bottom) / CELL_M)
    return Win(c0, r0, c1 - c0, r1 - r0)


def resample_to_grid(a: np.ndarray, transform: Affine, nodata: float) -> tuple[Win, np.ndarray]:
    """A Swedish tile's overview, resampled bilinearly to the global 10 m grid.

    SWEREF 99 TM and ETRS89 / UTM 33 share one projection and their datums differ
    by well under a metre, so tile coordinates are used as they are. On a seam the
    kernel only sees its own tile: the first row and column sit up to half an
    overview cell (4 m) off before smoothing, which is far below what a map shows.
    """
    h, w = a.shape
    left, top = transform.c, transform.f
    right, bottom = left + w * transform.a, top + h * transform.e
    win = footprint_window(left, bottom, right, top)
    out = np.full((win.h, win.w), NODATA, dtype="float32")
    # One replicated cell on each side keeps a centre exactly on the edge inside the
    # source, whatever the floating point rounding of an uneven overview cell does.
    padded = np.pad(a, 1, mode="edge")
    reproject(source=padded, destination=out, src_transform=transform @ Affine.translation(-1, -1),
              src_crs=UTM33, src_nodata=nodata,
              dst_transform=win.transform(), dst_crs=UTM33, dst_nodata=NODATA,
              resampling=Resampling.bilinear, num_threads=1)
    return win, out


def mercator_res(zoom: int) -> float:
    return 2 * MERCATOR_ORIGIN / (256 * 2 ** zoom)


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Bounds of an XYZ tile in EPSG:3857: left, bottom, right, top."""
    size = 2 * MERCATOR_ORIGIN / 2 ** z
    left = -MERCATOR_ORIGIN + x * size
    top = MERCATOR_ORIGIN - y * size
    return left, top - size, left + size, top


def z8_tiles_for_utm_bounds(left: float, bottom: float, right: float, top: float) -> set[tuple[int, int]]:
    tr = Transformer.from_crs(UTM33, "EPSG:3857", always_xy=True)
    ml, mb, mr, mt = tr.transform_bounds(left, bottom, right, top, densify_pts=21)
    size = 2 * MERCATOR_ORIGIN / 2 ** CHUNK_ZOOM
    x0, x1 = int((ml + MERCATOR_ORIGIN) // size), int((mr + MERCATOR_ORIGIN) // size)
    y0, y1 = int((MERCATOR_ORIGIN - mt) // size), int((MERCATOR_ORIGIN - mb) // size)
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


# ---------------------------------------------------------------- Sweden

def read_catalog(post, limit: int = 1000) -> list[dict]:
    """Every source tile, following the STAC search's pages. `post` takes a request
    body and returns the parsed answer. Raises unless the answers show the end was
    reached: a full page with no next link, a page without features, a repeated tile
    or a total that doesn't match all mean tiles could be missing."""
    items: list[dict] = []
    seen = set()
    body = {"collections": [COLLECTION], "limit": limit}
    while True:
        page = post(body)
        features = page.get("features")
        if not isinstance(features, list):
            raise BuildError(f"catalog: page {body.get('token')} has no feature list")
        nxt = [link for link in page.get("links") or [] if link.get("rel") == "next"]
        if nxt and not features:
            raise BuildError(f"catalog: empty page {body.get('token')} with a next link")
        if not nxt and len(features) >= limit:
            raise BuildError(f"catalog: page {body.get('token')} is full but has no next link")
        for f in features:
            if f["id"] in seen:
                raise BuildError(f"catalog: tile {f['id']} came twice, paging went wrong")
            seen.add(f["id"])
            data = f["assets"]["data"]
            items.append({"id": f["id"], "href": data["href"], "bbox": data["proj:bbox"]})
        matched = page.get("numberMatched")
        if matched is not None and matched < len(items):
            raise BuildError(f"catalog: {len(items)} tiles but the service matched {matched}")
        if not nxt:
            if matched is not None and matched != len(items):
                raise BuildError(f"catalog: {len(items)} tiles but the service matched {matched}")
            return items
        body = dict(body, **nxt[0]["body"])


def check_catalog(items: list[dict], min_tiles: int) -> None:
    """The search reports no total, so the known count is the only proof of a whole
    catalog. It also guards a catalog already cached."""
    if len(items) < min_tiles:
        raise BuildError(f"catalog has {len(items)} tiles, at least {min_tiles} are known to exist, so Sweden would "
                         "get holes. Delete its catalog.json to read it again. If the collection really shrank, "
                         "pass --se-min-tiles.")


class SwedenDtm(LantmaterietDtm):
    """Lantmäteriet's retries and login, but whole tiles from their overviews.

    LantmaterietDtm.window() suits small windows. For a national mosaic it would
    open every tile twice per window, repeat a STAC search per window, read slivers
    of the neighbours, and use nearest neighbour.
    """

    def post_search(self, body: dict) -> dict:
        data = json.dumps(body).encode()

        def go():
            req = urllib.request.Request(STAC_SEARCH, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        return self._retry(go)

    def read_tile(self, item: dict) -> tuple[np.ndarray, Affine, float]:
        """The overview closest to 10 m without being coarser, by HTTP range reads.
        A 10 km tile gives 1250 x 1250 cells of 8 m, out of 10000 x 10000."""
        def go():
            with Env(**SE_GDAL_ENV), rasterio.open("/vsicurl/" + item["href"]) as ds:
                factors = [f for f in ds.overviews(1) if f * ds.res[0] <= CELL_M]
                factor = max(factors) if factors else 1
                h, w = math.ceil(ds.height / factor), math.ceil(ds.width / factor)
                a = ds.read(1, out_shape=(h, w), resampling=Resampling.nearest)
                tf = ds.transform @ Affine.scale(ds.width / w, ds.height / h)
                return a, tf, (ds.nodata if ds.nodata is not None else NODATA)
        return self._retry(go)


@dataclass
class FetchStats:
    total: int = 0
    fetched: int = 0
    cached: int = 0
    failed: int = 0
    seconds: float = 0.0
    tile_seconds: float = 0.0
    gave_up: bool = False
    failed_ids: list = field(default_factory=list)


def se_tile_path(cache: Path, item: dict) -> Path:
    return cache / f"{item['id']}.tif"


def write_float_tif(path: Path, a: np.ndarray, transform: Affine) -> None:
    """Written beside the target and renamed, so a file that exists is complete."""
    tmp = path.with_name(f".{path.stem}.tmp.tif")
    with rasterio.open(tmp, "w", driver="GTiff", width=a.shape[1], height=a.shape[0], count=1,
                       dtype="float32", crs=UTM33, transform=transform, nodata=NODATA, tiled=True,
                       blockxsize=512, blockysize=512, compress="deflate", predictor=3) as ds:
        ds.write(a, 1)
    os.replace(tmp, path)


def _fetch_one(item: dict, cache: Path, read_tile) -> float:
    start = time.time()
    a, transform, nodata = read_tile(item)
    win, out = resample_to_grid(a.astype("float32"), transform, nodata)
    write_float_tif(se_tile_path(cache, item), out, win.transform())
    return time.time() - start


def fetch_sweden(items: list[dict], cache: Path, read_tile, workers: int = SE_WORKERS,
                 log=lambda _: None) -> FetchStats:
    """Fill the per-tile cache. A failed tile is counted and left for the next run.
    After SE_GIVE_UP_AFTER failures in a row it stops asking and gives up."""
    stats = FetchStats(total=len(items))
    todo = [it for it in items if not se_tile_path(cache, it).exists()]
    stats.cached = len(items) - len(todo)
    log(f"[sweden] {len(items)} tiles needed, {stats.cached} cached, {len(todo)} to fetch, {workers} at a time")
    start = last = time.time()
    in_a_row = 0
    pending = {}
    queue = iter(todo)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            while len(pending) < workers and not stats.gave_up:
                item = next(queue, None)
                if item is None:
                    break
                pending[pool.submit(_fetch_one, item, cache, read_tile)] = item
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                item = pending.pop(fut)
                try:
                    stats.tile_seconds += fut.result()
                    stats.fetched += 1
                    in_a_row = 0
                except Exception as e:  # noqa: BLE001  GDAL's errors inherit only from Exception
                    stats.failed += 1
                    stats.failed_ids.append(item["id"])
                    in_a_row += 1
                    log(f"[sweden] tile {item['id']} failed: {type(e).__name__}: {str(e)[:200]}")
                    if in_a_row >= SE_GIVE_UP_AFTER:
                        stats.gave_up = True
            now = time.time()
            finished = stats.fetched + stats.failed
            if now - last >= 30 or finished == len(todo):
                last = now
                avg = stats.tile_seconds / stats.fetched if stats.fetched else 0.0
                log(f"[sweden] {finished}/{len(todo)} fetched, {stats.failed} failed, "
                    f"{now - start:.0f}s elapsed, {avg:.2f}s per tile")
    stats.seconds = time.time() - start
    return stats


# ---------------------------------------------------------------- workers (separate processes)

def run(cmd: list) -> None:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"{cmd[0]} exit {r.returncode}: {(r.stderr or r.stdout).strip()[-800:]}")


def read_into(out: np.ndarray, win: Win, path: Path, tile: Win) -> None:
    """Paste a raster that starts at `tile` into `out`, which covers `win`, filling
    only cells that have no ground yet."""
    overlap = win.intersect(tile)
    if overlap is None:
        return
    with rasterio.open(path) as ds:
        a = ds.read(1, window=Window(overlap.col - tile.col, overlap.row - tile.row, overlap.w, overlap.h))
    r, c = overlap.row - win.row, overlap.col - win.col
    target = out[r:r + overlap.h, c:c + overlap.w]
    empty = ~land_mask(target)
    target[empty] = a[empty]


@lru_cache(maxsize=2)
def load_borders(path: str) -> tuple:
    """The national polygons in EPSG:25833, as (geometry, country value) pairs."""
    doc = json.loads(Path(path).read_text())
    tr = Transformer.from_crs("EPSG:4326", UTM33, always_xy=True)

    def to_utm(coords: np.ndarray) -> np.ndarray:
        return np.column_stack(tr.transform(coords[:, 0], coords[:, 1]))
    values = {code: value for value, code in COUNTRY_CODES.items()}
    return tuple((shapely.transform(shape(f["geometry"]), to_utm), values[f["properties"]["country"]])
                 for f in doc["features"])


def countries_raster(path: str, win: Win) -> np.ndarray:
    """Each cell's country, by its centre. The polygons share their land border way
    for way, so no cell on it falls in neither or both."""
    return rasterize(load_borders(path), out_shape=(win.h, win.w), transform=win.transform(),
                     fill=OUTSIDE, dtype="uint8")


@dataclass(frozen=True)
class SurfaceJob:
    core: Win
    borders: str
    no_vrt: str
    no_win: Win
    se_tiles: tuple  # (path, Win)
    out: str


def build_surface_chunk(job: SurfaceJob) -> None:
    out = Path(job.out)
    win = job.core.grown(SMOOTH_PAD)
    # Wider than the window, so the distance to the border is right at its edges.
    wide = countries_raster(job.borders, win.grown(BLEND_CELLS))
    trim = (slice(BLEND_CELLS, -BLEND_CELLS), slice(BLEND_CELLS, -BLEND_CELLS))
    countries = wide[trim]
    core = (slice(SMOOTH_PAD, -SMOOTH_PAD), slice(SMOOTH_PAD, -SMOOTH_PAD))
    if not countries[core].any():
        out.with_suffix(".empty").touch()
        return
    weight = se_weight(wide)[trim]
    no = np.full((win.h, win.w), NODATA, dtype="float32")
    read_into(no, win, Path(job.no_vrt), job.no_win)
    se = np.full((win.h, win.w), NODATA, dtype="float32")
    for path, tile in job.se_tiles:
        read_into(se, win, Path(path), tile)
    surface = mosaic(no, se, countries, weight)
    if not land_mask(surface[core]).any():
        out.with_suffix(".empty").touch()
        return
    write_float_tif(out, smooth(surface)[core], job.core.transform())


def _gpkg_wkb(blob: bytes) -> bytes:
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(blob[3] >> 1) & 7]
    return blob[8 + envelope:]


def ocean_raster(path: str, win: Win) -> np.ndarray:
    """True on cells in the sea, by the polygons from build_borders.py."""
    left, bottom, right, top = win.bounds()
    con = sqlite3.connect(path)
    blobs = con.execute("SELECT o.geom FROM ocean o JOIN rtree_ocean_geom r ON o.fid = r.id "
                        "WHERE r.maxx >= ? AND r.minx <= ? AND r.maxy >= ? AND r.miny <= ?",
                        (left, right, bottom, top)).fetchall()
    con.close()
    if not blobs:
        return np.zeros((win.h, win.w), dtype=bool)
    shapes = [(shapely.from_wkb(_gpkg_wkb(b)), 1) for (b,) in blobs]
    return rasterize(shapes, out_shape=(win.h, win.w), transform=win.transform(), fill=0, dtype="uint8").astype(bool)


def count_coverage(surface: np.ndarray | None, countries: np.ndarray, ocean: np.ndarray, win: Win) -> dict:
    """Land cells and how many have a height, per country, and the largest hole."""
    stats = {}
    for value, code in COUNTRY_CODES.items():
        land = (countries == value) & ~ocean
        covered = land & (surface != NODATA) if surface is not None else np.zeros_like(land)
        entry = {"land": int(land.sum()), "covered": int(covered.sum()), "hole": 0, "hole_at": None}
        holes = land & ~covered
        if holes.any():
            labels, _ = label(holes)
            sizes = np.bincount(labels.ravel())
            sizes[0] = 0
            biggest = int(sizes.argmax())
            rows, cols = np.nonzero(labels == biggest)
            r, c = int(rows.mean()), int(cols.mean())
            entry["hole"] = int(sizes[biggest])
            entry["hole_at"] = list(cell_centre(win.col + c, win.row + r))
        stats[code] = entry
    return stats


@dataclass(frozen=True)
class CoverageJob:
    core: Win
    borders: str
    ocean: str
    surface: str
    out: str


def check_chunk_coverage(job: CoverageJob) -> None:
    countries = countries_raster(job.borders, job.core)
    if countries.any():
        ocean = ocean_raster(job.ocean, job.core)
        surface = None
        if Path(job.surface).exists():
            with rasterio.open(job.surface) as ds:
                surface = ds.read(1)
        stats = count_coverage(surface, countries, ocean, job.core)
    else:
        stats = {}
    tmp = Path(job.out).with_suffix(".tmp")
    tmp.write_text(json.dumps(stats))
    os.replace(tmp, job.out)


def add_up_coverage(parts: list[dict]) -> dict:
    """Totals per country. A hole is measured per chunk, so one that crosses a chunk
    edge counts as its largest piece, which is still far above the limit for a tile."""
    total: dict = {}
    for code in COUNTRY_CODES.values():
        pieces = [p[code] for p in parts if code in p]
        land = sum(p["land"] for p in pieces)
        covered = sum(p["covered"] for p in pieces)
        worst = max(pieces, key=lambda p: p["hole"], default={"hole": 0, "hole_at": None})
        cell_km2 = (CELL_M / 1000) ** 2
        lon_lat = None
        if worst["hole_at"]:
            tr = Transformer.from_crs(UTM33, "EPSG:4326", always_xy=True)
            lon_lat = [round(v, 4) for v in tr.transform(*worst["hole_at"])]
        total[code] = {"land_km2": round(land * cell_km2, 1), "covered_km2": round(covered * cell_km2, 1),
                       "percent": round(100 * covered / land, 3) if land else None,
                       "largest_hole_km2": round(worst["hole"] * cell_km2, 2), "largest_hole_at": lon_lat}
    return total


def coverage_failures(total: dict, min_percent: float, max_hole_km2: float) -> list[str]:
    out = []
    for code, c in total.items():
        if c["land_km2"] >= COVERAGE_MIN_LAND_KM2 and c["percent"] < min_percent:
            out.append(f"{code}: {c['percent']} % of {c['land_km2']} km2 of land has a height, limit {min_percent} %")
        if c["largest_hole_km2"] > max_hole_km2:
            out.append(f"{code}: a hole of {c['largest_hole_km2']} km2 at {c['largest_hole_at']}, "
                       f"limit {max_hole_km2} km2")
    return out


def _wkb_type(blob: bytes) -> int:
    """Geometry type code of a GeoPackage geometry blob."""
    flags = blob[3]
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
    order = "<" if blob[8 + envelope] == 1 else ">"
    return struct.unpack_from(order + "I", blob, 9 + envelope)[0]


def _set_contour_fields(path: Path) -> None:
    """contour_props decides every curve's fields. Rows that clipping left as points
    (a line that only touched the cut) go."""
    con = sqlite3.connect(path)
    con.create_function("wkb_type", 1, _wkb_type, deterministic=True)
    with con:
        con.execute("DELETE FROM contour WHERE wkb_type(geom) % 1000 != 2")
        for (ele,) in con.execute("SELECT DISTINCT ele FROM contour").fetchall():
            props = contour_props(ele)
            if props is None:
                con.execute("DELETE FROM contour WHERE ele = ?", (ele,))
            else:
                con.execute('UPDATE contour SET "index" = ?, minzoom = ? WHERE ele = ?',
                            (props["index"], props["minzoom"], ele))
    con.close()


@dataclass(frozen=True)
class ContourJob:
    core: Win
    surface_vrt: str
    surface_win: Win
    tmp: str
    out: str


def source_window_vrt(surface_vrt: str, surface_win: Win, win: Win, path: Path) -> None:
    """A VRT of `win` from the whole surface, which is free to make."""
    run(["gdal_translate", "-q", "-of", "VRT", "-srcwin", win.col - surface_win.col, win.row - surface_win.row,
         win.w, win.h, surface_vrt, path])


def build_contour_chunk(job: ContourJob) -> None:
    tmp = Path(job.tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    win = job.core.grown(CONTOUR_PAD).intersect(job.surface_win)
    source_window_vrt(job.surface_vrt, job.surface_win, win, tmp / "win.vrt")
    run(["gdal_contour", "-q", "-i", CONTOUR_INTERVAL_M, "-a", "ele", "-nln", "contour", "-f", "GPKG",
         tmp / "win.vrt", tmp / "raw.gpkg"])
    left, bottom, right, top = job.core.bounds()
    run(["ogr2ogr", "-q", "-f", "GPKG", "-lco", "SPATIAL_INDEX=NO", "-nln", "contour",
         "-clipsrc", left, bottom, right, top, tmp / "clip.gpkg", tmp / "raw.gpkg", "contour"])
    run(["ogr2ogr", "-q", "-f", "GPKG", "-lco", "SPATIAL_INDEX=NO", "-nln", "contour", "-nlt", "LINESTRING",
         "-explodecollections", "-simplify", SIMPLIFY_M, "-t_srs", "EPSG:4326",
         "-dialect", "sqlite", "-sql",
         'SELECT geom, CAST(ele AS INTEGER) AS ele, 0 AS "index", 0 AS minzoom FROM contour',
         tmp / "lines.gpkg", tmp / "clip.gpkg"])
    _set_contour_fields(tmp / "lines.gpkg")
    os.replace(tmp / "lines.gpkg", job.out)
    shutil.rmtree(tmp, ignore_errors=True)


@dataclass(frozen=True)
class ShadeJob:
    z8: tuple
    surface_vrt: str
    surface_win: Win
    tmp: str
    out: str


def _mark_empty(job: ShadeJob, tmp: Path) -> None:
    Path(job.out).with_suffix(".empty").touch()
    shutil.rmtree(tmp, ignore_errors=True)


def build_shade_tile(job: ShadeJob) -> None:
    tmp = Path(job.tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    bounds = tile_bounds(CHUNK_ZOOM, *job.z8)
    tr = Transformer.from_crs("EPSG:3857", UTM33, always_xy=True)
    left, bottom, right, top = tr.transform_bounds(*bounds, densify_pts=21)
    win = utm_window(left - SHADE_PAD_M, bottom - SHADE_PAD_M, right + SHADE_PAD_M, top + SHADE_PAD_M) \
        .intersect(job.surface_win)
    if win is None:
        _mark_empty(job, tmp)
        return
    source_window_vrt(job.surface_vrt, job.surface_win, win, tmp / "win.vrt")
    run(["gdaldem", "hillshade", "-q", "-multidirectional", "-z", HILLSHADE_Z, "-compute_edges",
         tmp / "win.vrt", tmp / "hs.tif"])
    with rasterio.open(tmp / "hs.tif") as ds:
        hs = ds.read(1)
        profile = ds.profile
    if not hs.any():  # sea only
        _mark_empty(job, tmp)
        return
    rgba = shade_rgba(hs)
    del hs
    profile.update(count=4, dtype="uint8", nodata=None, photometric="RGB", alpha="yes")
    with rasterio.open(tmp / "rgba.tif", "w", **profile) as ds:
        ds.write(rgba)
    del rgba
    (tmp / "hs.tif").unlink()
    run(["gdalwarp", "-q", "-t_srs", "EPSG:3857", "-te", *bounds, "-ts", Z8_TILE_PX, Z8_TILE_PX,
         "-r", "bilinear", "-wm", "512", "-of", "GTiff", tmp / "rgba.tif", tmp / "warp.tif"])
    (tmp / "rgba.tif").unlink()
    with rasterio.open(tmp / "warp.tif") as ds:
        if not ds.read(4).any():  # the ground was all in the margin outside this map tile
            _mark_empty(job, tmp)
            return
    # The pixels already sit exactly on z13's tile grid, so nothing is resampled here.
    run(["gdal_translate", "-q", "-of", "MBTILES", "-co", "TILE_FORMAT=PNG", "-co", "ZOOM_LEVEL_STRATEGY=AUTO",
         tmp / "warp.tif", tmp / "t.mbtiles"])
    factors = [2 ** i for i in range(1, MAX_ZOOM - CHUNK_ZOOM + 1)]
    run(["gdaladdo", "-q", "-r", "average", tmp / "t.mbtiles", *factors])
    con = sqlite3.connect(tmp / "t.mbtiles")
    top_zoom = con.execute("SELECT max(zoom_level) FROM tiles").fetchone()[0]
    con.close()
    if top_zoom not in (None, MAX_ZOOM):
        raise BuildError(f"z8 tile {job.z8}: top zoom came out as {top_zoom}, expected {MAX_ZOOM}")
    os.replace(tmp / "t.mbtiles", job.out)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- final files

def merge_contours(parts: list[Path], out: Path, log) -> int:
    tmp = out.with_name("contours.tmp.gpkg")
    tmp.unlink(missing_ok=True)
    start = last = time.time()
    written = 0
    for i, part in enumerate(parts, 1):
        con = sqlite3.connect(part)
        n = con.execute("SELECT count(*) FROM contour").fetchone()[0]
        con.close()
        if n:
            mode = ["-append", "-update"] if written else ["-f", "GPKG", "-lco", "SPATIAL_INDEX=NO"]
            run(["ogr2ogr", "-q", *mode, "-nln", "contour", "-nlt", "LINESTRING", "-gt", "65536", tmp, part])
            written += n
        if time.time() - last >= 30 or i == len(parts):
            last = time.time()
            log(f"[merge] curves {i}/{len(parts)} chunks, {written} lines, {last - start:.0f}s elapsed")
    if not written:
        raise BuildError("no curves came out of this area")
    run(["ogrinfo", "-q", tmp, "-sql", "SELECT CreateSpatialIndex('contour', 'geom')"])
    os.replace(tmp, out)
    return written


def merge_hillshade(parts: list[Path], out: Path, wgs84: tuple, log) -> int:
    tmp = out.with_name("hillshade.tmp.mbtiles")
    tmp.unlink(missing_ok=True)
    start = last = time.time()
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    for i, part in enumerate(parts, 1):
        con.execute("ATTACH DATABASE ? AS part", (str(part),))
        con.execute("INSERT INTO tiles SELECT zoom_level, tile_column, tile_row, tile_data FROM part.tiles")
        con.commit()
        con.execute("DETACH DATABASE part")
        if time.time() - last >= 30 or i == len(parts):
            last = time.time()
            log(f"[merge] shading {i}/{len(parts)} map tiles, {last - start:.0f}s elapsed")
    con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    n = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    if not n:
        con.close()
        raise BuildError("no shading tiles came out of this area")
    w, s, e, nn = wgs84
    meta = {"name": "Turfangst hill shading", "type": "overlay", "version": "1", "format": "png",
            "description": "Hill shading of Norway and Sweden",
            "minzoom": str(CHUNK_ZOOM), "maxzoom": str(MAX_ZOOM),
            "bounds": f"{w:.5f},{s:.5f},{e:.5f},{nn:.5f}",
            "center": f"{(w + e) / 2:.5f},{(s + nn) / 2:.5f},{CHUNK_ZOOM}",
            "attribution": "Kartverket, Lantmäteriet"}
    con.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    con.commit()
    con.close()
    os.replace(tmp, out)
    return n


def loose_line_ends(gpkg: Path, bbox: tuple, margin_deg: float = 0.0005) -> int:
    """Line ends inside the box that no other line end meets. Over land with data on
    all sides this should be 0: a curve only stops at a coast or at a data gap, so
    this finds both broken seams and holes in the surface."""
    w, s, e, n = bbox
    con = sqlite3.connect(gpkg)
    ends: dict = {}
    for (blob,) in con.execute("SELECT geom FROM contour"):
        flags = blob[3]
        envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
        o = 8 + envelope
        order = "<" if blob[o] == 1 else ">"
        if struct.unpack_from(order + "I", blob, o + 1)[0] % 1000 != 2:
            continue
        count = struct.unpack_from(order + "I", blob, o + 5)[0]
        first = struct.unpack_from(order + "2d", blob, o + 9)
        last = struct.unpack_from(order + "2d", blob, o + 9 + 16 * (count - 1))
        for pt in (first, last):
            key = (round(pt[0], 9), round(pt[1], 9))
            ends[key] = ends.get(key, 0) + 1
    con.close()
    return sum(1 for (x, y), c in ends.items()
               if c % 2 and w + margin_deg < x < e - margin_deg and s + margin_deg < y < n - margin_deg)


# ---------------------------------------------------------------- orchestration

class Monitor(threading.Thread):
    """Samples the whole process tree's memory and the scratch folder's size, because
    those are the numbers a full build has to report."""

    def __init__(self, work: Path):
        super().__init__(daemon=True)
        self.work = work
        self.peak_rss_kb = 0
        self.peak_disk = 0
        self.stop_event = threading.Event()

    def sample_memory(self) -> None:
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True)
        kids: dict = {}
        rss = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3:
                pid, ppid, kb = map(int, parts)
                kids.setdefault(ppid, []).append(pid)
                rss[pid] = kb
        total = 0
        stack = [os.getpid()]
        while stack:
            pid = stack.pop()
            total += rss.get(pid, 0)
            stack.extend(kids.get(pid, []))
        self.peak_rss_kb = max(self.peak_rss_kb, total)

    def sample_disk(self) -> None:
        self.peak_disk = max(self.peak_disk, dir_size(self.work))

    def run(self) -> None:
        tick = 0
        while not self.stop_event.wait(5):
            self.sample_memory()
            if tick % 6 == 0:
                self.sample_disk()
            tick += 1


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def gb(n: float) -> str:
    return f"{n / 1e9:.2f}GB"


def run_pool(stage: str, fn, jobs: list, workers: int, log) -> None:
    if not jobs:
        return
    start = last = time.time()
    failures = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, job): job for job in jobs}
        for fut in as_completed(futures):
            done += 1
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001  reported below, and the stage then fails
                failures.append(f"{futures[fut].out}: {type(e).__name__}: {e}")
            now = time.time()
            if now - last >= 30 or done == len(jobs):
                last = now
                eta = (now - start) / done * (len(jobs) - done)
                log(f"[{stage}] {done}/{len(jobs)} done, {len(failures)} failed, "
                    f"{now - start:.0f}s elapsed, about {eta:.0f}s left (estimate)")
    if failures:
        raise BuildError(f"{stage}: {len(failures)} of {len(jobs)} failed. Re-run to continue. "
                         f"First failures:\n  " + "\n  ".join(failures[:5]))


def build_no_vrt(no_dir: Path, work: Path) -> tuple[Path, Win]:
    """A VRT over the DTM10 tiles, in name order so a rebuild picks the same tile
    where two overlap."""
    tifs = sorted(p for p in no_dir.rglob("*.tif"))
    if not tifs:
        raise BuildError(f"no GeoTIFFs under --no-dtm {no_dir}")
    listing = work / "no_tifs.txt"
    listing.write_text("".join(f"{p}\n" for p in tifs))
    vrt = work / "no10.vrt"
    run(["gdalbuildvrt", "-q", "-input_file_list", listing, vrt])
    with rasterio.open(vrt) as ds:
        if ds.crs.to_epsg() != 25833 or ds.res != (CELL_M, CELL_M):
            raise BuildError(f"{vrt}: expected EPSG:25833 at 10 m, got {ds.crs} at {ds.res}")
        col = (ds.bounds.left - GRID_X0) / CELL_M
        row = (GRID_Y0 - ds.bounds.top) / CELL_M
        if col != int(col) or row != int(row):
            raise BuildError(f"{vrt}: cells aren't on the expected grid: {ds.bounds}")
        return vrt, Win(int(col), int(row), ds.width, ds.height)


def load_catalog(cache: Path, log, min_tiles: int) -> list[dict]:
    path = cache / "catalog.json"
    if path.exists():
        items = json.loads(path.read_text())
    else:
        start = time.time()
        items = read_catalog(SwedenDtm().post_search)
        log(f"[sweden] catalog: {len(items)} tiles, {time.time() - start:.0f}s")
    check_catalog(items, min_tiles)
    if not path.exists():
        tmp = path.with_name(".catalog.tmp.json")
        tmp.write_text(json.dumps(items))
        os.replace(tmp, path)
    return items


def check_manifest(out: Path, settings: dict) -> None:
    """Beside the final files and outside work/, so deleting work/ can't let another
    area or other settings pass as the same build."""
    path = out / "manifest.json"
    if path.exists():
        if json.loads(path.read_text()) != settings:
            raise BuildError(f"{out} was started with other settings or another area. "
                             "Use --force to rebuild, or another --out.")
        return
    leftovers = [p for p in ("work", "contours.gpkg", "hillshade.mbtiles") if (out / p).exists()]
    if leftovers:
        raise BuildError(f"{out} holds {leftovers} from an unknown build. Use --force to rebuild, or another --out.")
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=1))


def build(args, log) -> int:
    t0 = time.time()
    out = args.out.expanduser().resolve()
    no_dir = args.no_dtm.expanduser().resolve()
    if not no_dir.is_dir():
        raise BuildError(f"--no-dtm isn't a folder: {no_dir}")
    borders = args.borders.expanduser().resolve()
    ocean = args.ocean.expanduser().resolve()
    for path, flag in ((borders, "--borders"), (ocean, "--ocean")):
        if not path.exists():
            raise BuildError(f"{flag} is missing: {path}. infra/map/build_borders.py makes it.")
    work = out / "work"
    se_cache = (args.se_cache or out / "se10").expanduser().resolve()
    contours_out, shade_out = out / "contours.gpkg", out / "hillshade.mbtiles"
    coverage_out = out / "coverage.json"
    if args.force:
        shutil.rmtree(work, ignore_errors=True)
        for path in (contours_out, shade_out, coverage_out, out / "manifest.json"):
            path.unlink(missing_ok=True)
    if args.refetch_se:
        shutil.rmtree(se_cache, ignore_errors=True)
    check_manifest(out, dict(BUILD_SETTINGS, bbox=args.bbox, no_dtm=str(no_dir),
                             borders=hashlib.sha256(borders.read_bytes()).hexdigest()))
    for d in (work, se_cache):
        d.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(work / "tmp", ignore_errors=True)
    os.environ["GDAL_CACHEMAX"] = GDAL_CACHE_MB

    monitor = Monitor(work)
    monitor.start()
    log(f"[start] out={out} workers={args.workers} se_workers={args.se_workers} bbox={args.bbox}")

    fetch = FetchStats()
    if contours_out.exists() and shade_out.exists():
        log("[start] both final files exist, nothing to do. Use --force to rebuild.")
    else:
        no_vrt, no_win = build_no_vrt(no_dir, work)
        catalog = load_catalog(se_cache, log, args.se_min_tiles)
        tiles = sorted(((it, footprint_window(*it["bbox"])) for it in catalog), key=lambda t: t[0]["id"])
        if args.bbox:
            area = bbox_window(args.bbox)
        else:
            area = no_win
            for _, tile in tiles:
                area = area.union(tile)
        needed = [(it, t) for it, t in tiles if t.intersect(area.grown(SMOOTH_PAD))]
        log(f"[start] area {area.w} x {area.h} cells, {len(needed)} of {len(tiles)} Swedish source tiles")

        # 1. Sweden
        dtm = SwedenDtm()
        fetch = fetch_sweden([it for it, _ in needed], se_cache, dtm.read_tile, args.se_workers, log)
        record_fetch(se_cache, fetch)
        if fetch.failed:
            how = "the service looks down, stopped early" if fetch.gave_up else "the rest are cached"
            raise BuildError(f"sweden: {fetch.failed} tiles failed ({how}). Re-run to fetch only what's missing. "
                             f"First: {fetch.failed_ids[:5]}")

        # 2. Surface
        (work / "surface").mkdir(exist_ok=True)
        jobs = []
        chunks = chunks_of(area)
        for core in chunks:
            path = work / "surface" / f"{core.name()}.tif"
            if path.exists() or path.with_suffix(".empty").exists():
                continue
            se = tuple((str(se_tile_path(se_cache, it)), t) for it, t in needed if t.intersect(core.grown(SMOOTH_PAD)))
            jobs.append(SurfaceJob(core, str(borders), str(no_vrt), no_win, se, str(path)))
        log(f"[surface] {len(chunks)} chunks, {len(chunks) - len(jobs)} already done")
        run_pool("surface", build_surface_chunk, jobs, args.workers, log)
        kept = [c for c in chunks if (work / "surface" / f"{c.name()}.tif").exists()]
        if not kept:
            raise BuildError("no elevation data in this area")
        listing = work / "surface.txt"
        listing.write_text("".join(f"{work / 'surface' / (c.name() + '.tif')}\n" for c in kept))
        surface_vrt = work / "surface.vrt"
        run(["gdalbuildvrt", "-q", "-input_file_list", listing, surface_vrt])
        surface_win = kept[0]
        for c in kept[1:]:
            surface_win = surface_win.union(c)
        log(f"[surface] {len(kept)} chunks with land, {gb(dir_size(work / 'surface'))}, "
            f"{time.time() - t0:.0f}s since start")

        # 3. Coverage, before the long stages, because a hole here means a bad build
        (work / "coverage").mkdir(exist_ok=True)
        cover_parts = {c: work / "coverage" / f"{c.name()}.json" for c in chunks}
        jobs = [CoverageJob(c, str(borders), str(ocean), str(work / "surface" / f"{c.name()}.tif"), str(p))
                for c, p in cover_parts.items() if not p.exists()]
        log(f"[coverage] {len(cover_parts)} chunks, {len(cover_parts) - len(jobs)} already done")
        run_pool("coverage", check_chunk_coverage, jobs, args.workers, log)
        coverage = add_up_coverage([json.loads(p.read_text()) for p in cover_parts.values()])
        for code, c in coverage.items():
            log(f"[coverage] {code}: {c['percent']} % of {c['land_km2']} km2 of land, "
                f"largest hole {c['largest_hole_km2']} km2 at {c['largest_hole_at']}")
        failures = coverage_failures(coverage, args.min_coverage, args.max_hole_km2)
        if failures:
            raise BuildError("coverage: " + "; ".join(failures))
        coverage_out.write_text(json.dumps(coverage, indent=1))

        # 4. Contours
        if not contours_out.exists():
            (work / "contours").mkdir(exist_ok=True)
            parts = [work / "contours" / f"{c.name()}.gpkg" for c in kept]
            jobs = [ContourJob(c, str(surface_vrt), surface_win, str(work / "tmp" / f"contour_{c.name()}"), str(p))
                    for c, p in zip(kept, parts) if not p.exists()]
            log(f"[contours] {len(parts)} chunks, {len(parts) - len(jobs)} already done")
            run_pool("contours", build_contour_chunk, jobs, args.workers, log)

        # 5. Shading
        if not shade_out.exists():
            (work / "hillshade").mkdir(exist_ok=True)
            z8s: set = set()
            for c in kept:
                z8s |= z8_tiles_for_utm_bounds(*c.bounds())
            parts_s = {z8: work / "hillshade" / f"z8_{z8[0]}_{z8[1]}.mbtiles" for z8 in sorted(z8s)}
            jobs = [ShadeJob(z8, str(surface_vrt), surface_win, str(work / "tmp" / f"shade_{z8[0]}_{z8[1]}"), str(p))
                    for z8, p in parts_s.items() if not p.exists() and not p.with_suffix(".empty").exists()]
            log(f"[shading] {len(parts_s)} z8 map tiles, {len(parts_s) - len(jobs)} already done")
            run_pool("shading", build_shade_tile, jobs, args.workers, log)

        # 6. Final files
        if not contours_out.exists():
            merge_contours([work / "contours" / f"{c.name()}.gpkg" for c in kept], contours_out, log)
        if not shade_out.exists():
            left, bottom, right, top = surface_win.bounds()
            merge_hillshade([p for p in parts_s.values() if p.exists()], shade_out,
                            utm_bounds_to_wgs84(left, bottom, right, top), log)
        # What's left to keep is the surface and the Swedish cache, and those are reusable.
        shutil.rmtree(work / "contours", ignore_errors=True)
        shutil.rmtree(work / "hillshade", ignore_errors=True)
        shutil.rmtree(work / "tmp", ignore_errors=True)

    monitor.stop_event.set()
    monitor.join(timeout=10)
    monitor.sample_memory()
    monitor.sample_disk()
    cumulative = read_fetch_record(se_cache)
    con = sqlite3.connect(contours_out)
    curves = con.execute("SELECT count(*) FROM contour").fetchone()[0]
    con.close()
    con = sqlite3.connect(shade_out)
    shade_tiles = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    con.close()
    coverage = json.loads(coverage_out.read_text())
    covered = " ".join(f"coverage_{code}={c['percent']}% hole_{code}={c['largest_hole_km2']}km2"
                       for code, c in coverage.items())
    log(f"SUMMARY ok {covered}  wall={time.time() - t0:.0f}s peak_rss={monitor.peak_rss_kb * 1024 / 1e9:.2f}GB "
        f"work_disk_peak={gb(monitor.peak_disk)} work_disk_now={gb(dir_size(work))} "
        f"se_cache={gb(dir_size(se_cache))} "
        f"se_fetch_this_run={fetch.seconds:.0f}s se_fetched_this_run={fetch.fetched} "
        f"se_fetch_all_runs={cumulative['seconds']:.0f}s se_fetched_all_runs={cumulative['tiles']} "
        f"contours.gpkg={gb(contours_out.stat().st_size)} curves={curves} "
        f"hillshade.mbtiles={gb(shade_out.stat().st_size)} shade_tiles={shade_tiles}")
    return 0


def record_fetch(cache: Path, stats: FetchStats) -> None:
    """Fetch time adds up across interrupted runs, so the report's number is real."""
    record = read_fetch_record(cache)
    record["seconds"] += stats.seconds
    record["tiles"] += stats.fetched
    path = cache / "fetch_record.json"
    tmp = path.with_name(".fetch_record.tmp.json")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, path)


def read_fetch_record(cache: Path) -> dict:
    path = cache / "fetch_record.json"
    return json.loads(path.read_text()) if path.exists() else {"seconds": 0.0, "tiles": 0}


def parse_bbox(text: str) -> list[float]:
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError as err:
        raise argparse.ArgumentTypeError("--bbox needs w,s,e,n in degrees") from err
    if not (w < e and s < n):
        raise argparse.ArgumentTypeError("--bbox needs w < e and s < n")
    return [w, s, e, n]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Elevation curves and hill shading for the outdoor map.")
    parser.add_argument("--no-dtm", type=Path, required=True, help="folder with Kartverket's DTM10 GeoTIFFs")
    parser.add_argument("--out", type=Path, default=Path("~/map-build/elevation"), help="output folder")
    parser.add_argument("--bbox", type=parse_bbox, help="w,s,e,n in degrees, default both countries")
    parser.add_argument("--force", action="store_true",
                        help="rebuild everything except the Swedish cache")
    parser.add_argument("--se-cache", type=Path, help="Swedish cache folder, default <out>/se10")
    parser.add_argument("--refetch-se", action="store_true", help="also empty the Swedish cache first")
    parser.add_argument("--se-min-tiles", type=int, default=SE_KNOWN_TILES,
                        help="fewest Swedish tiles a catalog may list")
    parser.add_argument("--borders", type=Path, default=BORDERS, help="national polygons, from build_borders.py")
    parser.add_argument("--ocean", type=Path, default=OCEAN, help="ocean polygons, from build_borders.py")
    parser.add_argument("--min-coverage", type=float, default=COVERAGE_MIN_PERCENT,
                        help="percent of each country's land that must have a height")
    parser.add_argument("--max-hole-km2", type=float, default=HOLE_MAX_KM2, help="largest hole allowed in the land")
    parser.add_argument("--workers", type=int, default=10, help="parallel processes for local work")
    parser.add_argument("--se-workers", type=int, default=SE_WORKERS,
                        help="parallel reads from Lantmäteriet, keep this low")
    args = parser.parse_args(argv)

    def log(line: str) -> None:
        print(line, flush=True)
    start = time.time()
    try:
        return build(args, log)
    except BuildError as e:
        log(f"FAILED after {time.time() - start:.0f}s: {e}")
        return 1
    except Exception as e:  # noqa: BLE001  any crash must still say FAILED and exit non-zero
        log(f"FAILED after {time.time() - start:.0f}s: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
