# Run scripts — Lüneburg case study

Shell wrappers around snakemake for the Lüneburg scenarios (S0, S1, S1_4h).
Each script `cd`s to the repo root itself, so it can be called from anywhere.
Logs go to `logs/lueneburg/` (git-ignored).

| Script | Purpose | Status |
|---|---|---|
| `run_recompose_threshold.sh` | compose + solve S0@2025, S1@2040, S1_4h@2040, then 2025 validation | current: use after config changes that only affect compose/solve |
| `run_rebuild_regionfix.sh` | full S1 rebuild from `add_lueneburg_synthetic_buses`, copy S1 resources to S0/S1_4h, compose + solve, validation | current: use after topology / resource changes |
| `run_overnight.sh` | S0@2025, S1@2025, S1@2040 compose + solve, then KPIs | older (pre S1_4h) |
| `run_compose_solve.sh` | S0 + S1 compose + solve in one snakemake call | older |
| `run_rerun.sh` | S0 + S1 without `--allowed-rules` (full DAG) | older |

Note: the copy step in `run_rebuild_regionfix.sh` (rsync S1 resources → S0/S1_4h)
and `--allowed-rules` are shortcuts; for the final paper runs do a clean rebuild
of every scenario without them.
