#!/usr/bin/env python3
"""Tests for check_tiles and merge_tiles. Run: python3 infra/map/test_check_tiles.py

No network. Every MBTiles file here is a small generated fixture.
"""
import gzip
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_tiles as ct  # noqa: E402
import merge_tiles as mt  # noqa: E402


def varint(value):
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, payload):
    """A length-delimited protobuf field."""
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def number(num, value):
    return varint(num << 3) + varint(value)


def encode_layer(name, features=1):
    # One value table entry and a point geometry, as a real tile has.
    feature = number(1, 1) + field(2, bytes([0, 0])) + number(3, 1) + field(4, bytes([9, 2, 2]))
    body = field(1, name.encode()) + b"".join(field(2, feature) for _ in range(features))
    body += field(3, b"class") + field(4, field(1, b"x")) + number(5, 4096) + number(15, 2)
    return body


def encode_tile(layers):
    return gzip.compress(b"".join(field(3, encode_layer(name)) for name in layers))


ALL_LAYERS = tuple(ct.REQUIRED_LAYERS)
GOOD_META = {
    "name": "Turfangst outdoor",
    "format": "pbf",
    "type": "baselayer",
    "minzoom": "0",
    "maxzoom": "14",
    "bounds": "-180,-85.05113,180,85.05113",
    "osm_date": "2026-11-02",
}


def vector_layers(layers=ALL_LAYERS):
    return json.dumps({"vector_layers": [{"id": name, "fields": {}, "minzoom": 0, "maxzoom": 14} for name in layers]})


def all_sample_tiles():
    return {(s.z, *ct.tile_of(s.lat, s.lng, s.z)): encode_tile(s.layers) for s in ct.SAMPLES}


