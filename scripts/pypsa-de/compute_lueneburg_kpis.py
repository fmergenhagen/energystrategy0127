"""Step 9 — KPIs for Lüneburg S0 vs S1.

WHY
---
KPIs = the few numbers that answer the study RQ:
  S0 = 2025 today; S1 = 2040 no fossil heating.
  What happens to electricity demand, generation, and storage?
We do NOT run the full Ariadne export (it filters like 'DE' and misses N01AC…).

WHAT
----
Compare primarily **S0@2025 vs S1@2040**.
1. Demand — exogenous el is for *today* only (≈521 GWh at S0);
             S1 adds endogenous electricity-for-heat (HP/RH); EV load separate
2. Capacity — RE / HP / boilers on LG
3. Storage — battery + heat/TES (water tanks etc.)
4. Imports — net el from DE-restAC → LG (abroad imports off in v1)
5. Cost / CO2 — system supporting
6. Baselines — Energiemonitor + MaStR

USAGE (after solve)
-------------------
  cd pypsa-de
  pixi run python scripts/pypsa-de/compute_lueneburg_kpis.py \
    --s0 results/lueneburg/S0/networks/solved_2040.nc \
    --s1 results/lueneburg/S1/networks/solved_2040.nc

Falls back to composed_*.nc if solved missing (partial KPIs only).
Writes data-lueneburg/kpis/lueneburg_kpis.csv and data-lueneburg/kpis/lueneburg_kpis.md
"""

import argparse
import logging
from pathlib import Path

import pandas as pd
import pypsa

logger = logging.getLogger(__name__)

PYPSA = Path(__file__).resolve().parents[2]
DATA = PYPSA / "data-lueneburg"
OUT_DIR = DATA / "kpis"
LG = [f"N{i:02d}AC" for i in range(1, 12)]
REST = "DE-restAC"

HEAT_EL = ("heat pump", "resistive heater")
CAP_CARRIERS = {
    "onwind": ("onwind",),
    "solar": ("solar", "solar rooftop"),
    "heat_pump": ("heat pump",),
    "boiler_fossil": ("gas boiler", "oil boiler"),
}
# Heat/thermal storage + battery (carrier name fragments)
STORAGE_CARRIERS = {
    "battery": ("battery",),
    "heat_tes": ("water tanks", "thermal", "heat storage", "ptes"),
}
EV_LOAD_CARRIERS = ("land transport EV", "EV", "bev")


def _is_solved(n: pypsa.Network) -> bool:
    try:
        return bool(getattr(n, "objective", None) is not None) or (
            hasattr(n, "generators_t")
            and "p" in n.generators_t
            and not n.generators_t.p.empty
        )
    except Exception:
        return False


def _lg_mask_buses(n: pypsa.Network) -> pd.Index:
    """AC bus names for Lüneburg nodes present in the network."""
    return n.buses.index.intersection(LG)


def _location(n: pypsa.Network, names: pd.Index) -> pd.Series:
    """Map component index → AC location (bus or buses.location)."""
    if "location" in n.buses.columns:
        # heat buses often named "{N01AC} urban central heat" — strip via location
        return names.map(
            lambda x: n.buses.at[x, "location"]
            if x in n.buses.index and pd.notna(n.buses.at[x, "location"])
            else str(x).split(" ")[0]
        )
    return names.map(lambda x: str(x).split(" ")[0])


def _annual_mwh(series: pd.Series, n: pypsa.Network) -> float:
    w = n.snapshot_weightings.generators
    if series.empty:
        return 0.0
    return float((series.mul(w, axis=0)).sum().sum()) if isinstance(series, pd.DataFrame) else float(
        (series * w).sum()
    )


