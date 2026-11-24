#!/bin/bash
set -u
cd "$(dirname "$0")/../.."  # repo root (pypsa-de/)
mkdir -p logs/lueneburg
export XDG_CACHE_HOME=/tmp/xdg-cache MPLCONFIGDIR=/tmp/mpl PYTHONUNBUFFERED=1 TMPDIR=/tmp
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
LOG=logs/lueneburg/compose_solve.log
echo "==== ALLOWED6 $(date) PID=$$ ====" >> "$LOG"
.pixi/envs/default/bin/snakemake -call \
  results/lueneburg/S0/networks/solved_2025.nc \
  results/lueneburg/S1/networks/solved_2040.nc \
  --configfile=config/config.lueneburg.yaml \
  -j 1 --rerun-triggers mtime \
  --allowed-rules compose_network solve_network \
  >> "$LOG" 2>&1
ec=$?
echo "==== EXIT $ec at $(date) ====" >> "$LOG"
echo $ec > logs/lueneburg/compose_solve.exit
exit $ec
