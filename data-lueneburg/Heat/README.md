# Heat overlays (Landkreis Lüneburg)

| File | Role |
|------|------|
| `heitkötter…/yearly_time_series_*_id=3355.csv` | Space + DHW load shape (15 min, MW) for AGS 3355 |
| `lueneburg_heat_demand_annual.csv` | Per-node annual GWh = Heitkötter totals × population share |
| `lueneburg_dh_share.csv` | Exogenous Fernwärme fraction per node (today / 2025) |
| `lueneburg_dh_share_2040.csv` | Pathway DH shares for horizon ≥2040 (from `dh_share_2040_source.csv`) |
| `dh_share_2040_source.csv` | Original node-name % shares (Hansestadt / Samtgemeinden) |
| `Gemeinden_…FW_Anteile(in).csv` | Source register for today’s DH shares |

Wired by `modify_heat_demand.py` and `modify_lueneburg_dh_share.py`
(horizon-aware: 2025 → today CSV, 2040 → pathway CSV).

S1 also injects **10.4 MW_th Abwasserwärme** (2 × 5.2 MW heat pumps) on `N01AC urban central heat`
(from 2030) via `modify_prenetwork.add_lueneburg_wastewater_waste_heat`.

Electricity demand is **not** here — see `data-lueneburg/Electricity/`.
