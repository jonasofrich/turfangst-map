#!/usr/bin/env python3
"""Checks a built outdoor MBTiles file before it may be shipped.

    check_tiles.py NEW.mbtiles [--previous OLD.mbtiles] [--stats]

Exit 0 when good, 1 when any check fails. It reads real tiles, so a good result
means the file decodes and holds the layers the style draws. Standard library
only, so it can run anywhere the file is.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import random
import re
import sqlite3
import sys
import zlib
from collections import namedtuple
from pathlib import Path

REQUIRED_LAYERS = (
    "ocean",
    "water",
    "water_label",
    "waterway",
    "landcover",
    "landuse",
    "protected",
    "road",
    "path",
    "rail",
    "ferry",
    "aerialway",
    "building",
    "boundary",
    "place",
    "peak",
    "outdoor",
    "contour",
)

MIN_ZOOM = 0
MAX_ZOOM = 14
# A new file may have at most this share fewer tiles than the previous one, per zoom.
MAX_TILE_DROP = 0.10
SAMPLES_PER_ZOOM = 25

Sample = namedtuple("Sample", "name z lat lng layers")

# Real tiles that must hold real data, and the layers read from them in a full build.
# Together they cover every required layer.
SAMPLES = (
    Sample("Oslo", 14, 59.9139, 10.7522, ("building", "road", "path", "rail", "contour", "place", "landuse")),
    Sample("Jotunheimen", 11, 61.6364, 8.3125, ("contour", "peak", "path", "water", "landcover", "outdoor")),
    Sample("Reine", 13, 67.9327, 13.0892, ("ocean", "contour", "road", "place", "building", "peak")),
    Sample("Storlien", 12, 63.3166, 12.1027, ("contour", "boundary", "road", "rail", "water", "waterway")),
    Sample("Åre", 11, 63.4, 13.08, ("aerialway", "water_label", "contour", "peak")),
    # Both sides of the seam between the two Planetiler runs. Natural Earth draws the border
    # at zoom 6 and OSM at zoom 7, and only zoom 6 has it from the first run.
    Sample("Oslo at z6", 6, 59.9139, 10.7522, ("ocean", "boundary", "place", "road", "water", "landcover")),
    Sample("Oslo at z7", 7, 59.9139, 10.7522, ("ocean", "place", "road", "water", "landcover", "protected")),
    Sample("Oslo at z8", 8, 59.9139, 10.7522, ("ferry", "road", "landcover")),
    Sample("Storlien at z6", 6, 63.3166, 12.1027, ("boundary", "place", "road", "water")),
    Sample("Storlien at z7", 7, 63.3166, 12.1027, ("boundary", "road", "water", "landcover", "protected")),
    # The world beyond the coverage, from Natural Earth and the frozen labels.
    Sample("The world", 0, 20.0, 0.0, ("ocean", "boundary", "landcover")),
    Sample("Rome at z5", 5, 41.9, 12.5, ("boundary", "place", "ocean")),
)
NAMED_PLACES = ("Oslo", "Jotunheimen", "Reine", "Storlien")


def tile_of(lat: float, lng: float, z: int) -> tuple[int, int]:
    """Slippy map tile address: x, y with y counted from the north."""
    n = 1 << z
    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(x, n - 1), min(y, n - 1)


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(data: bytes):
    """Yields (field number, wire type, value) of one protobuf message."""
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            length, pos = _varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            value = data[pos : pos + length]
            pos += length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > len(data):
                raise ValueError("truncated field")
            value = data[pos : pos + size]
            pos += size
        else:
            raise ValueError(f"unknown wire type {wire}")
        yield number, wire, value


def decode_layers(data: bytes) -> dict[str, int]:
    """Layer name to feature count, from an uncompressed vector tile."""
    layers: dict[str, int] = {}
    for number, wire, value in _fields(data):
        if number != 3 or wire != 2:
            continue
        name = None
        count = 0
        for inner, inner_wire, inner_value in _fields(value):
            if inner == 1 and inner_wire == 2:
                name = inner_value.decode()
            elif inner == 2 and inner_wire == 2:
                count += 1
                # Walks the feature too, so a corrupt one raises here.
                for _ in _fields(inner_value):
                    pass
        if not name:
            raise ValueError("layer without a name")
        layers[name] = layers.get(name, 0) + count
    return layers


def read_tile(con: sqlite3.Connection, z: int, x: int, y: int) -> dict[str, int] | None:
    """None when the file has no such tile. Raises ValueError on one that won't decode."""
    row = con.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
        (z, x, (1 << z) - 1 - y),
    ).fetchone()
    return None if row is None else decode_blob(row[0])


def decode_blob(blob: bytes) -> dict[str, int]:
    try:
        return decode_layers(gzip.decompress(blob))
    except (OSError, EOFError, zlib.error) as e:
        raise ValueError(f"not gzipped: {e}") from e
    except UnicodeDecodeError as e:
        raise ValueError(f"bad text in tile: {e}") from e


