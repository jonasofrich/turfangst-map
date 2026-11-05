#!/usr/bin/env python3
"""Tests for the stops in build_map.sh and for check_curves. Run: python3 infra/map/test_build_map.py

No network beyond a small server on 127.0.0.1. The shell tests source build_map.sh and
call one function, which is why its top level is only main.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_curves as cc  # noqa: E402

SCRIPT = HERE / "build_map.sh"
DOWN = "http://127.0.0.1:1"  # Nothing listens here, so curl fails at once.
DAY = 86400


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ShellBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="build_map_"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.sources = self.dir / "sources"
        self.sources.mkdir()
        self.www = self.dir / "www"
        self.www.mkdir()
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(self.www)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, function_call, **variables):
        """Sources the script, sets variables and calls one function. Returns (code, text)."""
        settings = {"SOURCES": str(self.sources), **variables}
        assignments = "".join(f"{k}={json.dumps(v)}\n" for k, v in settings.items())
        script = f'source "{SCRIPT}"\n{assignments}{function_call}\n'
        run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        return run.returncode, run.stdout + run.stderr


class Extracts(ShellBase):
    def publish(self, content, md5=None, age_days=0):
        path = self.www / "norway-latest.osm.pbf"
        path.write_bytes(content)
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
        digest = md5 or hashlib.md5(content).hexdigest()
        (self.www / "norway-latest.osm.pbf.md5").write_text(f"{digest}  norway-latest.osm.pbf\n")

    @property
    def kept(self):
        return self.sources / "norway-latest.osm.pbf"

    def get(self):
        return self.call("get_extract norway", GEOFABRIK=self.url)

    def test_good_download_lands(self):
        self.publish(b"good")
        code, text = self.get()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.kept.read_bytes(), b"good")
        self.assertEqual(list(self.sources.glob("*.part")), [])

    def test_not_modified_and_matching_stays(self):
        self.publish(b"good", age_days=2)
        self.assertEqual(self.get()[0], 0)
        before = self.kept.stat().st_mtime
        code, text = self.get()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.kept.stat().st_mtime, before)

    def test_bad_download_never_lands(self):
        self.publish(b"corrupt", md5="0" * 32)
        code, text = self.get()
        self.assertEqual(code, 1, text)
        self.assertIn("FAILED", text)
        self.assertFalse(self.kept.exists())
        self.assertEqual(list(self.sources.glob("*.part")), [])

    def test_bad_download_leaves_the_kept_file_alone(self):
        self.publish(b"old", age_days=5)
        self.assertEqual(self.get()[0], 0)
        self.publish(b"new but corrupt", md5="0" * 32)
        code, text = self.get()
        self.assertEqual(code, 1, text)
        self.assertEqual(self.kept.read_bytes(), b"old")

    def test_corrupt_kept_file_with_fresh_date_recovers_on_the_next_run(self):
        # The reviewer's case: not modified on the server, so curl fetches nothing, and the md5 fails.
        self.publish(b"good", age_days=2)
        self.kept.write_bytes(b"corrupt, and newer than the server's")
        code, text = self.get()
        self.assertEqual(code, 1, text)
        self.assertIn("has been deleted", text)
        self.assertFalse(self.kept.exists())
        code, text = self.get()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.kept.read_bytes(), b"good")

    def test_unreadable_checksum_stops_before_downloading(self):
        self.publish(b"good")
        (self.www / "norway-latest.osm.pbf.md5").unlink()
        code, text = self.get()
        self.assertEqual(code, 1, text)
        self.assertIn("checksum", text)
        self.assertFalse(self.kept.exists())


def make_zip(path, age_days=0):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("water.txt", "sea")
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


class Water(ShellBase):
    @property
    def kept(self):
        return self.sources / "water-polygons-split-3857.zip"

    def down(self):
        return self.call("REUSE=0; get_water", WATER_URL=f"{DOWN}/x.zip", WATER_MIRROR=DOWN)

    def test_both_down_kept_within_90_days_is_used(self):
        make_zip(self.kept, age_days=89)
        code, text = self.down()
        self.assertEqual(code, 0, text)
        self.assertIn("both servers failed", text)

    def test_both_down_kept_over_90_days_fails_and_says_what_to_do(self):
        make_zip(self.kept, age_days=91)
        code, text = self.down()
        self.assertEqual(code, 1, text)
        self.assertIn("91 days old", text)
        self.assertIn("the limit is 90", text)
        self.assertIn("download water-polygons-split-3857.zip by hand", text)
        self.assertTrue(self.kept.exists())

    def test_both_down_no_copy_fails(self):
        code, text = self.down()
        self.assertEqual(code, 1, text)
        self.assertIn("no kept copy", text)

    def test_both_down_corrupt_copy_fails(self):
        self.kept.write_bytes(b"not a zip")
        code, text = self.down()
        self.assertEqual(code, 1, text)
        self.assertIn("corrupt", text)

    def test_mirror_serving_only_a_very_old_file_fails(self):
        folder = self.www / "2026-01-01"
        folder.mkdir()
        make_zip(folder / "water-polygons-split-3857.zip", age_days=200)
        (self.www / "index.html").write_text('<a href="2026-01-01/">x</a>')
        code, text = self.call("REUSE=0; get_water", WATER_URL=f"{DOWN}/x.zip", WATER_MIRROR=self.url)
        self.assertEqual(code, 1, text)
        self.assertIn("200 days old", text)

    def test_mirror_is_used_when_the_first_host_is_down(self):
        folder = self.www / "2026-11-01"
        folder.mkdir()
        make_zip(folder / "water-polygons-split-3857.zip", age_days=3)
        (self.www / "index.html").write_text('<a href="2026-11-01/">x</a>')
        code, text = self.call("REUSE=0; get_water", WATER_URL=f"{DOWN}/x.zip", WATER_MIRROR=self.url)
        self.assertEqual(code, 0, text)
        self.assertIn("FAU mirror", text)
        self.assertTrue(self.kept.exists())

    def test_fresh_file_replaces_a_very_old_copy(self):
        make_zip(self.kept, age_days=400)
        make_zip(self.www / "w.zip", age_days=1)
        code, text = self.call("REUSE=0; get_water", WATER_URL=f"{self.url}/w.zip", WATER_MIRROR=DOWN)
        self.assertEqual(code, 0, text)
        self.assertIn("osmdata", text)


class Curves(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="check_curves_"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.gpkg = self.dir / "contours.gpkg"
        floor = mock.patch.object(cc, "MIN_CURVES", 3)
        floor.start()
        self.addCleanup(floor.stop)

    def finished_build(self, curves=5, log_curves=None, bbox=None):
        con = sqlite3.connect(self.gpkg)
        con.execute("CREATE TABLE contour (ele INT)")
        con.executemany("INSERT INTO contour VALUES (?)", [(i,) for i in range(curves)])
        con.commit()
        con.close()
        (self.dir / "manifest.json").write_text(json.dumps({"sigma": 1.3, "bbox": bbox}))
        (self.dir / "build.log").write_text(
            f"[start] out=x\nSUMMARY ok coverage_NO=99% contours.gpkg=1GB curves={log_curves or curves} shade_tiles=9\n"
        )

    def errors(self):
        return "\n".join(cc.problems(self.gpkg)[0])

    def test_finished_build_passes(self):
        self.finished_build()
        self.assertEqual(self.errors(), "")
        self.assertEqual(cc.main([str(self.gpkg)]), 0)

    def test_floor_is_stated_and_below_todays_count(self):
        mock.patch.stopall()
        self.assertEqual(cc.MIN_CURVES, 1_350_000)
        self.assertLess(cc.MIN_CURVES, 1_420_786)

    def test_missing_file_fails(self):
        self.assertIn("no curves", self.errors())
        self.assertEqual(cc.main([str(self.gpkg)]), 1)

    def test_under_the_floor_fails_even_from_a_finished_build(self):
        self.finished_build(curves=2)
        self.assertIn("the floor is 3", self.errors())

    def test_no_manifest_fails(self):
        self.finished_build()
        (self.dir / "manifest.json").unlink()
        self.assertIn("manifest.json", self.errors())

    def test_trial_area_fails(self):
        self.finished_build(bbox=[7.95, 61.4, 8.85, 61.8])
        self.assertIn("trial area", self.errors())

    def test_no_log_fails(self):
        self.finished_build()
        (self.dir / "build.log").unlink()
        self.assertIn("build.log", self.errors())

    def test_interrupted_build_fails(self):
        self.finished_build()
        (self.dir / "build.log").write_text("[start] out=x\n[contours] 12/435 done\n")
        self.assertIn("didn't finish", self.errors())

    def test_failed_rerun_after_a_good_one_fails(self):
        self.finished_build()
        with (self.dir / "build.log").open("a") as f:
            f.write("FAILED land check\n")
        self.assertIn("didn't finish", self.errors())

    def test_summary_of_another_file_fails(self):
        self.finished_build(curves=5, log_curves=1420786)
        text = self.errors()
        self.assertIn("reports 1420786 curves", text)
        self.assertIn("holds 5", text)

    def test_not_a_database_fails(self):
        self.finished_build()
        self.gpkg.write_bytes(b"nope" * 1000)
        self.assertIn("unreadable", self.errors())

    def test_build_map_stops_on_bad_curves_before_doing_anything(self):
        root = self.dir / "root"
        run = subprocess.run(
            [str(SCRIPT), "--root", str(root), "--contours", str(self.gpkg)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("no curves", run.stdout)
        self.assertIn("FAILED the curves", run.stdout)
        self.assertEqual(list((root / "bin").iterdir()), [])
        self.assertEqual(list((root / "sources").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
