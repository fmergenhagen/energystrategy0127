"""Step 6a — local heat demand on N01AC…N11AC (shape + annuals).

WHY this file exists
--------------------
PyPSA-DE builds heat shapes from atlite HDD and annuals from national totals × pop.
That misses synthetic nodes N02–N11 and does not match Landkreis heat (Heitkötter).
We overwrite Lüneburg annuals/shapes; DE-rest keeps the residual German heat.

WHAT we use
-----------
- Annual: Heitkötter AGS 3355 ≈ 1104 GWh space + 192 GWh DHW (residential dataset)
- Shape: Heitkötter 15-min series, hour-of-year → model snapshots (weather year 2013)
- Split: population shares; all Heitkötter energy → residential (no services split in source)
- Conserve DE totals via DE-restAC (same idea as Step 5 electricity)

NOT touched here: district-heat share (Step 6b), heating technology stock (later).

Snakemake: modify_heat_demand
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
HEAT = DATA / "Heat"
HEIT = (
    HEAT
    / "supplementary_material_publication_heitkoetter_regionalised_heat_demand_p2h_capacities_20190910"
    / "04_results"
    / "03_yearly_time_series"
    / "01_time_series_based_on_long_term_average_temperatures"
)
ANNUAL_CSV = HEAT / "lueneburg_heat_demand_annual.csv"
SPACE_CSV = HEIT / "yearly_time_series_space_heating_administrative_district_id=3355.csv"
DHW_CSV = HEIT / "yearly_time_series_dhw_administrative_district_id=3355.csv"

LG = [f"N{i:02d}AC" for i in range(1, 12)]
REST = "DE-restAC"
SPACE_GWH = 1103.546786135309
DHW_GWH = 192.44106172217337


def _heit_hourly_mw(path: Path, col: str) -> pd.Series:
    """15-min MW → hourly mean MW, indexed by hour-of-year 1…8760 (non-leap)."""
    raw = pd.read_csv(path)
    tcol = raw.columns[0]
    raw[tcol] = pd.to_datetime(raw[tcol])
    raw = raw.set_index(tcol)[col].astype(float)
    hourly = raw.resample("h").mean()
    # Map to hour-of-year of a non-leap reference (ignore absolute year)
    hoy = (hourly.index.dayofyear - 1) * 24 + hourly.index.hour + 1
    s = pd.Series(hourly.to_numpy(), index=hoy).groupby(level=0).mean()
    return s.reindex(range(1, 8761)).ffill().bfill()


def _shape_on_snapshots(snapshots: pd.DatetimeIndex, hourly_mw: pd.Series) -> np.ndarray:
    hoy = np.clip((snapshots.dayofyear - 1) * 24 + snapshots.hour + 1, 1, 8760)
    return hourly_mw.loc[hoy].to_numpy()


def _conserve(df: pd.DataFrame, new_lg: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = 0.0
        present = [b for b in LG if b in out.index]
        old = out.loc[present, c].sum() if present else 0.0
        new = float(new_lg[c].sum())
        if REST not in out.index:
            out.loc[REST] = 0.0
        out.loc[REST, c] = float(out.loc[REST, c]) + old - new
        for b in LG:
            out.loc[b, c] = float(new_lg.loc[b, c])
    return out


def run(
    heat_nc: Path,
    heat_totals: Path,
    energy_totals: Path,
    out_nc: Path,
    out_heat_totals: Path,
    out_energy_totals: Path,
) -> None:
    # --- annuals (TWh) ---
    # WHY residential only: Heitkötter is a residential census product.
    shares = pd.read_csv(ANNUAL_CSV).set_index("bus")["share"].astype(float)
    shares = (shares / shares.sum()).reindex(LG)
    space_twh = SPACE_GWH / 1000.0
    dhw_twh = DHW_GWH / 1000.0

    new_heat = pd.DataFrame(index=LG)
    new_heat["total residential space"] = shares * space_twh
    new_heat["electricity residential space"] = 0.0  # electric-heat subtraction: none assumed in overlay
    new_heat["total services space"] = 0.0
    new_heat["electricity services space"] = 0.0

    new_energy = pd.DataFrame(index=LG)
    new_energy["total residential water"] = shares * dhw_twh
    new_energy["electricity residential water"] = 0.0
    new_energy["total services water"] = 0.0
    new_energy["electricity services water"] = 0.0
    # space columns also live in energy_totals and get updated from heat_totals in compose;
    # set them here so a direct energy read stays consistent.
    for c in new_heat.columns:
        new_energy[c] = new_heat[c]

    ht = pd.read_csv(heat_totals, index_col=0)
    et = pd.read_csv(energy_totals, index_col=0)
    ht = _conserve(ht, new_heat, list(new_heat.columns))
    et = _conserve(et, new_energy, list(new_energy.columns))

    # --- shape NC ---
    # WHY hour-of-year: Heitkötter calendar is 2011; model weather year is 2013.
    ds = xr.open_dataset(heat_nc)
    snapshots = pd.to_datetime(ds.snapshots.values)
    space_shape = _shape_on_snapshots(snapshots, _heit_hourly_mw(SPACE_CSV, "space_heat_load_in_MW"))
    dhw_shape = _shape_on_snapshots(snapshots, _heit_hourly_mw(DHW_CSV, "dhw_load_in_MW"))

    nodes = list(ds.node.values)
    for b in LG + [REST]:
        if b not in nodes:
            nodes.append(b)
    data = {}
    for var in ds.data_vars:
        old = ds[var].to_pandas()  # snapshots × node
        frame = old.reindex(columns=nodes, fill_value=0.0)
        # relative shape only (prepare_sector normalises per column)
        profile = space_shape if "space" in var else dhw_shape
        for b in LG:
            frame[b] = profile
        data[var] = frame

    out = xr.Dataset(
        {v: (("snapshots", "node"), data[v].to_numpy()) for v in data},
        coords={"snapshots": snapshots, "node": nodes},
    )

    out_nc.parent.mkdir(parents=True, exist_ok=True)
    out.to_netcdf(out_nc)
    ht.to_csv(out_heat_totals)
    et.to_csv(out_energy_totals)
    logger.info(
        "wrote heat overlay %s (+ totals); LG space=%.2f TWh water=%.2f TWh",
        out_nc,
        space_twh,
        dhw_twh,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "snakemake" in globals():
        from scripts._helpers import configure_logging, set_scenario_config

        configure_logging(snakemake)
        set_scenario_config(snakemake)
        run(
            Path(snakemake.input.heat_nc),
            Path(snakemake.input.heat_totals),
            Path(snakemake.input.energy_totals),
            Path(snakemake.output.heat_nc),
            Path(snakemake.output.heat_totals),
            Path(snakemake.output.energy_totals),
        )
        sys.exit(0)

    # Standalone dry-run against DACH heat artefacts (rename not needed — we only need structure)
    base = PYPSA / "resources/test-sector-myopic-dach/KN2045_Mix"
    out_dir = DATA / "Heat" / "step6_dryrun"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Build a minimal NC with DE-restAC + N01AC so conserve path is exercised
    ds = xr.open_dataset(base / "hourly_heat_demand_total.nc")
    # take first DE-like node as template → write toy file
    node0 = str(ds.node.values[0])
    toy_nodes = [REST, "N01AC"]
    toy = xr.Dataset(
        {
            v: (
                ("snapshots", "node"),
                np.tile(ds[v].isel(node=0).values[:, None], (1, 2)),
            )
            for v in ds.data_vars
        },
        coords={"snapshots": ds.snapshots.values, "node": toy_nodes},
    )
    toy_nc = out_dir / "hourly_heat_demand_total_toy.nc"
    toy.to_netcdf(toy_nc)
    # toy totals
    cols_h = [
        "total residential space",
        "electricity residential space",
        "total services space",
        "electricity services space",
    ]
    cols_e = cols_h + [
        "total residential water",
        "electricity residential water",
        "total services water",
        "electricity services water",
    ]
    ht = pd.DataFrame(0.1, index=toy_nodes, columns=cols_h)
    et = pd.DataFrame(0.05, index=toy_nodes, columns=cols_e)
    ht_path, et_path = out_dir / "heat_totals.csv", out_dir / "energy_totals.csv"
    ht.to_csv(ht_path)
    et.to_csv(et_path)
    run(
        toy_nc,
        ht_path,
        et_path,
        out_dir / "hourly_heat_demand_total-modified.nc",
        out_dir / "pop_weighted_heat_totals-modified.csv",
        out_dir / "pop_weighted_energy_totals-modified.csv",
    )
    ht2 = pd.read_csv(out_dir / "pop_weighted_heat_totals-modified.csv", index_col=0)
    assert all(b in ht2.index for b in LG)
    assert abs(ht2.loc[LG, "total residential space"].sum() - SPACE_GWH / 1000) < 1e-6
    print("OK Step 6a dry-run", out_dir)
