"""Compare 2025 Lüneburg (N01–N11) model results with Avacon Energiemonitor 2025.

Usage (from anywhere):
    pixi run python scripts/pypsa-de/validate_lueneburg_2025.py [solved_2025.nc]

Default network: results/lueneburg/S0/networks/solved_2025.nc. Only model
*outputs* are meaningful here (generation, exchange, self-supply); demand and
capacities are inputs calibrated to the same data in the base year.
"""
import logging
import sys
import warnings
from pathlib import Path

logging.disable(logging.WARNING)
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import pypsa

pd.set_option("display.width", 220)
PYPSA = Path(__file__).resolve().parents[2]  # .../pypsa-de
NET = sys.argv[1] if len(sys.argv) > 1 else str(PYPSA / "results/lueneburg/S0/networks/solved_2025.nc")
EM = str(PYPSA.parent / "Data/Electricity/energiemonitor") + "/"
LG = [f"N{i:02d}AC" for i in range(1, 12)]

# ---------------- reference: Energiemonitor 2025
rd = lambda f: pd.read_csv(EM + f, parse_dates=["Datum"], encoding="utf-8-sig").set_index("Datum").loc["2025"]
r = rd("energiemonitor_de_renewables_landkreis-lueneburg_2026-08-10.csv")
b = rd("energiemonitor_de_balance_landkreis-lueneburg_2026-08-10.csv")
a = rd("energiemonitor_de_autarky_landkreis-lueneburg_2026-08-10.csv")
em_gen = r[["Biomasse [MWh]", "Wasserkraft [MWh]", "Weitere [MWh]", "Photovoltaik [MWh]", "Windkraft [MWh]"]].sum(axis=1)
em_cons = r["Verbrauch [MWh]"]

# ---------------- model
n = pypsa.Network(NET)
w = n.snapshot_weightings.generators
lg_buses = n.buses.index[n.buses.location.isin(LG)] if "location" in n.buses else n.buses.index[n.buses.index.str[:5].isin(LG)]
el_buses = n.buses.index[n.buses.index.isin(LG) | (n.buses.index.str[:5].isin(LG) & (n.buses.carrier == "low voltage"))]

def gen_daily(carriers):
    g = n.generators[n.generators.bus.isin(el_buses) & n.generators.carrier.isin(carriers)]
    return n.generators_t.p[g.index].sum(axis=1) * w if len(g) else pd.Series(0.0, n.snapshots)

def link_el_out(carrier_mask):
    """Electricity delivered to LG AC/LV buses by links matching carrier_mask (any port)."""
    tot = pd.Series(0.0, n.snapshots)
    L = n.links[carrier_mask(n.links.carrier) & n.links.index.str[:5].isin(LG)]
    for port in ["1", "2", "3", "4"]:
        col = f"bus{port}"
        if col not in L:
            continue
        sel = L.index[L[col].isin(el_buses)]
        if len(sel):
            tot += -n.links_t[f"p{port}"][sel].sum(axis=1) * w  # p<0 = injection
    return tot.clip(lower=0) if False else tot

wind = gen_daily(["onwind"])
solar = gen_daily(["solar", "solar rooftop", "solar-hsat"])
hydro = gen_daily(["ror", "hydro"])
bio = link_el_out(lambda c: c.str.contains("biomass|biogas", case=False) & c.str.contains("CHP|electricity", case=False))
waste = link_el_out(lambda c: c.str.contains("waste CHP", case=False))
fossil = link_el_out(lambda c: c.isin(["CCGT", "OCGT", "oil", "coal", "lignite"]) | (c.str.contains("CHP") & ~c.str.contains("biomass|biogas|waste|H2")))
model_gen = wind + solar + hydro + bio + waste + fossil

# consumption on LG electricity buses (loads + link withdrawals from AC/LV, excl. grid links between LG buses)
eb = n.statistics.energy_balance(groupby=["bus", "carrier"], aggregate_across_components=True)
lv = eb[eb.index.get_level_values("bus").isin([b_ for b_ in n.buses.index if b_[:5] in LG and n.buses.at[b_, "carrier"] == "low voltage"])]
lv = lv.groupby(level="carrier").sum() / 1e3
cons_by = lv[lv < 0].abs().sort_values(ascending=False)

# exchange with DE-rest over AC lines
L = n.lines[(n.lines.bus0 == "DE-restAC") & n.lines.bus1.isin(LG) | (n.lines.bus1 == "DE-restAC") & n.lines.bus0.isin(LG)]
flow = (n.lines_t.p0[L.index] * np.where(L.bus0 == "DE-restAC", 1, -1)).sum(axis=1) * w  # + = into LG
imp, exp = flow.clip(lower=0), -flow.clip(upper=0)
model_cons_total = model_gen + imp - exp  # balance closes (incl. storage losses)

