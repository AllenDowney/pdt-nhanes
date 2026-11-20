"""Build the NHANES records from NCHS's HTML codebooks and the XPT data files.

Usage (under the memory cap):
    python build/build_records.py

Reads build/slice.yaml (the cycles and files) and, for each cycle and file, the codebook
(data/raw/<FILE>.htm) and the data (data/raw/<FILE>.xpt). Every variable of each file is built:
the files are small. Writes metadata/releases/<release>/<table>/variables.jsonl,
metadata/derived/scale_signature.jsonl and build/report.md.

Where each field comes from (STANDARD.md §3):
- label (SAS Label), question text (English Text and English Instructions), universe (Target),
  codes and labels with their counts, value ranges: the codebook (publisher)
- which codes occur, availability counts, storage type: the data file
- missing types: rule nhanes_missing_from_labels (the codebook doesn't mark codes as missing;
  its labels are "Refused", "Don't know" and, for '.', "Missing")
- measure: none yet (the files are taken whole, not chosen)
- scale signatures: the shared library's rule (pdt.derived)
"""

import collections
import html
import json
import re
from pathlib import Path

import pandas as pd
import pyreadstat
import yaml

try:
    from pdt.derived import scale_signatures
except ImportError:
    import os
    import sys
    sys.path.insert(0, str(Path(os.environ.get("PDT", Path(__file__).resolve().parents[2] / "PublicDataToolkit"))
                           / "lib"))
    from pdt.derived import scale_signatures

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
META = ROOT / "metadata"
DATASET = "nhanes"
RULE_VERSION = "0.1"

# ---------------------------------------------------------------- the codebook

BLOCK = re.compile(r'<div class="pagebreak">(.*?)</div>\s*(?=<div class="pagebreak">|</div>|<div)', re.S)
TITLE = re.compile(r'<h3 class="vartitle" id="(?P<id>[^"]+)">(?P<title>.*?)</h3>', re.S)
DL = re.compile(r"<dt>(?P<key>.*?)</dt>\s*<dd[^>]*>(?P<value>.*?)</dd>", re.S)
ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def text(fragment):
    """Rule nhanes_text: tags removed, entities unescaped, line breaks from the page source joined."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_codebook(path):
    """{NAME: entry} for every variable block in an NHANES HTML codebook."""
    page = path.read_text(encoding="utf-8", errors="replace")
    entries = {}
    for chunk in page.split('<div class="pagebreak">')[1:]:
        m = TITLE.search(chunk)
        if not m:
            continue
        info = {text(d.group("key")).rstrip(":").strip(): text(d.group("value")) for d in DL.finditer(chunk)}
        rows = []
        table = chunk[chunk.find("<tbody>"):chunk.find("</tbody>")]
        for r in ROW.findall(table):
            cells = [text(c) for c in CELL.findall(r)]
            if len(cells) >= 3:
                rows.append({"code": cells[0], "label": cells[1], "count": int(cells[2]) if cells[2].isdigit() else None,
                             "skip": cells[4] if len(cells) > 4 else ""})
        name = info.get("Variable Name") or m.group("id")
        entries[name.upper()] = {"name": name, "info": info, "rows": rows, "anchor": m.group("id")}
    return entries


# ---------------------------------------------------------------- rules

MISSING_LABELS = {"refused": ["refused"], "don't know": ["dk"], "missing": ["unspecified"]}
RULE_MISSING = {"origin": "rule", "rule": "nhanes_missing_from_labels", "rule_version": RULE_VERSION,
                "note": "The codebook doesn't mark codes as missing. Its labels 'Refused' and 'Don't know' "
                        "(7, 9, 77, 99, 7777, 9999 ...) are typed refused and dk; '.' labeled 'Missing' is "
                        "missing with no reason given (unspecified): it covers skips and nonresponse alike."}


def norm(label):
    return label.replace("’", "'").strip().lower()


def parse_printed(code):
    """The range's ends as printed."""
    lo, hi = re.fullmatch(r"(-?[\d.eE+-]+)\s+to\s+(-?[\d.eE+-]+)", code).groups()
    return code_value(lo), code_value(hi)


def code_value(code):
    if code == ".":
        return "."
    x = float(code)
    return int(x) if x.is_integer() else x


def parse_range(code):
    """(low, high) for "X to Y", each widened by half a unit of the codebook's last printed decimal,
    since the codebook rounds the ends of a range (a weight of 338363.600192 shows as 338363.6)."""
    m = re.fullmatch(r"(-?[\d.eE+-]+)\s+to\s+(-?[\d.eE+-]+)", code)
    if not m:
        return None
    def slack(s):
        return 0.5 * 10 ** -len(s.split(".")[1]) if "." in s else 0
    return (code_value(m.group(1)) - slack(m.group(1)), code_value(m.group(2)) + slack(m.group(2)))


# ---------------------------------------------------------------- build

