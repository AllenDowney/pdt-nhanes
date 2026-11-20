#!/usr/bin/env python3
"""Write the NHANES data in the common format (STANDARD.md §8).

Reads each cycle's SAS transport files, data/raw/<FILE>.xpt (build/slice.yaml maps each
release's tables to files), and writes data/processed/nhanes-<cycle>-<table>.parquet with the
variables this repo has records for. pdt.release.write_release reads the columns in batches, and
checks each batch, then the whole file, by a round trip against the cells read. Run under the
memory cap.

pyreadstat, not pandas: pandas' XPORT reader turns an exact zero into 5.4e-79
(skills/from-data-labels/SKILL.md). SAS's missing value '.' (NaN once read) becomes Code("."),
as pdt.readers.read_sas gives it. pyreadstat's XPORT reader can't keep the special missing
values .A-.Z apart from '.' (it has no user_missing option); NHANES uses only '.'. Names are
matched case-insensitively.
"""

import sys
from pathlib import Path

import pandas as pd
import pyreadstat
import yaml

try:
    from pdt.data import Code
    from pdt.release import data_source, write_release
except ImportError:
    sys.exit("Needs the PublicDataToolkit library: pip install -e ../PublicDataToolkit, "
             "or run through `make data`.")

ROOT = Path(__file__).resolve().parent.parent


def read_columns(path, names):
    """The named columns of a SAS transport file as a raw frame."""
    _, meta = pyreadstat.read_xport(str(path), metadataonly=True, encoding="latin1")
    actual = {c.upper(): c for c in meta.column_names}
    if missing := [n for n in names if n.upper() not in actual]:
        raise KeyError(f"{path}: no column(s) {missing}")
    cols = [actual[n.upper()] for n in names]
    frame, meta = pyreadstat.read_xport(str(path), usecols=cols, encoding="latin1")
    types = meta.readstat_variable_types
    out = {}
    for name, col in zip(names, cols):
        numeric = types[col] in ("int8", "int16", "int32", "float", "double")
        cells = []
        for c in frame[col].tolist():
            if numeric and isinstance(c, float) and c != c:
                c = Code(".")                          # SAS missing
            cells.append(c)
        out[name] = pd.Series(cells, dtype=object)
    return pd.DataFrame(out)


def read(release, table, names):
    files = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())["releases"][release]["files"]
    path = ROOT / "data" / "raw" / f"{files[table]}.xpt"
    if not path.exists():
        sys.exit(f"{path} is missing; run `make download`")
    return read_columns(path, names), data_source(ROOT, release, table)["id"]


if __name__ == "__main__":
    write_release(ROOT, read)
