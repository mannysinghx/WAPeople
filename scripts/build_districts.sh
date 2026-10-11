#!/bin/sh
# Rebuild app/wapeople/data/wa_legislative_districts_2024.geojson from the official source.
# Run this if Washington's legislative map changes (e.g. after a new court order or redistricting),
# then update the plan name and the .meta.json next to the output.
#
# Requires: curl, node/npx (mapshaper, open source), python3.
set -eu
cd "$(dirname "$0")/.."
work=$(mktemp -d)
src="https://services.arcgis.com/bCYnGqM4FMTBSjd1/ArcGIS/rest/services/Washington_State_Legislative_Districts_2024/FeatureServer/0/query?where=1%3D1&outFields=District&outSR=4326&f=geojson"
curl -fsS -A "WAPeople district builder" "$src" -o "$work/official.geojson"

npx -y mapshaper@0.6.102 -i "$work/official.geojson" -filter-fields District \
  -simplify dp interval=5 keep-shapes -o precision=0.000001 "$work/simplified.geojson"
npx -y mapshaper@0.6.102 -i "$work/simplified.geojson" -points inner -o "$work/labels.geojson"

python3 - "$work" app/wapeople/data/wa_legislative_districts_2024.geojson <<'EOF'
import json, sys
work, out = sys.argv[1], sys.argv[2]
polys = json.load(open(f"{work}/simplified.geojson"))["features"]
labels = {f["properties"]["District"]: f["geometry"]["coordinates"]
          for f in json.load(open(f"{work}/labels.geojson"))["features"]}
assert sorted(f["properties"]["District"] for f in polys) == list(range(1, 50)), "expected districts 1-49"
feats = [{"type": "Feature",
          "properties": {"district": f["properties"]["District"],
                         "label_lon": round(labels[f["properties"]["District"]][0], 6),
                         "label_lat": round(labels[f["properties"]["District"]][1], 6)},
          "geometry": f["geometry"]}
         for f in sorted(polys, key=lambda f: f["properties"]["District"])]
json.dump({"type": "FeatureCollection", "features": feats}, open(out, "w"), separators=(",", ":"))
print(f"wrote {out}: {len(feats)} districts")
EOF
rm -rf "$work"