def build_record(release, table, entry, col, sources):
    info = entry["info"]
    rec = {"dataset": DATASET, "release": release, "table": table, "name": entry["name"]}
    if info.get("SAS Label"):
        rec["label"] = info["SAS Label"]
    q = {}
    if info.get("English Text"):
        q["text"] = info["English Text"]
    if info.get("English Instructions"):
        q["instructions"] = info["English Instructions"]
    if q.get("text"):
        rec["questions"] = [q]
    if info.get("Target"):
        rec["universe"] = info["Target"]
    present = col.dropna()
    num = pd.to_numeric(present, errors="coerce")
    rec["storage_type"] = ("string" if num.isna().any() else
                           "integer" if len(num) == 0 or (num % 1 == 0).all() else "float")
    values, ranges, report = [], [], []
    for row in entry["rows"]:
        rng = parse_range(row["code"])
        if rng:
            ranges.append(rng)
            continue
        try:
            code = code_value(row["code"])
        except ValueError:
            report.append(("unparsed", f"{release}/{table} `{entry['name']}` code {row['code']!r} ({row['label']})"))
            continue
        types = MISSING_LABELS.get(norm(row["label"]))
        item = {"code": code, "label": row["label"],
                "kind": "missing" if types else "valid"}
        if types:
            item["missing_types"] = types
        if row["count"] is not None:
            item["n"] = row["count"]
        values.append(item)
    if values:
        rec["values"] = values
    if ranges:
        printed = [parse_printed(row["code"]) for row in entry["rows"] if parse_range(row["code"])]
        rec["valid_range"] = {"min": min(lo for lo, _ in printed), "max": max(hi for _, hi in printed)}
    missing = {v["code"] for v in values if v["kind"] != "valid"}
    # Counts in the data, with '.' for blanks.
    counts = collections.Counter({("." if pd.isna(k) else code_value(k) if not isinstance(k, str) else k): int(v)
                                  for k, v in col.value_counts(dropna=False).items()})
    n = sum(c for code, c in counts.items() if code not in missing and code != ".")
    rec["availability"] = [{"wave": release, "n": n}] if n else []
    rec["source_location"] = {"source": sources["codebook"], "anchor": entry["anchor"]}
    fields = {"storage_type": {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                               "source": sources["data"]},
              "availability": {"origin": "data_file", "source": sources["data"],
                               "note": "n counts the cases whose code isn't missing."}}
    if "questions" in rec:
        fields["questions"] = {"origin": "rule", "rule": "nhanes_text", "rule_version": RULE_VERSION,
                               "source": sources["codebook"],
                               "note": "The codebook's English Text, with HTML tags removed and line breaks joined."}
    rec["provenance"] = {"record": {"origin": "publisher", "source": sources["codebook"],
                                    "location": f"#{entry['anchor']}"},
                         "fields": fields}
    if missing:
        rec["provenance"]["codes"] = RULE_MISSING
    # C4: codes in the data that the codebook doesn't list (and aren't in a range).
    listed = {v["code"] for v in values}
    extra = [(c, k) for c, k in counts.items()
             if c not in listed and not (c != "." and isinstance(c, (int, float))
                                         and any(lo <= c <= hi for lo, hi in ranges))]
    if extra and entry["rows"]:
        report.append(("undocumented", f"{release}/{table} `{entry['name']}`: {len(extra)} codes not in the codebook, "
                                       f"e.g. {sorted(extra, key=str)[:5]}"))
    # C5: the codebook's counts against the data's.
    for row in entry["rows"]:
        rng = parse_range(row["code"])
        if row["count"] is None:
            continue
        if rng:
            got = sum(k for c, k in counts.items() if c != "." and isinstance(c, (int, float)) and rng[0] <= c <= rng[1]
                      and c not in listed)
        else:
            try:
                got = counts.get(code_value(row["code"]), 0)
            except ValueError:
                continue
        if got != row["count"]:
            report.append(("counts", f"{release}/{table} `{entry['name']}` {row['code']} ({row['label']}): "
                                     f"codebook {row['count']}, data {got}"))
    return rec, report


def main():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    report, coverage, all_records = [], [], []
    for release, rel in spec["releases"].items():
        for table, fname in rel["files"].items():
            entries = parse_codebook(RAW / f"{fname}.htm")
            # pyreadstat, not pandas: pandas' XPORT reader turns an exact zero into 5.4e-79.
            frame, _ = pyreadstat.read_xport(str(RAW / f"{fname}.xpt"), encoding="latin1")
            cols = {c.upper(): c for c in frame.columns}
            coverage.append(f"- {release}/{table} ({fname}): {len(frame):,} rows, {len(cols)} columns, "
                            f"{len(entries)} codebook entries; in the data only: {sorted(set(cols) - set(entries))}; "
                            f"in the codebook only: {sorted(set(entries) - set(cols))}")
            sources = {"codebook": f"nhanes-{release}-{table}-codebook", "data": f"nhanes-{release}-{table}-data"}
            recs = []
            for name, entry in entries.items():
                if name not in cols:
                    continue
                rec, rep = build_record(release, table, entry, frame[cols[name]], sources)
                recs.append(rec)
                report += rep
            write_jsonl(META / "releases" / release / table / "variables.jsonl", recs)
            all_records += recs
            del frame
    write_jsonl(META / "derived" / "scale_signature.jsonl", scale_signatures(all_records, per_release=True))
    write_report(all_records, coverage, report)
    print(f"{len(all_records)} records; see build/report.md")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_report(records, coverage, report):
    by = collections.defaultdict(list)
    for kind, line in report:
        by[kind].append(line)
    per = collections.Counter((r["release"], r["table"]) for r in records)
    lines = ["# Build report: nhanes", "", "Generated by `build/build_records.py`. Do not edit.", "",
             *[f"- {rel}/{tab}: {n} records" for (rel, tab), n in sorted(per.items())],
             "", "## C3: coverage", "", *coverage, ""]
    for kind, title in [("undocumented", "C4: codes in the data that the codebook doesn't list"),
                        ("counts", "C5: codebook counts vs the data"),
                        ("unparsed", "Code cells the parser couldn't read")]:
        lines += [f"## {title}", "", *([f"- {x}" for x in by[kind]] or ["None."]), ""]
    (ROOT / "build" / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
