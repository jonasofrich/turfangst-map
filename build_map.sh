#!/usr/bin/env bash
# Builds the outdoor map's tiles on this Mac, from fresh sources, into
#   <root>/out/outdoor-YYYYMMDD.mbtiles, dated by the OSM extracts.
# Touches no server. A failed check leaves any earlier file alone.
#
#   infra/map/build_map.sh [--reuse-sources] [--contours FILE] [--root DIR] [--heap-gb N]
#
# Last line: SUMMARY ok ... on success, FAILED ... with exit 1 otherwise.
set -Eeuo pipefail

PLANETILER_VERSION=0.10.2
PLANETILER_URL="https://github.com/onthegomap/planetiler/releases/download/v${PLANETILER_VERSION}/planetiler.jar"
# GitHub's published digest for that release asset.
PLANETILER_SHA256=f310bd0413e2e4512b27f4046d418664e8e1d3bf31603c2a70e23de06c167e4d

GEOFABRIK=https://download.geofabrik.de/europe
WATER_FILE=water-polygons-split-3857.zip
WATER_URL="https://osmdata.openstreetmap.de/download/$WATER_FILE"
# FAU keeps dated copies, listed by day.
WATER_MIRROR=https://ftp.fau.de/mirrors/openstreetmapdata.com-archive
# Coastlines change slowly and both hosts can be away for a while, but not this long.
WATER_MAX_AGE_DAYS=90

# Natural Earth 5.1.2 as a GeoPackage, which Planetiler's custom schema can read and
# its SQLite download isn't. Public domain. Unchanged on the server since 2022.
NE_FILE=natural_earth_vector.gpkg.zip
NE_URL="https://naciscdn.org/naturalearth/packages/$NE_FILE"
NE_URL2="https://naturalearth.s3.amazonaws.com/packages/$NE_FILE"
NE_SHA256=b9e2e7b3d5c2c59593f6eafc19be2ba87fb04788a90c57ccd6cf32a587b6ecc3
NE_LAYERS=(ne_10m_lakes ne_10m_glaciated_areas ne_10m_antarctic_ice_shelves_polys ne_10m_admin_0_boundary_lines_land)

# The app's camera box. From zoom 7 up nothing is built outside it.
HIGH_BOUNDS=-11.4,52,40,81.1
SEAM_LOW=6
SEAM_HIGH=7
MAX_ZOOM=14

REUSE=0
ROOT="$HOME/map-build"
CONTOURS=""
HEAP_GB=10
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="$HERE/outdoor.yml"
LOW_LABELS="$HERE/data/low_labels.osm.pbf"

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "  --reuse-sources  use the OSM extracts and water polygons already in <root>/sources, download nothing"
  echo "  --contours FILE  curves to merge, default <root>/elevation/contours.gpkg"
  echo "  --root DIR       build folder, default ~/map-build"
  echo "  --heap-gb N      Java heap per Planetiler run, default 10"
}

die() {
  echo "FAILED $*"
  exit 1
}
trap 'echo "FAILED at line $LINENO of build_map.sh"' ERR

START=$SECONDS
say() { printf '[%5ss] %s\n' "$((SECONDS - START))" "$*"; }

sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }

# Downloads $1 to $2 only if the server has something newer. Returns non-zero if the
# server can't be reached. The file only moves into place if $3 (a check command) passes.
fetch_newer() {
  local url="$1" dest="$2" check="${3:-true}" part="$2.part"
  rm -f "$part"
  local args=(-fsSL -R --connect-timeout 20 --retry 2 --speed-limit 50000 --speed-time 120 -o "$part")
  [ -f "$dest" ] && args+=(-z "$dest")
  curl "${args[@]}" "$url" || { rm -f "$part"; return 1; }
  [ -s "$part" ] || { rm -f "$part"; return 0; }
  "$check" "$part" || { rm -f "$part"; return 1; }
  mv "$part" "$dest"
}

