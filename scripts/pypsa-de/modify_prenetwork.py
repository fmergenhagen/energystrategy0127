import importlib.util
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pypsa
from shapely.geometry import Point

from scripts._helpers import (
    name_country,
    sanitize_custom_columns,
)
from scripts.prepare_sector_network import lossy_bidirectional_links

logger = logging.getLogger(__name__)


def first_technology_occurrence(n, technology_occurrence, current_horizon):
    """
    Drop configured technologies before configured year.
    """

    for c, carriers in technology_occurrence.items():
        for carrier, first_year in carriers.items():
            if current_horizon < first_year:
                to_drop = n.df(c).query(f"carrier == '{carrier}'").index
                if to_drop.empty:
                    continue
                n.remove(c, to_drop)


def fix_new_boiler_profiles(n):
    logger.info("Forcing boiler profiles for new ones")

    decentral_boilers = n.links.index[
        n.links.carrier.str.contains("boiler")
        & ~n.links.carrier.str.contains("urban central")
        & n.links.p_nom_extendable
    ]

    if decentral_boilers.empty:
        return

    boiler_loads = n.links.loc[decentral_boilers, "bus1"]
    boiler_loads = boiler_loads[boiler_loads.isin(n.loads_t.p_set.columns)]
    decentral_boilers = boiler_loads.index
    boiler_profiles_pu = n.loads_t.p_set[boiler_loads].div(
        n.loads_t.p_set[boiler_loads].max(), axis=1
    )
    boiler_profiles_pu.columns = decentral_boilers

    for attr in ["p_min_pu", "p_max_pu"]:
        n.links_t[attr] = pd.concat([n.links_t[attr], boiler_profiles_pu], axis=1)


def remove_old_boiler_profiles(n):
    """
    Removed because this is handled in additional_functionality.

    This removes p_min/max_pu constraints added in previous years and
    carried over by add_brownfield.
    """

    logger.info("Removing p_min/max_pu constraints on old boiler profiles")

    decentral_boilers = n.links.index[
        n.links.carrier.str.contains("boiler")
        & ~n.links.carrier.str.contains("urban central")
        & ~n.links.p_nom_extendable
    ]

    for attr in ["p_min_pu", "p_max_pu"]:
        to_drop = decentral_boilers.intersection(n.links_t[attr].columns)
        logger.info(f"Dropping {to_drop} from n.links_t.{attr}")
        n.links_t[attr].drop(to_drop, axis=1, inplace=True)


def _links_in_country(n, ct: str) -> pd.Series:
    """Boolean mask: link name belongs to country (N**AC counts as DE).

    ct == "LG" selects only the Landkreis Lüneburg nodes N01AC…N11AC, so
    heat bans can be scoped to the study region while DE-rest follows the
    national pathway.
    """
    if ct == "LG":
        return n.links.index.str.match(LG_NODE_RE)
    return name_country(n.links.index) == ct


def _drop_links(n, links) -> None:
    """Drop links and any matching time-series columns."""
    links = pd.Index(links).intersection(n.links.index)
    if links.empty:
        return
    logger.info("Dropping %s links: %s", len(links), list(links))
    n.links.drop(links, inplace=True)
    for attr in list(getattr(n, "links_t", {})):
        cols = links.intersection(n.links_t[attr].columns)
        if len(cols):
            n.links_t[attr].drop(cols, axis=1, inplace=True)


def new_boiler_ban(n, fossil_boiler_ban, year):

    for ct in fossil_boiler_ban:
        ban_year = int(fossil_boiler_ban[ct])
        if ban_year < year:
            logger.info(
                f"For year {year} in {ct} implementing ban on new decentral oil & gas boilers from {ban_year}"
            )
            links = n.links.index[
                _links_in_country(n, ct)
                & (
                    n.links.index.str.contains("gas boiler")
                    ^ n.links.index.str.contains("oil boiler")
                )
                & n.links.p_nom_extendable
                & ~n.links.index.str.contains("urban central")
            ]
            _drop_links(n, links)


def absolute_fossil_heat_ban(n, fossil_heat_ban, year):
    """Remove all fossil heat supply (boilers + fossil CHP), not only new decentral.

    Active when ban_year < planning horizon (same convention as new_boiler_ban).
    Keeps non-fossil heat: HPs, resistive, biomass/H2/waste CHP, geothermal, waste heat.
    """
    if not fossil_heat_ban:
        return

    # carrier patterns: gas/oil boilers anywhere; fossil CHP (gas/oil/coal/lignite).
    # Exclude biogas/biomass/H2 (substring "gas CHP" would otherwise hit "biogas CHP").
    boiler = n.links.carrier.str.contains(
        r"(?:gas|oil) boiler", case=False, na=False, regex=True
    )
    fossil_chp = n.links.carrier.str.contains(
        r"(?:gas|oil|coal|lignite) CHP", case=False, na=False, regex=True
    ) & ~n.links.carrier.str.contains(
        r"biogas|biomass|H2", case=False, na=False, regex=True
    )
    is_fossil_heat = boiler | fossil_chp

    for ct, ban_year in fossil_heat_ban.items():
        if int(ban_year) >= year:
            continue
        logger.info(
            "For year %s in %s implementing ABSOLUTE fossil heat ban from %s",
            year,
            ct,
            ban_year,
        )
        links = n.links.index[_links_in_country(n, ct) & is_fossil_heat]
        _drop_links(n, links)


def _as_dict(cfg) -> dict:
    if isinstance(cfg, dict):
        return cfg
    return dict(cfg) if cfg else {}


def _wastewater_heat_pump_cop(n, inputs, params, node: str, source_temp_c: float):
    """Daily-resolved COP of a DH heat pump on treated wastewater at ``node``.

    Uses the stock CentralHeatingCopApproximator with the node's DH forward /
    return temperatures and the stock approximation settings, so the Klärwerk
    heat pump is consistent with the model's other central heat pumps.
    """
    import xarray as xr

    from scripts.build_cop_profiles.central_heating_cop_approximator import (
        CentralHeatingCopApproximator,
    )

    dh = params.sector["district_heating"]
    approx = dh["heat_pump_cop_approximation"]
    forward = xr.open_dataarray(
        inputs.central_heating_forward_temperature_profiles
    ).sel(name=node)
    ret = xr.open_dataarray(inputs.central_heating_return_temperature_profiles).sel(
        name=node
    )
    cop = CentralHeatingCopApproximator(
        sink_outlet_temperature_celsius=forward.values,
        sink_inlet_temperature_celsius=ret.values,
        source_inlet_temperature_celsius=source_temp_c,
        source_outlet_temperature_celsius=source_temp_c - dh["heat_source_cooling"],
        refrigerant=approx["refrigerant"],
        delta_t_pinch_point=approx["heat_exchanger_pinch_point_temperature_difference"],
        isentropic_compressor_efficiency=approx["isentropic_compressor_efficiency"],
        heat_loss=approx["heat_loss"],
        min_delta_t_lift=approx["min_delta_t_lift"],
    ).cop
    cop = pd.Series(np.asarray(cop, dtype=float), index=pd.DatetimeIndex(forward.time.values))
    # aggregate hourly COP onto the (possibly temporally clustered) snapshots
    pos = n.snapshots.searchsorted(cop.index, side="right") - 1
    cop = cop[pos >= 0].groupby(n.snapshots[pos[pos >= 0]]).mean()
    return cop.reindex(n.snapshots).ffill().bfill()


def add_lueneburg_wastewater_waste_heat(n, lueneburg_cfg, year, inputs, params):
    """Klärwerk Lüneburg: committed wastewater heat pump into N01 district heating.

    Two Großwärmepumpen of 5.2 MW Heizleistung each (Pressemitteilung Hansestadt
    Lüneburg, 25.08.2026), operation from early 2030. Modelled as a fixed
    (non-extendable, investment sunk) heat pump link with the stock heat pump
    sign convention: p_nom is heat output on bus0, electricity is drawn from
    bus1 with efficiency = 1/COP. COP follows the node's DH forward/return
    temperatures and a constant treated-wastewater source temperature.
    Enabled only when lueneburg.wastewater_waste_heat.enable (S1 scenario).
    """
    wh = _as_dict(_as_dict(lueneburg_cfg).get("wastewater_waste_heat"))
    if not wh.get("enable"):
        return
    start = int(wh.get("start_year", 2030))
    if year < start:
        logger.info("Skipping wastewater heat pump in %s (starts %s)", year, start)
        return

    bus_el = str(wh.get("bus", "N01AC"))
    heat_bus = str(wh.get("heat_bus", f"{bus_el} urban central heat"))
    if heat_bus not in n.buses.index:
        logger.warning("Wastewater heat pump: heat bus %s missing — skip", heat_bus)
        return

    p_nom = float(wh.get("p_nom_mw", 10.4))
    source_temp = float(wh.get("source_temperature_celsius", 15.0))
    cop = _wastewater_heat_pump_cop(n, inputs, params, bus_el, source_temp)

    carrier = "wastewater heat pump"
    if carrier not in n.carriers.index:
        n.add("Carrier", carrier)
    name = str(wh.get("name", f"{bus_el} {carrier}"))
    n.add(
        "Link",
        name,
        bus0=heat_bus,
        bus1=bus_el,
        carrier=carrier,
        p_nom=p_nom,
        p_nom_extendable=False,
        p_min_pu=-1.0,
        p_max_pu=0.0,
        efficiency=1.0 / cop.clip(lower=0.001),
        build_year=start,
    )
    logger.info(
        "Added %s: %.1f MW_th → %s, source %.0f °C, COP mean %.2f (min %.2f, max %.2f)",
        name,
        p_nom,
        heat_bus,
        source_temp,
        cop.mean(),
        cop.min(),
        cop.max(),
    )


LG_NODE_RE = r"^N\d{2}AC"


