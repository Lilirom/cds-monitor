"""
DTCC SEC SBSDR single-name CDS daily activity monitor.

Source: https://kgc0418-tdw-data-0.s3.amazonaws.com/sec/eod/SEC_CUMULATIVE_CREDITS_YYYY_MM_DD.zip
Public, free, no authentication. One file per report day (UTC).

Produces activity.json for the front-end.

WHAT THIS MEASURES
    New-trade counts per reference entity, day over day, ranked against each
    name's own trailing baseline. It is NOT volume: ~55% of disseminated
    notionals are capped, so notional is reported only as a floor.

SCOPE CAVEAT
    SEC regime only. European and UK single-name flow reports mainly to
    EMIR/UK-EMIR repositories and is largely absent here. Index (iTraxx/CDX)
    goes to the CFTC repositories, not this file.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

BASE = "https://kgc0418-tdw-data-0.s3.amazonaws.com/sec/eod"
BASELINE_DAYS = 20
LOOKBACK_DAYS = 25

# --- real column names, verified against live files (2026-07) ------------------
C_DISS = "Dissemination Identifier"
C_ORIG = "Original Dissemination Identifier"
C_ACTION = "Action type"
C_EVENT = "Event type"
C_EXEC = "Execution Timestamp"
C_EXPIRY = "Expiration Date"
C_NOTIONAL = "Notional amount-Leg 1"
C_CCY = "Notional currency-Leg 1"
C_UID = "Underlier ID-Leg 1"
C_UID_SRC = "Underlier ID source-Leg 1"
C_ASSET_NAME = "Underlying Asset Name"
C_UPI_NAME = "UPI Underlier Name"
C_FISN = "UPI FISN"
C_SPREAD = "Spread-Leg 1"
C_SPREAD_NOT = "Spread notation-Leg 1"
C_CLEARED = "Cleared"

# UPI FISN carries the product taxonomy. "CDS Corp SN Sr" = corporate single
# name, senior. Baskets, tranches and total-return swaps are out of scope.
SINGLE_NAME_FISN = re.compile(r"CDS (?:Corp|Sov) SN", re.I)

SENIORITY = {"SR": "senior", "SUB": "sub", "MZ": "mezz", "JR": "junior"}

# UPI Underlier Name frequently holds a bond descriptor rather than an issuer.
# These are useless as entity labels.
NAME_JUNK = {
    "NO NAME OBTAINABLE", "NT", "SR NT", "BD", "GLOBAL BD", "GLOBAL NT", "BOND",
    "SR NT 144A", "BASKET", "GTD NT", "SR GLOBAL NT", "GTD NT REG S",
    "SR NT REG S", "US$ GLOBAL BD", "SR SECD NT 144A", "SR PRIORITY GTD NT 144A",
    "MEDIUM TERM NOTES", "NA", "N/A", "SR SECD NT", "SUB NT", "GTD SR NT",
    # Generic obligation labels carrying no issuer. Left in, they merge
    # unrelated reference entities into one bogus high-activity row.
    "GOVERNMENT BOND", "GOVERNMENT BONDS", "GOVT BOND", "TREASURY BOND",
    "TREASURY BONDS", "TREASURY NOTE", "TREASURY BILL", "TREASURY BILLS",
    "SOVEREIGN BOND", "CORPORATE BOND", "SENIOR NOTES", "NOTES", "BONDS",
    "GLOBAL BOND", "EUROBOND", "OMO BILL", "T BILL", "BILL",
}

LEGAL_SUFFIX = re.compile(
    r"\b(SA|S A|SPA|S P A|NV|N V|BV|PLC|AG|SE|ASA|AB|OYJ|AS|LTD|LIMITED|INC|"
    r"INCORPORATED|CORP|CORPORATION|CO|COMPANY|GROUP|HOLDING|HOLDINGS|FINANCE|"
    r"FIN|CAPITAL|INTL|INTERNATIONAL|LLC|LP|AKTIENGESELLSCHAFT|INDUSTRIES|"
    r"THE|INSURED|GTD|GUARANTEED)\b"
)


# ------------------------------------------------------------------ acquisition
STATE = Path("state")          # committed to the repo: normalised daily rows
RETAIN_DAYS = 90               # rolling history kept for baselines

# The cache holds POST-normalisation rows, so any change to the name filters or
# field mapping makes older cached days stale. Bump this and the next run
# rebuilds them instead of silently serving results from the previous logic.
NORMALISER_VERSION = 2


def state_path(day: date) -> Path:
    return STATE / f"{day.isoformat()}.v{NORMALISER_VERSION}.csv.gz"


def fetch_day(day: date) -> pd.DataFrame | None:
    """Download and parse one report day. None if DTCC published no file."""
    stamp = day.strftime("%Y_%m_%d")
    url = f"{BASE}/SEC_CUMULATIVE_CREDITS_{stamp}.zip"
    try:
        raw = urlopen(url, timeout=180).read()
    except Exception:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return pd.read_csv(io.BytesIO(z.read(z.namelist()[0])),
                               low_memory=False, dtype=str)
    except zipfile.BadZipFile:
        return None


def load_or_fetch(day: date) -> pd.DataFrame | None:
    """
    Return normalised rows for one day, from the committed cache when present.
    Only unseen days hit the network, so a daily run pulls one file, not thirty.
    """
    p = state_path(day)
    if p.exists():
        df = pd.read_csv(p, low_memory=False, dtype=str, compression="gzip")
        for col in ("notional", "spread_bp"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "capped" in df:
            df["capped"] = df["capped"].astype(str).str.lower().isin(["true", "1"])
        # Empty strings round-trip through CSV as NaN; restore them so the
        # downstream string operations stay total.
        for col in ("orig_diss_id", "raw_name", "uid", "uid_src", "ccy",
                    "cleared", "action", "event", "seniority", "sector",
                    "tenor", "trade_date"):
            if col in df:
                df[col] = df[col].fillna("").astype(str)
        return df

    raw = fetch_day(day)
    if raw is None:
        return None
    df = normalise(raw)
    STATE.mkdir(exist_ok=True)
    df.to_csv(p, index=False, compression="gzip")
    return df


def prune_state(keep_from: date) -> int:
    """Drop cached days older than the retention window."""
    if not STATE.exists():
        return 0
    n = 0
    for f in STATE.glob("*.csv.gz"):
        stem = f.name.split(".")[0]
        stale_version = f".v{NORMALISER_VERSION}." not in f.name
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            f.unlink()
            n += 1
            continue
        if d < keep_from or stale_version:
            f.unlink()
            n += 1
    return n


# --------------------------------------------------------------- normalisation
def parse_notional(v) -> tuple[float | None, bool]:
    """
    Returns (amount, capped). A trailing '+' means DTCC capped the disseminated
    size, so the true notional is at or above the figure shown. For single-name
    credit the cap is USD 5m, per the DDR rulebook.
    """
    if not isinstance(v, str) or not v.strip():
        return None, False
    capped = v.strip().endswith("+")
    num = re.sub(r"[^0-9.]", "", v)
    try:
        return (float(num) if num else None), capped
    except ValueError:
        return None, capped


def parse_spread(val, notation) -> float | None:
    """Notation 3 = decimal rate (0.0091 -> 91bp). Notation 4 = already bp."""
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if str(notation).strip() == "4":
        return x
    return x * 10_000


DESCRIPTOR_PATTERNS = [
    re.compile(r"^\."),                                  # .NGOMO 12/08/26
    re.compile(r"\bCDS\s+[A-Z]{3}\s+(SR|SUB)\b", re.I), # BWA CDS USD SR 5Y D14
    re.compile(r"\b\d+Y\s*D\d+\b", re.I),              # ... 5Y D14
    re.compile(r"^(EUR|USD|GBP|JPY|CHF|TRY)\s+[\d.,]"),
    re.compile(r"^(TRANCHE|SHORT TERM NOTES|MEDIUM TERM NOTES)\b", re.I),
    re.compile(r"\b(TREASURY BILL|T[- ]BILL)\b.*\d", re.I),
    re.compile(r"\bVAR\d{6,}"),                          # VAR20260901GTD
    re.compile(r"^\d"),                                   # 37080 GOI 16FB31 ...
    re.compile(r"\d{2}/\d{2}/\d{2}"),                     # embedded dates
    re.compile(r"REGS|144A|PRRED|/CALL/", re.I),
    # Instrument-level identifiers embedded in the name, e.g.
    # "CREDIT SUISSE AT1 CL-CH036017271" - an obligation, not an entity.
    re.compile(r"\b(?:CH|XS|US|FR|DE|IT|ES|GB)\d{6,}", re.I),
    re.compile(r"\bAT1\b.*\d{5}", re.I),
]


def clean_display(v: str) -> str:
    """Submitters use ';' where a comma belongs and sometimes lose spacing."""
    v = v.replace(";", ", ").replace(" ,", ",")
    v = re.sub(r",\s*,", ",", v)
    v = re.sub(r"\s+", " ", v).strip().strip(",").strip()
    return v


def best_name(row) -> str:
    """
    Extract an issuer name, or "" if the file only offers a bond/contract
    descriptor. NaN must be tested with pd.isna - a float NaN is truthy, so
    `str(x or "")` yields the literal string "nan" and silently merges every
    unnamed row into one bogus entity.
    """
    for col in (C_ASSET_NAME, C_UPI_NAME):
        v = row.get(col)
        if v is None or pd.isna(v):
            continue
        v = str(v).strip()
        if not v or v.upper() in NAME_JUNK:
            continue
        if not re.search(r"[A-Za-z]{3}", v):
            continue
        if any(p.search(v) for p in DESCRIPTOR_PATTERNS):
            continue
        return clean_display(v)
    return ""


def entity_key(name: str) -> str:
    n = name.upper().replace(";", " ").replace("&", " AND ")
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    n = LEGAL_SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def seniority_of(fisn: str) -> str:
    parts = str(fisn or "").upper().split()
    for p in reversed(parts):
        if p in SENIORITY:
            return SENIORITY[p]
    return "unspecified"


def tenor_bucket(exec_ts, expiry) -> str:
    try:
        yrs = (pd.to_datetime(expiry) - pd.to_datetime(exec_ts)).days / 365.25
    except Exception:
        return "unknown"
    if yrs < 0:
        return "unknown"
    for cut, lab in ((1.5, "1y"), (2.5, "2y"), (4.0, "3y"), (6.0, "5y"), (8.5, "7y")):
        if yrs < cut:
            return lab
    return "10y+"


def normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """Reduce a raw file to the fields the monitor needs, single names only."""
    missing = {C_DISS, C_ACTION, C_FISN} - set(raw.columns)
    if missing:
        raise KeyError(f"schema drift - absent columns: {sorted(missing)}")

    df = raw[raw[C_FISN].fillna("").str.contains(SINGLE_NAME_FISN)].copy()

    out = pd.DataFrame(index=df.index)
    out["diss_id"] = df[C_DISS].astype(str).str.strip()
    out["orig_diss_id"] = (
        df[C_ORIG].fillna("").astype(str).str.strip() if C_ORIG in df else ""
    )
    out["action"] = df[C_ACTION].fillna("").str.strip().str.upper()
    # CORR/EROR/REVI carry a null Event type by design; blank is expected.
    out["event"] = df[C_EVENT].fillna("").str.strip().str.upper()

    exec_ts = pd.to_datetime(df[C_EXEC], errors="coerce", utc=True)
    out["trade_date"] = exec_ts.dt.date.astype(str)

    out["uid"] = df[C_UID].fillna("").astype(str).str.strip()
    out["uid_src"] = df[C_UID_SRC].fillna("").astype(str).str.strip()
    out["raw_name"] = df.apply(best_name, axis=1)
    out["seniority"] = df[C_FISN].map(seniority_of)
    out["sector"] = [
        "sovereign" if "SOV" in str(f).upper() else "corporate" for f in df[C_FISN]
    ]
    out["tenor"] = [tenor_bucket(a, b) for a, b in zip(df[C_EXEC], df[C_EXPIRY])]

    parsed = df[C_NOTIONAL].map(parse_notional)
    out["notional"] = [p[0] for p in parsed]
    out["capped"] = [p[1] for p in parsed]
    out["ccy"] = df[C_CCY].fillna("").str.upper()
    out["spread_bp"] = [
        parse_spread(s, n) for s, n in zip(df.get(C_SPREAD), df.get(C_SPREAD_NOT))
    ]
    out["cleared"] = df[C_CLEARED].fillna("").str.upper() if C_CLEARED in df else ""
    return out.reset_index(drop=True)


# ------------------------------------------------------------------- amendments
def resolve_lineage(combined: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse amendment chains. A CORR/MODI supersedes economics but must not
    remove the trade from the population; an EROR kills the whole chain unless
    a later REVI resurrects it. Amendments can reference trades reported days
    earlier, so this runs over the whole retained window, not just today.
    """
    combined = combined.copy()
    combined["orig_diss_id"] = combined["orig_diss_id"].fillna("").astype(str)
    combined.loc[
        combined["orig_diss_id"].str.lower().isin(["nan", "na", "n/a", "none"]),
        "orig_diss_id",
    ] = ""

    parent = dict(
        zip(
            combined.loc[combined["orig_diss_id"] != "", "diss_id"],
            combined.loc[combined["orig_diss_id"] != "", "orig_diss_id"],
        )
    )

    def root(did: str) -> str:
        seen = set()
        while did in parent and did not in seen:
            seen.add(did)
            did = parent[did]
        return did

    combined["lineage_id"] = [
        root(o) if o else root(d)
        for d, o in zip(combined["diss_id"], combined["orig_diss_id"])
    ]

    cancelled = set(combined.loc[combined["action"] == "EROR", "lineage_id"])
    revived = set(combined.loc[combined["action"] == "REVI", "lineage_id"])
    combined = combined[~combined["lineage_id"].isin(cancelled - revived)]
    combined = combined[~combined["action"].isin(["EROR", "REVI"])]

    origins = (
        combined[combined["action"] == "NEWT"]
        .drop_duplicates(subset="lineage_id", keep="first")
        .set_index("lineage_id")[["action", "event", "trade_date", "raw_name", "uid"]]
    )
    keep = combined.drop_duplicates(subset="lineage_id", keep="last").set_index("lineage_id")
    for col in ("action", "event", "trade_date", "raw_name", "uid"):
        inh = origins[col].reindex(keep.index)
        keep[col] = inh.where(inh.notna() & (inh != ""), keep[col])
    return keep.reset_index()


