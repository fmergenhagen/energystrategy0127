#!/usr/bin/env bash
# Recompose + solve all LG scenarios after existing_capacities.threshold_capacity 10 -> 0.1 MW
set -euo pipefail
cd "$(dirname "$0")/../.."  # repo root (pypsa-de/)
CFG=config/config.lueneburg.yaml
for T in results/lueneburg/S0/networks/solved_2025.nc results/lueneburg/S1/networks/solved_2040.nc results/lueneburg/S1_4h/networks/solved_2040.nc; do
  echo "=== $(date) $T"
  pixi run snakemake -call $T --configfile=$CFG -j 1 --rerun-triggers mtime \
    --allowed-rules compose_network solve_network --forcerun compose_network
done
echo "=== $(date) validation S0 2025"
pixi run python scripts/pypsa-de/validate_lueneburg_2025.py
echo "=== $(date) ALL DONE"
