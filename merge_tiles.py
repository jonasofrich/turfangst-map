#!/usr/bin/env python3
"""Folds the low zoom Planetiler run into the high zoom one, in place.

    merge_tiles.py LOW.mbtiles HIGH.mbtiles --osm-date YYYY-MM-DD

Copies the smaller file into the larger one, then rewrites the metadata to
describe the merged file. It runs in one transaction, so a failure leaves HIGH
as it was. Both must be Planetiler's compact format, tiles_shallow plus
tiles_data, which is its default.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


def _layers(meta: dict[str, str], label: str) -> list[dict]:
    try:
        return json.loads(meta["json"])["vector_layers"]
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError(f"{label} has no readable vector_layers: {e}") from e


def merge_layers(low: list[dict], high: list[dict]) -> list[dict]:
    """One entry per layer id: widest zoom range, all fields from either run."""
    merged: dict[str, dict] = {}
    for layer in low + high:
        into = merged.get(layer["id"])
        if into is None:
            merged[layer["id"]] = {**layer, "fields": dict(layer.get("fields", {}))}
            continue
        into["minzoom"] = min(into["minzoom"], layer["minzoom"])
        into["maxzoom"] = max(into["maxzoom"], layer["maxzoom"])
        for name, kind in layer.get("fields", {}).items():
            # A field two runs type differently can only be promised as text.
            into["fields"][name] = "String" if into["fields"].get(name, kind) != kind else kind
    return list(merged.values())


def _number(value: float) -> str:
    text = repr(value)
    return text[:-2] if text.endswith(".0") else text


def merge_bounds(low: str, high: str) -> str:
    a = [float(v) for v in low.split(",")]
    b = [float(v) for v in high.split(",")]
    box = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
    return ",".join(_number(v) for v in box)


def _zoom_range(con: sqlite3.Connection, schema: str) -> tuple[int, int]:
    low, high = con.execute(f"SELECT min(zoom_level), max(zoom_level) FROM {schema}.tiles_shallow").fetchone()
    if low is None:
        raise ValueError(f"{schema} has no tiles")
    return low, high


def merge(low_path: Path, high_path: Path, osm_date: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", osm_date):
        raise ValueError(f"osm_date must be YYYY-MM-DD: {osm_date}")
    con = sqlite3.connect(high_path, isolation_level=None)
    try:
        con.execute("ATTACH DATABASE ? AS low", (f"{low_path}",))
        low_range = _zoom_range(con, "low")
        high_range = _zoom_range(con, "main")
        if low_range[1] + 1 != high_range[0]:
            raise ValueError(f"zooms must meet: low {low_range}, high {high_range}")

        low_meta = dict(con.execute("SELECT name, value FROM low.metadata"))
        high_meta = dict(con.execute("SELECT name, value FROM main.metadata"))
        layers = merge_layers(_layers(low_meta, "low"), _layers(high_meta, "high"))
        document = json.loads(high_meta["json"])
        document["vector_layers"] = layers

        meta = {k: v for k, v in high_meta.items() if not k.startswith("planetiler:osm:")}
        meta.update(
            minzoom=str(low_range[0]),
            maxzoom=str(high_range[1]),
            bounds=merge_bounds(low_meta["bounds"], high_meta["bounds"]),
            json=json.dumps(document, separators=(",", ":")),
            osm_date=osm_date,
        )

        con.execute("BEGIN")
        try:
            offset = con.execute("SELECT coalesce(max(tile_data_id), 0) FROM main.tiles_data").fetchone()[0]
            con.execute(
                "INSERT INTO main.tiles_data (tile_data_id, tile_data) "
                "SELECT tile_data_id + ?, tile_data FROM low.tiles_data",
                (offset,),
            )
            con.execute(
                "INSERT INTO main.tiles_shallow (zoom_level, tile_column, tile_row, tile_data_id) "
                "SELECT zoom_level, tile_column, tile_row, tile_data_id + ? FROM low.tiles_shallow",
                (offset,),
            )
            con.execute("DELETE FROM main.metadata")
            con.executemany("INSERT INTO main.metadata (name, value) VALUES (?, ?)", meta.items())
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fold the low zoom run into the high zoom run")
    parser.add_argument("low", type=Path)
    parser.add_argument("high", type=Path, help="changed in place")
    parser.add_argument("--osm-date", required=True, help="YYYY-MM-DD, the replication day of the OSM sources")
    args = parser.parse_args(argv)
    try:
        merge(args.low, args.high, args.osm_date)
    except (ValueError, sqlite3.Error) as e:
        print(f"FAILED merging: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