def kpi_rows(scenario: str, horizon: str, path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="LG",
                kpi_family="meta",
                kpi="network_missing",
                carrier="",
                unit="-",
                value=1,
                source=str(path),
            )
        )
        return rows

    n = pypsa.Network(path)
    solved = _is_solved(n)
    rows.append(
        dict(
            scenario=scenario,
            horizon=horizon,
            region="meta",
            kpi_family="meta",
            kpi="solved",
            carrier="",
            unit="bool",
            value=int(solved),
            source=str(path),
        )
    )

    lg_buses = _lg_mask_buses(n)

    # --- 1. exogenous electricity demand on LG ---
    el_loads = n.loads.index[
        (n.loads.carrier == "electricity") & (n.loads.bus.isin(lg_buses))
    ]
    if len(el_loads) and "p_set" in n.loads_t:
        gwh = _annual_mwh(n.loads_t.p_set[el_loads], n) / 1e3
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="LG",
                kpi_family="demand",
                kpi="electricity_exogenous",
                carrier="electricity",
                unit="GWh",
                value=round(gwh, 3),
                source="loads_t.p_set",
            )
        )

    if not solved:
        # prenetwork: installed p_nom only
        for family, needles in CAP_CARRIERS.items():
            if family in ("heat_pump", "boiler_fossil"):
                comps = n.links
                idx = comps.index[
                    comps.carrier.astype(str).apply(
                        lambda c: any(k in c for k in needles)
                    )
                    & comps.bus0.map(lambda b: str(b).split(" ")[0]).isin(LG)
                ]
                mw = float(comps.loc[idx, "p_nom"].fillna(0).sum()) if len(idx) else 0.0
            else:
                comps = n.generators
                idx = comps.index[
                    comps.carrier.isin(needles) & comps.bus.isin(lg_buses)
                ]
                mw = float(comps.loc[idx, "p_nom"].fillna(0).sum()) if len(idx) else 0.0
            rows.append(
                dict(
                    scenario=scenario,
                    horizon=horizon,
                    region="LG",
                    kpi_family="capacity",
                    kpi="installed",
                    carrier=family,
                    unit="MW",
                    value=round(mw, 3),
                    source="p_nom",
                )
            )
        return rows

    # --- 1b. heat-driven electricity (RQ core) ---
    try:
        wd = n.statistics.withdrawal(
            bus_carrier=["low voltage", "AC"],
            groupby=["bus", "carrier"],
            nice_names=False,
        )
        # wd index often MultiIndex (component, bus, carrier) or (bus, carrier)
        if isinstance(wd.index, pd.MultiIndex):
            # find bus / carrier levels
            names = list(wd.index.names)
            # flatten to DataFrame
            s = wd.copy()
            if "bus" in names and "carrier" in names:
                df = s.reset_index()
                bus_col, car_col, val_col = "bus", "carrier", s.name or 0
                if val_col not in df.columns:
                    df = s.rename("value").reset_index()
                    val_col = "value"
            else:
                df = s.rename("value").reset_index()
                # heuristic: last two levels
                cols = [c for c in df.columns if c != "value"]
                bus_col, car_col, val_col = cols[-2], cols[-1], "value"
        else:
            df = wd.rename("value").reset_index()
            bus_col, car_col, val_col = df.columns[0], df.columns[1], "value"

        loc = df[bus_col].astype(str).map(lambda b: b.split(" ")[0])
        heat = df[
            loc.isin(LG)
            & df[car_col].astype(str).apply(lambda c: any(k in c for k in HEAT_EL))
        ]
        gwh = float(heat[val_col].sum()) / 1e3
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="LG",
                kpi_family="demand",
                kpi="electricity_for_heat",
                carrier="heat_pump+resistive",
                unit="GWh",
                value=round(gwh, 3),
                source="statistics.withdrawal",
            )
        )
    except Exception as e:
        logger.warning("heat-el KPI failed: %s", e)

    # --- 2. capacities ---
    try:
        opt = n.statistics.optimal_capacity(groupby=["bus", "carrier"], nice_names=False)
        ins = n.statistics.installed_capacity(
            groupby=["bus", "carrier"], nice_names=False
        )

        def _sum_cap(stat, family, needles) -> float:
            s = stat.rename("value") if not isinstance(stat, pd.Series) else stat
            df = s.rename("value").reset_index()
            cols = [c for c in df.columns if c != "value"]
            bus_col, car_col = cols[-2], cols[-1]
            loc = df[bus_col].astype(str).map(lambda b: b.split(" ")[0])
            m = loc.isin(LG) & df[car_col].astype(str).apply(
                lambda c: any(k in c for k in needles)
            )
            return float(df.loc[m, "value"].sum())

        for family, needles in CAP_CARRIERS.items():
            o = _sum_cap(opt, family, needles)
            i = _sum_cap(ins, family, needles)
            for kpi, val in (("installed", i), ("optimal", o), ("new", o - i)):
                rows.append(
                    dict(
                        scenario=scenario,
                        horizon=horizon,
                        region="LG",
                        kpi_family="capacity",
                        kpi=kpi,
                        carrier=family,
                        unit="MW",
                        value=round(val, 3),
                        source="statistics",
                    )
                )
    except Exception as e:
        logger.warning("capacity KPI failed: %s", e)

    # --- 2b. storage (battery + heat/TES) ---
    try:
        for store_kind, needles in STORAGE_CARRIERS.items():
            # stores
            if len(n.stores):
                idx = n.stores.index[
                    n.stores.carrier.astype(str).apply(
                        lambda c: any(k in c for k in needles)
                    )
                    & n.stores.bus.map(lambda b: str(b).split(" ")[0]).isin(LG)
                ]
                if len(idx):
                    e = float(n.stores.loc[idx, "e_nom_opt"].fillna(
                        n.stores.loc[idx, "e_nom"]
                    ).sum()) if "e_nom_opt" in n.stores.columns else float(
                        n.stores.loc[idx, "e_nom"].fillna(0).sum()
                    )
                    rows.append(
                        dict(
                            scenario=scenario,
                            horizon=horizon,
                            region="LG",
                            kpi_family="storage",
                            kpi="energy_capacity",
                            carrier=store_kind,
                            unit="MWh",
                            value=round(e, 3),
                            source="stores",
                        )
                    )
            # battery often as storage_units
            if len(n.storage_units):
                idx = n.storage_units.index[
                    n.storage_units.carrier.astype(str).apply(
                        lambda c: any(k in c for k in needles)
                    )
                    & n.storage_units.bus.isin(lg_buses)
                ]
                if len(idx):
                    col = "p_nom_opt" if "p_nom_opt" in n.storage_units.columns else "p_nom"
                    mw = float(n.storage_units.loc[idx, col].fillna(0).sum())
                    rows.append(
                        dict(
                            scenario=scenario,
                            horizon=horizon,
                            region="LG",
                            kpi_family="storage",
                            kpi="power_capacity",
                            carrier=store_kind,
                            unit="MW",
                            value=round(mw, 3),
                            source="storage_units",
                        )
                    )
    except Exception as e:
        logger.warning("storage KPI failed: %s", e)

    # --- 2c. EV load (separate from heat RQ) ---
    try:
        ev_loads = n.loads.index[
            n.loads.carrier.astype(str).apply(
                lambda c: any(k.lower() in c.lower() for k in EV_LOAD_CARRIERS)
            )
            & n.loads.bus.map(lambda b: str(b).split(" ")[0]).isin(LG)
        ]
        if len(ev_loads) and "p_set" in n.loads_t:
            gwh = _annual_mwh(n.loads_t.p_set[ev_loads], n) / 1e3
            rows.append(
                dict(
                    scenario=scenario,
                    horizon=horizon,
                    region="LG",
                    kpi_family="demand",
                    kpi="electricity_ev",
                    carrier="land transport EV",
                    unit="GWh",
                    value=round(gwh, 3),
                    source="loads_t.p_set",
                )
            )
    except Exception as e:
        logger.warning("EV KPI failed: %s", e)

    # --- 2d. Imports: net flow DE-restAC → LG (positive = LG imports) ---
    try:
        w = n.snapshot_weightings.generators
        net_mwh = 0.0
        peak_mw = 0.0
        # AC lines between DE-restAC and an N**AC bus
        if len(n.lines) and "p0" in n.lines_t:
            for i, row in n.lines.iterrows():
                b0, b1 = str(row.bus0).split(" ")[0], str(row.bus1).split(" ")[0]
                if not ((b0 == REST and b1 in LG) or (b1 == REST and b0 in LG)):
                    continue
                p0 = n.lines_t.p0[i]
                # p0 > 0 = flow bus0→bus1; import to LG = flow into LG
                flow_into_lg = p0 if b1 in LG else -p0
                net_mwh += float((flow_into_lg * w).sum())
                peak_mw = max(peak_mw, float(flow_into_lg.max()))
        # DC / other links on AC carrier if any
        if len(n.links) and "p0" in n.links_t:
            ac_links = n.links.index[
                n.links.carrier.astype(str).isin(["DC", "AC", "HVDC"])
                | n.links.carrier.astype(str).str.contains("transmission", case=False)
            ]
            for i in ac_links:
                b0 = str(n.links.at[i, "bus0"]).split(" ")[0]
                b1 = str(n.links.at[i, "bus1"]).split(" ")[0]
                if not ((b0 == REST and b1 in LG) or (b1 == REST and b0 in LG)):
                    continue
                p0 = n.links_t.p0[i]
                flow_into_lg = p0 if b1 in LG else -p0
                net_mwh += float((flow_into_lg * w).sum())
                peak_mw = max(peak_mw, float(flow_into_lg.max()))
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="LG",
                kpi_family="imports",
                kpi="net_electricity_from_DE_rest",
                carrier="AC",
                unit="GWh",
                value=round(net_mwh / 1e3, 3),
                source="lines_t/links_t vs DE-restAC",
            )
        )
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="LG",
                kpi_family="imports",
                kpi="peak_import_from_DE_rest",
                carrier="AC",
                unit="MW",
                value=round(peak_mw, 3),
                source="lines_t/links_t vs DE-restAC",
            )
        )
    except Exception as e:
        logger.warning("import KPI failed: %s", e)

    # --- 3. cost ---
    try:
        obj = float(n.objective) if n.objective is not None else float("nan")
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="system",
                kpi_family="cost",
                kpi="objective",
                carrier="",
                unit="EUR",
                value=obj,
                source="n.objective",
            )
        )
        ann = float(n.statistics.capex().sum() + n.statistics.opex().sum())
        rows.append(
            dict(
                scenario=scenario,
                horizon=horizon,
                region="system",
                kpi_family="cost",
                kpi="annualised_capex_opex",
                carrier="",
                unit="EUR/a",
                value=ann,
                source="statistics",
            )
        )
    except Exception as e:
        logger.warning("cost KPI failed: %s", e)

    # --- 4. CO2 (system, if co2 atmosphere present) ---
    try:
        if "co2 atmosphere" in n.buses.index:
            # links dumping to co2 atmosphere
            ports = [
                c
                for c in n.links.columns
                if c.startswith("bus") and c != "bus"
            ]
            co2 = 0.0
            for p in ["bus2", "bus3", "bus4"]:
                if p not in n.links.columns:
                    continue
                idx = n.links.index[n.links[p] == "co2 atmosphere"]
                if len(idx) and "p2" in n.links_t:
                    # emissions ≈ -p2 * weighting when bus2 is atmosphere (sign convention varies)
                    pass
            rows.append(
                dict(
                    scenario=scenario,
                    horizon=horizon,
                    region="system",
                    kpi_family="co2",
                    kpi="note",
                    carrier="",
                    unit="-",
                    value=0,
                    source="use statistics.energy_balance / CO2Limit in post",
                )
            )
    except Exception as e:
        logger.warning("co2 KPI failed: %s", e)

    return rows