def consolidate_entities(df: pd.DataFrame) -> pd.DataFrame:
    """
    One reference entity can appear under several identifiers - a RED code plus
    the ISINs of two deliverable obligations. Ranking by identifier splits the
    name and understates it. Resolve names first, then propagate the resolved
    name to identifier-only rows via an id -> entity map learned corpus-wide.
    """
    df = df.copy()
    df["key"] = df["raw_name"].map(entity_key)

    named = df[df["key"] != ""]
    id_to_key = (
        named[named["uid"] != ""]
        .groupby("uid")["key"]
        .agg(lambda s: s.value_counts().index[0])
        .to_dict()
    )
    blank = df["key"] == ""
    df.loc[blank, "key"] = df.loc[blank, "uid"].map(id_to_key).fillna("")

    # Rows still unidentified fall back to their raw identifier so they remain
    # visible as unmapped rather than being silently dropped.
    still = df["key"] == ""
    df.loc[still, "key"] = "UNMAPPED:" + df.loc[still, "uid"].replace("", "UNKNOWN")

    display = (
        named.groupby("key")["raw_name"]
        .agg(lambda s: s.value_counts().index[0])
        .to_dict()
    )
    df["display_name"] = [
        display.get(k, k.replace("UNMAPPED:", "RED/ISIN ") if k.startswith("UNMAPPED:") else k)
        for k in df["key"]
    ]
    return df


