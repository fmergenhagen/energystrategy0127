"""Inject synthetic AC buses for Lüneburg nodes N02–N11 into the simplified network.

N01 already has the real 380 kV UW Lüneburg substation. N02–N11 get a bus at the
polygon centroid. Lines follow Data/nodes/Lueneburg_11_adjacency_matrix.csv.
Matching polygons are appended to onshore_regions_simplified so cluster_regions
can dissolve them under custom_busshapes.

Standalone (uses the DACH test simplified network as a dry run):

    pixi run python scripts/pypsa-de/add_lueneburg_synthetic_buses.py

Snakemake: rule add_lueneburg_synthetic_buses in rules/pypsa-de/modifications.smk.
"""

import logging
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pypsa
from shapely.geometry import Point

logger = logging.getLogger(__name__)

# __file__ = <repo>/pypsa-de/scripts/pypsa-de/add_lueneburg_synthetic_buses.py
PYPSA = Path(__file__).resolve().parents[2]  # pypsa-de/
DATA_NODES = PYPSA.parent / "Data" / "nodes"  # V2.0_PyPsa/Data/nodes/

DEFAULT_NODES = DATA_NODES / "lueneburg_11_nodes.geojson"
DEFAULT_ADJ = DATA_NODES / "Lueneburg_11_adjacency_matrix.csv"
DEFAULT_NODES_CSV = DATA_NODES / "nodes.csv"

# Placeholder transfer capacity for intra-Landkreis links (MW). Refine in a later step.
DEFAULT_S_NOM = 200.0
V_NOM = 380.0


