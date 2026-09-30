"""Historical insider purchases from SEC's quarterly "Insider Transactions Data Sets".

https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets
Each quarter is one zip (<YYYY>q<N>_form345.zip) with tab-separated tables:
  SUBMISSION.tsv      accession, filing date, issuer CIK/ticker, document type
  REPORTINGOWNER.tsv  accession, owner CIK/name, relationship, officer title
  NONDERIV_TRANS.tsv  accession, transaction date/code/shares/price/A-D flag

Shares outstanding for the market-cap filter come from the XBRL "frames" API
(one call per calendar quarter covers every filer).
"""
from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.tracker import is_c_suite  # noqa: E402  (same role rules as the live tracker)

DATASET_URL = ("https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
               "{y}q{q}_form345.zip")
FRAMES_URL = ("https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/"
              "shares/CY{y}Q{q}I.json")

REQUIRED = {
    "SUBMISSION.tsv": ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK",
                       "ISSUERNAME", "ISSUERTRADINGSYMBOL"],
    "REPORTINGOWNER.tsv": ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME",
                           "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"],
    "NONDERIV_TRANS.tsv": ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                           "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"],
}


def quarters(start: str, end: str):
    s, e = pd.Period(start, "Q"), pd.Period(end, "Q")
    p = s
    while p <= e:
        yield p.year, p.quarter
        p += 1


def _session(user_agent: str) -> requests.Session:
    if "@" not in (user_agent or ""):
        raise SystemExit("set SEC_USER_AGENT='Your Name you@example.com' (SEC requirement)")
    s = requests.Session()
    s.headers["User-Agent"] = user_agent
    return s


def download(start: str, end: str, out_dir: Path, user_agent: str) -> list[Path]:
    """Download quarterly zips + shares-outstanding frames (cached on disk)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    s = _session(user_agent)
    paths = []
    for y, q in quarters(start, end):
        for url, name in ((DATASET_URL.format(y=y, q=q), f"{y}q{q}_form345.zip"),
                          (FRAMES_URL.format(y=y, q=q), f"shares_CY{y}Q{q}I.json")):
            p = out_dir / name
            if not p.exists():
                r = s.get(url, timeout=120)
                time.sleep(0.2)
                if r.status_code == 404:          # quarter not published yet
                    print(f"  not available yet: {name}")
                    continue
                r.raise_for_status()
                p.write_bytes(r.content)
                print(f"  downloaded {name} ({len(r.content) / 1e6:.1f} MB)")
            if name.endswith(".zip"):
                paths.append(p)
    return paths


def _read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    member = next((n for n in z.namelist() if n.upper().endswith(name.upper())), None)
    if member is None:
        raise ValueError(f"{z.filename}: missing {name}")
    df = pd.read_csv(io.BytesIO(z.read(member)), sep="\t", dtype=str, quoting=3,
                     on_bad_lines="skip", encoding_errors="replace")
    missing = [c for c in REQUIRED[name] if c not in df.columns]
    if missing:
        raise ValueError(f"{z.filename}:{name} lacks columns {missing}; SEC schema changed?")
    return df[REQUIRED[name]]


def _role(relationship: str, title: str) -> str | None:
    rel = (relationship or "").lower()
    roles = []
    if "officer" in rel and is_c_suite(title):
        roles.append(title)
    if "director" in rel:
        roles.append("Director")
    return ", ".join(roles) if roles else None


def load_purchases(zips: list[Path]) -> pd.DataFrame:
    """Open-market purchases ('P', acquired) by C-suite officers or directors, one row per
    owner x transaction, with filing date (when the public could first see it)."""
    frames = []
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            sub = _read(z, "SUBMISSION.tsv")
            own = _read(z, "REPORTINGOWNER.tsv")
            tx = _read(z, "NONDERIV_TRANS.tsv")
        sub = sub[sub.DOCUMENT_TYPE == "4"]                       # skip 4/A amendments
        tx = tx[(tx.TRANS_CODE == "P") & (tx.TRANS_ACQUIRED_DISP_CD == "A")]
        own = own.assign(role=[_role(r, t) for r, t in zip(own.RPTOWNER_RELATIONSHIP, own.RPTOWNER_TITLE)])
        own = own[own.role.notna()]
        df = tx.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "accession": df.ACCESSION_NUMBER,
        "filed": pd.to_datetime(df.FILING_DATE, format="mixed", dayfirst=True, errors="coerce"),
        "tx_date": pd.to_datetime(df.TRANS_DATE, format="mixed", dayfirst=True, errors="coerce"),
        "issuer_cik": pd.to_numeric(df.ISSUERCIK, errors="coerce"),
        "issuer_name": df.ISSUERNAME,
        "ticker": df.ISSUERTRADINGSYMBOL.str.upper().str.strip(),
        "owner_cik": pd.to_numeric(df.RPTOWNERCIK, errors="coerce"),
        "owner_name": df.RPTOWNERNAME,
        "role": df.role,
        "shares": pd.to_numeric(df.TRANS_SHARES, errors="coerce"),
        "price": pd.to_numeric(df.TRANS_PRICEPERSHARE, errors="coerce"),
    })
    out = out.dropna(subset=["filed", "tx_date", "issuer_cik", "owner_cik", "shares", "price", "ticker"])
    out = out[(out.shares > 0) & (out.price > 0) & ~out.ticker.isin(["", "NONE", "N/A", "NA"])]
    out["value"] = out.shares * out.price
    return out.drop_duplicates(["issuer_cik", "owner_cik", "tx_date", "shares", "price"]).reset_index(drop=True)


def load_shares_outstanding(data_dir: Path) -> pd.DataFrame:
    """[cik, as_of, shares] from cached XBRL frames (as_of = quarter end, point-in-time-ish)."""
    import json
    rows = []
    for p in sorted(data_dir.glob("shares_CY*.json")):
        d = json.loads(p.read_text())
        for f in d.get("data", []):
            rows.append((int(f["cik"]), pd.Timestamp(f["end"]), float(f["val"])))
    df = pd.DataFrame(rows, columns=["cik", "as_of", "shares"])
    return df.groupby(["cik", "as_of"], as_index=False).shares.sum().sort_values("as_of")