def new_trades(df: pd.DataFrame) -> pd.DataFrame:
    """
    Economic new risk. NEWT+TRAD is a new trade; NEWT+NOVA is a novation, which
    transfers existing risk and is tracked separately. COMP (compression) and
    CORP (corporate action) are lifecycle noise and excluded.
    """
    return df[(df["action"] == "NEWT") & (df["event"].isin(["TRAD", "NOVA"]))]


# ---------------------------------------------------------------------- reports
def export(df: pd.DataFrame, dates: list[str]) -> dict:
    """
    Emit every (entity, date) cell so the front-end can select any date and
    recompute baselines itself. Baselines are deliberately NOT precomputed:
    the denominator depends on which date you are standing on.
    """
    trades = new_trades(df)
    trades = trades[trades["trade_date"].isin(dates)]

    meta = (
        trades.groupby("key")
        .agg(
            name=("display_name", lambda s: s.value_counts().index[0]),
            sector=("sector", lambda s: s.value_counts().index[0]),
            seniority=("seniority", lambda s: s.value_counts().index[0]),
            unmapped=("key", lambda s: str(s.iat[0]).startswith("UNMAPPED:")),
            total=("key", "size"),
        )
    )

    di = {d: i for i, d in enumerate(dates)}
    cells: dict[str, dict[int, list]] = {}
    for (k, d), g in trades.groupby(["key", "trade_date"]):
        sp = g["spread_bp"].dropna()
        notl = g.loc[g["notional"].notna()]
        cells.setdefault(k, {})[di[d]] = [
            int(len(g)),                                        # 0 count
            round(float(g["capped"].mean()), 2),                # 1 capped share
            round(float(sp.median()), 1) if len(sp) else None,  # 2 median spread bp
            int((g["event"] == "NOVA").sum()),                  # 3 novations
            {c: int(round(v / 1e6)) for c, v in
             notl.groupby("ccy")["notional"].sum().items() if c},  # 4 notional floor, m
            g["tenor"].value_counts().to_dict(),                # 5 tenors
        ]

    entities = []
    for k, m in meta.iterrows():
        entities.append({
            "k": k,
            "n": str(m["name"])[:44],
            "sec": m["sector"],
            "sen": m["seniority"],
            "u": bool(m["unmapped"]),
            "tot": int(m["total"]),
            "s": {str(i): v for i, v in sorted(cells.get(k, {}).items())},
        })
    entities.sort(key=lambda e: -e["tot"])

    totals = {}
    for d in dates:
        g = trades[trades["trade_date"] == d]
        totals[d] = {
            "trades": int(len(g)),
            "names": int(g["key"].nunique()),
            "capped": round(float(g["capped"].mean()), 2) if len(g) else 0,
            "sov": int((g["sector"] == "sovereign").sum()),
            "unmapped": int(g["key"].astype(str).str.startswith("UNMAPPED:").sum()),
        }

    return {
        "dates": dates,
        "generated_utc": pd.Timestamp.now("UTC").isoformat(),
        "source": "DTCC SEC SBSDR cumulative credits (public dissemination)",
        "scope": "Single-name CDS only (UPI FISN 'CDS Corp/Sov SN'). SEC regime; "
                 "excludes EMIR-reported EU/UK flow and CFTC index.",
        "baseline_note": "Baseline is mean trades per business day over the "
                         "preceding window, counting days with no trades as zero.",
        "caveats": [
            "Trade counts, not volume. Most notionals are capped at USD 5m.",
            "Notional shown is a floor, never a measured amount.",
            "Earlier dates restate as late cancels and corrections arrive.",
            "Entities identified only by a Markit RED code show as RED-only.",
            "A name with few active days has a thin baseline - check days traded.",
        ],
        "totals": totals,
        "entities": entities,
    }


