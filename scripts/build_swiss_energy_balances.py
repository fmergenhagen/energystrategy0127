# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Extract historic Swiss energy balances in TWh/a from Excel file.
"""

import logging

import pandas as pd

from scripts._helpers import configure_logging, set_scenario_config

logger = logging.getLogger(__name__)


def load_sheet(xlsx: pd.ExcelFile, sheet_name: str, index_col: str) -> pd.DataFrame:
    df = xlsx.parse(sheet_name, skiprows=5, index_col=index_col)
    # WHY strip: BFE 2025 tables indent sub-rows with leading spaces.
    df.index = df.index.map(lambda x: x.strip() if isinstance(x, str) else x)
    year_columns = [col for col in df.columns if isinstance(col, int)]
    df = df[year_columns].dropna(how="all", axis=0)
    return df


def row(df: pd.DataFrame, *labels: str) -> pd.Series:
    """Pick first matching index label; sum if several labels given.

    WHY aliases: BFE renamed rows in 2024/2025 VWZ tables
    (e.g. Total Endenergieverbrauch → Total; Schiene → Schiene*).
    """
    present = [lab for lab in labels if lab in df.index]
    if not present:
        raise KeyError(f"none of {labels} in {list(df.index)}")
    if len(present) == 1 and len(labels) == 1:
        return df.loc[present[0]]
    # sum requested parts that exist (cooking split; alias fallbacks)
    return df.loc[present].sum(axis=0)


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake("build_swiss_energy_balances")

    configure_logging(snakemake)
    set_scenario_config(snakemake)

    xlsx = pd.ExcelFile(snakemake.input.xlsx)

    tab1 = load_sheet(xlsx, "Tabelle1", index_col="Verwendungszweck")

    tab17 = load_sheet(xlsx, "Tabelle17", index_col="Verwendungszweck")

    tab18 = load_sheet(xlsx, "Tabelle18", index_col="Verwendungszweck")

    tab26 = load_sheet(xlsx, "Tabelle26", index_col="Verwendungszweck")

    tab28 = load_sheet(xlsx, "Tabelle28", index_col="Verwendungszweck")

    tab35 = load_sheet(xlsx, "Tabelle35", index_col="Verkehrsträger")

    tab38 = load_sheet(xlsx, "Tabelle38", index_col="Verkehrsträger")

    data = {
        "total residential": row(
            tab17, "Total Endenergieverbrauch", "Total"
        ),
        "total residential space": row(tab17, "Raumwärme"),
        "total residential water": row(tab17, "Warmwasser"),
        "total residential cooking": row(
            tab17, "Kochen / Geschirrspüler", "Kochen", "Geschirrspüler"
        ),
        "electricity residential": row(tab18, "Total"),
        "electricity residential space": row(tab18, "Raumwärme"),
        "electricity residential water": row(tab18, "Warmwasser"),
        "electricity residential cooking": row(tab18, "Kochherde"),
        "total services": row(tab26, "Total Endenergie", "Total"),
        "total services space": row(tab26, "Raumwärme"),
        "total services water": row(tab26, "Warmwasser"),
        "total services cooking": row(tab26, "Prozesswärme"),
        "electricity services": row(tab28, "Total Elektrizität", "Total"),
        "electricity services space": row(tab28, "Raumwärme"),
        "electricity services water": row(tab28, "Warmwasser"),
        "electricity services cooking": row(tab28, "Prozesswärme"),
        "total rail": row(tab35, "Schiene"),
        "total road": row(tab35, "Strasse"),
        "electricity road": row(tab38, "Strasse"),
        "electricity rail": row(tab38, "Schiene", "Schiene*"),
        "total domestic aviation": row(tab35, "Luft (Inland)"),
        "total international aviation": row(tab1, "int. Flugverkehr"),
        "total domestic navigation": row(tab35, "Wasser"),
        "total international navigation": 0,
    }

    df = pd.DataFrame(data)
    df.columns.name = None
    df.index = pd.MultiIndex.from_product([["CH"], df.index], names=["country", "year"])

    # convert PJ/a to TWh/a
    df /= 3.6

    df.to_csv(snakemake.output.csv)