def make_mbtiles(path, *, meta=None, layers=ALL_LAYERS, tiles=None, extra_at_z14=0, compact=False):
    """A file that passes every check unless a test takes something away."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    values = dict(GOOD_META if meta is None else meta)
    if layers is not None:
        values["json"] = vector_layers(layers)
    con.executemany("INSERT INTO metadata VALUES (?, ?)", values.items())

    rows = {(z, 0, 0): encode_tile(["ocean"]) for z in range(15)}
    rows.update(all_sample_tiles() if tiles is None else tiles)
    for i in range(extra_at_z14):
        rows[(14, 1, 1 + i)] = encode_tile(["ocean"])

    if compact:
        con.execute("CREATE TABLE tiles_shallow (zoom_level INT, tile_column INT, tile_row INT, tile_data_id INT)")
        con.execute("CREATE TABLE tiles_data (tile_data_id INTEGER PRIMARY KEY, tile_data BLOB)")
        con.execute(
            "CREATE VIEW tiles AS SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles_shallow "
            "JOIN tiles_data ON tiles_shallow.tile_data_id = tiles_data.tile_data_id"
        )
        for i, ((z, x, y), data) in enumerate(rows.items(), start=1):
            con.execute("INSERT INTO tiles_data VALUES (?, ?)", (i, data))
            con.execute("INSERT INTO tiles_shallow VALUES (?, ?, ?, ?)", (z, x, (1 << z) - 1 - y, i))
    else:
        con.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
        con.executemany(
            "INSERT INTO tiles VALUES (?, ?, ?, ?)",
            [(z, x, (1 << z) - 1 - y, data) for (z, x, y), data in rows.items()],
        )
    con.commit()
    con.close()
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="check_tiles_"))
        self.addCleanup(shutil.rmtree, self.dir)

    def run_check(self, path, previous=None):
        """Returns the exit code and what the tool printed."""
        out = io.StringIO()
        with redirect_stdout(out):
            args = [str(path)] + (["--previous", str(previous)] if previous else [])
            code = ct.main(args)
        return code, out.getvalue()


class CheckTiles(Base):
    def test_passes_on_good_file(self):
        code, text = self.run_check(make_mbtiles(self.dir / "good.mbtiles"))
        self.assertEqual(code, 0, text)
        self.assertIn("OK", text)

    def test_passes_on_compact_file(self):
        code, text = self.run_check(make_mbtiles(self.dir / "good.mbtiles", compact=True))
        self.assertEqual(code, 0, text)

    def test_cli_exit_codes(self):
        good = make_mbtiles(self.dir / "good.mbtiles")
        bad = make_mbtiles(self.dir / "bad.mbtiles", layers=ALL_LAYERS[:-1])
        tool = str(HERE / "check_tiles.py")
        self.assertEqual(subprocess.run([sys.executable, tool, str(good)], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run([sys.executable, tool, str(bad)], capture_output=True).returncode, 1)

    def test_fails_on_missing_layer(self):
        for name in ("contour", "ocean"):
            layers = tuple(x for x in ALL_LAYERS if x != name)
            code, text = self.run_check(make_mbtiles(self.dir / f"no_{name}.mbtiles", layers=layers))
            self.assertEqual(code, 1, text)
            self.assertIn(name, text)

    def test_fails_when_a_sample_tile_lacks_its_layer(self):
        # The metadata lists contour, but the real tile at Jotunheimen has none.
        tiles = all_sample_tiles()
        s = next(x for x in ct.SAMPLES if x.name == "Jotunheimen")
        tiles[(s.z, *ct.tile_of(s.lat, s.lng, s.z))] = encode_tile([x for x in s.layers if x != "contour"])
        code, text = self.run_check(make_mbtiles(self.dir / "a.mbtiles", tiles=tiles))
        self.assertEqual(code, 1, text)
        self.assertIn("Jotunheimen", text)
        self.assertIn("contour", text)

    def z14_fixture(self, name, tiles_at_z14):
        """A file with exactly this many tiles at z14. Only z14 differs between two of these."""
        base = make_mbtiles(self.dir / f"base_{name}")
        before = ct.zoom_counts(sqlite3.connect(base))[14]
        return make_mbtiles(self.dir / name, extra_at_z14=tiles_at_z14 - before)

    def test_fails_when_tile_count_drops_over_10_percent_from_previous(self):
        previous = self.z14_fixture("old.mbtiles", 100)
        code, text = self.run_check(self.z14_fixture("new.mbtiles", 89), previous)
        self.assertEqual(code, 1, text)
        self.assertIn("tile count at z14", text)

    def test_passes_when_tile_count_drops_exactly_10_percent(self):
        previous = self.z14_fixture("old.mbtiles", 100)
        code, text = self.run_check(self.z14_fixture("new.mbtiles", 90), previous)
        self.assertEqual(code, 0, text)

    def test_fails_when_one_zoom_drops_over_10_percent(self):
        previous = make_mbtiles(self.dir / "old.mbtiles", extra_at_z14=2000)
        # z13 has only a few tiles, so the total barely moves.
        tiles = all_sample_tiles()
        con = sqlite3.connect(previous)
        con.executemany(
            "INSERT INTO tiles VALUES (13, 1, ?, ?)", [(10 + i, encode_tile(["ocean"])) for i in range(40)]
        )
        con.commit()
        con.close()
        code, text = self.run_check(make_mbtiles(self.dir / "new.mbtiles", tiles=tiles, extra_at_z14=2000), previous)
        self.assertEqual(code, 1, text)
        self.assertIn("z13", text)

    def test_fails_when_previous_is_unreadable(self):
        good = make_mbtiles(self.dir / "good.mbtiles")
        code, text = self.run_check(good, self.dir / "missing.mbtiles")
        self.assertEqual(code, 1, text)

    def test_fails_without_osm_date(self):
        meta = {k: v for k, v in GOOD_META.items() if k != "osm_date"}
        code, text = self.run_check(make_mbtiles(self.dir / "a.mbtiles", meta=meta))
        self.assertEqual(code, 1, text)
        self.assertIn("osm_date", text)

    def test_fails_on_malformed_osm_date(self):
        meta = dict(GOOD_META, osm_date="November")
        code, text = self.run_check(make_mbtiles(self.dir / "a.mbtiles", meta=meta))
        self.assertEqual(code, 1, text)
        self.assertIn("osm_date", text)

    def test_fails_when_named_place_tile_is_empty(self):
        for place in ct.NAMED_PLACES:
            s = next(x for x in ct.SAMPLES if x.name == place)
            key = (s.z, *ct.tile_of(s.lat, s.lng, s.z))
            for label, value in (("no features", gzip.compress(b"")), ("zero bytes", b"")):
                tiles = all_sample_tiles()
                tiles[key] = value
                path = self.dir / f"{place}_{label}.mbtiles"
                code, text = self.run_check(make_mbtiles(path, tiles=tiles))
                self.assertEqual(code, 1, f"{place} {label}: {text}")
                self.assertIn(place, text)
            tiles = all_sample_tiles()
            del tiles[key]
            code, text = self.run_check(make_mbtiles(self.dir / f"{place}_gone.mbtiles", tiles=tiles))
            self.assertEqual(code, 1, f"{place} gone: {text}")
            self.assertIn(place, text)

    def test_fails_when_a_tile_is_not_gzipped_protobuf(self):
        tiles = all_sample_tiles()
        s = ct.SAMPLES[0]
        tiles[(s.z, *ct.tile_of(s.lat, s.lng, s.z))] = b"not a tile"
        code, text = self.run_check(make_mbtiles(self.dir / "a.mbtiles", tiles=tiles))
        self.assertEqual(code, 1, text)

    def test_fails_when_metadata_zooms_lie(self):
        meta = dict(GOOD_META, maxzoom="6")
        code, text = self.run_check(make_mbtiles(self.dir / "a.mbtiles", meta=meta))
        self.assertEqual(code, 1, text)
        self.assertIn("maxzoom", text)

    def test_fails_when_a_zoom_has_no_tiles(self):
        path = make_mbtiles(self.dir / "a.mbtiles")
        con = sqlite3.connect(path)
        con.execute("DELETE FROM tiles WHERE zoom_level = 5")
        con.commit()
        con.close()
        code, text = self.run_check(path)
        self.assertEqual(code, 1, text)
        self.assertIn("z5", text)

    def test_fails_on_file_that_is_not_mbtiles(self):
        path = self.dir / "a.mbtiles"
        path.write_bytes(b"nope")
        code, text = self.run_check(path)
        self.assertEqual(code, 1, text)

    def test_stats_prints_average_and_largest_per_zoom(self):
        path = make_mbtiles(self.dir / "a.mbtiles", compact=True)
        out = io.StringIO()
        with redirect_stdout(out):
            code = ct.main([str(path), "--stats"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertRegex(out.getvalue(), r"z14\s+\d+\s+\d+\s+\d+")


class Samples(unittest.TestCase):
    def test_samples_cover_every_required_layer(self):
        covered = {layer for s in ct.SAMPLES for layer in s.layers}
        self.assertEqual(set(ct.REQUIRED_LAYERS) - covered, set())

    def test_ski_is_expected_and_sampled_in_both_countries(self):
        self.assertIn("ski", ct.REQUIRED_LAYERS)
        samples = [s.name for s in ct.SAMPLES if "ski" in s.layers]
        self.assertEqual(samples, ["Nordmarka", "Sälen"])

    def test_samples_read_both_sides_of_the_seam(self):
        self.assertTrue({6, 7} <= {s.z for s in ct.SAMPLES})


class TileMaths(unittest.TestCase):
    def test_tile_of_known_place(self):
        # Oslo at z14, worked out by hand with the sec form of the formula.
        self.assertEqual(ct.tile_of(59.9139, 10.7522, 14), (8681, 4765))

    def test_decode_reads_layer_names_and_counts(self):
        data = gzip.decompress(encode_tile(["road", "peak"]))
        self.assertEqual(ct.decode_layers(data), {"road": 1, "peak": 1})

    def test_decode_rejects_truncated_data(self):
        data = gzip.decompress(encode_tile(["road"]))
        with self.assertRaises(ValueError):
            ct.decode_layers(data[:-2])


def fixture_run(path, zooms, layer_fields, *, meta_extra=None):
    """A Planetiler-shaped compact file: tiles_shallow, tiles_data and the view."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE metadata (name TEXT, value TEXT, PRIMARY KEY (name))")
    con.execute(
        "CREATE TABLE tiles_shallow (zoom_level INT, tile_column INT, tile_row INT, tile_data_id INT, "
        "PRIMARY KEY (zoom_level, tile_column, tile_row)) WITHOUT ROWID"
    )
    con.execute("CREATE TABLE tiles_data (tile_data_id INTEGER PRIMARY KEY, tile_data BLOB)")
    con.execute(
        "CREATE VIEW tiles AS SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles_shallow "
        "JOIN tiles_data ON tiles_shallow.tile_data_id = tiles_data.tile_data_id"
    )
    for i, z in enumerate(zooms, start=1):
        con.execute("INSERT INTO tiles_data VALUES (?, ?)", (i, encode_tile([f"layer{z}"])))
        con.execute("INSERT INTO tiles_shallow VALUES (?, 0, 0, ?)", (z, i))
    meta = {
        "name": "Turfangst outdoor",
        "format": "pbf",
        "type": "baselayer",
        "attribution": "OSM",
        "version": "1",
        "minzoom": str(min(zooms)),
        "maxzoom": str(max(zooms)),
        "bounds": "-180,-85.05113,180,85.05113" if max(zooms) <= 6 else "-11.4,52,40,81.1",
        "center": "0,0,1",
        "json": json.dumps(
            {
                "vector_layers": [
                    {"id": name, "fields": fields, "minzoom": min(zooms), "maxzoom": max(zooms)}
                    for name, fields in layer_fields.items()
                ]
            }
        ),
        "planetiler:osm:osmosisreplicationtime": "stale",
        **(meta_extra or {}),
    }
    con.executemany("INSERT INTO metadata VALUES (?, ?)", meta.items())
    con.commit()
    con.close()
    return path


