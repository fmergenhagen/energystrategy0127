#!/usr/bin/env bash
# Rebuild after the N01 region fix (3728598b) + LG-scoped heat ban (95d96374).
# 1) S1: everything downstream of add_lueneburg_synthetic_buses (57 jobs)
# 2) copy S1 resources to S0 and S1_4h (identical up to compose), then
#    time_aggregation + compose + solve there
# 3) 2025 validation against Energiemonitor
# Run from anywhere: scripts/lueneburg/run_rebuild_regionfix.sh > logs/lueneburg/run_rebuild_regionfix.log 2>&1
# Watch:  tail -f logs/lueneburg/run_rebuild_regionfix.log
set -euo pipefail
cd "$(dirname "$0")/../.."  # repo root (pypsa-de/)

CFG=config/config.lueneburg.yaml
echo "=== $(date) S1 full rebuild"
pixi run snakemake -call results/lueneburg/S1/networks/solved_2040.nc \
  --configfile=$CFG -j 1 --rerun-triggers mtime \
  --forcerun add_lueneburg_synthetic_buses build_population_layouts

for S in S0 S1_4h; do
  echo "=== $(date) copy S1 resources -> $S"
  rsync -a --exclude 'networks/composed_*' resources/lueneburg/S1/ resources/lueneburg/$S/
done

echo "=== $(date) S0 2025"
pixi run snakemake -call results/lueneburg/S0/networks/solved_2025.nc \
  --configfile=$CFG -j 1 --rerun-triggers mtime \
  --allowed-rules time_aggregation compose_network solve_network \
  --forcerun time_aggregation compose_network

echo "=== $(date) S1_4h 2040"
pixi run snakemake -call results/lueneburg/S1_4h/networks/solved_2040.nc \
  --configfile=$CFG -j 1 --rerun-triggers mtime \
  --allowed-rules time_aggregation compose_network solve_network \
  --forcerun time_aggregation compose_network

echo "=== $(date) validation S0 2025"
pixi run python scripts/pypsa-de/validate_lueneburg_2025.py
echo "=== $(date) ALL DONE"