def connect_lueneburg_nodes_to_de_rest(n, lueneburg_cfg, hub: str = "DE-restAC"):
    """Give every Lüneburg node (N01AC…N11AC) its own AC line to copperplate DE-rest.

    WHY: the clustered grid only links DE-rest to N01AC, so all trade of the
    other Gemeinden had to pass through Hansestadt. New lines copy the existing
    DE-rest–N** line (type, length, s_max_pu, capital cost, extension headroom),
    start at ``lueneburg.s_nom`` MW and are extendable like it.
    Must run before brownfield so line names match across horizons.
    """
    cfg = _as_dict(lueneburg_cfg)
    if not (cfg.get("enable") and cfg.get("connect_all_to_de_rest")):
        return
    if hub not in n.buses.index:
        logger.warning("connect_all_to_de_rest: hub bus %s missing — skip", hub)
        return

    lg = n.buses.index[n.buses.index.str.match(LG_NODE_RE + "$")]
    to_hub = n.lines.bus0.eq(hub) & n.lines.bus1.isin(lg)
    from_hub = n.lines.bus1.eq(hub) & n.lines.bus0.isin(lg)
    if not (to_hub | from_hub).any():
        logger.warning("connect_all_to_de_rest: no DE-rest–N** template line — skip")
        return
    connected = set(n.lines.loc[to_hub, "bus1"]) | set(n.lines.loc[from_hub, "bus0"])
    new_buses = [b for b in lg if b not in connected]
    if not new_buses:
        return

    template = n.lines.loc[(to_hub | from_hub)].iloc[0]
    s_nom = float(cfg.get("s_nom", 200.0))
    scale = s_nom / template.s_nom if template.s_nom > 0 else 1.0
    attrs = n.component_attrs["Line"]
    inputs = attrs.index[attrs.status.str.startswith("Input")].intersection(
        n.lines.columns
    )
    rows = pd.DataFrame(
        [template[inputs]] * len(new_buses),
        index=pd.Index([f"{hub}-{b}" for b in new_buses], name="Line"),
    )
    rows["bus0"] = hub
    rows["bus1"] = new_buses
    rows["s_nom"] = s_nom
    rows["s_nom_min"] = s_nom
    rows["s_nom_max"] = s_nom + (template.s_nom_max - template.s_nom)
    rows["num_parallel"] = template.num_parallel * scale
    # electrical parameters of `scale` x the template's parallel circuits
    rows["x"] = template.x / scale
    rows["r"] = template.r / scale
    n.add("Line", rows.index, **rows)
    logger.info(
        "Connected %s Lüneburg nodes directly to %s (%.0f MW each, extendable=%s): %s",
        len(new_buses),
        hub,
        s_nom,
        bool(template.s_nom_extendable),
        new_buses,
    )


def ban_new_fossil_power_lueneburg(n, lueneburg_cfg):
    """S1: new power capacity in Landkreis Lüneburg (N01AC…N11AC) only renewable.

    Drops extendable fossil-fuelled power plants and CHPs (gas/oil/coal/lignite;
    CCGT/OCGT) at N** nodes. Existing (brownfield, non-extendable) plants stay.
    Biomass/biogas/waste and H2-fuelled units are kept. DE-rest is untouched and
    follows the national pathway (incl. its CO2 budget).
    """
    if not _as_dict(lueneburg_cfg).get("renewables_only_new_power"):
        return
    carrier = n.links.carrier
    fossil_power = carrier.isin(["CCGT", "OCGT", "coal", "lignite", "oil"]) | (
        carrier.str.contains(r"(?:gas|oil|coal|lignite) CHP", regex=True, na=False)
        & ~carrier.str.contains(r"biogas|biomass|H2", regex=True, na=False)
    )
    links = n.links.index[
        n.links.index.str.match(LG_NODE_RE) & fossil_power & n.links.p_nom_extendable
    ]
    logger.info("Lüneburg renewables-only: dropping %s new fossil power links", len(links))
    _drop_links(n, links)


LG_SYSTEM_STORE_CARRIERS = ["battery", "H2 Store"]
LG_SYSTEM_LINK_CARRIERS = [
    "battery charger",
    "H2 Electrolysis",
    "H2 Fuel Cell",
    "H2 OCGT",
    "H2 CCGT",
    "methanolisation",
    "Fischer-Tropsch",
    "Sabatier",
    "electrobiofuels",
]


def surcharge_lueneburg_system_assets(n, lueneburg_cfg):
    """Tie-breaker: small capex surcharge on system-scale storage/PtX at N01–N11.

    WHY: Lüneburg and DE-rest are joined by rarely congested lines, so prices
    are equal (S1_4h 2040: mean |ΔP| N01 vs DE-rest = 0 EUR/MWh) and a grid
    battery, H2 store or electrolyser in the Landkreis is worth exactly as much
    as one in DE-rest at identical cost. That LP is degenerate; Gurobi barrier
    without crossover then spreads such assets across equal-cost nodes (4h run:
    52 GWh of grid batteries in LG). A surcharge of a few per mille to 1 %
    makes the model site them in the Landkreis only when there is a local reason
    (congested connection, local surplus). Home batteries, heat storage, heat
    pumps and the wastewater heat pump are not affected.
    """
    eps = float(_as_dict(lueneburg_cfg).get("system_asset_surcharge", 0.0) or 0.0)
    if eps <= 0:
        return
    st = n.stores
    stores = st.index[
        st.bus.str.match(LG_NODE_RE)
        & st.carrier.isin(LG_SYSTEM_STORE_CARRIERS)
        & st.e_nom_extendable
    ]
    n.stores.loc[stores, "capital_cost"] *= 1 + eps
    lk = n.links
    links = lk.index[
        lk.index.str.match(LG_NODE_RE)
        & lk.carrier.isin(LG_SYSTEM_LINK_CARRIERS)
        & lk.p_nom_extendable
    ]
    n.links.loc[links, "capital_cost"] *= 1 + eps
    logger.info(
        "Lüneburg tie-breaker: +%.1f%% capex on %s stores, %s links (system storage/PtX)",
        eps * 100,
        len(stores),
        len(links),
    )


LG_RENEWABLE_GENERATORS = ["onwind", "solar", "solar rooftop", "solar-hsat", "ror"]
LG_FROZEN_LINK_CARRIERS = [
    "CCGT",
    "OCGT",
    "H2 OCGT",
    "H2 CCGT",
    "oil",
    "coal",
    "lignite",
    "nuclear",
    "H2 Fuel Cell",
    "H2 Electrolysis",
    "methanolisation",
    "Fischer-Tropsch",
    "Sabatier",
    "electrobiofuels",
    "biomass to liquid",
    "biomass to liquid CC",
]


def freeze_lueneburg_base_year(n, lueneburg_cfg):
    """Base year: no new power/PtX capacity on N01AC…N11AC (Lüneburg = MaStR + stock).

    WHY: once N** count as DE, national minimum targets (e.g. 67 GW onwind in
    2025) can be met cheaply in the Landkreis, so the optimiser built ~1.6 GW
    of "2025" wind there. The base year should reproduce today's Landkreis.
    Freezes extendable renewable generators, power plants, all CHPs and PtX
    at N** nodes, plus new heat pumps and resistive heaters (otherwise the
    optimiser replaced existing oil boilers with ~120 MW of new direct electric
    heating in "2025"). Boilers stay extendable so heat demand is always met.
    Call only for the first planning horizon, after the MaStR overwrite.
    """
    if not _as_dict(lueneburg_cfg).get("freeze_base_year"):
        return
    g = n.generators
    gens = g.index[
        g.bus.str.match(LG_NODE_RE)
        & g.carrier.isin(LG_RENEWABLE_GENERATORS)
        & g.p_nom_extendable
    ]
    n.generators.loc[gens, "p_nom_extendable"] = False

    carrier = n.links.carrier
    power_or_ptx = carrier.isin(LG_FROZEN_LINK_CARRIERS) | carrier.str.contains(
        "CHP|resistive heater|heat pump", regex=True, na=False
    )
    links = n.links.index[
        n.links.index.str.match(LG_NODE_RE) & power_or_ptx & n.links.p_nom_extendable
    ]
    n.links.loc[links, "p_nom_extendable"] = False
    logger.info(
        "Lüneburg base year frozen: %s generators, %s power/CHP/PtX/HP/resistive links non-extendable",
        len(gens),
        len(links),
    )


LG_ELECTRICITY_LOADS = [
    "electricity",
    "industry electricity",
    "agriculture electricity",
    "agriculture machinery electric",
    "land transport EV",
]


def scale_lueneburg_base_year_electricity(n, lueneburg_cfg):
    """Base year: scale exogenous LG electricity loads to measured consumption.

    WHY: the 521 GWh overlay only sets the residual "electricity" load; PyPSA
    then adds its own industry (NUTS3-distributed), agriculture and EV loads on
    top, giving ~830 GWh exogenous load vs 521 GWh Energiemonitor Verbrauch
    2025. All exogenous electricity loads on N01–N11 are scaled by one common
    factor (split between them is kept). Endogenous heat electricity (existing
    heat pumps / resistive heaters) comes on top of the target and is reported
    in the validation, optionally reserved via base_year_heat_electricity_gwh.
    """
    cfg = _as_dict(lueneburg_cfg)
    target_gwh = cfg.get("base_year_electricity_gwh")
    if not target_gwh:
        return
    target = (float(target_gwh) - float(cfg.get("base_year_heat_electricity_gwh", 0))) * 1e3
    w = n.snapshot_weightings.generators
    loads = n.loads.index[
        n.loads.bus.str.match(LG_NODE_RE) & n.loads.carrier.isin(LG_ELECTRICITY_LOADS)
    ]
    ts = loads.intersection(n.loads_t.p_set.columns)
    static = loads.difference(ts)
    current = (n.loads_t.p_set[ts].mul(w, axis=0).sum().sum()) + (
        n.loads.loc[static, "p_set"].sum() * w.sum()
    )
    if current <= 0:
        return
    factor = target / current
    n.loads_t.p_set[ts] *= factor
    n.loads.loc[static, "p_set"] *= factor
    logger.info(
        "Lüneburg base year: scaled %s electricity loads %.0f → %.0f GWh (factor %.2f)",
        len(loads),
        current / 1e3,
        target / 1e3,
        factor,
    )


