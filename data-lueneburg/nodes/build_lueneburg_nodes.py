"""Build Data/nodes/lueneburg_11_nodes.geojson from GISCO LAU 2019.

Run from the V2.0_PyPsa root (or any cwd; paths below are absolute to this file):

    cd Data/nodes
    ../../pypsa-de/.pixi/envs/default/bin/python build_lueneburg_nodes.py

Or, from pypsa-de/:

    pixi run python ../Data/nodes/build_lueneburg_nodes.py
"""

from __future__ import annotations

import re
import sys
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.validation import make_valid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # V2.0_PyPsa
PYPSA = ROOT / "pypsa-de"
DIETER = Path("/Users/felixmergenhagen/Desktop/Work/Waermeziel/DIETER_WÄRME")

LAU_ZIP = PYPSA / "data/lau_regions/archive/2019/lau_regions.zip"
LAU_URL = (
    "https://data.pypsa.org/workflows/eur/lau_regions/2019/ref-lau-2019-01m.geojson.zip"
)
LAU_MEMBER = "LAU_RG_01M_2019_3035.geojson"

MAPPING_CSV = DIETER / "V1/nodes/gemeinde_to_sg_mapping.csv"
NODES_CSV = HERE / "nodes.csv"
NUTS3_GEOJSON = (
    PYPSA / "resources/test-sector-myopic-dach/KN2045_Mix/nuts3_shapes.geojson"
)

OUT_GEOJSON = HERE / "lueneburg_11_nodes.geojson"
OUT_AUDIT = HERE / "lueneburg_lau_to_node.csv"

AGS_PREFIX = "03355"
TARGET_CRS = "EPSG:4326"


