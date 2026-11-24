"""Step 7 — set existing generation on N01AC…N11AC from MaStR.

WHY this file exists
--------------------
Under foresight: myopic, wind/PV brownfield come from IRENA×p_nom_max, not MaStR.
That mis-allocates Landkreis capacity (and invents plant where there is none).
After stock compose/baseyear, we overwrite LG buses from
data-lueneburg/Electricity/lueneburg_existing_capacity.csv and shrink DE-restAC by the
same delta so DE totals stay roughly conserved.

WHAT
----
- onwind / solar (generators): set p_nom = p_nom_min to MaStR MW on each LG bus
- biomass / combustion / hydro: same if matching generators exist; else log & skip
- Hook: called from modify_prenetwork.main when lueneburg.enable

NOT: full CHP link rebuild (german_chp) — leave stock egon/MaStR CHP path for now.
"""

import logging
from pathlib import Path

import pandas as pd
import pypsa

logger = logging.getLogger(__name__)

PYPSA = Path(__file__).resolve().parents[2]
CAPACITY_CSV = PYPSA / "data-lueneburg" / "Electricity" / "lueneburg_existing_capacity.csv"
REST = "DE-restAC"
# CSV column → generator carrier name(s) to match
CARRIERS = {
    "onwind_MW": ["onwind"],
    # WHY rooftop: sector networks carry rooftop PV as a separate carrier on the
    # node's low-voltage bus; without it LG had MaStR 79 MW + ~151 MW stock rooftop.
    "solar_MW": ["solar", "solar rooftop"],
    "biomass_MW": ["biomass", "solid biomass", "biogas"],
    "combustion_MW": ["OCGT", "CCGT", "oil", "lignite", "coal", "gas"],
    "hydro_MW": ["ror", "hydro"],
}


def _set_bus_capacity(n: pypsa.Network, bus: str, carriers: list[str], target_mw: float) -> float:
    """Put target_mw on one generator at bus; zero siblings. Returns previous sum.

    Matches the AC bus and its low-voltage bus (rooftop PV lives there).
    """
    gens = n.generators.index[
        n.generators.bus.isin([bus, f"{bus} low voltage"])
        & (n.generators.carrier.isin(carriers))
    ]
    if len(gens) == 0:
        return 0.0
    old = float(n.generators.loc[gens, "p_nom"].fillna(0).sum())
    # Prefer the unit with the largest potential headroom
    if "p_nom_max" in n.generators.columns:
        main = n.generators.loc[gens, "p_nom_max"].fillna(0).idxmax()
    else:
        main = gens[0]
    for g in gens:
        val = float(target_mw) if g == main else 0.0
        n.generators.at[g, "p_nom"] = val
        if "p_nom_min" in n.generators.columns:
            n.generators.at[g, "p_nom_min"] = val
    return old


def _scale_rest(n: pypsa.Network, carriers: list[str], delta_mw: float) -> None:
    """Reduce DE-restAC generators of these carriers by delta_mw (can be negative)."""
    gens = n.generators.index[
        n.generators.bus.isin([REST, f"{REST} low voltage"])
        & (n.generators.carrier.isin(carriers))
    ]
    if len(gens) == 0 or abs(delta_mw) < 1e-9:
        return
    current = n.generators.loc[gens, "p_nom"].fillna(0).astype(float)
    total = float(current.sum())
    if total <= 0:
        return
    # new_total = total - delta; delta = new_lg - old_lg → rest shrinks when LG grows
    new_total = max(total - delta_mw, 0.0)
    scale = new_total / total
    for g in gens:
        new_p = float(current[g]) * scale
        n.generators.at[g, "p_nom"] = new_p
        if "p_nom_min" in n.generators.columns:
            n.generators.at[g, "p_nom_min"] = new_p


def apply(n: pypsa.Network, capacity_csv: Path = CAPACITY_CSV) -> pypsa.Network:
    caps = pd.read_csv(capacity_csv).set_index("bus")
    for col, carriers in CARRIERS.items():
        if col not in caps.columns:
            continue
        old_lg = 0.0
        new_lg = 0.0
        for bus, row in caps.iterrows():
            target = float(row[col])
            old_lg += _set_bus_capacity(n, bus, carriers, target)
            new_lg += target
        _scale_rest(n, carriers, new_lg - old_lg)
        logger.info(
            "Lüneburg %s: LG %.1f→%.1f MW; adjusted %s",
            col,
            old_lg,
            new_lg,
            REST,
        )
    return n


def apply_if_enabled(n: pypsa.Network, lueneburg_cfg) -> pypsa.Network:
    """No-op unless lueneburg.enable is true."""
    cfg = lueneburg_cfg or {}
    if isinstance(cfg, dict):
        enabled = bool(cfg.get("enable", False))
    else:
        enabled = bool(getattr(cfg, "enable", False))
    if not enabled:
        return n
    return apply(n)