def add_lueneburg_existing_batteries(n, lueneburg_cfg):
    """Base year: put MaStR battery stock on N01AC…N11AC home/grid batteries.

    Reads data-lueneburg/Electricity/lueneburg_existing_batteries.csv (inverter MW per node,
    built by data-lueneburg/Electricity/build_lueneburg_existing_batteries.py). Energy =
    MW x assumed hours (MaStR dump has no usable kWh). Sets p_nom/e_nom and the
    *_min bounds on the base-year units; with freeze_base_year they are fixed.
    """
    cfg = _as_dict(lueneburg_cfg)
    bcfg = _as_dict(cfg.get("existing_batteries"))
    if not bcfg.get("enable"):
        return
    path = Path(__file__).resolve().parents[2] / "data-lueneburg" / "Electricity" / "lueneburg_existing_batteries.csv"
    if not path.exists():
        logger.warning("existing_batteries: %s missing — skip", path)
        return
    caps = pd.read_csv(path).set_index("bus")
    freeze = bool(cfg.get("freeze_base_year"))
    kinds = {
        "home battery": ("home_battery_MW", float(bcfg.get("home_battery_hours", 1.7))),
        "battery": ("battery_MW", float(bcfg.get("battery_hours", 2.0))),
    }
    total = {}
    for carrier, (col, hours) in kinds.items():
        for bus, row in caps.iterrows():
            store_bus = f"{bus} {carrier}"
            stores = n.stores.index[(n.stores.bus == store_bus) & (n.stores.carrier == carrier)]
            ch = n.links.index[(n.links.carrier == f"{carrier} charger") & (n.links.bus1 == store_bus)]
            dis = n.links.index[(n.links.carrier == f"{carrier} discharger") & (n.links.bus0 == store_bus)]
            if not (len(stores) and len(ch) and len(dis)):
                continue
            mw = float(row.get(col, 0.0))
            eff = float(n.links.at[dis[0], "efficiency"]) or 1.0
            for s in stores[:1]:
                n.stores.loc[s, ["e_nom", "e_nom_min"]] = mw * hours
                if freeze:
                    n.stores.at[s, "e_nom_extendable"] = False
            for link, p in ((ch[0], mw), (dis[0], mw / eff)):
                n.links.loc[link, ["p_nom", "p_nom_min"]] = p
                if freeze:
                    n.links.at[link, "p_nom_extendable"] = False
            total[carrier] = total.get(carrier, 0.0) + mw
    logger.info(
        "Lüneburg existing batteries (MaStR): %s MW%s",
        {k: round(v, 2) for k, v in total.items()},
        " (fixed, base year frozen)" if freeze else " (as minimum)",
    )


def export_lueneburg_waste_to_de_rest(n, lueneburg_cfg, hub: str = "DE-restAC"):
    """Ship Lüneburg's non-sequestered HVC (plastic waste) to DE-rest incinerators.

    WHY: every node has its own waste bus that must be emptied by a waste CHP or
    "HVC to air". There is no incinerator in the Landkreis, and once N** count
    as DE the national "HVC to air <= 0" limit applies, so without transport the
    model must build waste CHPs in Lüneburg (or is infeasible in the frozen base
    year). Free, unconstrained transport mirrors real waste logistics.
    """
    if not _as_dict(lueneburg_cfg).get("export_waste_to_de_rest"):
        return
    carrier = "HVC transport"
    target = f"{hub} non-sequestered HVC"
    if target not in n.buses.index:
        logger.warning("export_waste_to_de_rest: %s missing — skip", target)
        return
    src = n.buses.index[
        n.buses.index.str.match(LG_NODE_RE + " non-sequestered HVC$")
    ]
    names = src.str.replace(" non-sequestered HVC", f" {carrier}")
    new = ~names.isin(n.links.index)
    if not new.any():
        return
    if carrier not in n.carriers.index:
        n.add("Carrier", carrier)
    n.add(
        "Link",
        names[new],
        bus0=src[new],
        bus1=target,
        carrier=carrier,
        p_nom_extendable=True,
        capital_cost=0.0,
        efficiency=1.0,
    )
    logger.info("Lüneburg waste: %s HVC transport links → %s", int(new.sum()), target)


def coal_generation_ban(n, coal_ban, year):

    for ct in coal_ban:
        ban_year = int(coal_ban[ct])
        if ban_year < year:
            logger.info(
                f"For year {year} in {ct} implementing coal and lignite ban from {ban_year}"
            )
            links = n.links.index[
                (name_country(n.links.index) == ct)
                & n.links.carrier.isin(["coal", "lignite"])
            ]
            logger.info(f"Dropping {links}")
            n.links.drop(links, inplace=True)


def nuclear_generation_ban(n, nuclear_ban, year):

    for ct in nuclear_ban:
        ban_year = int(nuclear_ban[ct])
        if ban_year < year:
            logger.info(
                f"For year {year} in {ct} implementing nuclear ban from {ban_year}"
            )
            links = n.links.index[
                (name_country(n.links.index) == ct) & n.links.carrier.isin(["nuclear"])
            ]
            logger.info(f"Dropping {links}")
            n.links.drop(links, inplace=True)


def restrict_nuclear_capacity_factor(n):
    """
    Restrict nuclear capacity factors to 0.9 for all nuclear generators in the network, because they run baseload and else exceed real annual generation
    """
    idx = n.links.query("carrier == 'nuclear'").index
    n.links.loc[idx, "p_max_pu"] = 0.85


def scale_DE_elec_load_to_AGEB(n):
    idx = n.loads.query("bus.str.contains('DE') and carrier == 'electricity'").index
    AGEB_2025 = 924144 / 3.6e3
    DE_elec_load_2025 = (
        n.loads_t.p_set[idx].mul(n.snapshot_weightings.generators, axis=0).sum().sum()
        / 1e6
    )
    scaling_factor = AGEB_2025 / DE_elec_load_2025
    n.loads_t.p_set[idx] *= scaling_factor


def scale_DE_heat_load(n, DE_factor, EU_factor):
    building_heat_carriers = [
        "urban central heat",
        "urban decentral heat",
        "rural heat",
        "residential urban decentral heat",
        "services urban decentral heat",
        "residential rural heat",
        "services rural heat",
    ]
    heat_scale_factor = (1 - DE_factor) / (1 - EU_factor)
    idx = n.loads.index[
        (name_country(n.loads["bus"]) == "DE")
        & n.loads["carrier"].isin(building_heat_carriers)
    ]
    idx = n.loads_t.p_set.columns.intersection(idx)
    logger.info(
        f"Space heat demand before scaling: {n.loads_t.p_set[idx].mul(n.snapshot_weightings.generators.values, axis=0).sum().sum() / 1e6} TWh"
    )
    n.loads_t.p_set[idx] *= heat_scale_factor
    logger.info(
        f"Space heat demand after scaling: {n.loads_t.p_set[idx].mul(n.snapshot_weightings.generators.values, axis=0).sum().sum() / 1e6} TWh"
    )


def add_reversed_pipes(df):
    df_rev = df.copy().rename({"bus0": "bus1", "bus1": "bus0"}, axis=1)
    df_rev.index = df_rev.index + "-reversed"
    return pd.concat([df, df_rev], sort=False)


def reduce_capacity(
    targets,
    origins,
    carrier,
    origin_attr="removed_gas_cap",
    target_attr="p_nom",
    conversion_rate=1,
):
    """
    Reduce the capacity of pipes in a dataframe based on specified criteria.

    Args:
        target (DataFrame): The dataframe containing pipelines from which to reduce capacitiy.
        origin (DataFrame): The dataframe containing data about added pipelines.
        carrier (str): The carrier of the pipelines.
        origin_attr (str, optional): The column name in `origin` representing the original capacity of the pipelines. Defaults to "removed_gas_cap".
        target_attr (str, optional): The column name in `target` representing the target capacity to be modified. Defaults to "p_nom".
        conversion_rate (float, optional): The conversion rate to reduce the capacity. Defaults to 1.

    Returns:
        DataFrame: The modified dataframe with reduced pipe capacities.
    """

    targets = targets.copy()

    def apply_cut(row):
        match = targets[
            (targets.bus0 == row.bus0 + " " + carrier)
            & (targets.bus1 == row.bus1 + " " + carrier)
        ].sort_index()
        cut = row[origin_attr] * conversion_rate
        for idx, target_row in match.iterrows():
            if cut <= 0:
                break
            target_value = target_row[target_attr]
            reduction = min(target_value, cut)
            targets.at[idx, target_attr] -= reduction
            cut -= reduction

    origins.apply(apply_cut, axis=1)
    return targets


