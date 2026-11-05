#!/usr/bin/env python3
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import reencode_shading


def png(alpha: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (64, 64), (30, 40, 60, alpha)).save(out, "PNG")
    return out.getvalue()


def relief_png() -> bytes:
    """512 px, with a red left half and a blue right half, so a quarter shows which side it came from."""
    image = Image.new("RGBA", (512, 512), (0, 0, 255, 255))
    image.paste((255, 0, 0, 255), (0, 0, 256, 512))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


class Reencode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "hillshade.mbtiles"
        db = sqlite3.connect(self.source)
        db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        db.execute("INSERT INTO metadata VALUES ('format', 'png'), ('name', 'x'), ('minzoom', '8'), ('bounds', '1,2,3,4')")
        db.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", [(8, x, 1, png(100)) for x in range(5)])
        db.commit()
        db.close()
        self.before = self.source.read_bytes()
        self.relief = Path(self.dir.name) / "relief"
        for z, x, y in [(0, 0, 0), (6, 10, 20)]:
            file = self.relief / str(z) / str(x) / f"{y}.png"
            file.parent.mkdir(parents=True)
            file.write_bytes(relief_png())

    def test_keeps_every_tile_and_alpha_and_the_input(self):
        target = Path(self.dir.name) / "out.mbtiles"
        result = reencode_shading.reencode(self.source, target, 2, self.relief)
        self.assertEqual(result["tiles"], 5)
        self.assertEqual(self.source.read_bytes(), self.before)
        db = sqlite3.connect(target)
        self.assertEqual(db.execute("SELECT value FROM metadata WHERE name='format'").fetchone()[0], "webp")
        data = db.execute("SELECT tile_data FROM tiles WHERE zoom_level = 8").fetchall()
        self.assertEqual(len(data), 5)
        image = Image.open(io.BytesIO(data[0][0]))
        self.assertEqual(image.format, "WEBP")
        self.assertAlmostEqual(image.convert("RGBA").getpixel((10, 10))[3], 100, delta=3)
        self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_relief_goes_in_below_the_shading(self):
        target = Path(self.dir.name) / "out.mbtiles"
        result = reencode_shading.reencode(self.source, target, 2, self.relief)
        self.assertEqual(result["relief_tiles"], 6)
        db = sqlite3.connect(target)
        rows = {(z, x, y) for z, x, y in db.execute("SELECT zoom_level, tile_column, tile_row FROM tiles WHERE zoom_level < 8")}
        # MBTiles rows count from the bottom, the relief folders from the top.
        self.assertIn((0, 0, 0), rows)
        self.assertIn((6, 10, 63 - 20), rows)
        self.assertEqual({r for r in rows if r[0] == 7}, {(7, x, 127 - y) for x in (20, 21) for y in (40, 41)})

    def test_zoom_7_is_the_right_quarter(self):
        target = Path(self.dir.name) / "out.mbtiles"
        reencode_shading.reencode(self.source, target, 2, self.relief)
        db = sqlite3.connect(target)
        for x, is_red in [(20, True), (21, False)]:
            data = db.execute("SELECT tile_data FROM tiles WHERE zoom_level = 7 AND tile_column = ? AND tile_row = ?", (x, 127 - 40)).fetchone()[0]
            image = Image.open(io.BytesIO(data))
            self.assertEqual(image.size, (256, 256))
            r, _, b, a = image.convert("RGBA").getpixel((128, 128))
            self.assertEqual(r > b, is_red)
            self.assertEqual(a, 255)

    def test_metadata_lets_a_server_answer_the_relief(self):
        target = Path(self.dir.name) / "out.mbtiles"
        reencode_shading.reencode(self.source, target, 2, self.relief)
        meta = dict(sqlite3.connect(target).execute("SELECT name, value FROM metadata"))
        self.assertEqual(meta["minzoom"], "0")
        self.assertEqual(meta["bounds"], reencode_shading.WORLD_BOUNDS)
        self.assertEqual(meta["name"], "x")

    def test_stops_without_relief(self):
        target = Path(self.dir.name) / "out.mbtiles"
        with self.assertRaises(RuntimeError):
            reencode_shading.reencode(self.source, target, 2, Path(self.dir.name) / "nothing")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
