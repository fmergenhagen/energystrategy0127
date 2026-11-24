# Lüneburg KPIs (S0 vs S1)

**KPI** = key performance indicator: numbers that answer the RQ.

## Framing
- **S0 = 2025 today**; **S1 = 2040 no fossil heating**
- Exogenous 521 GWh electricity = **today only** (not the full 2040 demand)
- 2040 heat-electricity is endogenous (HP / resistive heaters)

## Families
1. **demand** — today exogenous el; S1 electricity-for-heat; EV load (separate)
2. **capacity** — RE / HP / boilers on N01–N11
3. **storage** — battery + heat/TES (water tanks / thermal stores)
4. **imports** — net electricity LG←DE-rest (no abroad imports in v1)
5. **cost / co2** — system supporting metrics
6. **baselines** — Energiemonitor 521 GWh; MaStR wind≈266 / solar≈79 MW

## EVs
On by default (DE transport pathway → more EVs by 2040).
They raise electricity demand independently of the heat RQ.
Report as own KPI, or freeze EV share at 2025 for a heat-only run.

## Imports
Abroad: off (`countries: [DE]`, `sector.imports.enable: false`).
Landkreis←DE-rest: yes — net GWh and peak MW over lines/links to DE-restAC.

## Compare
Primary: **S0@2025 vs S1@2040**.

## Results

```
scenario horizon kpi_family                          kpi             carrier  unit         value
      S0    2025     demand         electricity_for_heat heat_pump+resistive   GWh  4.113430e+02
      S0    2025   capacity                    installed              onwind    MW  2.655400e+02
      S0    2025   capacity                      optimal              onwind    MW  2.660540e+02
      S0    2025   capacity                          new              onwind    MW  5.140000e-01
      S0    2025   capacity                    installed               solar    MW  2.296670e+02
      S0    2025   capacity                      optimal               solar    MW  2.345630e+02
      S0    2025   capacity                          new               solar    MW  4.895000e+00
      S0    2025   capacity                    installed           heat_pump    MW  0.000000e+00
      S0    2025   capacity                      optimal           heat_pump    MW  4.616600e+01
      S0    2025   capacity                          new           heat_pump    MW  4.616600e+01
      S0    2025   capacity                    installed       boiler_fossil    MW  0.000000e+00
      S0    2025   capacity                      optimal       boiler_fossil    MW  0.000000e+00
      S0    2025   capacity                          new       boiler_fossil    MW  0.000000e+00
      S0    2025    storage              energy_capacity             battery   MWh  8.787000e+00
      S0    2025    storage              energy_capacity            heat_tes   MWh  9.919580e+02
      S0    2025     demand               electricity_ev   land transport EV   GWh  4.018500e+01
      S0    2025    imports net_electricity_from_DE_rest                  AC   GWh  1.619010e+02
      S0    2025    imports     peak_import_from_DE_rest                  AC    MW  1.646550e+02
      S0    2025       cost                    objective                       EUR  9.257356e+10
      S0    2025       cost        annualised_capex_opex                     EUR/a  1.677755e+11
      S0    2025        co2                         note                         -  0.000000e+00
      S1    2040     demand         electricity_for_heat heat_pump+resistive   GWh  3.639110e+02
      S1    2040   capacity                    installed              onwind    MW  2.655400e+02
      S1    2040   capacity                      optimal              onwind    MW  3.205000e+02
      S1    2040   capacity                          new              onwind    MW  5.496000e+01
      S1    2040   capacity                    installed               solar    MW  2.296680e+02
      S1    2040   capacity                      optimal               solar    MW  7.553550e+02
      S1    2040   capacity                          new               solar    MW  5.256870e+02
      S1    2040   capacity                    installed           heat_pump    MW  0.000000e+00
      S1    2040   capacity                      optimal           heat_pump    MW  8.917700e+01
      S1    2040   capacity                          new           heat_pump    MW  8.917700e+01
      S1    2040   capacity                    installed       boiler_fossil    MW  0.000000e+00
      S1    2040   capacity                      optimal       boiler_fossil    MW  0.000000e+00
      S1    2040   capacity                          new       boiler_fossil    MW  0.000000e+00
      S1    2040    storage              energy_capacity             battery   MWh  4.340380e+03
      S1    2040    storage              energy_capacity            heat_tes   MWh  2.150660e+03
      S1    2040     demand               electricity_ev   land transport EV   GWh  5.625860e+02
      S1    2040    imports net_electricity_from_DE_rest                  AC   GWh -4.473630e+04
      S1    2040    imports     peak_import_from_DE_rest                  AC    MW  4.900080e+02
      S1    2040       cost                    objective                       EUR  1.132573e+11
      S1    2040       cost        annualised_capex_opex                     EUR/a  1.540585e+11
      S1    2040        co2                         note                         -  0.000000e+00
baseline    2025   baseline     energiemonitor_verbrauch         electricity   GWh  5.210000e+02
baseline    2025   baseline              mastr_installed              onwind    MW  2.655400e+02
baseline    2025   baseline              mastr_installed               solar    MW  7.891100e+01
baseline    2025   baseline              mastr_installed             biomass    MW  3.812500e+01
```