get_planetiler() {
  JAR="$BIN/planetiler-$PLANETILER_VERSION.jar"
  if [ ! -f "$JAR" ]; then
    say "downloading Planetiler $PLANETILER_VERSION"
    curl -fsSL --connect-timeout 20 --retry 2 -o "$JAR.part" "$PLANETILER_URL" || die "can't download Planetiler"
    mv "$JAR.part" "$JAR"
  fi
  [ "$(sha256_of "$JAR")" = "$PLANETILER_SHA256" ] || die "$JAR doesn't match the published sha256"
}

get_natural_earth() {
  local zip="$SOURCES/$NE_FILE"
  NE_LOW="$SOURCES/natural_earth_low.gpkg"
  if [ ! -f "$zip" ]; then
    say "downloading Natural Earth"
    curl -fsSL --connect-timeout 20 --retry 2 -o "$zip.part" "$NE_URL" \
      || curl -fsSL --connect-timeout 20 --retry 2 -o "$zip.part" "$NE_URL2" \
      || die "can't download Natural Earth"
    mv "$zip.part" "$zip"
  fi
  [ "$(sha256_of "$zip")" = "$NE_SHA256" ] \
    || die "$zip doesn't match the recorded sha256. If Natural Earth released a new version, check it and update NE_SHA256"
  if [ ! -f "$NE_LOW" ] || [ "$zip" -nt "$NE_LOW" ]; then
    # Only the layers the schema reads, so neither run unpacks 850 MB for nothing.
    rm -f "$NE_LOW.tmp.gpkg"
    ogr2ogr -f GPKG -nlt PROMOTE_TO_MULTI "$NE_LOW.tmp.gpkg" \
      "/vsizip/$zip/packages/natural_earth_vector.gpkg" "${NE_LAYERS[@]}"
    mv "$NE_LOW.tmp.gpkg" "$NE_LOW"
  fi
}

zip_is_good() { unzip -tq "$1" >/dev/null 2>&1; }

get_water() {
  WATER="$SOURCES/$WATER_FILE"
  if [ "$REUSE" = 1 ]; then
    [ -f "$WATER" ] || die "--reuse-sources, but no $WATER"
    say "water polygons: reusing the kept copy of $(date -r "$WATER" +%F)"
    return
  fi
  if fetch_newer "$WATER_URL" "$WATER" zip_is_good; then
    say "water polygons: osmdata.openstreetmap.de, copy of $(date -r "$WATER" +%F)"
    check_water_age
    return
  fi
  # The newest folder doesn't always hold this file yet, so look through the last few.
  local folder
  for folder in $(curl -fsSL --connect-timeout 20 "$WATER_MIRROR/" | grep -oE 'href="[0-9]{4}-[0-9]{2}-[0-9]{2}/"' | cut -d'"' -f2 | sort -r | head -5 || true); do
    if fetch_newer "$WATER_MIRROR/$folder$WATER_FILE" "$WATER" zip_is_good; then
      say "water polygons: FAU mirror $folder, using the copy of $(date -r "$WATER" +%F)"
      check_water_age
      return
    fi
  done
  [ -f "$WATER" ] || die "no water polygons: both servers failed and there is no kept copy"
  zip_is_good "$WATER" || die "both servers failed and the kept $WATER is corrupt"
  say "water polygons: both servers failed, using the kept copy of $(date -r "$WATER" +%F)"
  check_water_age
}

check_water_age() {
  local days=$(( ($(date +%s) - $(date -r "$WATER" +%s)) / 86400 ))
  [ "$days" -le "$WATER_MAX_AGE_DAYS" ] || die "the water polygons are $days days old, the limit is $WATER_MAX_AGE_DAYS. \
Wait until osmdata.openstreetmap.de or the FAU mirror is back and run again, or download $WATER_FILE by hand into $SOURCES"
}