class MergeTiles(Base):
    def merged(self):
        low = fixture_run(self.dir / "low.mbtiles", [0, 6], {"ocean": {}, "place": {"name": "String"}})
        high = fixture_run(
            self.dir / "high.mbtiles", [7, 14], {"ocean": {}, "place": {"class": "String"}, "contour": {"ele": "Number"}}
        )
        mt.merge(low, high, "2026-11-02")
        con = sqlite3.connect(high)
        self.addCleanup(con.close)
        return con

    def test_both_sides_of_the_seam_read_their_own_tile(self):
        con = self.merged()
        rows = {
            z: ct.decode_layers(gzip.decompress(data))
            for z, data in con.execute("SELECT zoom_level, tile_data FROM tiles")
        }
        self.assertEqual(set(rows), {0, 6, 7, 14})
        for z, layers in rows.items():
            self.assertEqual(set(layers), {f"layer{z}"})

    def test_metadata_describes_the_merged_file(self):
        con = self.merged()
        meta = dict(con.execute("SELECT name, value FROM metadata"))
        self.assertEqual(meta["minzoom"], "0")
        self.assertEqual(meta["maxzoom"], "14")
        self.assertEqual(meta["bounds"], "-180,-85.05113,180,85.05113")
        self.assertEqual(meta["osm_date"], "2026-11-02")
        self.assertNotIn("planetiler:osm:osmosisreplicationtime", meta)

        layers = {x["id"]: x for x in json.loads(meta["json"])["vector_layers"]}
        self.assertEqual(set(layers), {"ocean", "place", "contour"})
        self.assertEqual((layers["ocean"]["minzoom"], layers["ocean"]["maxzoom"]), (0, 14))
        # A layer only one run wrote keeps that run's zooms.
        self.assertEqual((layers["contour"]["minzoom"], layers["contour"]["maxzoom"]), (7, 14))
        self.assertEqual(layers["place"]["fields"], {"name": "String", "class": "String"})

    def test_refuses_overlapping_zooms(self):
        low = fixture_run(self.dir / "low.mbtiles", [0, 7], {"ocean": {}})
        high = fixture_run(self.dir / "high.mbtiles", [7, 14], {"ocean": {}})
        with self.assertRaises(ValueError):
            mt.merge(low, high, "2026-11-02")

    def test_refuses_a_gap_in_zooms(self):
        low = fixture_run(self.dir / "low.mbtiles", [0, 5], {"ocean": {}})
        high = fixture_run(self.dir / "high.mbtiles", [7, 14], {"ocean": {}})
        with self.assertRaises(ValueError):
            mt.merge(low, high, "2026-11-02")

    def test_refuses_a_bad_date(self):
        low = fixture_run(self.dir / "low.mbtiles", [0, 6], {"ocean": {}})
        high = fixture_run(self.dir / "high.mbtiles", [7, 14], {"ocean": {}})
        with self.assertRaises(ValueError):
            mt.merge(low, high, "2026-11-02T21:21:26Z")

    def test_a_failed_merge_leaves_the_file_as_it_was(self):
        low = fixture_run(self.dir / "low.mbtiles", [0, 6], {"ocean": {}})
        high = fixture_run(self.dir / "high.mbtiles", [7, 14], {"ocean": {}})
        con = sqlite3.connect(low)
        con.execute("UPDATE metadata SET value = '{broken' WHERE name = 'json'")
        con.commit()
        con.close()
        with self.assertRaises(ValueError):
            mt.merge(low, high, "2026-11-02")
        con = sqlite3.connect(high)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT count(*) FROM tiles").fetchone()[0], 2)
        self.assertEqual(con.execute("SELECT value FROM metadata WHERE name = 'minzoom'").fetchone()[0], "7")


if __name__ == "__main__":
    unittest.main()