def add_wasserstoff_kernnetz(
    n,
    wkn,
    costs,
    planning_horizons,
    H2_transmission_efficiency,
    H2_retrofit,
    current_horizon,
):
    logger.info("adding wasserstoff kernnetz")

    investment_year = current_horizon

    # get previous planning horizon
    i = planning_horizons.index(current_horizon)
    previous_investment_year = int(planning_horizons[i - 1]) if i != 0 else 2015  # noqa

    # use only pipes added since the previous investment period
    wkn_new = wkn.query(
        "build_year > @previous_investment_year & build_year <= @investment_year"
    )

    if not wkn_new.empty:
        names = wkn_new.index + f"-kernnetz-{investment_year}"

        # capital_costs = np.where(
        #     wkn_new.retrofitted == False,
        #     costs.at["H2 (g) pipeline", "capital_cost"] * wkn_new.length.values,
        #     costs.at["H2 (g) pipeline repurposed", "capital_cost"] * wkn_new.length.values,
        # )

        # onight_costs = np.where(
        #     wkn_new.retrofitted == False,
        #     costs.at["H2 (g) pipeline", "investment"] * wkn_new.length.values,
        #     costs.at["H2 (g) pipeline repurposed", "investment"]
        #     * wkn_new.length.values,
        # )

        # fix: pipes from Kernnetz data in the model are over-proportinally many retrofitted projects as they are longer
        # do not use own cost data for retrofitted and new build pipes as this would underestimate the investment cost
        # reconstruct average Kernnetz invest (250€/MW*km) from our costs data

        capital_costs = (
            0.7 * costs.at["H2 (g) pipeline", "capital_cost"]
            + 0.3 * costs.at["H2 (g) pipeline repurposed", "capital_cost"]
        ) * wkn_new.length.values
        onight_costs = (
            0.7 * costs.at["H2 (g) pipeline", "investment"]
            + 0.3 * costs.at["H2 (g) pipeline repurposed", "investment"]
        ) * wkn_new.length.values

        lifetime = np.where(
            wkn_new.retrofitted == False,
            costs.at["H2 (g) pipeline", "lifetime"],
            costs.at["H2 (g) pipeline repurposed", "lifetime"],
        )

        # add kernnetz to network
        n.add(
            "Link",
            names,
            bus0=wkn_new.bus0.values + " H2",
            bus1=wkn_new.bus1.values + " H2",
            p_min_pu=-1,
            p_nom_extendable=False,
            p_nom=wkn_new.p_nom.values,
            build_year=wkn_new.build_year.values,
            length=wkn_new.length.values,
            capital_cost=capital_costs,
            onight_cost=onight_costs,
            carrier="H2 pipeline (Kernnetz)",
            lifetime=lifetime,
            retrofitted=wkn_new.retrofitted.values,
        )

        # add tags
        tags = wkn_new.apply(
            lambda row: {
                "pci": row["pci"],
                "ipcei": row["ipcei"],
                "investment_costs (Mio. Euro)": row["investment_costs (Mio. Euro)"],
            },
            axis=1,
        )
        n.links.loc[names, "tags"] = tags.values.astype(str)

        # add reversed pipes and losses
        lossy_bidirectional_links(
            n, "H2 pipeline (Kernnetz)", H2_transmission_efficiency, subset=names
        )

        # reduce the gas network capacity of retrofitted lines from kernnetz
        # which is build in the current period
        gas_pipes = n.links.query("carrier == 'gas pipeline'")
        if not gas_pipes.empty:
            res_gas_pipes = reduce_capacity(
                gas_pipes,
                add_reversed_pipes(wkn_new),
                carrier="gas",
            )
            n.links.loc[n.links.carrier == "gas pipeline", "p_nom"] = res_gas_pipes[
                "p_nom"
            ]

    # reduce H2 retrofitting potential from gas network for all kernnetz
    # pipelines which are being build in total (more conservative approach)
    if not wkn.empty and H2_retrofit:
        retrofitted_b = (
            n.links.carrier == "H2 pipeline retrofitted"
        ) & n.links.index.str.contains(str(investment_year))
        h2_pipes_retrofitted = n.links.loc[retrofitted_b]

        if not h2_pipes_retrofitted.empty:
            res_h2_pipes_retrofitted = reduce_capacity(
                h2_pipes_retrofitted,
                add_reversed_pipes(wkn),
                carrier="H2",
                target_attr="p_nom_max",
            )
            n.links.loc[retrofitted_b, "p_nom_max"] = res_h2_pipes_retrofitted[
                "p_nom_max"
            ]

    if investment_year <= 2030:
        # assume that only pipelines from kernnetz are built (within Germany):
        # make pipes within Germany not extendable and all others extendable (but only from current year)
        to_fix = (
            (name_country(n.links.bus0) == "DE")
            & (name_country(n.links.bus1) == "DE")
            & n.links.carrier.isin(["H2 pipeline", "H2 pipeline retrofitted"])
        )
        n.links.loc[to_fix, "p_nom_extendable"] = False

    # from 2030 onwards all pipes are extendable (except from the ones the model build up before and the kernnetz lines)


def unravel_carbonaceous_fuels(
    n,
    costs,
    industry,
    efuel_export_ban,
    inputs,
    shipping_oil_efficiency,
    shipping_methanol_efficiency,
    shipping_methanol_share,
    current_horizon,
):
    """
    Unravel European carbonaceous buses and if necessary their loads to enable
    energy balances for import and export of carbonaceous fuels.
    """
    ### oil bus
    logger.info("Unraveling oil bus")
    # add buses
    n.add("Carrier", "renewable oil")

    n.add("Bus", "DE", x=10.5, y=51.2, carrier="none")
    n.add("Bus", "DE oil", location="DE", carrier="oil", unit="MWh_th")
    n.add("Bus", "DE oil primary", location="DE", carrier="oil primary", unit="MWh_th")
    n.add(
        "Bus",
        "DE renewable oil",
        location="DE",
        carrier="renewable oil",
        unit="MWh_th",
    )
    n.add(
        "Bus",
        "EU renewable oil",
        location="EU",
        carrier="renewable oil",
        unit="MWh_th",
    )

    # add one generator for DE oil primary
    n.add(
        "Generator",
        name="DE oil primary",
        bus="DE oil primary",
        carrier="oil primary",
        p_nom_extendable=True,
        marginal_cost=n.generators.loc["EU oil primary"].marginal_cost,
    )

    # add link for DE oil refining
    n.add(
        "Link",
        "DE oil refining",
        bus0="DE oil primary",
        bus1="DE oil",
        bus2="co2 atmosphere",
        carrier="oil refining",
        p_nom=1e6,
        efficiency=1
        - (industry["oil_refining_emissions"] / costs.at["oil", "CO2 intensity"]),
        efficiency2=industry["oil_refining_emissions"],
    )

    ### renewable oil
    renewable_oil_carrier = [
        "unsustainable bioliquids",
        "biomass to liquid",
        "biomass to liquid CC",
        "electrobiofuels",
        "Fischer-Tropsch",
    ]
    renewable_oil_DE = n.links[
        (n.links.carrier.isin(renewable_oil_carrier)) & (name_country(n.links.index) == "DE")
    ]
    n.links.loc[renewable_oil_DE.index, "bus1"] = "DE renewable oil"

    renewable_oil_EU = n.links[
        (n.links.carrier.isin(renewable_oil_carrier)) & (name_country(n.links.index) != "DE")
    ]
    n.links.loc[renewable_oil_EU.index, "bus1"] = "EU renewable oil"

    # change links from EU oil to DE oil
    german_oil_consumers = n.links[
        (n.links.bus0 == "EU oil") & ((name_country(n.links.index) == "DE"))
    ].index

    n.links.loc[german_oil_consumers, "bus0"] = "DE oil"

    # add links between oil buses
    n.add(
        "Link",
        [
            "EU renewable oil -> DE oil",
            "DE renewable oil -> EU oil",
        ],
        bus0=[
            "EU renewable oil",
            "DE renewable oil",
        ],
        bus1=["DE oil", "EU oil"],
        carrier="renewable oil",
        p_nom=1e6,
        p_min_pu=0,
        marginal_cost=0.01,
    )

    if efuel_export_ban:
        logger.info(
            "Efuel export ban: Setting p_max_pu to 0 for DE renewable oil -> EU oil"
        )
        n.links.loc["DE renewable oil -> EU oil", "p_max_pu"] = 0

    n.add(
        "Link",
        [
            "EU renewable oil -> EU oil",
            "DE renewable oil -> DE oil",
        ],
        bus0=[
            "EU renewable oil",
            "DE renewable oil",
        ],
        bus1=["EU oil", "DE oil"],
        carrier="renewable oil",
        p_nom=1e6,
        p_min_pu=0,
    )

    # add stores
    EU_oil_store = n.stores.loc["EU oil Store"].copy()
    n.add(
        "Store",
        "DE oil Store",
        bus="DE oil",
        carrier="oil",
        e_nom_extendable=EU_oil_store.e_nom_extendable,
        e_cyclic=EU_oil_store.e_cyclic,
        capital_cost=EU_oil_store.capital_cost,
        onight_cost=EU_oil_store.onight_cost,
        lifetime=costs.at["General liquid hydrocarbon storage (product)", "lifetime"],
    )
    # check if there are loads at the EU oil bus
    if n.loads.index.isin(
        [
            "EU naphtha for industry",
            "EU kerosene for aviation",
            "EU shipping oil",
            "EU agriculture machinery oil",
            "EU land transport oil",
        ]
    ).any():
        logger.error(
            "There are loads at the EU oil bus. Please set config[sector][regional_oil_demand] to True to enable energy balances for oil."
        )
        raise ValueError(
            "There are loads at the EU oil bus. Please set config[sector][regional_oil_demand] to True to enable energy balances for oil."
        )

    ##########################################
    ### meoh bus
    logger.info("Unraveling methanol bus")
    # add bus
    n.add(
        "Bus",
        "DE methanol",
        location="DE",
        carrier="methanol",
        unit="MWh_th",
    )

    # change links from EU meoh to DE meoh
    DE_meoh_out = n.links[
        (n.links.bus0 == "EU methanol") & (name_country(n.links.index) == "DE")
    ].index
    n.links.loc[DE_meoh_out, "bus0"] = "DE methanol"
    DE_meoh_in = n.links[
        (n.links.bus1 == "EU methanol") & (name_country(n.links.index) == "DE")
    ].index
    n.links.loc[DE_meoh_in, "bus1"] = "DE methanol"

    # add links between methanol buses
    n.add(
        "Link",
        ["EU methanol -> DE methanol", "DE methanol -> EU methanol"],
        bus0=["EU methanol", "DE methanol"],
        bus1=["DE methanol", "EU methanol"],
        carrier="methanol",
        p_nom=1e6,
        p_min_pu=0,
        marginal_cost=0.01,
    )

    if efuel_export_ban:
        logger.info(
            "Efuel export ban: Setting p_max_pu to 0 for DE methanol -> EU methanol"
        )
        n.links.loc["DE methanol -> EU methanol", "p_max_pu"] = 0

    # add stores
    EU_meoh_store = n.stores.loc["EU methanol Store"].copy()
    n.add(
        "Store",
        "DE methanol Store",
        bus="DE methanol",
        carrier="methanol",
        e_nom_extendable=EU_meoh_store.e_nom_extendable,
        e_cyclic=EU_meoh_store.e_cyclic,
        capital_cost=EU_meoh_store.capital_cost,
        onight_cost=EU_meoh_store.onight_cost,
        lifetime=costs.at["General liquid hydrocarbon storage (product)", "lifetime"],
    )
    # check for loads
    # industry load
    if "EU industry methanol" in n.loads.index:
        n.add(
            "Bus",
            "DE industry methanol",
            carrier="industry methanol",
            location="DE",
            unit="MWh_LHV",
        )
        industrial_demand = (
            pd.read_csv(inputs.industrial_demand, index_col=0) * 1e6
        )  # TWh/a to MWh/a
        DE_meoh = (
            industrial_demand["methanol"].pipe(lambda d: d[name_country(d.index) == "DE"]).sum() / 8760
        )  # MWh/a to MW hourly resolution
        n.add(
            "Load",
            "DE industry methanol",
            bus="DE industry methanol",
            carrier="industry methanol",
            p_set=DE_meoh,
        )
        n.loads.loc["EU industry methanol", "p_set"] -= DE_meoh

        n.add(
            "Link",
            "DE industry methanol",
            bus0="DE methanol",
            bus1="DE industry methanol",
            bus2="co2 atmosphere",
            carrier="industry methanol",
            p_nom_extendable=True,
            efficiency2=n.links.loc["EU industry methanol", "efficiency2"],
        )

    # shipping load
    if "EU shipping methanol" in n.loads.index:
        # get German shipping demand for domestic and international navigation
        # TWh/a
        pop_weighted_energy_totals = pd.read_csv(
            inputs.pop_weighted_energy_totals, index_col=0
        )
        # TWh/a
        domestic_navigation = (
            pop_weighted_energy_totals["total domestic navigation"]
            .pipe(lambda d: d[name_country(d.index) == "DE"])
            .sum()
        )
        # TWh/a
        international_navigation = (
            (pd.read_csv(inputs.shipping_demand, index_col=0).squeeze(axis=1))
            .pipe(lambda d: d[name_country(d.index) == "DE"])
            .sum()
        )
        all_navigation = domestic_navigation + international_navigation
        p_set = all_navigation * 1e6 / 8760  # convert TWh/a to MW hourly resolution

        # transfer oil demand to methanol demand
        efficiency = shipping_oil_efficiency / shipping_methanol_efficiency
        # get share of shipping done with methanol
        p_set = shipping_methanol_share[current_horizon] * p_set * efficiency
        n.add(
            "Load",
            "DE shipping methanol",
            bus="DE shipping methanol",
            carrier="shipping methanol",
            p_set=p_set,
        )
        n.loads.loc["EU shipping methanol", "p_set"] -= p_set

        n.add(
            "Bus",
            "DE shipping methanol",
            carrier="shipping methanol",
            location="DE",
            unit="MWh_LHV",
        )
        n.add(
            "Link",
            "DE shipping methanol",
            bus0="DE methanol",
            bus1="DE shipping methanol",
            bus2="co2 atmosphere",
            carrier="shipping methanol",
            p_nom_extendable=True,
            efficiency2=n.links.loc["EU shipping methanol", "efficiency2"],
        )