def _haversine_km(lon1, lat1, lon2, lat2) -> float:
    r = 6371.0
    lon1, lat1, lon2, lat2 = map(np.radians, [lon1, lat1, lon2, lat2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def load_adjacency(path: Path, nodes_csv: Path) -> pd.DataFrame:
    """Return upper-triangle edges as DataFrame with columns node_i, node_j (N01..)."""
    raw = pd.read_csv(path, index_col=0)
    # blank diagonal / empty cells → 0
    raw = raw.replace("", 0).fillna(0).astype(int)
    name_to_id = pd.read_csv(nodes_csv).set_index("node")["node_id"]
    # matrix labels are German names; map to N01..
    raw.index = raw.index.map(lambda n: name_to_id[n])
    raw.columns = raw.columns.map(lambda n: name_to_id[n])
    edges = []
    for i, a in enumerate(raw.index):
        for b in raw.columns[i + 1 :]:
            if raw.loc[a, b] == 1:
                edges.append((a, b))
    return pd.DataFrame(edges, columns=["node_i", "node_j"])


def find_n01_bus(n: pypsa.Network, n01_geom) -> str:
    """Real AC bus inside the N01 polygon (UW Lüneburg)."""
    pts = gpd.GeoSeries(
        gpd.points_from_xy(n.buses.x, n.buses.y), index=n.buses.index, crs=4326
    )
    inside = n.buses.index[pts.within(n01_geom)]
    if len(inside) == 0:
        raise RuntimeError("no AC bus found inside N01 — unexpected after Step 1")
    if len(inside) > 1:
        logger.warning("multiple buses in N01 %s; using the first", list(inside))
    return inside[0]


def add_synthetic_buses(
    n: pypsa.Network,
    nodes: gpd.GeoDataFrame,
    edges: pd.DataFrame,
    s_nom: float = DEFAULT_S_NOM,
) -> tuple[pypsa.Network, list[str]]:
    """Add N02–N11 buses + adjacency lines. Returns (network, list of new bus names)."""
    nodes = nodes.to_crs(4326).set_index("name")
    n01_bus = find_n01_bus(n, nodes.loc["N01", "geometry"])
    bus_of = {"N01": n01_bus}

    # Electrical template from an existing 380 kV line
    tmpl = n.lines.iloc[0]
    x_per_km = tmpl.x / tmpl.length
    r_per_km = tmpl.r / tmpl.length
    b_per_km = tmpl.b / tmpl.length if tmpl.length else 0.0

    new_buses = []
    for name, row in nodes.iterrows():
        if name == "N01":
            continue
        if name in n.buses.index:
            raise RuntimeError(f"bus {name} already exists")
        c = row.geometry.centroid
        n.add(
            "Bus",
            name,
            v_nom=V_NOM,
            x=c.x,
            y=c.y,
            carrier="AC",
            country="DE",
            unit=n.buses.unit.iloc[0] if "unit" in n.buses.columns else "MWh_el",
        )
        bus_of[name] = name
        new_buses.append(name)
        logger.info("added synthetic bus %s at (%.4f, %.4f)", name, c.x, c.y)

    # Adjacency lines
    for _, e in edges.iterrows():
        a, b = e["node_i"], e["node_j"]
        bus0, bus1 = bus_of[a], bus_of[b]
        length = _haversine_km(
            n.buses.at[bus0, "x"],
            n.buses.at[bus0, "y"],
            n.buses.at[bus1, "x"],
            n.buses.at[bus1, "y"],
        )
        # floor so zero-length never happens
        length = max(length, 0.5)
        line_name = f"LG-{a}-{b}"
        n.add(
            "Line",
            line_name,
            bus0=bus0,
            bus1=bus1,
            v_nom=V_NOM,
            x=x_per_km * length,
            r=r_per_km * length,
            b=b_per_km * length,
            s_nom=s_nom,
            length=length,
            carrier="AC",
            type=tmpl.type,
            num_parallel=1.0,
            capital_cost=0.0,
            s_nom_extendable=False,
        )
        logger.info("added line %s (%.1f km, s_nom=%.0f MW)", line_name, length, s_nom)

    return n, new_buses


def extend_onshore_regions(
    regions: gpd.GeoDataFrame, nodes: gpd.GeoDataFrame, new_buses: list[str]
) -> gpd.GeoDataFrame:
    """Append node polygons as regions named after the synthetic buses."""
    nodes = nodes.to_crs(regions.crs).set_index("name")
    extras = []
    for name in new_buses:
        extras.append(
            {
                "name": name,
                "country": "DE",
                "geometry": nodes.loc[name, "geometry"],
            }
        )
    extra = gpd.GeoDataFrame(extras, crs=regions.crs)
    out = pd.concat([regions, extra], ignore_index=True)
    if out["name"].duplicated().any():
        raise RuntimeError("duplicate region names after appending synthetics")
    return gpd.GeoDataFrame(out, crs=regions.crs)


def run(
    network_path: Path,
    regions_path: Path,
    nodes_path: Path,
    adjacency_path: Path,
    nodes_csv: Path,
    out_network: Path,
    out_regions: Path,
    s_nom: float = DEFAULT_S_NOM,
) -> None:
    n = pypsa.Network(network_path)
    regions = gpd.read_file(regions_path)
    nodes = gpd.read_file(nodes_path)
    edges = load_adjacency(adjacency_path, nodes_csv)

    n, new_buses = add_synthetic_buses(n, nodes, edges, s_nom=s_nom)
    regions = extend_onshore_regions(regions, nodes, new_buses)

    out_network.parent.mkdir(parents=True, exist_ok=True)
    out_regions.parent.mkdir(parents=True, exist_ok=True)
    n.export_to_netcdf(out_network)
    regions.to_file(out_regions, driver="GeoJSON")
    logger.info(
        "wrote %s (%d buses) and %s (%d regions); synthetics=%s",
        out_network,
        len(n.buses),
        out_regions,
        len(regions),
        new_buses,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "snakemake" in globals():
        from scripts._helpers import configure_logging, set_scenario_config

        configure_logging(snakemake)
        set_scenario_config(snakemake)
        run(
            network_path=Path(snakemake.input.network),
            regions_path=Path(snakemake.input.onshore_regions),
            nodes_path=Path(snakemake.input.nodes),
            adjacency_path=Path(snakemake.input.adjacency),
            nodes_csv=Path(snakemake.input.nodes_csv),
            out_network=Path(snakemake.output.network),
            out_regions=Path(snakemake.output.onshore_regions),
            s_nom=float(getattr(snakemake.params, "s_nom", DEFAULT_S_NOM)),
        )
        sys.exit(0)

    # Standalone dry run against the DACH test artefacts
    base = PYPSA / "resources/test-sector-myopic-dach/KN2045_Mix"
    out_dir = DATA_NODES / "step3b_dryrun"
    run(
        network_path=base / "networks/simplified.nc",
        regions_path=base / "onshore_regions_simplified.geojson",
        nodes_path=DEFAULT_NODES,
        adjacency_path=DEFAULT_ADJ,
        nodes_csv=DEFAULT_NODES_CSV,
        out_network=out_dir / "simplified_lueneburg.nc",
        out_regions=out_dir / "onshore_regions_simplified_lueneburg.geojson",
    )
    n = pypsa.Network(out_dir / "simplified_lueneburg.nc")
    assert all(f"N{i:02d}" in n.buses.index for i in range(2, 12)), list(
        n.buses.index[-15:]
    )
    assert "N01" not in n.buses.index  # N01 = real OSM bus, not a synthetic
    assert any(n.buses.index.str.contains("28327356")), "UW Lüneburg missing"
    n_lines = n.lines.index[n.lines.index.str.startswith("LG-")]
    print(f"OK: {len(n_lines)} LG lines, buses={len(n.buses)}")
    sys.exit(0)
