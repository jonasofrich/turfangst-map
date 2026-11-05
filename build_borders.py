#!/usr/bin/env python3
"""The two polygons build_elevation.py needs to split and check its surface.

    python3 infra/map/build_borders.py --pbf <osm extract> --water <water polygons zip> [--ocean-out <gpkg>]

National borders: OSM's country boundaries for mainland Norway and Sweden, written to
infra/map/data/national_borders.geojson, which is committed. They include sea out to
the territorial line, so coasts aren't cut, and they share their land border way for
way. The extract must hold both relations whole, which a merged extract of the two
countries and their neighbours does.

Ocean: osmdata.openstreetmap.de's water polygons cut to the two countries, in
EPSG:25833, for the land check. It's about 270 MB, so it isn't committed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pyproj import Geod
from shapely.geometry import shape

HERE = Path(__file__).resolve().parent
BORDERS = HERE / "data" / "national_borders.geojson"
OCEAN = Path("~/map-build/ocean_no_se.gpkg")
# Norway's admin_level=2 relation, 2978650, also holds Svalbard and Jan Mayen, which
# no mainland extract holds. 1059668 is its mainland member, with the same land border.
RELATIONS = {"NO": 1059668, "SE": 52822}
# Land and territorial sea, km2, both from OSM on 2026-11-03. A polygon far from
# these has lost a part or gained an overseas one.
EXPECTED_KM2 = {"NO": 469251, "SE": 532008}
OCEAN_SPAT = ("3", "54", "33", "73")


def round_coords(coords, digits: int = 7):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], digits), round(coords[1], digits)]
    return [round_coords(c, digits) for c in coords]


def area_km2(geometry: dict) -> float:
    return abs(Geod(ellps="GRS80").geometry_area_perimeter(shape(geometry))[0]) / 1e6


def check(features: dict[str, dict]) -> None:
    """Raises when the polygons aren't the two countries, or don't meet cleanly."""
    for code, feature in features.items():
        km2 = area_km2(feature["geometry"])
        if abs(km2 - EXPECTED_KM2[code]) / EXPECTED_KM2[code] > 0.01:
            raise SystemExit(f"{code}: {km2:.0f} km2, expected about {EXPECTED_KM2[code]}")
        if not shape(feature["geometry"]).is_valid:
            raise SystemExit(f"{code}: invalid polygon")
    a, b = (shape(features[c]["geometry"]) for c in ("NO", "SE"))
    overlap = abs(Geod(ellps="GRS80").geometry_area_perimeter(a.intersection(b))[0]) / 1e6
    if overlap > 0.01:
        raise SystemExit(f"Norway and Sweden overlap by {overlap:.3f} km2")
    if a.touches(b) is False:
        raise SystemExit("Norway and Sweden don't share a border")


def build_borders(pbf: Path) -> None:
    with tempfile.TemporaryDirectory(dir=pbf.parent) as tmp:
        rel = Path(tmp) / "rel.osm.pbf"
        seq = Path(tmp) / "rel.geojsonseq"
        ids = [f"r{i}" for i in RELATIONS.values()]
        # getid exits 1 when a member isn't in the extract, such as a node far away.
        # Whether the polygons came out whole is decided by the checks below.
        subprocess.run(["osmium", "getid", "-O", "-r", "-o", rel, pbf, *ids], capture_output=True)
        subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", "--geometry-types=polygon",
                        "-a", "type,id", "-o", seq, rel], check=True, capture_output=True)
        features = {}
        with seq.open() as fh:
            for line in fh:
                f = json.loads(line.strip("\x1e\n "))
                props = f["properties"]
                for code, rid in RELATIONS.items():
                    if props.get("@type") == "relation" and props.get("@id") == rid:
                        features[code] = {
                            "type": "Feature",
                            "properties": {"country": code, "name": props.get("name"), "osm_relation": rid},
                            "geometry": {"type": f["geometry"]["type"],
                                         "coordinates": round_coords(f["geometry"]["coordinates"])},
                        }
    missing = set(RELATIONS) - set(features)
    if missing:
        raise SystemExit(f"{pbf}: no whole polygon for {sorted(missing)}. Use a merged extract.")
    check(features)
    doc = {"type": "FeatureCollection",
           "attribution": "OpenStreetMap contributors, ODbL",
           "features": [features["NO"], features["SE"]]}
    BORDERS.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"{BORDERS}: {BORDERS.stat().st_size / 1e6:.2f} MB")


def build_ocean(water_zip: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.gpkg")
    tmp.unlink(missing_ok=True)
    src = f"/vsizip/{water_zip}/water-polygons-split-3857/water_polygons.shp"
    subprocess.run(["ogr2ogr", "-f", "GPKG", "-t_srs", "EPSG:25833", "-spat", *OCEAN_SPAT, "-spat_srs", "EPSG:4326",
                    "-nln", "ocean", "-nlt", "MULTIPOLYGON", "-gt", "65536", tmp, src], check=True)
    tmp.replace(out)
    print(f"{out}: {out.stat().st_size / 1e6:.0f} MB")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pbf", type=Path, help="OSM extract holding both countries whole")
    parser.add_argument("--water", type=Path, help="water-polygons-split-3857.zip")
    parser.add_argument("--ocean-out", type=Path, default=OCEAN)
    args = parser.parse_args(argv)
    if not args.pbf and not args.water:
        parser.error("give --pbf, --water or both")
    if args.pbf:
        build_borders(args.pbf.expanduser().resolve())
    if args.water:
        build_ocean(args.water.expanduser().resolve(), args.ocean_out.expanduser())
    return 0


if __name__ == "__main__":
    sys.exit(main())
