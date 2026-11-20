# Electricity overlays (Landkreis Lüneburg)

| File / folder | Role |
|---------------|------|
| `energiemonitor/` | Annual Verbrauch (2025 ≈ **521 GWh**) and local generation |
| `Avacon/load_profile_hourly_norm.csv` | Hourly shape (8760 shares, sum = 1) |
| `lueneburg_el_demand_annual.csv` | Per-node annual GWh = 521 × population share |
| `mastr_extract/` | Filtered MaStR rows for AGS 03355 |
| `lueneburg_existing_capacity.csv` | Per-node existing MW (MaStR In Betrieb) |
| `build_lueneburg_existing_capacity.py` | Rebuild the capacity CSV from extracts |

**Step 5 demand:** `modify_electricity_demand.py` → `electricity_demand-modified.nc`.

**Step 7 capacities:** MaStR nameplate In Betrieb (wind≈**266**, solar≈**79**, biomass≈**38** MW) —
Energiemonitor peak-day (228/44/17) is a sanity check only, not the overlay source.
Applied in `modify_prenetwork` via `modify_lueneburg_existing_capacities.py` when
`lueneburg.enable`.