def unravel_gasbus(n, costs, industry, efuel_export_ban):
    """
    Unravel European gas bus to enable energy balances for import of gas
    products.
    """
    logger.info("Unraveling gas bus")

    if "DE" not in n.buses:
        n.add("Bus", "DE", location="DE", x=10.5, y=51.2, carrier="none")

    ### create DE gas bus/generator/store
    n.add(
        "Bus",
        "DE gas",
        location="DE",
        carrier="gas",
        unit="MWh_LHV",
    )

    n.add(
        "Bus",
        "DE gas primary",
        location="DE",
        carrier="gas primary",
        unit="MWh_LHV",
    )

    n.add(
        "Store",
        "DE gas Store",
        bus="DE gas",
        carrier="gas",
        e_nom_extendable=True,
        e_cyclic=True,
        capital_cost=costs.at["gas storage", "capital_cost"],
        onight_cost=costs.at["gas storage", "investment"],
        lifetime=costs.at["gas storage", "lifetime"],
    )

    n.add(
        "Generator",
        "DE gas primary",
        bus="DE gas primary",
        p_nom_extendable=True,
        carrier="gas primary",
        marginal_cost=costs.at["gas", "fuel"],
    )

    # add link for DE gas compressing
    n.add(
        "Link",
        "DE gas compressing",
        bus0="DE gas primary",
        bus1="DE gas",
        bus2="co2 atmosphere",
        carrier="gas compressing",
        p_nom=1e6,
        efficiency=1 - industry["gas_compression_losses"],
        efficiency2=industry["gas_compression_losses"]
        * costs.at["gas", "CO2 intensity"],
    )

    ### create renewable gas buses
    n.add("Carrier", "renewable gas")

    n.add(
        "Bus",
        "DE renewable gas",
        carrier="renewable gas",
        location="DE",
        unit="MWh_LHV",
    )
    n.add(
        "Bus",
        "EU renewable gas",
        location="EU",
        carrier="renewable gas",
        unit="MWh_LHV",
    )

    ### renewable gas
    renewable_gas_carrier = ["biogas to gas", "biogas to gas CC", "Sabatier"]
    renewable_gas_DE = n.links[
        (n.links.carrier.isin(renewable_gas_carrier)) & (name_country(n.links.index) == "DE")
    ]
    n.links.loc[renewable_gas_DE.index, "bus1"] = "DE renewable gas"

    renewable_gas_EU = n.links[
        (n.links.carrier.isin(renewable_gas_carrier)) & (name_country(n.links.index) != "DE")
    ]
    n.links.loc[renewable_gas_EU.index, "bus1"] = "EU renewable gas"

    ### change buses of German gas links
    fossil_links = n.links[(n.links.bus0 == "EU gas") & (name_country(n.links.index) == "DE")]
    n.links.loc[fossil_links.index, "bus0"] = "DE gas"

    ### add import/export links
    n.add(
        "Link",
        ["EU renewable gas -> DE gas", "DE renewable gas -> EU gas"],
        bus0=["EU renewable gas", "DE renewable gas"],
        bus1=["DE gas", "EU gas"],
        carrier="renewable gas",
        p_nom=1e6,
        p_min_pu=0,
        marginal_cost=0.01,
    )

    if efuel_export_ban:
        logger.info(
            "Efuel export ban: Setting p_max_pu to 0 for DE renewable gas -> EU gas"
        )
        n.links.loc["DE renewable gas -> EU gas", "p_max_pu"] = 0

    ### add links between renewable and fossil gas buses
    n.add(
        "Link",
        ["EU renewable gas -> EU gas", "DE renewable gas -> DE gas"],
        bus0=["EU renewable gas", "DE renewable gas"],
        bus1=["EU gas", "DE gas"],
        carrier="renewable gas",
        p_nom=1e6,
        p_min_pu=0,
    )

    # check if loads are connected to EU gas bus
    if "EU gas for industry" in n.loads.index:
        logger.error(
            "There are loads at the EU gas bus. Please set config[sector][regional_gas_demand] to True to enable energy balances for gas."
        )
        raise ValueError(
            "There are loads at the EU gas bus. Please set config[sector][regional_gas_demand] to True to enable energy balances for gas."
        )


def transmission_costs_from_modified_cost_data(n, costs, transmission):
    # copying the the function update_transmission_costs from add_electricity
    # slight change to the function so it works in modify_prenetwork

    n.lines["capital_cost"] = (
        n.lines["length"] * costs.at["HVAC overhead", "capital_cost"]
    )
    n.lines["onight_cost"] = n.lines["length"] * costs.at["HVAC overhead", "investment"]

    if n.links.empty:
        return
    # get all DC links that are not the reverse links
    dc_b = (n.links.carrier == "DC") & ~(n.links.index.str.contains("reverse"))

    # If there are no dc links, then the 'underwater_fraction' column
    # may be missing. Therefore we have to return here.
    if n.links.loc[dc_b].empty:
        return

    if transmission == "overhead":
        links_costs = "HVDC overhead"
    elif transmission == "underground":
        links_costs = "HVDC submarine"

    capital_cost = (
        n.links.loc[dc_b, "length"]
        * (
            (1.0 - n.links.loc[dc_b, "underwater_fraction"])
            * costs.at[links_costs, "capital_cost"]
            + n.links.loc[dc_b, "underwater_fraction"]
            * costs.at["HVDC submarine", "capital_cost"]
        )
        + costs.at["HVDC inverter pair", "capital_cost"]
    )

    onight_cost = (
        n.links.loc[dc_b, "length"]
        * (
            (1.0 - n.links.loc[dc_b, "underwater_fraction"])
            * costs.at[links_costs, "investment"]
            + n.links.loc[dc_b, "underwater_fraction"]
            * costs.at["HVDC submarine", "investment"]
        )
        + costs.at["HVDC inverter pair", "investment"]
    )
    n.links.loc[dc_b, "capital_cost"] = capital_cost
    n.links.loc[dc_b, "onight_cost"] = onight_cost


def must_run(n, params, planning_horizons, current_horizon):
    """
    Set p_min_pu for links to the specified value or reset to 0 if not specified.
    """

    investment_year = current_horizon
    i = planning_horizons.index(current_horizon)
    previous_investment_year = int(planning_horizons[i - 1]) if i != 0 else np.nan

    # Get params for the current and previous years
    current_params = params.get(investment_year, {})
    previous_params = params.get(previous_investment_year, {})

    # Collect all carriers and regions from the previous period
    for region in previous_params:
        for carrier in previous_params[region]:
            # Check if the carrier is not in the current period
            if region not in current_params or carrier not in current_params.get(
                region, {}
            ):
                # Reset p_min_pu to 0 for this carrier in the previous region
                logger.info(
                    f"Must-run condition disabled: Resetting p_min_pu to 0 for {carrier} "
                    f"in region {region} (was specified in {previous_investment_year}, but not in {investment_year})."
                )
                links_i = n.links[
                    (n.links.carrier == carrier) & n.links.index.str.contains(region)
                ].index
                n.links.loc[links_i, "p_min_pu"] = 0

    # Set p_min_pu for carriers specified in the current investment period
    for region in current_params:
        for carrier in current_params[region]:
            p_min_pu = current_params[region][carrier]
            logger.info(
                f"Must-run condition enabled: Setting p_min_pu = {p_min_pu} for {carrier} "
                f"in year {investment_year} and region {region}."
            )
            links_i = n.links[
                (n.links.carrier == carrier) & n.links.index.str.contains(region)
            ].index
            n.links.loc[links_i, "p_min_pu"] = p_min_pu


