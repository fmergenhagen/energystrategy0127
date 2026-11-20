"""Build Data/Electricity/lueneburg_existing_capacity.csv from MaStR (AGS 03355).

WHY: chunked read of full open-MaStR dump — grep extracts miss some wind rows.
Source: pypsa-de/data/mastr/bnetza_open_mastr_2023-08-08_B_*.csv
Filter: Gemeindeschluessel startswith 03355, EinheitBetriebsstatus == In Betrieb.
Nettonennleistung is kW → divide by 1000 for MW.
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]  # Data/
MASTR = ROOT.parent / "pypsa-de" / "data" / "mastr"
NODES = ROOT / "nodes" / "lueneburg_lau_to_node.csv"
OUT = Path(__file__).resolve().parent / "lueneburg_existing_capacity.csv"

TECH = {
    "wind": ("bnetza_open_mastr_2023-08-08_B_wind.csv", "onwind_MW", 100_000),
    "solar": ("bnetza_open_mastr_2023-08-08_B_solar.csv", "solar_MW", 400_000),
    "biomass": ("bnetza_open_mastr_2023-08-08_B_biomass.csv", "biomass_MW", 100_000),
    "combustion": ("bnetza_open_mastr_2023-08-08_B_combustion.csv", "combustion_MW", 100_000),
    "hydro": ("bnetza_open_mastr_2023-08-08_B_hydro.csv", "hydro_MW", 100_000),
}
USECOLS = ["Gemeindeschluessel", "EinheitBetriebsstatus", "Nettonennleistung"]


def _by_node(fname: str, map_node: pd.Series, chunksize: int) -> pd.Series:
    parts = []
    for chunk in pd.read_csv(
        MASTR / fname,
        usecols=USECOLS,
        dtype={"Gemeindeschluessel": str},
        low_memory=False,
        chunksize=chunksize,
    ):
        g = (
            chunk["Gemeindeschluessel"]
            .fillna("")
            .astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(8)
        )
        m = g.str.startswith("03355") & (chunk["EinheitBetriebsstatus"] == "In Betrieb")
        if not m.any():
            continue
        sub = chunk.loc[m].copy()
        sub["Gemeindeschluessel"] = g[m].values
        sub["MW"] = pd.to_numeric(sub["Nettonennleistung"], errors="coerce").fillna(0) / 1000.0
        sub["node_id"] = sub["Gemeindeschluessel"].map(map_node)
        parts.append(sub)
    if not parts:
        return pd.Series(dtype=float)
    df = pd.concat(parts, ignore_index=True)
    return df.dropna(subset=["node_id"]).groupby("node_id")["MW"].sum()


def main() -> None:
    lau = pd.read_csv(NODES, dtype={"LAU_ID": str})
    lau["LAU_ID"] = lau["LAU_ID"].str.zfill(8)
    map_node = lau.set_index("LAU_ID")["node_id"]
    nodes = [f"N{i:02d}" for i in range(1, 12)]
    out = pd.DataFrame({"node_id": nodes, "bus": [n + "AC" for n in nodes]})
    for _tech, (fname, col, cs) in TECH.items():
        s = _by_node(fname, map_node, cs)
        out[col] = s.reindex(nodes).fillna(0).values
        print(f"{col}: {out[col].sum():.2f} MW")
    out.to_csv(OUT, index=False, float_format="%.4f")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