def open_read_only(path: Path) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise sqlite3.OperationalError(f"no such file: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def zoom_counts(con: sqlite3.Connection) -> dict[int, int]:
    return dict(con.execute("SELECT zoom_level, count(*) FROM tiles GROUP BY zoom_level"))


def total_tiles(con: sqlite3.Connection) -> int:
    return sum(zoom_counts(con).values())


def check_metadata(meta: dict[str, str], counts: dict[int, int]) -> list[str]:
    errors = []
    osm_date = meta.get("osm_date")
    if not osm_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", osm_date):
        errors.append(f"metadata osm_date is missing or not YYYY-MM-DD: {osm_date!r}")
    if meta.get("format") != "pbf":
        errors.append(f"metadata format is {meta.get('format')!r}, expected pbf")
    for key, expected in (("minzoom", MIN_ZOOM), ("maxzoom", MAX_ZOOM)):
        if meta.get(key) != str(expected):
            errors.append(f"metadata {key} is {meta.get(key)!r}, expected {expected}")
    try:
        west, south, east, north = [float(v) for v in meta.get("bounds", "").split(",")]
        if west > -179 or south > -85 or east < 179 or north < 85:
            errors.append(f"metadata bounds don't cover the world: {meta['bounds']}")
    except ValueError:
        errors.append(f"metadata bounds are unreadable: {meta.get('bounds')!r}")

    try:
        listed = {layer["id"] for layer in json.loads(meta.get("json", ""))["vector_layers"]}
    except (ValueError, KeyError, TypeError) as e:
        errors.append(f"metadata json has no readable vector_layers: {e}")
    else:
        for layer in REQUIRED_LAYERS:
            if layer not in listed:
                errors.append(f"layer {layer} is missing from metadata")

    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        if not counts.get(z):
            errors.append(f"z{z} has no tiles")
    return errors


def check_samples(con: sqlite3.Connection) -> list[str]:
    errors = []
    for s in SAMPLES:
        x, y = tile_of(s.lat, s.lng, s.z)
        where = f"{s.name} ({s.z}/{x}/{y})"
        try:
            layers = read_tile(con, s.z, x, y)
        except ValueError as e:
            errors.append(f"{where} doesn't decode: {e}")
            continue
        if layers is None:
            errors.append(f"{where} has no tile")
        elif not any(layers.values()):
            errors.append(f"{where} is empty")
        else:
            missing = [name for name in s.layers if not layers.get(name)]
            if missing:
                errors.append(f"{where} lacks {', '.join(missing)}, has {', '.join(sorted(layers))}")
            else:
                print(f"ok   {where}: {sum(layers.values())} features in {len(layers)} layers")
    return errors


def check_random_tiles(con: sqlite3.Connection, counts: dict[int, int]) -> list[str]:
    """Random tiles at every zoom must decode. Seeded, so a failure repeats."""
    errors = []
    rng = random.Random(14)
    for z in counts:
        for _ in range(SAMPLES_PER_ZOOM):
            column = rng.randrange(1 << z)
            row = con.execute(
                "SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level = ? AND tile_column >= ? "
                "ORDER BY tile_column, tile_row LIMIT 1",
                (z, column),
            ).fetchone()
            if row is None:
                continue
            try:
                decode_blob(row[2])
            except ValueError as e:
                errors.append(f"tile z{z} column {row[0]} row {row[1]} doesn't decode: {e}")
    return errors


def check_previous(counts: dict[int, int], previous: Path) -> list[str]:
    try:
        old = open_read_only(previous)
        try:
            old_counts = zoom_counts(old)
        finally:
            old.close()
    except sqlite3.Error as e:
        return [f"previous file {previous} is unreadable: {e}"]
    errors = []
    for z, was in sorted(old_counts.items()):
        now = counts.get(z, 0)
        if now < was * (1 - MAX_TILE_DROP):
            errors.append(f"tile count at z{z} fell from {was} to {now}, over {MAX_TILE_DROP:.0%}")
    print(f"     previous {previous.name}: {sum(old_counts.values())} tiles, now {sum(counts.values())}")
    return errors


def print_stats(con: sqlite3.Connection) -> None:
    print("zoom   tiles  avg_bytes  largest_bytes")
    rows = con.execute(
        "SELECT zoom_level, count(*), avg(length(tile_data)), max(length(tile_data)) FROM tiles GROUP BY zoom_level"
    )
    for z, count, avg, largest in rows:
        print(f"z{z:<3} {count:>8} {round(avg):>10} {largest:>14}")


def run_checks(path: Path, previous: Path | None, stats: bool) -> list[str]:
    con = open_read_only(path)
    try:
        counts = zoom_counts(con)
        meta = dict(con.execute("SELECT name, value FROM metadata"))
        errors = check_metadata(meta, counts)
        errors += check_samples(con)
        errors += check_random_tiles(con, counts)
        if previous is not None:
            errors += check_previous(counts, previous)
        print(f"     {path.name}: {sum(counts.values())} tiles, osm_date {meta.get('osm_date')}")
        if stats:
            print_stats(con)
        return errors
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a built outdoor MBTiles file")
    parser.add_argument("mbtiles", type=Path)
    parser.add_argument("--previous", type=Path, help="the build before it, to compare tile counts with")
    parser.add_argument("--stats", action="store_true", help="also print tiles, average and largest size per zoom")
    args = parser.parse_args(argv)
    try:
        errors = run_checks(args.mbtiles, args.previous, args.stats)
    except sqlite3.Error as e:
        errors = [f"{args.mbtiles} is unreadable: {e}"]
    for error in errors:
        print(f"FAIL {error}")
    print(f"FAILED {len(errors)} checks" if errors else "OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
