"""Build pypsa-de/data/busshapes/simplified_12_osm.geojson for custom_busshapes.

12 shapes: N01..N11 (Lüneburg) + DE-rest (Germany minus the 11).
Filename encodes n_clusters=12 and electricity.base_network=osm.

Run from pypsa-de/:
    pixi run python data-lueneburg/nodes/build_busshapes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
from shapely.ops import unary_union
from shapely.validation import make_valid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PYPSA = ROOT  # pypsa-de/

NODES = HERE / "lueneburg_11_nodes.geojson"
COUNTRY = (
    PYPSA / "resources/test-sector-myopic-dach/KN2045_Mix/country_shapes.geojson"
)
OUT = PYPSA / "data/busshapes/simplified_12_osm.geojson"

CRS = "EPSG:4326"
AREA_CRS = "EPSG:3035"
NODE_NAMES = [f"N{i:02d}" for i in range(1, 12)]
REST_NAME = "DE-rest"


def area_m2(geom, crs=CRS) -> float:
    return float(gpd.GeoSeries([geom], crs=crs).to_crs(AREA_CRS).area.iloc[0])


def main() -> int:
    nodes = gpd.read_file(NODES).to_crs(CRS)
    if sorted(nodes["name"].tolist()) != NODE_NAMES:
        raise RuntimeError(f"expected {NODE_NAMES}, got {sorted(nodes['name'])}")

    countries = gpd.read_file(COUNTRY).to_crs(CRS)
    de = countries.loc[countries["name"] == "DE", "geometry"]
    if len(de) != 1:
        raise RuntimeError(f"expected one DE polygon, got {len(de)}")
    de_geom = make_valid(de.iloc[0])

    lg_union = make_valid(unary_union(nodes.geometry.map(make_valid).tolist()))
    rest_geom = make_valid(de_geom.difference(lg_union))
    if rest_geom.is_empty:
        raise RuntimeError("DE-rest is empty — check CRS / node coverage")

    # Rel. area of the 11 nodes vs DE935 is already checked in build_lueneburg_nodes.
    # Here: N01..N11 ∪ DE-rest must cover DE (tiny sliver loss from difference is OK).
    covered = make_valid(unary_union([lg_union, rest_geom]))
    de_a = area_m2(de_geom)
    cov_a = area_m2(covered)
    rel = abs(cov_a - de_a) / de_a
    print(f"coverage vs DE: rel. diff = {rel:.4%} (limit 0.5%)")
    if rel > 0.005:
        raise RuntimeError("N01..N11 ∪ DE-rest does not cover DE within 0.5%")

    # No overlap between any Lüneburg node and DE-rest.
    rest_3035 = gpd.GeoSeries([rest_geom], crs=CRS).to_crs(AREA_CRS).iloc[0]
    nodes_3035 = nodes.to_crs(AREA_CRS)
    overlap = sum(
        nodes_3035.geometry.iloc[i].intersection(rest_3035).area
        for i in range(len(nodes_3035))
    )
    print(f"overlap N01..N11 ∩ DE-rest (m²) = {overlap:.1f}")
    if overlap > 1e4:
        raise RuntimeError("non-trivial overlap between Lüneburg nodes and DE-rest")

    lg = nodes[["name", "geometry"]].copy()
    lg["country"] = "DE"
    rest = gpd.GeoDataFrame(
        {"name": [REST_NAME], "country": ["DE"], "geometry": [rest_geom]},
        crs=CRS,
    )
    out = gpd.GeoDataFrame(pd_concat_gdfs(lg, rest), crs=CRS)

    expected = NODE_NAMES + [REST_NAME]
    if out["name"].tolist() != expected:
        # keep stable order: N01..N11 then DE-rest
        out = out.set_index("name").loc[expected].reset_index()

    if len(out) != 12:
        raise RuntimeError(f"expected 12 shapes, got {len(out)}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out[["name", "country", "geometry"]].to_file(OUT, driver="GeoJSON")
    print(f"wrote {OUT}")
    print("names:", out["name"].tolist())
    return 0


def pd_concat_gdfs(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    import pandas as pd

    return gpd.GeoDataFrame(pd.concat([a, b], ignore_index=True), crs=a.crs)


if __name__ == "__main__":
    sys.exit(main())