get_extract() {
  local name="$1" dest="$SOURCES/$1-latest.osm.pbf"
  if [ "$REUSE" = 1 ]; then
    [ -f "$dest" ] || die "--reuse-sources, but no $dest"
    return
  fi
  say "fetching $name from Geofabrik"
  EXPECTED_MD5="$(curl -fsSL --connect-timeout 20 "$GEOFABRIK/$name-latest.osm.pbf.md5" | cut -d' ' -f1)" || die "can't read $name's checksum"
  [ -n "$EXPECTED_MD5" ] || die "Geofabrik's checksum for $name is empty"
  # A bad download never replaces the kept file. Geofabrik may also have published a new
  # file between the two requests, and running again sorts that out.
  fetch_newer "$GEOFABRIK/$name-latest.osm.pbf" "$dest" md5_matches \
    || die "can't download $name, or it doesn't match Geofabrik's md5. Run again"
  # With nothing newer on the server, the kept file was never checked above. Left in place
  # it would answer "not modified" and fail here on every run, so it goes.
  if ! md5_matches "$dest"; then
    rm -f "$dest"
    die "$dest doesn't match Geofabrik's md5 and has been deleted. Run again to download it"
  fi
}

md5_matches() { [ "$(md5 -q "$1")" = "$EXPECTED_MD5" ]; }

replication_time() {
  osmium fileinfo -g header.option.osmosis_replication_timestamp "$1"
}

# Reads the date before merging, because merging drops it from the header.
read_osm_date() {
  local no se
  no="$(replication_time "$SOURCES/norway-latest.osm.pbf")"
  se="$(replication_time "$SOURCES/sweden-latest.osm.pbf")"
  [ -n "$no" ] && [ -n "$se" ] || die "an extract has no replication date: Norway '$no', Sweden '$se'"
  [ "${no:0:10}" = "${se:0:10}" ] || die "extracts differ in replication day: Norway $no, Sweden $se. Wait for Geofabrik and run again"
  [ "$no" = "$se" ] || say "note: same day, but not the same second: Norway $no, Sweden $se"
  OSM_DATE="${no:0:10}"
  say "OSM date $OSM_DATE"
}

merge_extracts() {
  OSM_HIGH="$WORK/norway-sweden.osm.pbf"
  OSM_LOW="$WORK/norway-sweden-labels.osm.pbf"
  say "merging extracts"
  osmium merge --overwrite -o "$OSM_HIGH" "$SOURCES/norway-latest.osm.pbf" "$SOURCES/sweden-latest.osm.pbf"
  # Planetiler reads tags on ways only, and many ski ways carry theirs on a route relation.
  python3 "$HERE/tag_ski_routes.py" "$OSM_HIGH" "$WORK/norway-sweden-ski.osm.pbf" || die "tagging ski ways"
  mv "$WORK/norway-sweden-ski.osm.pbf" "$OSM_HIGH"
  # The frozen labels are older than the extracts. Osmium keeps both versions of an
  # object it finds twice, so what the extracts already hold leaves the label file first.
  # getid exits 1 when an id isn't found, as most aren't, and also on a data error. A bad
  # output is caught by removeid reading it on the next line.
  local rc=0
  osmium getid --overwrite -I "$LOW_LABELS" -o "$WORK/labels-in-extracts.osm.pbf" "$OSM_HIGH" 2>/dev/null || rc=$?
  [ "$rc" -le 1 ] || die "osmium getid failed with $rc"
  osmium removeid --overwrite -I "$WORK/labels-in-extracts.osm.pbf" -o "$WORK/labels-outside.osm.pbf" "$LOW_LABELS"
  python3 "$HERE/tag_outside_labels.py" "$WORK/labels-outside.osm.pbf" "$WORK/labels-outside-tagged.osm.pbf" || die "tagging outside labels"
  osmium merge --overwrite -o "$OSM_LOW" "$OSM_HIGH" "$WORK/labels-outside-tagged.osm.pbf"
}

