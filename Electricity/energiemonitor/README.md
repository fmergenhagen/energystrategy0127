# Avacon Energiemonitor — Landkreis Lüneburg

Daily series (export date in filenames):

| File | Content |
|------|---------|
| `*_renewables_*.csv` | Biomass, hydro, other, PV, wind, Verbrauch (MWh/day) |
| `*_balance_*.csv` | Netzbezug, Netzeinspeisung, Netzbilanz |
| `*_autarky_*.csv` | Eigenversorgung (%) |

## 2025 headline numbers (used in V1)

- Verbrauch ≈ **521 GWh**
- Local gen ≈ **713 GWh** (wind ≈488, biomass ≈121, PV ≈73)
- Mean Eigenversorgung ≈ **85%** (min days ≈27%; share of days ≥65% ≈85%)
- Existing capacity (peak-day estimate): wind ≈**228 MW**, PV ≈**44 MW**, biomass ≈**17 MW**

Wired into V1 via `V1/energiemonitor.jl` → `create_v1_testdata.jl` (`existing_capacity.csv`, residual load).
