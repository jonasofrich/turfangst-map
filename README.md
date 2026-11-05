# turfangst-map

How the vector tiles behind the map in Turfangst are made from OpenStreetMap: Norway and Sweden in detail, the world at low zoom, and elevation curves and hill shading from the two national mapping agencies. It is published because the Open Database License asks for the method of making a derivative database to be available, free of charge.

`outdoor.yml` is the source of truth: a [Planetiler](https://github.com/onthegomap/planetiler) custom schema that says which tags go into which layer at which zoom. The rest makes its inputs and checks its output.

## Sources

| Source | Used for | Licence |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright), Geofabrik's Norway and Sweden extracts | Everything from zoom 7, and labels below it | ODbL |
| OpenStreetMap water polygons ([osmdata.openstreetmap.de](https://osmdata.openstreetmap.de/)) | Sea | ODbL |
| [Natural Earth](https://www.naturalearthdata.com/) | The world at low zoom, and shaded relief below zoom 8 | Public domain |
| [Kartverket](https://www.kartverket.no) DTM10 | Curves and shading in Norway | CC BY 4.0 |
| [Lantmäteriet](https://www.lantmateriet.se) Markhöjdmodell | Curves and shading in Sweden | CC BY 4.0 |

`data/` holds two small OSM extracts, see `data/LICENSE.md`. `tag_outside_labels.py` and `build_borders.py` make them.

## Build

Needs Java 21 or newer, osmium, GDAL's command line tools and Python 3 with rasterio, numpy, scipy, pyproj, shapely and Pillow. Swedish elevation needs a `~/.netrc` login for `dl1.lantmateriet.se`. Everything goes in `~/map-build`, and `--root` on `build_map.sh` changes that.

```bash
# The borders, once. They are already in data/, so only when a border moves
python3 build_borders.py --pbf <extract with both countries> --water <water-polygons-split-3857.zip>

# Elevation, only when the elevation data changes
python3 build_elevation.py --no-dtm <DTM10 folder> --out ~/map-build/elevation

# Shading as WebP
python3 reencode_shading.py

# The tiles, about 4 minutes on a recent laptop
./build_map.sh
```

The result is `~/map-build/out/outdoor-YYYYMMDD.mbtiles`, dated by the OSM extracts, and `~/map-build/elevation/hillshade-webp.mbtiles`. `check_tiles.py` compares a new build with the one before, and `build_map.sh` won't finish on one that fails.

## Layers

`ocean, water, water_label, waterway, landcover, landuse, protected, road, path, rail, ferry, aerialway, building, boundary, place, peak, outdoor, contour`

## Tests

```bash
for t in test_*.py; do python3 "$t"; done
```

They need no network, apart from a server on 127.0.0.1.

## Attribution

Tiles built this way must credit © OpenStreetMap contributors, © Kartverket and © Lantmäteriet. The tile database itself is under the ODbL.

## Licence

The scripts are under the MIT licence, see `LICENSE`. The data in `data/` and the tile database are under the ODbL.
