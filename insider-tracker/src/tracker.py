"""SEC EDGAR Form 4 insider cluster-buy tracker.

Pipeline
--------
1. Discover Form 4 filings:
     * backfill  - EDGAR daily form indexes (one file per business day)
     * live      - EDGAR "latest filings" Atom feed (updated through the day)
2. Download each filing's full-submission .txt once, extract the
   <ownershipDocument> XML, and keep only open-market purchases
   (non-derivative, transactionCode == "P", acquired).
3. Keep only purchases by C-suite officers or directors.
4. Group by issuer; a "cluster buy" is >= MIN_INSIDERS distinct qualifying
   insiders buying within the rolling CLUSTER_WINDOW_DAYS window.
5. Estimate market cap (XBRL shares outstanding x latest purchase price)
   and keep clusters under MAX_MARKET_CAP_USD.

SEC fair-access rules: a descriptive User-Agent with contact email is
mandatory and requests must stay under 10/second. Both are enforced here.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element

import pandas as pd
import requests
from defusedxml import ElementTree as SafeET

log = logging.getLogger(__name__)

SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar"
SEC_ATOM_FEED = "https://www.sec.gov/cgi-bin/browse-edgar"
SEC_XBRL_CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityCommonStockSharesOutstanding.json"

# "Chief ... Officer" / CxO titles are C-suite even with an EVP/SVP prefix;
# "President" / "Chair" count only when not qualified as Vice/Assistant/etc.
CHIEF_OFFICER_RE = re.compile(r"\bchief\b.*\bofficer\b|\bc[efotiamrsd]o\b", re.IGNORECASE)
TOP_TITLE_RE = re.compile(r"\b(president|chair(man|woman|person)?)\b", re.IGNORECASE)
NOT_TOP_RE = re.compile(r"\b(vice|vp|assistant|asst|deputy|division|divisional|regional)\b", re.IGNORECASE)


# --------------------------------------------------------------------- config
@dataclass
class TrackerConfig:
    user_agent: str                      # e.g. "Jane Doe jane@example.com" (SEC requirement)
    db_path: Path = Path("data/insider.db")
    min_insiders: int = 2
    cluster_window_days: int = 30
    max_market_cap_usd: float = 2_000_000_000
    min_cluster_value_usd: float = 0.0   # optional floor on total $ bought in the window
    skip_unknown_market_cap: bool = True
    max_requests_per_second: float = 8.0  # SEC limit is 10


@dataclass
class ClusterSignal:
    ticker: str
    issuer_cik: int
    issuer_name: str
    insiders: list[dict]                 # [{name, cik, role, shares, price, value, date}]
    total_value_usd: float
    first_date: dt.date
    last_date: dt.date
    est_market_cap_usd: float | None
    last_price: float
    key: str = field(default="")         # dedupe key: changes when a new insider joins

    def summary(self) -> str:
        mc = f"${self.est_market_cap_usd / 1e6:,.0f}M" if self.est_market_cap_usd else "unknown"
        lines = [
            f"{self.ticker} - {self.issuer_name}  (est. market cap {mc})",
            f"  {len(self.insiders)} insiders bought ${self.total_value_usd:,.0f} "
            f"between {self.first_date} and {self.last_date}:",
        ]
        for i in self.insiders:
            lines.append(f"    - {i['name']} ({i['role']}): {i['shares']:,.0f} sh @ ${i['price']:.2f} "
                         f"= ${i['value']:,.0f} on {i['date']}")
        return "\n".join(lines)


# --------------------------------------------------------------------- HTTP
class SecClient:
    """requests.Session wrapper that enforces SEC's UA + rate-limit policy."""

    def __init__(self, user_agent: str, max_rps: float = 8.0):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must include a contact email, e.g. 'Jane Doe jane@example.com'")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._min_interval = 1.0 / max_rps
        self._last = 0.0
        self._lock = threading.Lock()

    def get(self, url: str, **kw) -> requests.Response:
        for attempt in range(4):
            with self._lock:
                wait = self._last + self._min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            r = self.s.get(url, timeout=30, **kw)
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt * 2)
                continue
            return r
        return r


