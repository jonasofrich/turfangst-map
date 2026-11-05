#!/usr/bin/env python3
"""Tests for build_elevation. Run: python3 infra/map/test_build_elevation.py

No network. The end-to-end test builds a small made-up area with the real GDAL
tools, from a pre-filled Swedish cache.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_elevation as be  # noqa: E402

ND = be.NODATA


def row(*values):
    return np.array([values], dtype="float32")


def countries(*values):
    return np.array([values], dtype="uint8")


# Far enough from any border that the ramp doesn't reach these cells.
DEEP_NO = countries(be.NORWAY)
DEEP_SE = countries(be.SWEDEN)


class Mosaic(unittest.TestCase):
    def test_mosaic_prefers_norway(self):
        """The name is the brief's. Norway's data only wins inside Norway now."""
        self.assertEqual(be.mosaic(row(100.0), row(200.0), DEEP_NO).tolist(), [[100.0]])

    def test_swedish_cell_with_both_valid_uses_swedish(self):
        self.assertEqual(be.mosaic(row(100.0), row(200.0), DEEP_SE).tolist(), [[200.0]])

    def test_cell_outside_both_is_nodata_whatever_the_sources_hold(self):
        out = be.mosaic(row(100.0), row(200.0), countries(be.OUTSIDE))
        self.assertEqual(out.tolist(), [[ND]])

    def test_hole_in_own_source_is_filled_from_the_other(self):
        for marker in (0.0, -32767.0, ND, np.nan):
            self.assertEqual(be.mosaic(row(marker), row(200.0), DEEP_NO).tolist(), [[200.0]])
            self.assertEqual(be.mosaic(row(100.0), row(marker), DEEP_SE).tolist(), [[100.0]])

    def test_hole_in_both_is_nodata(self):
        self.assertEqual(be.mosaic(row(0.0), row(ND), DEEP_NO).tolist(), [[ND]])

    def test_cell_in_the_blend_band_is_between_the_two(self):
        n = 200
        c = np.full((1, n), be.NORWAY, dtype="uint8")
        c[0, n // 2:] = be.SWEDEN
        no, se = np.full((1, n), 100.0, dtype="float32"), np.full((1, n), 200.0, dtype="float32")
        out = be.mosaic(no, se, c)[0]
        border = n // 2
        # Deep on each side, its own source exactly.
        self.assertEqual(out[border - be.BLEND_CELLS - 1], 100.0)
        self.assertEqual(out[border + be.BLEND_CELLS], 200.0)
        # Inside the band, strictly between, and rising from west to east.
        band = out[border - be.BLEND_CELLS + 1:border + be.BLEND_CELLS - 1]
        self.assertTrue(((band > 100.0) & (band < 200.0)).all())
        self.assertTrue((np.diff(band) > 0).all())
        # Equal shares where the two countries meet.
        self.assertAlmostEqual((out[border - 1] + out[border]) / 2, 150.0, places=3)

    def test_blend_with_only_one_source_uses_that_source(self):
        c = countries(be.NORWAY, be.SWEDEN)
        out = be.mosaic(row(100.0, 0.0), row(ND, 200.0), c)
        self.assertEqual(out.tolist(), [[100.0, 200.0]])

    def test_outer_border_has_no_ramp(self):
        # A border with Finland, say: no second country, so no blending and a hard stop.
        c = countries(be.NORWAY, be.NORWAY, be.OUTSIDE)
        out = be.mosaic(row(100.0, 100.0, 100.0), row(200.0, 200.0, 200.0), c)
        self.assertEqual(out.tolist(), [[100.0, 100.0, ND]])


class SeWeight(unittest.TestCase):
    def test_one_country_needs_no_distances(self):
        self.assertTrue((be.se_weight(np.full((5, 5), be.SWEDEN, dtype="uint8")) == 1).all())
        self.assertTrue((be.se_weight(np.full((5, 5), be.NORWAY, dtype="uint8")) == 0).all())

    def test_ramp_is_symmetric_about_the_border(self):
        c = np.full((1, 100), be.NORWAY, dtype="uint8")
        c[0, 50:] = be.SWEDEN
        w = be.se_weight(c, 10)[0]
        for i in range(12):
            self.assertAlmostEqual(w[49 - i] + w[50 + i], 1.0, places=5)

    def test_real_polygons_leave_no_gap_on_the_land_border(self):
        # Storlien's latitude, where the border crosses this window.
        win = be.bbox_window((11.9, 63.2, 12.3, 63.4))
        c = be.countries_raster(str(be.BORDERS), win)
        self.assertEqual(set(np.unique(c).tolist()), {be.NORWAY, be.SWEDEN})


class LandMask(unittest.TestCase):
    def test_land_mask_treats_zero_as_nodata(self):
        a = np.array([0.0, -32767.0, ND, np.nan, 0.5, 2469.0, 9500.0], dtype="float32")
        self.assertEqual(be.land_mask(a).tolist(),
                         [False, False, False, False, True, True, False])


class Smooth(unittest.TestCase):
    def test_smooth_keeps_nodata(self):
        a = np.full((21, 21), 100.0, dtype="float32")
        a[:, :10] = ND
        out = be.smooth(a, 1.3)
        self.assertTrue((out[:, :10] == ND).all())
        # Coast cells stay at the land's height, they aren't pulled toward 0.
        self.assertTrue(np.allclose(out[:, 10:], 100.0, atol=0.01))

    def test_smooth_treats_zero_sea_as_nodata(self):
        a = np.zeros((21, 21), dtype="float32")
        a[:, 10:] = 50.0
        out = be.smooth(a, 1.3)
        self.assertTrue((out[:, :10] == ND).all())
        self.assertTrue(np.allclose(out[:, 10:], 50.0, atol=0.01))

    def test_smooth_really_smooths(self):
        a = np.full((21, 21), 100.0, dtype="float32")
        a[10, 10] = 200.0
        out = be.smooth(a, 1.3)
        self.assertLess(out[10, 10], 130.0)
        self.assertGreater(out[10, 11], 100.0)


class ContourProps(unittest.TestCase):
    def test_contour_props(self):
        self.assertEqual(be.contour_props(1200), {"ele": 1200, "index": 1, "minzoom": 9})
        self.assertEqual(be.contour_props(1150), {"ele": 1150, "index": 0, "minzoom": 11})
        self.assertEqual(be.contour_props(1120), {"ele": 1120, "index": 0, "minzoom": 12})
        self.assertIsNone(be.contour_props(0))
        self.assertIsNone(be.contour_props(-20))


class Shade(unittest.TestCase):
    """The mapping was chosen by eye on real terrain. These pin its values."""

    def test_nodata_is_transparent(self):
        self.assertEqual(be.shade_rgba(np.array([[0]], dtype="uint8"))[3, 0, 0], 0)

    def test_shadow_is_dark_and_translucent(self):
        out = be.shade_rgba(np.array([[64]], dtype="uint8"))[:, 0, 0]
        v = 64 / 255.0
        self.assertEqual(out[:3].tolist(), [28, 38, 56])
        self.assertEqual(out[3], int(((0.72 - v) / 0.72) ** 1.2 * 150))

    def test_highlight_is_white_and_translucent(self):
        out = be.shade_rgba(np.array([[230]], dtype="uint8"))[:, 0, 0]
        v = 230 / 255.0
        self.assertEqual(out[:3].tolist(), [255, 255, 255])
        self.assertEqual(out[3], int((v - 0.72) / 0.28 * 90))

    def test_flat_ground_is_nearly_invisible(self):
        # gdaldem's flat value is 181 with this setup, just under the 0.72 turn.
        self.assertLess(be.shade_rgba(np.array([[181]], dtype="uint8"))[3, 0, 0], 2)


class Grid(unittest.TestCase):
    def test_grid_matches_norwegian_cell_centres(self):
        # Kartverket's cells have centres on multiples of 10 m in both axes.
        x, y = be.cell_centre(0, 0)
        self.assertEqual(x % 10, 0)
        self.assertEqual(y % 10, 0)

    def test_chunks_partition_the_area(self):
        area = be.Win(4990, 4990, 20, 20)
        chunks = be.chunks_of(area)
        self.assertEqual(sum(c.w * c.h for c in chunks), 400)
        self.assertEqual({(c.col, c.row) for c in chunks},
                         {(4990, 4990), (5000, 4990), (4990, 5000), (5000, 5000)})

    def test_bbox_window_covers_the_box_on_the_grid(self):
        win = be.bbox_window((7.95, 61.40, 8.85, 61.80))
        left, bottom, right, top = win.bounds()
        self.assertEqual((left - 5) % 10, 0)
        self.assertEqual((top - 5) % 10, 0)
        # The trial's Jotunheimen box, which rounded instead of snapping outward.
        self.assertLessEqual(left, 124015 + 10)
        self.assertGreaterEqual(right, 176115 - 10)

    def test_z13_pixels_fill_a_z8_tile_exactly(self):
        left, bottom, right, top = be.tile_bounds(8, 136, 60)
        self.assertAlmostEqual((right - left) / be.Z8_TILE_PX, be.mercator_res(13), places=6)
        self.assertEqual(be.Z8_TILE_PX, 8192)

    def test_z8_tiles_touching_bounds(self):
        tiles = be.z8_tiles_for_utm_bounds(120000, 6820000, 180000, 6880000)
        self.assertIn((133, 72), tiles)
        self.assertLess(len(tiles), 10)


class ResampleTile(unittest.TestCase):
    def ramp_tile(self, left, top, step, n):
        xs = left + step * (np.arange(n) + 0.5)
        a = np.tile(xs * 0.1, (n, 1)).astype("float32")
        return a, from_origin(left, top, step, step)

    def test_two_tiles_leave_no_gap_and_no_double_cover(self):
        grid = {}
        for left in (150000.0, 160000.0):
            a, tf = self.ramp_tile(left, 6850000.0, 8.0, 1250)
            win, out = be.resample_to_grid(a, tf, -9999.0)
            self.assertFalse((out == ND).any())
            for c in range(win.w):
                self.assertNotIn(win.col + c, grid)
                grid[win.col + c] = float(out[5, c])
        cols = sorted(grid)
        self.assertEqual(cols, list(range(cols[0], cols[-1] + 1)))
        # Bilinear in the interior, one-sided by at most 4 m on the seam column.
        for c in cols:
            x, _ = be.cell_centre(c, 0)
            self.assertLess(abs(grid[c] - x * 0.1), 0.41 + 0.01)

    def test_source_nodata_stays_nodata(self):
        a, tf = self.ramp_tile(150000.0, 6850000.0, 8.0, 125)
        a[:, :60] = -9999.0
        win, out = be.resample_to_grid(a, tf, -9999.0)
        self.assertTrue((out[:, :40] == ND).all())
        self.assertTrue((out[:, 60:] != ND).all())

    def test_tile_with_uneven_overview_cells(self):
        # 2500 m tiles give a 313 px overview, so cells of 7.987 m.
        a, _ = self.ramp_tile(152500.0, 6850000.0, 2500 / 313, 313)
        tf = from_origin(152500.0, 6850000.0, 2500 / 313, 2500 / 313)
        win, out = be.resample_to_grid(a, tf, -9999.0)
        self.assertEqual((win.w, win.h), (250, 250))
        self.assertFalse((out == ND).any())


class FetchSweden(unittest.TestCase):
    def item(self, i):
        left = 300000.0 + 10000 * i
        return {"id": f"t{i}", "href": f"https://example.invalid/{i}.tif",
                "bbox": [left, 6500000.0, left + 10000, 6510000.0]}

    @staticmethod
    def fake_read(item):
        left, _, _, top = item["bbox"]
        return np.full((1250, 1250), 123.0, dtype="float32"), from_origin(left, top, 8.0, 8.0), -9999.0

    def test_fetch_writes_cache_and_resumes_without_reading(self):
        with tempfile.TemporaryDirectory() as d:
            items = [self.item(i) for i in range(3)]
            stats = be.fetch_sweden(items, Path(d), self.fake_read, workers=2)
            self.assertEqual((stats.fetched, stats.failed), (3, 0))
            for it in items:
                with rasterio.open(be.se_tile_path(Path(d), it)) as ds:
                    self.assertEqual(ds.read(1)[0, 0], 123.0)
                    self.assertEqual(ds.crs.to_epsg(), 25833)

            def boom(_):
                raise AssertionError("cached tile read again")
            stats = be.fetch_sweden(items, Path(d), boom, workers=2)
            self.assertEqual((stats.fetched, stats.cached, stats.failed), (0, 3, 0))

    def test_failed_tile_leaves_no_file_and_is_counted(self):
        def flaky(item):
            if item["id"] == "t1":
                raise OSError("HTTP 503")
            return self.fake_read(item)
        with tempfile.TemporaryDirectory() as d:
            items = [self.item(i) for i in range(3)]
            stats = be.fetch_sweden(items, Path(d), flaky, workers=2)
            self.assertEqual((stats.fetched, stats.failed), (2, 1))
            self.assertFalse(be.se_tile_path(Path(d), items[1]).exists())
            self.assertEqual(sorted(os.listdir(d)), sorted(
                be.se_tile_path(Path(d), it).name for it in (items[0], items[2])))

    def test_stops_early_when_the_service_is_down(self):
        calls = []

        def down(item):
            calls.append(item["id"])
            raise OSError("HTTP 503")
        with tempfile.TemporaryDirectory() as d:
            items = [self.item(i) for i in range(200)]
            stats = be.fetch_sweden(items, Path(d), down, workers=2)
            self.assertTrue(stats.gave_up)
            self.assertLess(len(calls), 60)


def feature(i):
    return {"id": f"t{i}", "assets": {"data": {
        "href": f"https://example.invalid/{i}.tif", "proj:bbox": [0.0, 0.0, 10000.0, 10000.0]}}}


def next_link(token):
    return {"rel": "next", "body": {"token": token}}


class Catalog(unittest.TestCase):
    @staticmethod
    def poster(pages):
        seen = []

        def post(body):
            seen.append(body)
            return pages[len(seen) - 1]
        post.seen = seen
        return post

    def test_follows_every_page(self):
        post = self.poster([
            {"features": [feature(0), feature(1)], "links": [next_link("next:a")]},
            {"features": [feature(2)], "links": []},
        ])
        items = be.read_catalog(post)
        self.assertEqual([i["id"] for i in items], ["t0", "t1", "t2"])
        self.assertEqual(items[0]["bbox"], [0.0, 0.0, 10000.0, 10000.0])
        self.assertEqual(post.seen[1]["token"], "next:a")

    def test_full_page_without_next_link_fails(self):
        post = self.poster([{"features": [feature(0), feature(1)], "links": []}])
        with self.assertRaisesRegex(be.BuildError, "full"):
            be.read_catalog(post, limit=2)

    def test_empty_page_with_next_link_fails(self):
        post = self.poster([{"features": [], "links": [next_link("a")]}])
        with self.assertRaisesRegex(be.BuildError, "empty"):
            be.read_catalog(post)

    def test_answer_without_features_fails(self):
        post = self.poster([{"code": "ServerError"}])
        with self.assertRaisesRegex(be.BuildError, "feature list"):
            be.read_catalog(post)

    def test_repeated_tile_fails(self):
        post = self.poster([{"features": [feature(0)], "links": [next_link("a")]},
                            {"features": [feature(0)], "links": []}])
        with self.assertRaisesRegex(be.BuildError, "twice"):
            be.read_catalog(post)

    def test_total_that_does_not_match_fails(self):
        post = self.poster([{"features": [feature(0)], "links": [], "numberMatched": 2}])
        with self.assertRaisesRegex(be.BuildError, "matched"):
            be.read_catalog(post)

    def test_short_catalog_fails_the_known_count(self):
        items = [{"id": "a"}] * 5003
        with self.assertRaisesRegex(be.BuildError, "5003"):
            be.check_catalog(items, be.SE_KNOWN_TILES)
        be.check_catalog(items + [{"id": "b"}], be.SE_KNOWN_TILES)

    def test_truncated_catalog_on_disk_fails_and_stays_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "catalog.json").write_text(json.dumps([{"id": "a"}]))
            with self.assertRaises(be.BuildError):
                be.load_catalog(Path(d), lambda _: None, 2)


class Coverage(unittest.TestCase):
    WIN = be.Win(30000, 22000, 100, 100)

    def land(self, value=be.NORWAY):
        return np.full((100, 100), value, dtype="uint8")

    def test_full_cover(self):
        surface = np.full((100, 100), 10.0, dtype="float32")
        stats = be.count_coverage(surface, self.land(), np.zeros((100, 100), dtype=bool), self.WIN)
        self.assertEqual(stats["NO"], {"land": 10000, "covered": 10000, "hole": 0, "hole_at": None})
        self.assertEqual(stats["SE"]["land"], 0)

    def test_sea_is_not_land_and_nodata_on_it_is_no_hole(self):
        surface = np.full((100, 100), ND, dtype="float32")
        ocean = np.ones((100, 100), dtype=bool)
        stats = be.count_coverage(surface, self.land(), ocean, self.WIN)
        self.assertEqual(stats["NO"]["land"], 0)
        self.assertEqual(stats["NO"]["hole"], 0)

    def test_hole_is_found_with_size_and_place(self):
        surface = np.full((100, 100), 10.0, dtype="float32")
        surface[10:20, 30:50] = ND
        surface[80, 80] = ND
        stats = be.count_coverage(surface, self.land(), np.zeros((100, 100), dtype=bool), self.WIN)["NO"]
        self.assertEqual(stats["hole"], 200)
        self.assertEqual(stats["covered"], 10000 - 201)
        self.assertEqual(stats["hole_at"], list(be.cell_centre(30000 + 39, 22000 + 14)))

    def test_chunk_without_surface_counts_as_one_big_hole(self):
        stats = be.count_coverage(None, self.land(be.SWEDEN), np.zeros((100, 100), dtype=bool), self.WIN)["SE"]
        self.assertEqual((stats["covered"], stats["hole"]), (0, 10000))

    def test_missing_source_tile_fails_though_the_percentage_passes(self):
        # 100 km2 gone from a country: 0.02 %, so only the hole limit can catch it.
        land = 450000 * 10000
        part = {"SE": {"land": land, "covered": land - 1000000, "hole": 1000000, "hole_at": [500000.0, 6600000.0]},
                "NO": {"land": land, "covered": land, "hole": 0, "hole_at": None}}
        total = be.add_up_coverage([part])
        self.assertGreater(total["SE"]["percent"], be.COVERAGE_MIN_PERCENT)
        fails = be.coverage_failures(total, be.COVERAGE_MIN_PERCENT, be.HOLE_MAX_KM2)
        self.assertEqual(len(fails), 1)
        self.assertIn("hole of 100.0 km2", fails[0])

    def test_low_percentage_fails(self):
        land = 450000 * 10000
        part = {"SE": {"land": land, "covered": land // 2, "hole": 100, "hole_at": [500000.0, 6600000.0]}}
        fails = be.coverage_failures(be.add_up_coverage([part]), be.COVERAGE_MIN_PERCENT, be.HOLE_MAX_KM2)
        self.assertIn("50.0 %", fails[0])

    def test_sliver_of_a_country_in_a_small_box_passes(self):
        part = {"SE": {"land": 100, "covered": 50, "hole": 50, "hole_at": [500000.0, 6600000.0]}}
        self.assertEqual(be.coverage_failures(be.add_up_coverage([part]), 99.0, 25.0), [])


class Borders(unittest.TestCase):
    def test_committed_polygons_are_the_two_countries(self):
        doc = json.loads(be.BORDERS.read_text())
        self.assertEqual([f["properties"]["country"] for f in doc["features"]], ["NO", "SE"])

    def test_places_fall_in_the_right_country(self):
        places = {(10.75, 59.91): be.NORWAY, (8.31, 61.64): be.NORWAY, (18.07, 59.33): be.SWEDEN,
                  (13.0, 63.32): be.SWEDEN, (24.94, 60.17): be.OUTSIDE, (20.9, 69.0): be.OUTSIDE,
                  (12.57, 55.68): be.OUTSIDE,  # Copenhagen
                  (5.33, 60.39): be.NORWAY}
        for (lon, lat), want in places.items():
            win = be.bbox_window((lon - 0.0001, lat - 0.0001, lon + 0.0001, lat + 0.0001))
            c = be.countries_raster(str(be.BORDERS), win)
            self.assertEqual(c[c.shape[0] // 2, c.shape[1] // 2], want, (lon, lat))

    def test_sea_inside_the_territorial_line_belongs_to_the_country(self):
        # Off Bergen, so the coast and its skerries aren't cut.
        win = be.bbox_window((4.5, 60.4, 4.5001, 60.4001))
        self.assertTrue((be.countries_raster(str(be.BORDERS), win) == be.NORWAY).all())


class ShadeEmpty(unittest.TestCase):
    def test_map_tile_outside_the_surface_is_marked_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "z8.mbtiles"
            job = be.ShadeJob((0, 0), "unused.vrt", be.Win(30000, 22000, 10, 10), str(Path(tmp) / "t"), str(out))
            be.build_shade_tile(job)
            self.assertFalse(out.exists())
            self.assertTrue(out.with_suffix(".empty").exists())

    def test_sea_only_map_tile_is_marked_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            win = be.Win(30000, 22000, 100, 100)
            tif = Path(tmp) / "s.tif"
            be.write_float_tif(tif, np.full((100, 100), ND, dtype="float32"), win.transform())
            z8 = sorted(be.z8_tiles_for_utm_bounds(*win.bounds()))[0]
            out = Path(tmp) / "z8.mbtiles"
            be.build_shade_tile(be.ShadeJob(z8, str(tif), win, str(Path(tmp) / "t"), str(out)))
            self.assertFalse(out.exists())
            self.assertTrue(out.with_suffix(".empty").exists())


class EndToEnd(unittest.TestCase):
    """Real GDAL tools on a made-up hill that spans a chunk seam, with Sweden
    filling the eastern half from a pre-filled cache, so no network."""

    # A corner shared by four chunks, in EPSG:25833.
    CX, CY = be.cell_edge(30000, 22000)

    def hill(self, n, left, top):
        xs = left + 10.0 * (np.arange(n) + 0.5)
        ys = top - 10.0 * (np.arange(n) + 0.5)
        xx, yy = np.meshgrid(xs, ys)
        d2 = (xx - self.CX) ** 2 + (yy - self.CY) ** 2
        return (1000.0 * np.exp(-d2 / (2 * 800.0 ** 2)) + 5).astype("float32")

    def write_tif(self, path, a, left, top, nodata):
        with rasterio.open(path, "w", driver="GTiff", width=a.shape[1], height=a.shape[0], count=1,
                           dtype="float32", crs="EPSG:25833", transform=from_origin(left, top, 10, 10),
                           nodata=nodata) as ds:
            ds.write(a, 1)

    def write_polygons(self, tmp, left, top, size):
        """Everything west of the hill's centre is Norway, the rest Sweden, plus a
        rectangle of sea that the test's hill doesn't reach."""
        tr = Transformer.from_crs(be.UTM33, "EPSG:4326", always_xy=True)

        def box(l, b, r, t):
            ring = [tr.transform(x, y) for x, y in ((l, b), (r, b), (r, t), (l, t), (l, b))]
            return {"type": "Polygon", "coordinates": [[list(p) for p in ring]]}
        pad = 20000
        features = [
            {"type": "Feature", "properties": {"country": "NO"},
             "geometry": box(left - pad, top - size - pad, self.CX, top + pad)},
            {"type": "Feature", "properties": {"country": "SE"},
             "geometry": box(self.CX, top - size - pad, left + size + pad, top + pad)}]
        borders = tmp / "borders.geojson"
        borders.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        ocean_src = tmp / "ocean.geojson"
        ocean_src.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": box(left - pad, top + 5000, left - pad + 1000, top + 6000)}]}))
        ocean = tmp / "ocean.gpkg"
        subprocess.run(["ogr2ogr", "-f", "GPKG", "-t_srs", be.UTM33, "-nln", "ocean", "-nlt", "MULTIPOLYGON",
                        ocean, ocean_src], check=True, capture_output=True)
        return borders, ocean

    def run_cli(self, no_dir, out, se_cache, bbox, *extra):
        cmd = [sys.executable, str(HERE / "build_elevation.py"), "--no-dtm", str(no_dir), "--out", str(out),
               "--se-cache", str(se_cache), "--bbox", ",".join(f"{v:.6f}" for v in bbox), "--workers", "2",
               "--se-min-tiles", "1", "--borders", str(self.borders), "--ocean", str(self.ocean), *extra]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    def test_build_resume_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            n = 500
            left, top = self.CX - 2500, self.CY + 2500
            self.borders, self.ocean = self.write_polygons(tmp, left, top, 5000)
            no_dir = tmp / "no"
            no_dir.mkdir()
            full = self.hill(n, left, top)
            # Kartverket holds a raised hill all over, 40 m higher than the Swedish one,
            # so the output shows which source each side used.
            no = full + 40.0
            self.write_tif(no_dir / "a.tif", no, left, top, -32767.0)
            # Lantmäteriet holds the hill too.
            se_cache = tmp / "se"
            se_cache.mkdir()
            item = {"id": "s1", "href": "https://example.invalid/s1.tif",
                    "bbox": [left, top - 5000, left + 5000, top]}
            (se_cache / "catalog.json").write_text(json.dumps([item]))
            self.write_tif(be.se_tile_path(se_cache, item), full, left, top, be.NODATA)

            # Well inside the tif, because a box in degrees is wider once snapped to UTM.
            bbox = be.utm_bounds_to_wgs84(left + 800, top - 4200, left + 4200, top - 800)
            out = tmp / "out"
            r = self.run_cli(no_dir, out, se_cache, bbox)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("SUMMARY", r.stdout)
            self.assertTrue(r.stdout.strip().splitlines()[-1].startswith("SUMMARY"))

            con = sqlite3.connect(out / "contours.gpkg")
            cols = {c[1]: c[2] for c in con.execute('pragma table_info("contour")')}
            self.assertIn("ele", cols)
            self.assertIn("index", cols)
            self.assertTrue(cols["ele"].upper().endswith("INT") or "INT" in cols["ele"].upper())
            srs = con.execute("select srs_id from gpkg_geometry_columns where table_name='contour'").fetchone()[0]
            self.assertEqual(srs, 4326)
            rows = con.execute('select ele, "index", minzoom from contour').fetchall()
            self.assertGreater(len(rows), 40)
            for ele, index, minzoom in rows:
                self.assertEqual(ele % 20, 0)
                self.assertGreater(ele, 0)
                props = be.contour_props(ele)
                self.assertEqual((index, minzoom), (props["index"], props["minzoom"]))
            con.close()

            # The curves cross the seam between the four chunks as joined lines.
            inner = be.utm_bounds_to_wgs84(self.CX - 1200, self.CY - 1200, self.CX + 1200, self.CY + 1200)
            self.assertEqual(be.loose_line_ends(out / "contours.gpkg", inner), 0)

            con = sqlite3.connect(out / "hillshade.mbtiles")
            zooms = [z for (z,) in con.execute("select distinct zoom_level from tiles order by 1")]
            self.assertEqual(zooms, list(range(8, 14)))
            meta = dict(con.execute("select name, value from metadata"))
            self.assertEqual(meta["format"], "png")
            self.assertEqual(meta["minzoom"], "8")
            self.assertEqual(meta["maxzoom"], "13")
            uniq = con.execute("select count(*) from (select distinct zoom_level, tile_column, tile_row from tiles)").fetchone()[0]
            self.assertEqual(uniq, con.execute("select count(*) from tiles").fetchone()[0])
            data = con.execute("select tile_data from tiles where zoom_level=13 limit 1").fetchone()[0]
            con.close()
            self.assertEqual(data[:4], b"\x89PNG")

            # Each side used its own source, with the ramp between them.
            with rasterio.open(out / "work" / "surface.vrt") as ds:
                surface = ds.read(1, window=ds.window(self.CX - 1500, self.CY - 5, self.CX + 1500, self.CY + 5)
                                  .round_offsets().round_lengths())[0]
            hill_at = self.hill(n, left, top)[n // 2]
            first = int(((self.CX - 1500) - left) / 10)
            hill_at = hill_at[first:first + surface.size]
            diff = surface - hill_at
            self.assertLess(abs(diff[0] - 40.0), 1.0)  # Norway, Kartverket's raised hill
            self.assertLess(abs(diff[-1]), 1.0)  # Sweden, Lantmäteriet's
            middle = diff[surface.size // 2 - 1:surface.size // 2 + 1].mean()
            self.assertLess(abs(middle - 20.0), 5.0)  # the border, half and half

            coverage = json.loads((out / "coverage.json").read_text())
            self.assertEqual(coverage["NO"]["percent"], 100.0)
            self.assertEqual(coverage["SE"]["percent"], 100.0)
            self.assertIn("coverage_SE=100.0%", r.stdout)

            # A second run has nothing to do and leaves the outputs alone.
            before = (out / "contours.gpkg").stat().st_mtime_ns
            r = self.run_cli(no_dir, out, se_cache, bbox)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((out / "contours.gpkg").stat().st_mtime_ns, before)

            # With work/ deleted, the same area still has nothing to do.
            shutil.rmtree(out / "work")
            r = self.run_cli(no_dir, out, se_cache, bbox)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("nothing to do", r.stdout)
            self.assertEqual((out / "contours.gpkg").stat().st_mtime_ns, before)

            # Another area in the same folder is refused, not mixed in, with or without work/.
            other = be.utm_bounds_to_wgs84(left + 900, top - 4100, left + 4100, top - 900)
            r = self.run_cli(no_dir, out, se_cache, other)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--force", r.stdout + r.stderr)

            # --force rebuilds and keeps the Swedish cache.
            r = self.run_cli(no_dir, out, se_cache, other, "--force")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotEqual((out / "contours.gpkg").stat().st_mtime_ns, before)
            self.assertTrue(be.se_tile_path(se_cache, item).exists())

    def test_bad_cached_tile_exits_non_zero_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            left, top = self.CX - 500, self.CY + 500
            no_dir = tmp / "no"
            no_dir.mkdir()
            self.write_tif(no_dir / "a.tif", self.hill(100, left, top), left, top, -32767.0)
            self.borders, self.ocean = self.write_polygons(tmp, left, top, 1000)
            se_cache = tmp / "se"
            se_cache.mkdir()
            item = {"id": "s1", "href": "https://example.invalid/s1.tif",
                    "bbox": [left, top - 1000, left + 1000, top]}
            (se_cache / "catalog.json").write_text(json.dumps([item]))
            be.se_tile_path(se_cache, item).write_bytes(b"not a tiff")
            bbox = be.utm_bounds_to_wgs84(left + 100, top - 900, left + 900, top - 100)
            r = self.run_cli(no_dir, tmp / "out", se_cache, bbox)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("FAILED", r.stdout)
            self.assertIn("surface", r.stdout)
            self.assertNotIn("SUMMARY", r.stdout)

    def test_hole_in_the_land_fails_before_the_long_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            left, top = self.CX - 2500, self.CY + 2500
            self.borders, self.ocean = self.write_polygons(tmp, left, top, 5000)
            no_dir = tmp / "no"
            no_dir.mkdir()
            # A tile's worth of nothing in Norway, and nothing from Sweden to fill it.
            a = self.hill(500, left, top)
            a[100:400, 10:240] = 0.0
            self.write_tif(no_dir / "a.tif", a, left, top, -32767.0)
            se_cache = tmp / "se"
            se_cache.mkdir()
            (se_cache / "catalog.json").write_text("[]")
            bbox = be.utm_bounds_to_wgs84(left + 100, top - 4900, left + 4900, top - 100)
            r = self.run_cli(no_dir, tmp / "out", se_cache, bbox, "--se-min-tiles", "0", "--max-hole-km2", "1",
                             "--min-coverage", "1")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("coverage: NO: a hole of", r.stdout)
            self.assertFalse((tmp / "out" / "contours.gpkg").exists())

    def test_missing_input_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(HERE / "build_elevation.py"), "--no-dtm", tmp + "/nope", "--out", tmp + "/o"],
                capture_output=True, text=True, timeout=120)
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