def main(end: date | None = None, days: int = RETAIN_DAYS) -> dict:
    """
    Build activity.json over a rolling window ending at the most recent
    published day. Existing days come from the committed cache; only new days
    are downloaded.
    """
    end = end or date.today()

    # Walk back to the latest day DTCC has actually published.
    probe, tries = end, 0
    while tries < 6:
        if probe.weekday() < 5 and load_or_fetch(probe) is not None:
            break
        probe -= timedelta(days=1)
        tries += 1
    else:
        raise RuntimeError("no published file found in the last 6 days")
    end = probe

    frames, got = [], []
    d, guard = end, 0
    while len(got) < days and guard < days * 2:
        if d.weekday() < 5:
            df = load_or_fetch(d)
            if df is not None:
                frames.append(df)
                got.append(d.isoformat())
        d -= timedelta(days=1)
        guard += 1

    if not frames:
        raise RuntimeError("no files retrieved")

    removed = prune_state(d)

    all_rows = resolve_lineage(pd.concat(frames, ignore_index=True))
    all_rows = consolidate_entities(all_rows)

    dates = sorted(got)
    rep = export(all_rows, dates)
    rep["pruned"] = removed
    Path("activity.json").write_text(json.dumps(rep, separators=(",", ":"), default=str))
    return rep


if __name__ == "__main__":
    r = main()
    last = r["dates"][-1]
    t = r["totals"][last]
    print(f"{len(r['dates'])} dates {r['dates'][0]}..{last} | "
          f"{len(r['entities'])} entities | {last}: {t['trades']} trades, "
          f"{t['names']} names, {t['capped']:.0%} capped")
