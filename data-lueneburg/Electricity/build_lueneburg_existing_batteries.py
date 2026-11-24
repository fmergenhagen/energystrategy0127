"""Build Data/Electricity/lueneburg_existing_batteries.csv from MaStR (AGS 03355).

WHY: the 2025 base year should contain today's battery stock. Source is the
open-MaStR dump used for the other capacities
(pypsa-de/data/mastr/bnetza_open_mastr_2023-08-08_B_storage.csv, stand 2023-08).

Only units "In Betrieb" with Technologie == "Batterie" are counted.
Nettonennleistung (kW) is the inverter power. The dump has no usable storage
capacity (NutzbareSpeicherkapazitaet is empty), so energy is derived in the
model from an assumed duration (lueneburg.existing_batteries.*_hours).

Split: <= 30 kW -> "home battery" (low-voltage bus), > 30 kW -> "battery".
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]  # .../V2.0_PyPsa.nosync/Data
MASTR = ROOT.parent / "pypsa-de" / "data" / "mastr" / "bnetza_open_mastr_2023-08-08_B_storage.csv"
NODES = ROOT / "nodes" / "lueneburg_lau_to_node.csv"
OUT = Path(__file__).resolve().parent / "lueneburg_existing_batteries.csv"
HOME_MAX_KW = 30.0
USECOLS = ["Technologie", "EinheitBetriebsstatus", "Gemeindeschluessel", "Nettonennleistung"]


def main() -> None:
    lau = pd.read_csv(NODES, dtype={"LAU_ID": str})
    map_node = lau.assign(LAU_ID=lau.LAU_ID.str.zfill(8)).set_index("LAU_ID")["node_id"]

    parts = []
    for chunk in pd.read_csv(
        MASTR, usecols=USECOLS, dtype={"Gemeindeschluessel": str}, chunksize=200_000, low_memory=False
    ):
        ags = chunk["Gemeindeschluessel"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(8)
        m = (
            ags.str.startswith("03355")
            & (chunk["EinheitBetriebsstatus"] == "In Betrieb")
            & (chunk["Technologie"] == "Batterie")
        )
        if m.any():
            parts.append(chunk.loc[m].assign(AGS=ags[m]))
    df = pd.concat(parts, ignore_index=True)
    df["kW"] = pd.to_numeric(df["Nettonennleistung"], errors="coerce").fillna(0.0)
    df["node_id"] = df["AGS"].map(map_node)
    missing = df["node_id"].isna().sum()
    if missing:
        print(f"WARNING: {missing} units with unmapped Gemeindeschluessel dropped")
    df = df.dropna(subset=["node_id"])
    df["kind"] = (df["kW"] <= HOME_MAX_KW).map({True: "home_battery", False: "battery"})

    nodes = [f"N{i:02d}" for i in range(1, 12)]
    agg = df.groupby(["node_id", "kind"]).agg(MW=("kW", lambda x: x.sum() / 1e3), units=("kW", "size")).unstack("kind")
    out = pd.DataFrame({"node_id": nodes, "bus": [f"{x}AC" for x in nodes]})
    for kind in ["home_battery", "battery"]:
        out[f"{kind}_MW"] = agg.get(("MW", kind), pd.Series(dtype=float)).reindex(nodes).fillna(0).values
        out[f"{kind}_units"] = agg.get(("units", kind), pd.Series(dtype=float)).reindex(nodes).fillna(0).astype(int).values
    out.to_csv(OUT, index=False, float_format="%.4f")
    print(out.to_string(index=False))
    print(f"total home {out.home_battery_MW.sum():.2f} MW ({out.home_battery_units.sum()} units), "
          f"large {out.battery_MW.sum():.2f} MW ({out.battery_units.sum()} units)")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
