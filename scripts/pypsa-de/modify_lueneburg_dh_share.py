"""Step 6b — exogenous district-heat share for N01AC…N11AC.

WHY this file exists
--------------------
Stock modify_district_heat_share uses eGo^N NUTS3 averages. For Lüneburg we have
Gemeinde-/Samtgemeinde-level Fernwärme shares — use those on N01–N11.
DE-rest keeps the egon-modified value.

WHAT we use
-----------
- Horizon 2025: Data/Heat/lueneburg_dh_share.csv (today register)
- Horizon ≥2040: Data/Heat/lueneburg_dh_share_2040.csv (pathway shares)
- urban fraction floored to >= district fraction (stock convention)

Runs after modify_district_heat_share; compose reads *-lueneburg.csv when enable.
"""

import logging
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

PYPSA = Path(__file__).resolve().parents[2]
HEAT = PYPSA.parent / "Data" / "Heat"
DH_CSV_TODAY = HEAT / "lueneburg_dh_share.csv"
DH_CSV_2040 = HEAT / "lueneburg_dh_share_2040.csv"
LG = [f"N{i:02d}AC" for i in range(1, 12)]


def _local_csv_for_horizon(horizon: int | str | None) -> Path:
    """Pick today vs 2040 pathway share table."""
    try:
        y = int(horizon) if horizon is not None else 2025
    except (TypeError, ValueError):
        y = 2025
    if y >= 2040:
        if not DH_CSV_2040.exists():
            raise FileNotFoundError(f"Missing 2040 DH shares: {DH_CSV_2040}")
        return DH_CSV_2040
    return DH_CSV_TODAY


def run(dh_path: Path, out_path: Path, horizon: int | str | None = None) -> None:
    dh = pd.read_csv(dh_path, index_col=0)
    local_path = _local_csv_for_horizon(horizon)
    local = pd.read_csv(local_path).set_index("bus")["district_fraction"].astype(float)
    logger.info("Lüneburg DH shares from %s (horizon=%s)", local_path.name, horizon)

    for b in LG:
        if b not in dh.index:
            # copy a DE row template if node missing
            template = dh.loc[dh.index.astype(str).str.startswith("DE")].iloc[0].copy()
            dh.loc[b] = template
        frac = float(local[b])
        dh.loc[b, "district fraction of node"] = frac
        # WHY floor urban: stock heat systems need urban_fraction >= district share
        urban = float(dh.loc[b, "urban fraction"]) if "urban fraction" in dh.columns else 0.0
        dh.loc[b, "urban fraction"] = max(urban, frac)
        if "original district heat share" in dh.columns:
            dh.loc[b, "original district heat share"] = frac

    out_path.parent.mkdir(parents=True, exist_ok=True)
    dh.to_csv(out_path)
    logger.info("wrote Lüneburg DH shares for %s → %s", LG, out_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "snakemake" in globals():
        from scripts._helpers import configure_logging, set_scenario_config

        configure_logging(snakemake)
        set_scenario_config(snakemake)
        horizon = getattr(snakemake.wildcards, "horizon", None)
        run(
            Path(snakemake.input.district_heat_share),
            Path(snakemake.output.district_heat_share),
            horizon=horizon,
        )
        sys.exit(0)

    base = PYPSA / "resources/test-sector-myopic-dach/KN2045_Mix"
    src = base / "district_heat_share_2025-modified.csv"
    if not src.exists():
        # any modified file
        cands = sorted(base.glob("district_heat_share_*-modified.csv"))
        src = cands[0]
    out = PYPSA.parent / "Data" / "Heat" / "step6_dryrun" / "district_heat_share-lueneburg.csv"
    run(src, out, horizon=2025)
    print("OK Step 6b dry-run", out)
