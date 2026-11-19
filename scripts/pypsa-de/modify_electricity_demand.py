"""Step 5 — put local electricity demand on N01AC…N11AC.

WHY this file exists
--------------------
PyPSA-DE builds German demand from national totals and spreads it by raster.
That under-represents Landkreis Lüneburg and has the wrong shape for our study.
We only override the 11 Lüneburg buses; DE-rest keeps the rest of Germany.

WHAT we use
-----------
- Annual energy: Energiemonitor Verbrauch 2025 ≈ 521 GWh  (not Heitkötter — that is heat)
- Shape: Avacon hourly shares (sum=1), mapped by hour-of-year onto model timestamps
- Split across nodes: population shares
- DE total conserved: old N01 load is moved into DE-restAC when we write the new 521 GWh

Snakemake rule: modify_electricity_demand
"""

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

logger = logging.getLogger(__name__)

PYPSA = Path(__file__).resolve().parents[2]
DATA = PYPSA.parent / "Data"
ANNUAL_CSV = DATA / "Electricity" / "lueneburg_el_demand_annual.csv"
PROFILE_CSV = DATA / "Electricity" / "Avacon" / "load_profile_hourly_norm.csv"
ANNUAL_GWH = 521.0
LG = [f"N{i:02d}AC" for i in range(1, 12)]
REST = "DE-restAC"


def run(demand_path: Path, out_path: Path, annual_gwh: float = ANNUAL_GWH) -> None:
    # Clustered demand is MW on (time × bus). Today only DE-restAC + N01AC exist;
    # N02–N11 are missing because synthetics were never in the raster step.
    demand = xr.open_dataarray(demand_path).to_pandas()
    demand.index = pd.to_datetime(demand.index)

    # WHY population: no node-level meter data; Destatis pop is the agreed spatial key.
    shares = pd.read_csv(ANNUAL_CSV).set_index("bus")["share"].astype(float)
    shares = (shares / shares.sum()).reindex(LG)

    # WHY Avacon profile: local DSO shape for the Landkreis; hour-of-year → weather year 2013.
    avacon = pd.read_csv(PROFILE_CSV).set_index("Hour")["Share"].astype(float)
    avacon = avacon / avacon.sum()
    hoy = np.clip(
        (demand.index.dayofyear - 1) * 24 + demand.index.hour + 1, 1, 8760
    )
    shape = avacon.loc[hoy].to_numpy()  # share of annual MWh per hour → MW factor

    # P_i(t) [MW] = (GWh→MWh) * w_i * s(t)   with sum_t s(t)=1 for hourly steps
    total_mw = annual_gwh * 1000.0 * shape
    new_lg = pd.DataFrame({b: total_mw * float(shares[b]) for b in LG}, index=demand.index)

    # WHY conserve: keep Germany's total load; only relocate energy onto N01–N11.
    present = [b for b in LG if b in demand.columns]
    old_lg = demand[present].sum(axis=1) if present else 0.0
    out = demand.copy()
    out[REST] = out[REST] + old_lg - new_lg.sum(axis=1)
    for b in LG:
        out[b] = new_lg[b]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    da = xr.DataArray(out, name="electricity demand (MW)")
    da.to_netcdf(
        out_path,
        encoding={da.name: dict(zlib=True, complevel=9, least_significant_digit=5)},
    )
    logger.info("wrote %s — LG buses %s", out_path, LG)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "snakemake" in globals():
        # No __future__ import: Snakemake prepends a wrapper (see Step 3 SyntaxError).
        from scripts._helpers import configure_logging, set_scenario_config

        configure_logging(snakemake)
        set_scenario_config(snakemake)
        run(
            Path(snakemake.input.demand),
            Path(snakemake.output.demand),
            float(getattr(snakemake.params, "annual_gwh", ANNUAL_GWH)),
        )
        sys.exit(0)

    base = PYPSA / "resources/lueneburg-smoke/KN2045_Mix"
    run(base / "electricity_demand.nc", base / "electricity_demand-modified.nc")
