#!/bin/bash
set -u
cd "$(dirname "$0")/../.."  # repo root (pypsa-de/)
mkdir -p logs/lueneburg
export XDG_CACHE_HOME=/tmp/xdg-cache MPLCONFIGDIR=/tmp/mpl PYTHONUNBUFFERED=1 TMPDIR=/tmp
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
LOG=logs/lueneburg/overnight_solve.log
echo "==== START $(date) ====" > "$LOG"
.pixi/envs/default/bin/snakemake --unlock --configfile=config/config.lueneburg.yaml >>"$LOG" 2>&1 || true

run_smk() {
  echo "==== TARGET $* $(date) ====" >> "$LOG"
  .pixi/envs/default/bin/snakemake -call "$@" \
    --configfile=config/config.lueneburg.yaml \
    -j 1 --rerun-triggers mtime \
    --allowed-rules compose_network solve_network \
    >> "$LOG" 2>&1
  ec=$?
  echo "==== EXIT $ec for $* $(date) ====" >> "$LOG"
  return $ec
}

run_smk results/lueneburg/S0/networks/solved_2025.nc || exit 1
run_smk results/lueneburg/S1/networks/solved_2025.nc || exit 1
run_smk results/lueneburg/S1/networks/solved_2040.nc || exit 1

echo "==== ALL DONE $(date) ====" >> "$LOG"
echo 0 > logs/lueneburg/overnight_solve.exit
.pixi/envs/default/bin/python scripts/pypsa-de/compute_lueneburg_kpis.py >> "$LOG" 2>&1
echo "==== KPIs DONE $(date) ====" >> "$LOG"