# --------------------------------------------------------------------- Form 4 parsing
def _txt(el: Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip()


def _flag(el: Element | None, path: str) -> bool:
    return (_txt(el, path) or "").lower() in ("1", "true")


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def is_c_suite(title: str | None) -> bool:
    if not title:
        return False
    if CHIEF_OFFICER_RE.search(title):
        return True
    return bool(TOP_TITLE_RE.search(title)) and not NOT_TOP_RE.search(title)


def insider_role(rel: Element | None) -> str | None:
    """Return a role label if the owner is a director or C-suite officer, else None."""
    title = _txt(rel, "officerTitle")
    roles = []
    if _flag(rel, "isOfficer") and is_c_suite(title):
        roles.append(title)
    if _flag(rel, "isDirector"):
        roles.append("Director")
    return ", ".join(roles) if roles else None


def extract_ownership_xml(submission_text: str) -> str | None:
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", submission_text, re.DOTALL)
    return m.group(0) if m else None


def parse_form4(xml: str, accession: str, filed: str | None = None) -> list[dict]:
    """Return one row per (qualifying owner x open-market purchase) in the filing."""
    root = SafeET.fromstring(xml)
    if (_txt(root, "documentType") or "") != "4":
        return []
    issuer = root.find("issuer")
    ticker = (_txt(issuer, "issuerTradingSymbol") or "").upper().strip()
    if not ticker or ticker in ("NONE", "N/A"):
        return []
    issuer_cik = int(_txt(issuer, "issuerCik") or 0)
    issuer_name = _txt(issuer, "issuerName") or ""

    owners = []
    for ro in root.findall("reportingOwner"):
        role = insider_role(ro.find("reportingOwnerRelationship"))
        if role:
            owners.append(dict(owner_cik=int(_txt(ro, "reportingOwnerId/rptOwnerCik") or 0),
                               owner_name=_txt(ro, "reportingOwnerId/rptOwnerName") or "",
                               role=role))
    if not owners:
        return []

    rows = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _txt(tx, "transactionCoding/transactionCode") != "P":
            continue
        if _txt(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") != "A":
            continue
        shares = _float(_txt(tx, "transactionAmounts/transactionShares/value"))
        price = _float(_txt(tx, "transactionAmounts/transactionPricePerShare/value"))
        date = (_txt(tx, "transactionDate/value") or "")[:10]
        if not shares or not price or not date:
            continue
        for o in owners:
            rows.append(dict(accession=accession, filed=filed, issuer_cik=issuer_cik,
                             issuer_name=issuer_name, ticker=ticker, tx_date=date,
                             shares=shares, price=price, value=shares * price, **o))
    return rows


# --------------------------------------------------------------------- tracker
class InsiderTracker:
    def __init__(self, cfg: TrackerConfig):
        self.cfg = cfg
        self.sec = SecClient(cfg.user_agent, cfg.max_requests_per_second)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cfg.db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS processed (accession TEXT PRIMARY KEY, processed_at TEXT);
            CREATE TABLE IF NOT EXISTS purchases (
                accession TEXT, filed TEXT, issuer_cik INTEGER, issuer_name TEXT, ticker TEXT,
                tx_date TEXT, shares REAL, price REAL, value REAL,
                owner_cik INTEGER, owner_name TEXT, role TEXT);
            CREATE INDEX IF NOT EXISTS ix_p_issuer ON purchases(issuer_cik, tx_date);
            CREATE TABLE IF NOT EXISTS shares_out (issuer_cik INTEGER PRIMARY KEY, shares REAL, as_of TEXT, fetched TEXT);
            CREATE TABLE IF NOT EXISTS alerted (key TEXT PRIMARY KEY, alerted_at TEXT);
        """)

    # ---- filing discovery
    def daily_index_filings(self, day: dt.date) -> list[tuple[str, str, str]]:
        """[(accession, txt_url, filed_date)] for every Form 4 in a day's form index."""
        q = (day.month - 1) // 3 + 1
        url = f"{SEC_ARCHIVES}/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx"
        r = self.sec.get(url)
        if r.status_code == 404:          # weekend / holiday / not yet published
            return []
        r.raise_for_status()
        out = []
        for line in r.text.splitlines():
            if not line.startswith("4 "):  # exact form "4" (skips 4/A amendments)
                continue
            path = line.split()[-1]        # edgar/data/<cik>/<accession>.txt
            acc = path.rsplit("/", 1)[-1].removesuffix(".txt")
            out.append((acc, f"https://www.sec.gov/Archives/{path}", day.isoformat()))
        return out

    def latest_feed_filings(self, pages: int = 3) -> list[tuple[str, str, str]]:
        """Most recent Form 4s from EDGAR's live Atom feed (100 per page)."""
        out, seen = [], set()
        for p in range(pages):
            r = self.sec.get(SEC_ATOM_FEED, params=dict(action="getcurrent", type="4", owner="include",
                                                         count=100, start=p * 100, output="atom"))
            r.raise_for_status()
            root = SafeET.fromstring(r.content)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            entries = root.findall("a:entry", ns)
            for e in entries:
                cat = e.find("a:category", ns)
                if cat is None or cat.get("term") != "4":
                    continue
                m = re.search(r"accession-number=(\d{10}-\d{2}-\d{6})", e.findtext("a:id", "", ns))
                link = e.find("a:link", ns)
                if not m or link is None:
                    continue
                acc = m.group(1)
                cik_m = re.search(r"/data/(\d+)/", link.get("href", ""))
                if acc in seen or not cik_m:
                    continue
                seen.add(acc)
                updated = (e.findtext("a:updated", "", ns) or "")[:10]
                out.append((acc, f"{SEC_ARCHIVES}/data/{cik_m.group(1)}/{acc}.txt", updated))
            if len(entries) < 100:
                break
        return out

    # ---- ingestion
    def _is_processed(self, acc: str) -> bool:
        return self.db.execute("SELECT 1 FROM processed WHERE accession=?", (acc,)).fetchone() is not None

    def ingest(self, filings: list[tuple[str, str, str]]) -> int:
        """Download + parse unseen filings. Returns number of qualifying purchase rows added."""
        added = 0
        todo = [f for f in filings if not self._is_processed(f[0])]
        for n, (acc, url, filed) in enumerate(todo, 1):
            try:
                r = self.sec.get(url)
                r.raise_for_status()
                xml = extract_ownership_xml(r.text)
                rows = parse_form4(xml, acc, filed) if xml else []
            except Exception as e:  # malformed filing: log and move on, don't mark processed
                log.warning("failed %s: %s", acc, e)
                continue
            if rows:
                self.db.executemany(
                    "INSERT INTO purchases VALUES (:accession,:filed,:issuer_cik,:issuer_name,:ticker,"
                    ":tx_date,:shares,:price,:value,:owner_cik,:owner_name,:role)", rows)
                added += len(rows)
            self.db.execute("INSERT OR IGNORE INTO processed VALUES (?, ?)", (acc, dt.datetime.now(dt.UTC).isoformat()))
            if n % 200 == 0:
                self.db.commit()
                log.info("processed %d/%d filings (%d purchase rows so far)", n, len(todo), added)
        self.db.commit()
        return added

    def backfill(self, days: int | None = None) -> int:
        days = days or self.cfg.cluster_window_days
        today = dt.date.today()
        total = 0
        for k in range(days, 0, -1):
            day = today - dt.timedelta(days=k)
            if day.weekday() >= 5:
                continue
            filings = self.daily_index_filings(day)
            log.info("%s: %d Form 4 filings", day, len(filings))
            total += self.ingest(filings)
        return total

    def poll(self) -> int:
        return self.ingest(self.latest_feed_filings())

    # ---- market cap
    def shares_outstanding(self, cik: int) -> float | None:
        row = self.db.execute("SELECT shares, fetched FROM shares_out WHERE issuer_cik=?", (cik,)).fetchone()
        if row and row[1] >= (dt.date.today() - dt.timedelta(days=7)).isoformat():
            return row[0]
        r = self.sec.get(SEC_XBRL_CONCEPT.format(cik=cik))
        shares = as_of = None
        if r.status_code == 200:
            facts = r.json().get("units", {}).get("shares", [])
            if facts:
                latest = max(f["end"] for f in facts)
                # multi-class issuers report one fact per class for the same date: sum them
                by_filing = {}
                for f in facts:
                    if f["end"] == latest:
                        by_filing.setdefault(f.get("accn"), 0.0)
                        by_filing[f.get("accn")] += float(f["val"])
                shares, as_of = max(by_filing.values()), latest
        self.db.execute("INSERT OR REPLACE INTO shares_out VALUES (?,?,?,?)",
                        (cik, shares, as_of, dt.date.today().isoformat()))
        self.db.commit()
        return shares

    # ---- clustering
    def clusters(self, as_of: dt.date | None = None, only_new: bool = True) -> list[ClusterSignal]:
        as_of = as_of or dt.date.today()
        start = as_of - dt.timedelta(days=self.cfg.cluster_window_days)
        df = pd.read_sql_query(
            "SELECT * FROM purchases WHERE tx_date >= ? AND tx_date <= ?",
            self.db, params=(start.isoformat(), as_of.isoformat()))
        if df.empty:
            return []
        # joint filings can repeat the same owner/transaction: dedupe
        df = df.drop_duplicates(subset=["issuer_cik", "owner_cik", "tx_date", "shares", "price"])

        signals = []
        for cik, g in df.groupby("issuer_cik"):
            if g.owner_cik.nunique() < self.cfg.min_insiders:
                continue
            total = float(g.value.sum())
            if total < self.cfg.min_cluster_value_usd:
                continue
            g = g.sort_values("tx_date")
            last_price = float(g.price.iloc[-1])
            so = self.shares_outstanding(int(cik))
            mcap = so * last_price if so else None
            if mcap is None and self.cfg.skip_unknown_market_cap:
                continue
            if mcap is not None and mcap >= self.cfg.max_market_cap_usd:
                continue
            per_owner = (g.groupby(["owner_cik", "owner_name", "role"])
                         .agg(shares=("shares", "sum"), value=("value", "sum"), date=("tx_date", "max"))
                         .reset_index())
            insiders = [dict(cik=int(r.owner_cik), name=r.owner_name, role=r.role, shares=r.shares,
                             value=r.value, price=r.value / r.shares, date=r.date)
                        for r in per_owner.itertuples()]
            key = f"{cik}:" + ",".join(sorted(str(i["cik"]) for i in insiders))
            if only_new and self.db.execute("SELECT 1 FROM alerted WHERE key=?", (key,)).fetchone():
                continue
            signals.append(ClusterSignal(
                ticker=g.ticker.iloc[-1], issuer_cik=int(cik), issuer_name=g.issuer_name.iloc[-1],
                insiders=insiders, total_value_usd=total,
                first_date=dt.date.fromisoformat(g.tx_date.iloc[0]),
                last_date=dt.date.fromisoformat(g.tx_date.iloc[-1]),
                est_market_cap_usd=mcap, last_price=last_price, key=key))
        signals.sort(key=lambda s: -s.total_value_usd)
        return signals

    def mark_alerted(self, sig: ClusterSignal) -> None:
        self.db.execute("INSERT OR IGNORE INTO alerted VALUES (?, ?)", (sig.key, dt.datetime.now(dt.UTC).isoformat()))
        self.db.commit()