def modify_mobility_demand(
    n, mobility_data_file, bev_charge_rate, bev_dsm_availability, bev_energy
):
    """
    Change loads in Germany to use exogenous data for road demand.

    The mobility_data contains the demand of Electricity, Hydrogen and Liquids in MWh/a, and the number of EVs in million.
    """
    logger.info(
        "Overwriting land transport demand. In particular the `land_transport_electric_share` config setting will not be used."
    )

    fraction_modelyear = n.snapshot_weightings.stores.sum() / 8760

    new_demand = pd.read_csv(mobility_data_file, header=None, index_col=0).iloc[:, 0]

    number_of_EVs = new_demand.pop("million_EVs") * 1e6

    new_demand *= fraction_modelyear

    carrier_fuel_map = {
        "land transport EV": "Electricity",
        "land transport fuel cell": "Hydrogen",
        "land transport oil": "Liquids",
    }
    for carrier, fuel in carrier_fuel_map.items():
        loads_i = n.loads[
            (n.loads.carrier == carrier) & (name_country(n.loads.index) == "DE")
        ]
        old_demand = (
            n.loads_t.p_set.loc[:, loads_i.index]
            .sum(axis=1)
            .mul(n.snapshot_weightings.stores)
            .sum()
        )
        scale_factor = new_demand[fuel] / old_demand
        logger.info(
            f"Scaling {carrier} loads in Germany by {scale_factor:.2f}.\nPrevious total demand: {old_demand:.2f} MWh/a, new total demand: {new_demand[fuel]:.2f} MWh/a."
        )
        n.loads_t.p_set.loc[:, loads_i.index] *= scale_factor

    # adjust BEV charger and V2G capacities

    BEV_chargers = n.links[
        (n.links.carrier == "BEV charger") & ((name_country(n.links.bus0) == "DE"))
    ]

    scale_factor = number_of_EVs * bev_charge_rate / BEV_chargers.p_nom.sum()
    logger.info(
        f"Scaling BEV charger capacities in Germany by {scale_factor:.2f} to match the new number of EVs.\nPrevious total capacity: {BEV_chargers.p_nom.sum():.2f} MW, new total capacity: {number_of_EVs * bev_charge_rate:.2f} MW."
    )
    n.links.loc[BEV_chargers.index, "p_nom"] *= scale_factor

    V2G = n.links[(n.links.carrier == "V2G") & ((name_country(n.links.bus0) == "DE"))]

    if not V2G.empty:
        n.links.loc[V2G.index, "p_nom"] *= scale_factor * bev_dsm_availability

    dsm = n.stores[
        (n.stores.carrier == "EV battery") & ((name_country(n.stores.bus) == "DE"))
    ]

    if not dsm.empty:
        scale_factor = (
            number_of_EVs * bev_energy * bev_dsm_availability
        ) / dsm.e_nom.sum()
        n.stores.loc[dsm.index, "e_nom"] *= scale_factor


def modify_industry_demand(
    n,
    year,
    industry_energy_demand_file,
    industry_production_file,
    sector_ratios_file,
    scale_non_energy=False,
):
    logger.info("Modifying industry demand in Germany.")

    industry_production = pd.read_csv(
        industry_production_file,
        index_col="kton/a",
    ).rename_axis("country")

    sector_ratios = pd.read_csv(
        sector_ratios_file,
        header=[0, 1],
        index_col=0,
    ).rename_axis("carrier")

    new_demand = pd.read_csv(
        industry_energy_demand_file,
        index_col=0,
    )[str(year)].mul(1e6)

    subcategories = ["HVC", "Methanol", "Chlorine", "Ammonia"]
    carrier = ["hydrogen", "methane", "naphtha"]

    ip = industry_production.loc["DE", subcategories]  # kt/a
    sr = sector_ratios["DE"].loc[carrier, subcategories]  # MWh/tMaterial
    _non_energy = sr.multiply(ip).sum(axis=1) * 1e3

    non_energy = pd.Series(
        {
            "industry electricity": 0.0,
            "low-temperature heat for industry": 0.0,
            "solid biomass for industry": 0.0,
            "H2 for industry": _non_energy["hydrogen"],
            "coal for industry": 0.0,
            "gas for industry": _non_energy["methane"],
            "naphtha for industry": _non_energy["naphtha"],
        }
    )

    _industry_loads = [
        "solid biomass for industry",
        "gas for industry",
        "H2 for industry",
        "industry methanol",
        "naphtha for industry",
        "low-temperature heat for industry",
        "industry electricity",
        "coal for industry",
    ]
    industry_loads = n.loads[
        n.loads.carrier.isin(_industry_loads) & (name_country(n.loads.bus) == "DE")
    ]

    if scale_non_energy:
        new_demand_without_non_energy = new_demand.sum()
        pypsa_industry_without_non_energy = (
            industry_loads.p_set.sum() * 8760 - non_energy.sum()
        )
        non_energy_scaling_factor = (
            new_demand_without_non_energy / pypsa_industry_without_non_energy
        )
        logger.info(
            f"Scaling non-energy use by {non_energy_scaling_factor:.2f} to match UBA data."
        )
        non_energy_corrected = non_energy * non_energy_scaling_factor
    else:
        non_energy_corrected = non_energy

    for carrier in [
        "industry electricity",
        # "H2 for industry", # skip because UBA is too optimistic on H2
        "solid biomass for industry",
        "low-temperature heat for industry",
    ]:
        loads_i = n.loads.index[
            (n.loads.carrier == carrier) & (name_country(n.loads.bus) == "DE")
        ]
        logger.info(
            f"Total load of {carrier} in DE before scaling: {n.loads.loc[loads_i, 'p_set'].sum() * 8760:.2f} MWh/a"
        )
        total_load = industry_loads.p_set.loc[loads_i].sum() * 8760
        scaling_factor = (
            new_demand[carrier] + non_energy_corrected[carrier]
        ) / total_load
        n.loads.loc[loads_i, "p_set"] *= scaling_factor
        logger.info(
            f"Total load of {carrier} in DE after scaling: {n.loads.loc[loads_i, 'p_set'].sum() * 8760:.2f} MWh/a"
        )

    # Fossil fuels are aggregated in UBA MWMS but have to be scaled separately
    fossil_loads = industry_loads.query("carrier.str.contains('gas|coal|naphtha')")
    fossil_totals = (
        fossil_loads[["p_set", "carrier"]].groupby("carrier").p_set.sum() * 8760
    )
    fossil_energy = fossil_totals - non_energy[fossil_totals.index]
    fossil_energy_corrected = fossil_energy * new_demand["fossil"] / fossil_energy.sum()
    fossil_totals_corrected = (
        fossil_energy_corrected + non_energy_corrected[fossil_totals.index]
    )
    for carrier in fossil_totals.index:
        loads_i = fossil_loads.index[
            (fossil_loads.carrier == carrier)
            & (name_country(fossil_loads.bus) == "DE")
        ]
        n.loads.loc[loads_i, "p_set"] *= (
            fossil_totals_corrected[carrier] / fossil_totals[carrier]
        )


def add_hydrogen_turbines(n, H2_plants):
    """
    This adds links that instead of a gas turbine use a hydrogen turbine.

    It is assumed that the efficiency stays the same. This function is
    only applied to German nodes.
    """

    scope = H2_plants.get("enable")
    if scope not in ["DE", "EU"]:
        msg = f"Invalid value in `H2_plants:enable` for adding hydrogen turbines: {scope}. Expected 'DE' or 'EU'."
        logger.error(msg)
        raise ValueError(msg)
    logger.info(f"Adding hydrogen turbine technologies in {scope}.")
    if scope == "EU":
        scope = ""
    gas_carrier = ["OCGT", "CCGT"]
    for carrier in gas_carrier:
        gas_plants = n.links[
            (n.links.carrier == carrier)
            & ((name_country(n.links.index) == scope) if scope else True)
            & (n.links.p_nom_extendable)
        ].index
        if gas_plants.empty:
            continue
        h2_plants = n.links.loc[gas_plants].copy()
        h2_plants.carrier = h2_plants.carrier.str.replace(carrier, "H2 " + carrier)
        h2_plants.index = h2_plants.index.str.replace(carrier, "H2 " + carrier)
        h2_plants.bus0 = h2_plants.bus1 + " H2"
        h2_plants.bus2 = ""
        h2_plants.efficiency2 = 1
        # add the new links
        n.add("Link", h2_plants.index, **h2_plants)

    # special handling of CHPs
    gas_plants = n.links[
        (n.links.carrier == "urban central gas CHP")
        & ((name_country(n.links.index) == scope) if scope else True)
        & (n.links.p_nom_extendable)
    ].index
    h2_plants = n.links.loc[gas_plants].copy()
    h2_plants.carrier = h2_plants.carrier.str.replace("gas", "H2")
    h2_plants.index = h2_plants.index.str.replace("gas", "H2")
    h2_plants.bus0 = h2_plants.bus1 + " H2"
    h2_plants.bus3 = ""
    h2_plants.efficiency3 = 1
    n.add("Link", h2_plants.index, **h2_plants)


def force_retrofit(n, params):
    """
    This function forces the retrofit of gas turbines from params["force"] on.

    Extendable gas links are deleted.
    """

    logger.info("Forcing retrofit of gas turbines to hydrogen turbines.")

    gas_carrier = ["OCGT", "CCGT", "urban central gas CHP"]
    # deleting extendable gas turbine plants
    to_drop = n.links[
        (n.links.carrier.isin(gas_carrier))
        & (n.links.p_nom_extendable)
        & (name_country(n.links.index) == "DE")
    ].index
    n.links.drop(to_drop, inplace=True)

    # forcing retrofit
    for carrier in ["OCGT", "CCGT"]:
        gas_plants = n.links[
            (n.links.carrier == carrier)
            & (name_country(n.links.index) == "DE")
            & (n.links.build_year >= params["start"])
        ].index
        if gas_plants.empty:
            continue

        h2_plants = n.links.loc[gas_plants].copy()
        h2_plants.carrier = h2_plants.carrier.str.replace(
            carrier, "H2 retrofit " + carrier
        )
        h2_plants.index = h2_plants.index.str.replace(carrier, "H2 retrofit " + carrier)
        h2_plants.bus0 = h2_plants.bus1 + " H2"
        h2_plants.bus2 = ""
        h2_plants.efficiency -= params["efficiency_loss"]
        h2_plants.efficiency2 = 1  # default value
        h2_plants.capital_cost *= 1 + params["cost_factor"]
        h2_plants.onight_cost *= 1 + params["cost_factor"]
        # add the new links
        n.add("Link", h2_plants.index, **h2_plants)
        n.links.drop(gas_plants, inplace=True)

    # special handling of CHPs
    gas_plants = n.links[
        (n.links.carrier == "urban central gas CHP")
        & (name_country(n.links.index) == "DE")
        & (n.links.build_year >= params["start"])
    ].index
    if gas_plants.empty:
        return

    h2_plants = n.links.loc[gas_plants].copy()
    h2_plants.carrier = h2_plants.carrier.str.replace("gas", "H2 retrofit")
    h2_plants.index = h2_plants.index.str.replace("gas", "H2 retrofit")
    h2_plants.bus0 = h2_plants.bus1 + " H2"
    h2_plants.bus3 = ""
    h2_plants.efficiency -= params["efficiency_loss"]
    h2_plants.efficiency3 = 1  # default value
    h2_plants.capital_cost *= 1 + params["cost_factor"]
    h2_plants.onight_cost *= 1 + params["cost_factor"]
    n.add("Link", h2_plants.index, **h2_plants)
    n.links.drop(gas_plants, inplace=True)


