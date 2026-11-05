#!/usr/bin/env python3
"""Makes the production shading file: hillshade.mbtiles as WebP with alpha, beside it,
plus Natural Earth relief for the zooms below our shading. The original is only read.

    infra/map/reencode_shading.py                        hillshade-webp.mbtiles
    infra/map/reencode_shading.py --in A --out B --jobs 8 --relief DIR

Quality 50 with alpha quality 80 is the lowest that shows no banding on smooth slopes.
At alpha quality 70, Jotunheimen at zoom 13 shows steps, side by side with the PNG.

Relief covers the world from zoom 0 to 7. Zoom 7 has no files of its own, each tile is a
quarter of a 512 px zoom 6 tile.

Needs Pillow. Writes to a temporary name and renames at the end, so a stopped run
leaves nothing that looks finished. Prints a SUMMARY line with time and sizes.
"""
from __future__ import annotations

import argparse
import io
import multiprocessing
import os
import sqlite3
import sys
import time
from pathlib import Path

from PIL import Image

DEFAULT_IN = Path.home() / "map-build/elevation/hillshade.mbtiles"
DEFAULT_RELIEF = Path.home() / "map-build/assets/natural_earth/ne2sr"
WORLD_BOUNDS = "-180,-85.05113,180,85.05113"
QUALITY = 50
ALPHA_QUALITY = 80
RELIEF_QUALITY = 60
METHOD = 4
BATCH = 256

_source: sqlite3.Connection | None = None


def encode_webp(png: bytes) -> bytes:
    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGBA").save(out, "WEBP", quality=QUALITY, alpha_quality=ALPHA_QUALITY, method=METHOD)
    return out.getvalue()


def relief_webp(png: bytes) -> bytes:
    out = io.BytesIO()
    Image.open(io.BytesIO(png)).save(out, "WEBP", quality=RELIEF_QUALITY, method=METHOD)
    return out.getvalue()


def relief_at_zoom_7(relief: Path, x: int, y: int) -> bytes | None:
    parent = relief / "6" / str(x // 2) / f"{y // 2}.png"
    if not parent.is_file():
        return None
    image = Image.open(parent)
    half = image.width // 2
    left, top = (x % 2) * half, (y % 2) * half
    out = io.BytesIO()
    image.crop((left, top, left + half, top + half)).save(out, "PNG")
    return out.getvalue()


def relief_jobs(relief: Path) -> list[tuple[Path, int, int, int]]:
    """One (folder, z, x, y) per relief tile, in the folder's own top-down numbering."""
    jobs = []
    for file in relief.glob("[0-6]/*/*.png"):
        z, x, y = int(file.parts[-3]), int(file.parts[-2]), int(file.stem)
        jobs.append((relief, z, x, y))
        if z == 6:
            jobs += [(relief, 7, 2 * x + i, 2 * y + j) for i in (0, 1) for j in (0, 1)]
    return jobs


def _encode_relief(job: tuple[Path, int, int, int]) -> tuple[int, int, int, bytes, int]:
    relief, z, x, y = job
    png = relief_at_zoom_7(relief, x, y) if z == 7 else (relief / str(z) / str(x) / f"{y}.png").read_bytes()
    return z, x, (1 << z) - 1 - y, relief_webp(png), len(png)


def production_metadata(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Martin answers only inside the zooms and bounds it reads here, so they must include the relief."""
    changes = {"format": "webp", "minzoom": "0", "bounds": WORLD_BOUNDS}
    return [(k, changes.get(k, v)) for k, v in rows]


def _open(path: Path) -> None:
    global _source
    _source = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def _encode_batch(rowids: list[int]) -> list[tuple[int, int, int, bytes, int]]:
    done = []
    marks = ",".join("?" * len(rowids))
    for z, x, y, data in _source.execute(f"SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles WHERE rowid IN ({marks})", rowids):
        done.append((z, x, y, encode_webp(data), len(data)))
    return done


def reencode(source: Path, target: Path, jobs: int, relief: Path) -> dict:
    relief_list = relief_jobs(relief)
    if not any(job[1] == 7 for job in relief_list):
        raise RuntimeError(f"no zoom 6 relief in {relief}")
    temp = target.with_name(target.name + ".part")
    temp.unlink(missing_ok=True)
    started = time.time()
    with sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True) as reader:
        rowids = [row[0] for row in reader.execute("SELECT rowid FROM tiles")]
        metadata = production_metadata(list(reader.execute("SELECT name, value FROM metadata")))
    writer = sqlite3.connect(temp)
    writer.execute("PRAGMA journal_mode=OFF")
    writer.execute("PRAGMA synchronous=OFF")
    writer.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    writer.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    writer.executemany("INSERT INTO metadata VALUES (?, ?)", metadata)
    batches = [rowids[i : i + BATCH] for i in range(0, len(rowids), BATCH)]
    bytes_in = bytes_out = tiles = 0
    with multiprocessing.Pool(jobs, _open, (source,)) as pool:
        for finished in pool.imap_unordered(_encode_batch, batches):
            writer.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [row[:4] for row in finished])
            tiles += len(finished)
            bytes_in += sum(row[4] for row in finished)
            bytes_out += sum(len(row[3]) for row in finished)
            if tiles % (BATCH * 100) < len(finished):
                print(f"{tiles}/{len(rowids)} tiles, {time.time() - started:.0f} s", flush=True)
        if tiles != len(rowids):
            raise RuntimeError(f"encoded {tiles} of {len(rowids)} tiles")
        relief_bytes = 0
        for finished in pool.imap_unordered(_encode_relief, relief_list, chunksize=64):
            writer.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", finished[:4])
            relief_bytes += len(finished[3])
    writer.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    writer.commit()
    writer.close()
    os.replace(temp, target)
    return {"tiles": tiles, "bytes_in": bytes_in, "bytes_out": bytes_out, "relief_tiles": len(relief_list), "relief_bytes": relief_bytes, "seconds": time.time() - started}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="source", type=Path, default=DEFAULT_IN)
    parser.add_argument("--out", type=Path, default=None, help="default: hillshade-webp.mbtiles beside the input")
    parser.add_argument("--relief", type=Path, default=DEFAULT_RELIEF, help="Natural Earth relief, {z}/{x}/{y}.png from zoom 0 to 6")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 4))
    args = parser.parse_args(argv)
    target = args.out or args.source.with_name(args.source.stem + "-webp.mbtiles")
    if target.resolve() == args.source.resolve():
        print("FAILED refusing to overwrite the input", file=sys.stderr)
        return 1
    result = reencode(args.source, target, args.jobs, args.relief)
    print(f"SUMMARY ok tiles={result['tiles']} relief_tiles={result['relief_tiles']} relief_bytes={result['relief_bytes']} seconds={result['seconds']:.0f} jobs={args.jobs} png_bytes={result['bytes_in']} webp_bytes={result['bytes_out']} out={target}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
