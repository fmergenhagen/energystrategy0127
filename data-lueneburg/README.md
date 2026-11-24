# data-lueneburg — study inputs for the Landkreis Lüneburg case study

Small, derived input data for the Lüneburg overlay of pypsa-de
(`config/config.lueneburg.yaml`, `config/scenarios.lueneburg.yaml`).
Read by the scripts in `scripts/pypsa-de/` (paths are relative to the repo root).
History imported from the former stand-alone `Data/` repo via `git subtree`.

| Folder | Content | Used by |
|---|---|---|
| `nodes/` | 11 Lüneburg nodes (N01–N11): polygons, LAU→node map, adjacency, population; build scripts for nodes and `data/busshapes/simplified_12_osm.geojson` | `add_lueneburg_synthetic_buses.py`, clustering (`custom_busshapes`) |
| `Electricity/` | Energiemonitor 2025 extracts, MaStR extracts (AGS 03355), existing capacities and batteries per node, normalised Avacon load profile, annual demand | `modify_electricity_demand.py`, `modify_lueneburg_existing_capacities.py`, `modify_prenetwork.py`, `validate_lueneburg_2025.py` |
| `Heat/` | annual heat demand per node, district-heating shares (2025 register, 2040 pathway), Heitkötter et al. hourly series for AGS 3355 | `modify_heat_demand.py`, `modify_lueneburg_dh_share.py` |
| `kpis/` | KPI tables written by `compute_lueneburg_kpis.py` | output |

## Sources and licences

| Data | Source | Licence / status |
|---|---|---|
| MaStR extracts | Bundesnetzagentur Marktstammdatenregister (via pypsa-de `data/mastr`, stand 2023-08) | DL-DE-BY-2.0 |
| Heitkötter et al. series (`Heat/heitkoetter_3355/`) | supplementary material, regionalised heat demand & P2H capacities | CC BY 4.0 |
| Energiemonitor extracts | Energiemonitor Landkreis Lüneburg (download 2026-08-10) | **to check** before public release |
| Avacon load profile (`Electricity/Avacon/load_profile_hourly_norm.csv`) | normalised from Avacon §23c EnWG publications | **to check** before public release |
| LAU polygons | GISCO LAU 2019 (via pypsa-de retrieve) | © EuroGeographics, GISCO terms |

## Not tracked (git-ignored, local only)

- Full Heitkötter package (`Heat/supplementary_material_…`, ~1.2 GB, re-downloadable)
- Avacon raw `.xlsx` publications (`Electricity/Avacon/*.xlsx`)
- Dry-run networks (`*.nc`)