def enforce_transmission_project_build_years(n, current_year, onshore_nep_force):
    # this step is necessary for any links w/
    # current year >= build_year > previous year
    # it undoes the p_nom_min = p_nom_opt from add_brownfield
    dc_previously_deactivated = n.links.index[
        (n.links.carrier == "DC")
        & (n.links.p_nom > 0)
        & (n.links.p_nom_opt == 0)
        & (n.links.build_year <= onshore_nep_force["cutout_year"])
        & (n.links.build_year >= onshore_nep_force["cutin_year"])
    ]

    n.links.loc[dc_previously_deactivated, "p_nom_min"] = (
        n.links.loc[dc_previously_deactivated, ["p_nom_min", "p_nom"]]
        .max(axis=1)
        .round(3)
    )

    # this forces p_nom_opt = 0 for links w/ build_year > current year
    dc_future = n.links.index[
        n.links.carrier.str.fullmatch("DC") & (n.links.build_year > current_year)
    ]
    n.links.loc[dc_future, "p_nom_min"] = 0.0
    n.links.loc[dc_future, "p_nom_max"] = 0.0


def force_connection_nep_offshore(
    n,
    current_year,
    costs,
    renewable,
    offshore_nep_force,
    offshore_connection_points,
    onshore_regions_file,
    regions_offshore_file,
):
    # WARNING this code adds a new generator for the offwind connection
    # at an onshore locations. These extra capacities are not accounted
    # for in the land use constraint
    if not renewable["offwind-dc"]["resource_classes"] == 1:
        logger.warning(
            "Number of offshore wind resource classes are not equal to 0. Assigning all offshore wind from NEP to class 0."
        )

    # Load shapes and projects
    offshore = pd.read_csv(offshore_connection_points, index_col=0)

    if int(offshore_nep_force["delay_years"]) != 0:
        # Modify 'Inbetriebnahmejahr' by adding the delay years for rows where 'Inbetriebnahmejahr' > 2025
        offshore.loc[offshore["Inbetriebnahmejahr"] > 2025, "Inbetriebnahmejahr"] += (
            int(offshore_nep_force["delay_years"])
        )
        offshore.loc[offshore["Inbetriebnahmejahr"] <= 2025, "Inbetriebnahmejahr"] += 1
        logger.info(
            f"Delaying NEP offshore connection points by {offshore_nep_force['delay_years']} years."
        )
        # This is a hack s.t. for CurPol and WorstCase the 2030 projects are delayed to the 2035 period, but the later projects are ignored
        offshore.loc[offshore["Inbetriebnahmejahr"] > 2031, "Inbetriebnahmejahr"] += 5
        logger.info(
            "Delaying NEP offshore connection points after 2031 by another 5 years."
        )

    goffshore = gpd.GeoDataFrame(
        offshore,
        geometry=gpd.points_from_xy(offshore.lon, offshore.lat),
        crs="EPSG:4326",
    )

    onshore_regions = gpd.read_file(onshore_regions_file).set_index("name")
    regions_offshore = gpd.read_file(regions_offshore_file).set_index("name")

    # find connection point nodes for each project
    goffshore = gpd.sjoin(goffshore, onshore_regions, how="inner", predicate="within")

    # use for offshore profile of nodes connected in non-offshore nodes
    # point is chosen far out in EEZ duck
    nordsee_duck_node = regions_offshore.index[
        regions_offshore.contains(Point(6.19628, 54.38543))
    ][0]
    nordsee_duck_off = f"{nordsee_duck_node} 0 offwind-dc-{current_year}"

    dc_projects = goffshore[
        (goffshore.Inbetriebnahmejahr > current_year - 5)
        & (goffshore.Inbetriebnahmejahr <= current_year)
        & goffshore["Bezeichnung des Projekts"].str.contains("HGÜ-|DC-")
    ]

    dc_power = dc_projects["Übertragungsleistung in MW"].groupby(dc_projects.name).sum()

    dc_connection_totals = (
        dc_projects["Trassenlänge in km"]
        * (
            2 / 3 * costs.at["offwind-dc-connection-submarine", "investment"]
            + 1 / 3 * costs.at["offwind-dc-connection-underground", "investment"]
        )
        + costs.at["offwind-dc-station", "investment"]
    ) * dc_projects["Übertragungsleistung in MW"]

    dc_connection_onight_costs = (
        dc_connection_totals.groupby(dc_projects.name).sum().div(dc_power)
    )
    # Instead of taking over capacities from add_existing, set everything to 0 and use only the NEP projects.
    current_offwind = n.generators.index[
        n.generators.carrier.str.contains("offwind")
        & (name_country(n.generators.index) == "DE")
        & (n.generators.build_year == current_year)
    ]

    n.generators.loc[current_offwind, "p_nom_min"] = 0
    n.generators.loc[current_offwind, "p_nom"] = 0

    if (current_year >= int(offshore_nep_force["cutin_year"])) and (
        current_year <= int(offshore_nep_force["cutout_year"])
    ):
        logger.info(f"Forcing in NEP offshore DC projects with capacity:\n {dc_power}")

        for node in dc_power.index:
            node_off = f"{node} 0 offwind-dc-{current_year}"

            if node_off not in n.generators.index:
                logger.info(f"Adding generator {node_off}")
                n.generators.loc[node_off] = n.generators.loc[nordsee_duck_off]
                n.generators.at[node_off, "bus"] = node
                n.generators_t.p_max_pu[node_off] = n.generators_t.p_max_pu[
                    nordsee_duck_off
                ]
                n.generators.at[node_off, "p_nom_min"] = 0
                n.generators.at[node_off, "p_nom"] = 0

            n.generators.at[node_off, "p_nom_min"] = dc_power.loc[node]
            n.generators.at[node_off, "connection_onight_cost"] = (
                dc_connection_onight_costs.loc[node]
            )
            # Differing from add_existing_baseyear "p_nom" is not set,
            # because we want to fully account the capacity expansion in the exporter

    # this is a hack to stop solve_network.py > _add_land_use_constraint breaking
    # if there are existing generators, add a new extendable one
    # WARNING land_use_constraint is not respected for the onshore generators
    # TODO rewrite this part less hacky
    existings = n.generators.index[
        (n.generators.carrier == "offwind-dc") & ~n.generators.p_nom_extendable
    ]
    for existing in existings:
        node = n.generators.at[existing, "bus"]
        node_off = f"{node} 0 offwind-dc-{current_year}"
        if node_off not in n.generators.index:
            logger.info(f"adding for dummy land constraint {node_off}")
            n.generators.loc[node_off] = n.generators.loc[nordsee_duck_off]
            n.generators.at[node_off, "bus"] = node
            n.generators_t.p_max_pu[node_off] = n.generators_t.p_max_pu[
                nordsee_duck_off
            ]
            # The new dummy generator shall not be extended
            n.generators.at[node_off, "p_nom_max"] = 0
            # Try out the new pypsa keyword to ignore the generator during optimization
            n.generators.at[node_off, "active"] = False

    # WARNING: Code duplication ahead
    ac_projects = goffshore[
        (goffshore.Inbetriebnahmejahr > current_year - 5)
        & (goffshore.Inbetriebnahmejahr <= current_year)
        & goffshore["Bezeichnung des Projekts"].str.contains("AC-")
    ]

    ac_power = ac_projects["Übertragungsleistung in MW"].groupby(ac_projects.name).sum()

    ac_connection_totals = (
        ac_projects["Trassenlänge in km"]
        * (
            2 / 3 * costs.at["offwind-ac-connection-submarine", "investment"]
            + 1 / 3 * costs.at["offwind-ac-connection-underground", "investment"]
        )
        + costs.at["offwind-ac-station", "investment"]
    ) * ac_projects["Übertragungsleistung in MW"]

    ac_connection_onight_costs = (
        ac_connection_totals.groupby(ac_projects.name).sum().div(ac_power)
    )

    if (current_year >= int(offshore_nep_force["cutin_year"])) and (
        current_year <= int(offshore_nep_force["cutout_year"])
    ):
        logger.info(f"Forcing in NEP offshore AC projects with capacity:\n {ac_power}")

        for node in ac_power.index:
            node_off = f"{node} 0 offwind-ac-{current_year}"

            if node_off not in n.generators.index:
                logger.error(
                    f"Assuming all AC projects are connected at locations where other generators exists. That is not the case for {node_off}. Terminating"
                )
                raise ValueError(
                    f"Assuming all AC projects are connected at locations where other generators exists. That is not the case for {node_off}. Terminating"
                )

            n.generators.at[node_off, "p_nom_min"] = ac_power.loc[node]
            n.generators.at[node_off, "connection_onight_cost"] = (
                ac_connection_onight_costs.loc[node]
            )


def drop_duplicate_transmission_projects(n):
    year = 2024

    logger.info(
        f"Dropping transmission projects with build year <= {year}. They are likely already in the OSM base network."
    )  # Maybe one 2024 line is missing in the OSM base network

    to_drop = n.lines.query("0 < build_year <= @year").index

    n.remove("Line", to_drop)


def scale_capacity(n, scaling, current_horizon):
    """
    Scale the output capacity of energy system links based on predefined scaling limits.

    Parameters
    ----------
    - n: The network/model object representing the energy system.
    - scaling: A dictionary with scaling limits structured as
               {year: {region: {carrier: limit}}}.
    """
    investment_year = current_horizon
    if investment_year in scaling.keys():
        for region in scaling[investment_year].keys():
            for carrier in scaling[investment_year][region].keys():
                limit = scaling[investment_year][region][carrier]
                logger.info(
                    f"Scaling output capacity (bus1) of {carrier} in region {region} to {limit} MW"
                )

                links_i = n.links[
                    (n.links.carrier == carrier) & n.links.index.str.contains(region)
                ].index

                installed_cap = n.links.loc[links_i].eval("p_nom * efficiency").sum()
                if installed_cap == 0:
                    logger.warning(
                        f"No installed capacity for {carrier} in region {region}. Skipping adjustment."
                    )
                    continue

                diff_cap = limit - installed_cap
                avg_efficiency = n.links.loc[links_i, "efficiency"].mean()
                if avg_efficiency == 0 or np.isnan(avg_efficiency):
                    logger.warning(
                        f"Invalid average efficiency for {carrier} in region {region}. Skipping adjustment."
                    )
                    continue

                diff_cap_0 = diff_cap / avg_efficiency
                p_nom_sum = n.links.loc[links_i, "p_nom"].sum()
                if p_nom_sum == 0:
                    logger.warning(
                        f"Zero total p_nom for {carrier} in region {region}. Skipping adjustment."
                    )
                    continue

                scaling_factors = n.links.loc[links_i].eval("p_nom / @p_nom_sum")
                n.links.loc[links_i, "p_nom"] += scaling_factors * diff_cap_0

                links_i_current = n.links.loc[links_i][
                    (n.links.loc[links_i].p_nom_min != 0)
                    & n.links.loc[links_i].p_nom_extendable
                ].index
                n.links.loc[links_i_current, "p_nom_min"] = n.links.loc[
                    links_i_current, "p_nom"
                ]
                # !!! eventually remove this again
                # Do not allow further extension of assets for which the capacity has been scaled
                n.links.loc[links_i, "p_nom_extendable"] = False