caps = lambda carriers: n.generators.loc[n.generators.bus.isin(el_buses) & n.generators.carrier.isin(carriers), "p_nom_opt"].sum()
em_mon = lambda s: s.groupby(s.index.month).sum() / 1e3
mo = lambda s: s.groupby(s.index.month).sum() / 1e3

rows = [
    ("Wind generation [GWh]", r["Windkraft [MWh]"].sum() / 1e3, wind.sum() / 1e3),
    ("Solar generation [GWh]", r["Photovoltaik [MWh]"].sum() / 1e3, solar.sum() / 1e3),
    ("Biomass/biogas generation [GWh]", r["Biomasse [MWh]"].sum() / 1e3, bio.sum() / 1e3),
    ("Hydro [GWh]", r["Wasserkraft [MWh]"].sum() / 1e3, hydro.sum() / 1e3),
    ("Other ('Weitere') vs waste+fossil [GWh]", r["Weitere [MWh]"].sum() / 1e3, (waste + fossil).sum() / 1e3),
    ("Local generation total [GWh]", em_gen.sum() / 1e3, model_gen.sum() / 1e3),
    ("Consumption [GWh] (EM: Verbrauch; model: gen+imp-exp)", em_cons.sum() / 1e3, model_cons_total.sum() / 1e3),
    ("Grid import (Netzbezug) [GWh]", abs(b["Netzbezug [MWh]"].sum()) / 1e3, imp.sum() / 1e3),
    ("Grid export (Netzeinspeisung) [GWh]", b["Netzeinspeisung [MWh]"].sum() / 1e3, exp.sum() / 1e3),
    ("Net export [GWh]", b["Netzbilanz [MWh]"].sum() / 1e3, (exp - imp).sum() / 1e3),
    ("Generation / consumption [-]", em_gen.sum() / em_cons.sum(), model_gen.sum() / model_cons_total.sum()),
]
cons_d = model_cons_total.where(model_cons_total > 0)
selfsup = (model_gen.clip(upper=cons_d) / cons_d * 100)
em_self_daily = (em_gen.clip(upper=em_cons) / em_cons * 100)
rows += [
    ("Self-supply daily mean [%] (EM reported / EM daily formula)", f"{a.Eigenversorgung.mean():.0f} / {em_self_daily.mean():.0f}", selfsup.mean()),
    ("Days with self-supply >= 65% [%]", (a.Eigenversorgung >= 65).mean() * 100, (selfsup >= 65).mean() * 100),
    ("Min daily self-supply [%]", a.Eigenversorgung.min(), selfsup.min()),
]
cmp = pd.DataFrame(rows, columns=["metric", "Energiemonitor 2025", "PyPSA S0 2025"]).set_index("metric")

def dev(x):
    try:
        e, m = float(x["Energiemonitor 2025"]), float(x["PyPSA S0 2025"])
        return f"{(m - e) / e:+.0%}" if e else ""
    except (TypeError, ValueError):
        return ""
cmp["deviation"] = cmp.apply(dev, axis=1)
fmt = lambda v: f"{v:,.1f}" if isinstance(v, (int, float, np.floating)) else v
print(cmp.map(fmt).to_string())

print("\nInstalled capacity LG [MW] (model; MaStR wind 266 / PV 79 / biomass 38):")
print({"wind": round(caps(["onwind"]), 1), "solar": round(caps(["solar", "solar rooftop", "solar-hsat"]), 1)})
print("wind full-load hours: EM", round(r["Windkraft [MWh]"].sum() / 266), "(MaStR 266 MW) | model", round(wind.sum() / max(caps(["onwind"]), 1e-9)))
print("PV full-load hours:   EM", round(r["Photovoltaik [MWh]"].sum() / 79), "(MaStR 79 MW) | model", round(solar.sum() / max(caps(["solar", "solar rooftop", "solar-hsat"]), 1e-9)))

print("\nModel LG electricity use by carrier (low-voltage + AC) [GWh]:")
print(cons_by[cons_by > 1].round(1).to_string())

M = pd.DataFrame({
    "wind EM": em_mon(r["Windkraft [MWh]"]), "wind model": mo(wind),
    "PV EM": em_mon(r["Photovoltaik [MWh]"]), "PV model": mo(solar),
    "cons EM": em_mon(em_cons), "cons model": mo(model_cons_total),
}).round(1)
print("\nMonthly [GWh]:"); print(M.T.to_string())
print("\ncorr monthly wind", round(M["wind EM"].corr(M["wind model"]), 2), "| PV", round(M["PV EM"].corr(M["PV model"]), 2), "| cons", round(M["cons EM"].corr(M["cons model"]), 2))