def write_note(df: pd.DataFrame, path: Path) -> None:
    lines = [
        "# Lüneburg KPIs (S0 vs S1)",
        "",
        "**KPI** = key performance indicator: numbers that answer the RQ.",
        "",
        "## Framing",
        "- **S0 = 2025 today**; **S1 = 2040 no fossil heating**",
        "- Exogenous 521 GWh electricity = **today only** (not the full 2040 demand)",
        "- 2040 heat-electricity is endogenous (HP / resistive heaters)",
        "",
        "## Families",
        "1. **demand** — today exogenous el; S1 electricity-for-heat; EV load (separate)",
        "2. **capacity** — RE / HP / boilers on N01–N11",
        "3. **storage** — battery + heat/TES (water tanks / thermal stores)",
        "4. **imports** — net electricity LG←DE-rest (no abroad imports in v1)",
        "5. **cost / co2** — system supporting metrics",
        "6. **baselines** — Energiemonitor 521 GWh; MaStR wind≈266 / solar≈79 MW",
        "",
        "## EVs",
        "On by default (DE transport pathway → more EVs by 2040).",
        "They raise electricity demand independently of the heat RQ.",
        "Report as own KPI, or freeze EV share at 2025 for a heat-only run.",
        "",
        "## Imports",
        "Abroad: off (`countries: [DE]`, `sector.imports.enable: false`).",
        "Landkreis←DE-rest: yes — net GWh and peak MW over lines/links to DE-restAC.",
        "",
        "## Compare",
        "Primary: **S0@2025 vs S1@2040**.",
        "",
    ]
    if df.empty or df["kpi"].eq("network_missing").all():
        lines += [
            "> No solved networks found yet. Run S0/S1 solve, then re-run this script.",
            "",
            "```bash",
            "pixi run snakemake -call \\",
            "  results/lueneburg/S0/networks/solved_2025.nc \\",
            "  results/lueneburg/S1/networks/solved_2040.nc \\",
            "  --configfile=config/config.lueneburg.yaml",
            "```",
            "",
        ]
    else:
        pivot = df[df.region.isin(["LG", "system"])][
            ["scenario", "horizon", "kpi_family", "kpi", "carrier", "unit", "value"]
        ]
        lines += ["## Results", "", "```", pivot.to_string(index=False), "```", ""]
    path.write_text("\n".join(lines))


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser()
    # WHY: S0 reports 2025 (today); S1 reports 2040 (no fossil heating).
    p.add_argument("--s0-horizon", default="2025")
    p.add_argument("--s1-horizon", default="2040")
    p.add_argument(
        "--s0",
        default=str(PYPSA / "results/lueneburg/S0/networks/solved_{h}.nc"),
    )
    p.add_argument(
        "--s1",
        default=str(PYPSA / "results/lueneburg/S1/networks/solved_{h}.nc"),
    )
    args = p.parse_args(argv)

    def resolve(template: str, scenario: str, horizon: str) -> Path:
        path = Path(template.format(h=horizon))
        if path.exists():
            return path
        fb = PYPSA / f"resources/lueneburg/{scenario}/networks/composed_{horizon}.nc"
        return fb if fb.exists() else path

    rows = []
    for sc, tmpl, h in (
        ("S0", args.s0, args.s0_horizon),
        ("S1", args.s1, args.s1_horizon),
    ):
        path = resolve(tmpl, sc, h)
        logger.info("KPIs for %s@%s ← %s", sc, h, path)
        rows.extend(kpi_rows(sc, h, path))

    # baselines (static)
    rows.append(
        dict(
            scenario="baseline",
            horizon="2025",
            region="LG",
            kpi_family="baseline",
            kpi="energiemonitor_verbrauch",
            carrier="electricity",
            unit="GWh",
            value=521.0,
            source="data-lueneburg/Electricity/energiemonitor",
        )
    )
    caps = DATA / "Electricity" / "lueneburg_existing_capacity.csv"
    if caps.exists():
        c = pd.read_csv(caps)
        for col, name in (
            ("onwind_MW", "onwind"),
            ("solar_MW", "solar"),
            ("biomass_MW", "biomass"),
        ):
            rows.append(
                dict(
                    scenario="baseline",
                    horizon="2025",
                    region="LG",
                    kpi_family="baseline",
                    kpi="mastr_installed",
                    carrier=name,
                    unit="MW",
                    value=round(float(c[col].sum()), 3),
                    source=str(caps),
                )
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    csv_path = OUT_DIR / "lueneburg_kpis.csv"
    md_path = OUT_DIR / "lueneburg_kpis.md"
    df.to_csv(csv_path, index=False)
    write_note(df, md_path)
    print(f"wrote {csv_path} ({len(df)} rows) and {md_path}")


if __name__ == "__main__":
    # No __future__ needed at runtime for snakemake; keep annotations for clarity locally.
    main()