def limit_cross_border_flows_ac(n, s_max_pu):
    logger.info(
        f"Limiting AC cross-border flows between all countries to {s_max_pu} of maximum capacity."
    )
    cross_border_lines = n.lines.index[name_country(n.lines.bus0) != name_country(n.lines.bus1)]
    n.lines.loc[cross_border_lines, "s_max_pu"] = s_max_pu


def scale_industry_elec_to_2025(n, industrial_energy_demand_2025):
    """
    Scale the electricity demand of industry in Germany to match the 2025 value from UBA.

    This is necessary because the scaling of industry demand to UBA values is only applied to the total demand, but not to the individual carriers. This can lead to unrealistic electricity demand for industry in 2025 if the original PyPSA-DE network has a different split between electricity and other carriers than the UBA data.
    """
    ageb = 663868 / 3.6e-3

    logger.info(
        f"Scaling electricity demand of industry in Germany to respect 2025 AGEB value of {ageb} MWh."
    )

    model_demand_2025 = (
        (pd.read_csv(industrial_energy_demand_2025, index_col=0) * 1e6)
        .pipe(lambda d: d[name_country(d.index) == "DE"])
        .sum()["electricity"]
    )

    delta = ageb - model_demand_2025

    loads = n.loads[
        (name_country(n.loads.bus) == "DE")
        & (n.loads.carrier == "industry electricity")
    ]

    logger.info(
        "Industry electricity demand in Germany before scaling: "
        + str(loads.p_set.sum() * 8760)
        + " MWh/a"
    )
    scaling_factor = 1 + (delta / (loads.p_set.sum() * 8760))
    n.loads.loc[loads.index, "p_set"] *= scaling_factor
    logger.info(
        "Industry electricity demand in Germany after scaling: "
        + str(n.loads.loc[loads.index, "p_set"].sum() * 8760)
        + " MWh/a"
    )


def deactivate_early_transmission_expansion(n, current_year):
    logger.info(f"Deactivating transmission expansion for year {current_year}.")
    # Assumption 1: The current base network captures all existing lines already -> no expansion in 2025
    # Assumption 2: All international transmission projects and all within Germany are in NEP or TYNDP
    n.lines.loc[n.lines.build_year > current_year, "active"] = False
    n.links.loc[n.links.build_year > current_year, "active"] = False
    n.lines.s_nom_extendable = False
    n.links.loc[n.links.carrier == "DC", "p_nom_extendable"] = False


def main(n, inputs, params, costs, current_horizon: int) -> pypsa.Network:
    if not params.pypsa_de_enabled:
        logger.info(
            "Skipping PyPSA-DE modifications for horizon %s because pypsa-de.enable is false.",
            current_horizon,
        )
        return n
    if not params.sector["enabled"]:
        logger.warning(
            "Skipping PyPSA-DE modifications for horizon %s because sector.enabled is false.",
            current_horizon,
        )
        return n

    logger.info("Applying PyPSA-DE modifications for horizon %s.", current_horizon)

    if params.sector["transport"]:
        modify_mobility_demand(
            n,
            inputs.modified_mobility_data,
            params.bev_charge_rate,
            params.bev_dsm_availability,
            params.bev_energy,
        )
    else:
        logger.info(
            "Skipping exogenous mobility adjustment because transport is disabled."
        )

    new_boiler_ban(n, params.fossil_boiler_ban, current_horizon)
    absolute_fossil_heat_ban(
        n, getattr(params, "absolute_fossil_heat_ban", {}) or {}, current_horizon
    )
    fix_new_boiler_profiles(n)
    remove_old_boiler_profiles(n)
    coal_generation_ban(n, params.coal_ban, current_horizon)
    nuclear_generation_ban(n, params.nuclear_ban, current_horizon)
    restrict_nuclear_capacity_factor(n)
    first_technology_occurrence(n, params.technology_occurrence, current_horizon)

    unravel_carbonaceous_fuels(
        n,
        costs,
        params.industry,
        params.efuel_export_ban,
        inputs,
        params.shipping_oil_efficiency,
        params.shipping_methanol_efficiency,
        params.shipping_methanol_share,
        current_horizon,
    )
    unravel_gasbus(n, costs, params.industry, params.efuel_export_ban)

    if params.enable_kernnetz:
        wkn = pd.read_csv(inputs.wkn, index_col=0)
        add_wasserstoff_kernnetz(
            n,
            wkn,
            costs,
            params.planning_horizons,
            params.H2_transmission_efficiency,
            params.H2_retrofit,
            current_horizon,
        )

    transmission_costs_from_modified_cost_data(
        n,
        costs,
        params.transmission_costs,
    )

    if params.must_run is not None:
        must_run(n, params.must_run, params.planning_horizons, current_horizon)

    if params.H2_plants["enable"]:
        if params.H2_plants["start"] <= current_horizon:
            add_hydrogen_turbines(n, params.H2_plants)
        if params.H2_plants["force"] <= current_horizon:
            force_retrofit(n, params.H2_plants)

    enforce_transmission_project_build_years(
        n, current_horizon, params.onshore_nep_force
    )
    drop_duplicate_transmission_projects(n)
    force_connection_nep_offshore(
        n,
        current_horizon,
        costs,
        params.renewable,
        params.offshore_nep_force,
        inputs.offshore_connection_points,
        inputs.onshore_regions,
        inputs.regions_offshore,
    )
    scale_capacity(n, params.scale_capacity, current_horizon)
    sanitize_custom_columns(n)

    if params.sector["industry"]:
        if current_horizon in params.uba_for_industry:
            if current_horizon not in [2025, 2030, 2035]:
                logger.error(
                    "The UBA for industry data is only available for 2025, 2030 and 2035. Please check your config."
                )
            modify_industry_demand(
                n,
                current_horizon,
                inputs.new_industrial_energy_demand,
                inputs.industrial_production_per_country_tomorrow,
                inputs.industry_sector_ratios,
                scale_non_energy=params.scale_industry_non_energy,
            )
        scale_industry_elec_to_2025(n, inputs.industrial_demand_2025)
    else:
        logger.info(
            "Skipping industry demand modifications because industry is disabled."
        )
    scale_DE_elec_load_to_AGEB(n)

    scale_DE_heat_load(
        n,
        DE_factor=params.space_heat_DE_factor[current_horizon],
        EU_factor=params.space_heat_EU_factor[current_horizon],
    )

    # WHY Step 7: myopic IRENA brownfield ≠ local MaStR; overwrite N01–N11, adjust DE-rest.
    lueneburg_cfg = getattr(params, "lueneburg", {}) or {}
    _cap_path = Path(__file__).parent / "modify_lueneburg_existing_capacities.py"
    _spec = importlib.util.spec_from_file_location(
        "modify_lueneburg_existing_capacities", _cap_path
    )
    _mod = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(_mod)
    is_base_year = current_horizon == params.horizons[0]
    if is_base_year:
        # Only the base year: later horizons inherit MaStR stock + earlier
        # investments via brownfield; overwriting again would delete them.
        _mod.apply_if_enabled(n, lueneburg_cfg)
        freeze_lueneburg_base_year(n, lueneburg_cfg)
        scale_lueneburg_base_year_electricity(n, lueneburg_cfg)
        add_lueneburg_existing_batteries(n, lueneburg_cfg)
    # every horizon: Lüneburg plastic waste may go to DE-rest incinerators
    export_lueneburg_waste_to_de_rest(n, lueneburg_cfg)
    # S1: Abwasserwärme Fernwärme Hansestadt (10.4 MW_th heat pump from 2030)
    add_lueneburg_wastewater_waste_heat(
        n, lueneburg_cfg, current_horizon, inputs, params
    )
    # S1: new power capacity in the Landkreis only renewable (after H2 turbines)
    ban_new_fossil_power_lueneburg(n, lueneburg_cfg)
    # every horizon: tie-breaker so grid batteries/PtX are not spread into LG
    surcharge_lueneburg_system_assets(n, lueneburg_cfg)

    # WHY: MaStR existing can sit slightly above atlite p_nom_max (e.g. N09
    # onwind) → HiGHS infeasible. Raise max to min. Also fill NaN boiler
    # profiles on empty rural/urban nodes (no heat demand → all-NaN p_min_pu).
    bad_g = n.generators.p_nom_max < n.generators.p_nom_min
    if bad_g.any():
        logger.warning(
            "Raising p_nom_max to p_nom_min for %s generators: %s",
            int(bad_g.sum()),
            list(n.generators.index[bad_g]),
        )
        n.generators.loc[bad_g, "p_nom_max"] = n.generators.loc[bad_g, "p_nom_min"]
    if hasattr(n, "links_t"):
        if "p_min_pu" in n.links_t:
            n.links_t.p_min_pu = n.links_t.p_min_pu.fillna(0.0)
        if "p_max_pu" in n.links_t:
            n.links_t.p_max_pu = n.links_t.p_max_pu.fillna(1.0)

    if current_horizon in params.limit_cross_border_flows_ac:
        limit_cross_border_flows_ac(
            n, params.limit_cross_border_flows_ac[current_horizon]
        )

    if current_horizon in params.deactivate_early_transmission_expansion:
        deactivate_early_transmission_expansion(n, current_horizon)

    logger.info("Completed PyPSA-DE modifications for horizon %s.", current_horizon)
    return n