PEAK_RSS=0
# planetiler NAME OSM CONTOURS MIN MAX BOUNDS
planetiler() {
  local name="$1" osm="$2" contours="$3" min="$4" max="$5" bounds="$6"
  local log="$WORK/planetiler-$name.log" begin=$SECONDS
  say "Planetiler $name: zoom $min to $max, bounds $bounds"
  # Its own working directory: Planetiler writes data/ wherever it stands.
  mkdir -p "$WORK/run-$name"
  if ! (cd "$WORK/run-$name" && /usr/bin/time -l java "-Xmx${HEAP_GB}g" -jar "$JAR" generate-custom \
    --schema="$SCHEMA" \
    --osm_path="$osm" \
    --ocean_path="$WATER" \
    --natural_earth_path="$NE_LOW" \
    --contours_path="$contours" \
    --minzoom="$min" --maxzoom="$max" --bounds="$bounds" \
    --output="$WORK/$name.mbtiles" --force --tmpdir="$WORK/run-$name/tmp" >"$log" 2>&1); then
    tail -20 "$log"
    die "Planetiler $name failed, see $log"
  fi
  local rss
  rss="$(awk '/maximum resident set size/ {print $1}' "$log")"
  [ "${rss:-0}" -gt "$PEAK_RSS" ] && PEAK_RSS="$rss"
  say "Planetiler $name done in $((SECONDS - begin))s, peak resident $((rss / 1024 / 1024)) MB, $(du -h "$WORK/$name.mbtiles" | cut -f1)"
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --reuse-sources) REUSE=1 ;;
      --contours) CONTOURS="$2"; shift ;;
      --root) ROOT="$2"; shift ;;
      --heap-gb) HEAP_GB="$2"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
    shift
  done

  SOURCES="$ROOT/sources"
  WORK="$ROOT/work"
  OUT="$ROOT/out"
  BIN="$ROOT/bin"
  CONTOURS="${CONTOURS:-$ROOT/elevation/contours.gpkg}"
  mkdir -p "$SOURCES" "$WORK" "$OUT" "$BIN"

  for tool in java osmium ogr2ogr ogrinfo curl unzip shasum python3 md5; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
  done
  python3 "$HERE/check_curves.py" "$CONTOURS" || die "the curves at $CONTOURS can't be used, see the line above"
  [ -f "$LOW_LABELS" ] || die "no label file at $LOW_LABELS"

  command -v caffeinate >/dev/null && caffeinate -i -w $$ &

  get_planetiler
  get_natural_earth
  get_water
  get_extract norway
  get_extract sweden
  read_osm_date
  merge_extracts

  # The low run has no curve below zoom 9 to draw, so it gets an empty file with the same
  # layout instead of spending minutes reading all of them.
  ogrinfo -so "$CONTOURS" contour >/dev/null || die "$CONTOURS has no contour layer"
  NO_CURVES="$WORK/no_curves.gpkg"
  rm -f "$NO_CURVES"
  ogr2ogr -f GPKG -where "1 = 0" "$NO_CURVES" "$CONTOURS" contour

  planetiler low "$OSM_LOW" "$NO_CURVES" 0 "$SEAM_LOW" world
  planetiler high "$OSM_HIGH" "$CONTOURS" "$SEAM_HIGH" "$MAX_ZOOM" "$HIGH_BOUNDS"

  say "merging zoom 0 to $SEAM_LOW into zoom $SEAM_HIGH to $MAX_ZOOM"
  python3 "$HERE/merge_tiles.py" "$WORK/low.mbtiles" "$WORK/high.mbtiles" --osm-date "$OSM_DATE" || die "merging tiles"

  FINAL="$OUT/outdoor-${OSM_DATE//-/}.mbtiles"
  PREVIOUS="$(find "$OUT" -maxdepth 1 -name 'outdoor-*.mbtiles' | sort | tail -1)"
  say "checking, previous build: ${PREVIOUS:-none}"
  if ! python3 "$HERE/check_tiles.py" "$WORK/high.mbtiles" --stats ${PREVIOUS:+--previous "$PREVIOUS"}; then
    die "check_tiles.py rejected the build, it stays at $WORK/high.mbtiles. $FINAL wasn't touched"
  fi
  mv "$WORK/high.mbtiles" "$FINAL"
  rm -f "$WORK/low.mbtiles" "$OSM_HIGH" "$OSM_LOW" "$WORK/labels-in-extracts.osm.pbf" "$WORK/labels-outside.osm.pbf" "$WORK/labels-outside-tagged.osm.pbf" "$NO_CURVES"

  echo "SUMMARY ok wall=$((SECONDS - START))s peak_rss=$((PEAK_RSS / 1024 / 1024))MB heap=${HEAP_GB}g size=$(du -h "$FINAL" | cut -f1) osm_date=$OSM_DATE file=$FINAL"
}

# Sourced by test_build_map.py, which calls the functions above one at a time.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