def normalize_name(s: str) -> str:
    """Lowercase, strip diacritics, drop LAU suffixes / parentheticals for matching."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # drop common German admin suffixes after a comma
    s = re.split(r",\s*", s, maxsplit=1)[0]
    # drop parentheticals: "(Luhe)", "(Elbe)"
    s = re.sub(r"\s*\([^)]*\)", "", s)
    # collapse whitespace / hyphens
    s = re.sub(r"[\s\-]+", "", s)
    return s


def ensure_lau_zip() -> Path:
    if LAU_ZIP.exists() and LAU_ZIP.stat().st_size > 1_000_000:
        return LAU_ZIP
    LAU_ZIP.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading LAU zip → {LAU_ZIP}")
    urllib.request.urlretrieve(LAU_URL, LAU_ZIP)
    # sanity: must contain the geojson member
    with zipfile.ZipFile(LAU_ZIP) as zf:
        if LAU_MEMBER not in zf.namelist():
            raise RuntimeError(f"{LAU_MEMBER} missing from {LAU_ZIP}: {zf.namelist()[:5]}")
    return LAU_ZIP


def load_mapping() -> pd.DataFrame:
    m = pd.read_csv(MAPPING_CSV)
    # DIETER splits Hansestadt into Zentrum/Stadtrand; both → N01. One LAU polygon.
    # Bavendorf is an Ortsteil, not a 2019 LAU Gemeinde — drop it.
    m = m[
        ~m["gemeinde"].isin(
            ["Lüneburg-Zentrum", "Lüneburg-Stadtrand", "Bavendorf"]
        )
    ]
    # Aliases: LAU city name; Tosterglope ∈ SG Dahlenburg (missing from DIETER CSV).
    m = pd.concat(
        [
            m,
            pd.DataFrame(
                [
                    {
                        "gemeinde": "Lüneburg",
                        "node": "Hansestadt Lüneburg",
                        "node_id": "N01",
                        "node_type": "Einheitsgemeinde",
                    },
                    {
                        "gemeinde": "Tosterglope",
                        "node": "Dahlenburg",
                        "node_id": "N07",
                        "node_type": "Samtgemeinde",
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    m["key"] = m["gemeinde"].map(normalize_name)
    if m["key"].duplicated().any():
        raise RuntimeError(f"duplicate keys after normalize:\n{m[m['key'].duplicated(keep=False)]}")
    return m


def main() -> int:
    ensure_lau_zip()
    mapping = load_mapping()
    nodes = pd.read_csv(NODES_CSV).set_index("node_id")

    lau = gpd.read_file(f"zip://{LAU_ZIP}!{LAU_MEMBER}")
    de935 = lau[
        (lau["CNTR_CODE"] == "DE") & (lau["LAU_ID"].astype(str).str.startswith(AGS_PREFIX))
    ].copy()
    if de935.empty:
        raise RuntimeError(f"no LAU polygons with LAU_ID starting {AGS_PREFIX}")

    de935["key"] = de935["LAU_NAME"].map(normalize_name)
    de935 = de935.merge(
        mapping[["key", "node_id", "node", "node_type"]],
        on="key",
        how="left",
        validate="one_to_one",
    )

    unmatched_lau = de935[de935["node_id"].isna()][["LAU_ID", "LAU_NAME", "key"]]
    mapped_keys = set(de935["key"].dropna())
    unmatched_map = mapping[~mapping["key"].isin(mapped_keys)][["gemeinde", "node_id", "key"]]

    audit = de935[["LAU_ID", "LAU_NAME", "key", "node_id", "node", "node_type"]].copy()
    audit.to_csv(OUT_AUDIT, index=False)
    print(f"wrote audit → {OUT_AUDIT} ({len(audit)} LAU polygons)")

    if len(unmatched_lau) or len(unmatched_map):
        print("UNMATCHED LAU polygons:\n", unmatched_lau.to_string(index=False))
        print("UNMATCHED mapping rows:\n", unmatched_map.to_string(index=False))
        raise SystemExit("name matching failed — fix normalize_name / mapping, then rerun")

    # dissolve Gemeindestufe → 11 nodes
    de935["geometry"] = de935.geometry.map(make_valid)
    nodes_gdf = (
        de935.dissolve(by="node_id", as_index=False)
        .set_crs(de935.crs)
        .to_crs(TARGET_CRS)
    )
    nodes_gdf["geometry"] = nodes_gdf.geometry.map(make_valid).buffer(0)

    # attach population / canonical name from nodes.csv; keep node_type from dissolve
    nodes_gdf = nodes_gdf.merge(
        nodes[["node", "node_type", "population"]],
        left_on="node_id",
        right_index=True,
        how="left",
        suffixes=("_dissolved", ""),
    )
    # busmap_from_shapes uses column "name"
    nodes_gdf["name"] = nodes_gdf["node_id"]
    nodes_gdf = nodes_gdf[["name", "node_id", "node", "node_type", "population", "geometry"]]

    if sorted(nodes_gdf["name"]) != [f"N{i:02d}" for i in range(1, 12)]:
        raise RuntimeError(f"expected N01..N11, got {sorted(nodes_gdf['name'])}")

    # coverage vs DE935 from the DACH test run (optional if nuts3 shapes exist)
    if NUTS3_GEOJSON.exists():
        nuts3 = gpd.read_file(NUTS3_GEOJSON).set_index("index").to_crs(TARGET_CRS)
        ref = nuts3.loc["DE935", "geometry"]
        union = make_valid(nodes_gdf.union_all())
        # equal-area CRS for area comparison
        ref_a = gpd.GeoSeries([ref], crs=TARGET_CRS).to_crs(3035).area.iloc[0]
        uni_a = gpd.GeoSeries([union], crs=TARGET_CRS).to_crs(3035).area.iloc[0]
        rel = abs(uni_a - ref_a) / ref_a
        print(f"area check vs DE935: rel. diff = {rel:.4%} (limit 1%)")
        if rel > 0.01:
            raise RuntimeError("union of 11 nodes differs from DE935 by >1%")
        # overlaps between nodes (should be empty / tiny)
        overlaps = 0.0
        geoms_3035 = nodes_gdf.to_crs(3035).geometry
        for i in range(len(geoms_3035)):
            for j in range(i + 1, len(geoms_3035)):
                inter = geoms_3035.iloc[i].intersection(geoms_3035.iloc[j])
                overlaps += inter.area
        print(f"pairwise overlap area (m², EPSG:3035) = {overlaps:.1f}")
        if overlaps > 1e4:  # > 1 ha of overlap is suspicious
            raise RuntimeError("non-trivial overlaps between node polygons")

    nodes_gdf.to_file(OUT_GEOJSON, driver="GeoJSON")
    print(f"wrote {OUT_GEOJSON} ({len(nodes_gdf)} features, CRS={nodes_gdf.crs})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
