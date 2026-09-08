"""
Mirror the "Wasserstand Rohdaten" (raw water-level) data from the PEGELONLINE
"Freier Download" area (https://pegelonline.wsv.de/webservices/files/) and turn
it into one tidy Parquet file.

Two stages:
  1. mirror()  - walk the HTML directory tree and download every missing
                 down.csv into raw/. Idempotent: files already on disk are
                 skipped, so an interrupted run just resumes.
  2. build()   - read every raw/**/*.csv, untangle the German-CSV quirks
                 (semicolon separator, decimal comma, Latin-1 text, "XXX,XXX"
                 placeholders, a header row with a different column count) and
                 write the result to data/.

The download area only keeps roughly the last 80 days per gauge, so this is
meant to run once a day: over time data/wasserstand.parquet accumulates more
history than the source itself exposes.

Data licence: DL-DE->Zero-2.0 (public domain), source WSV. The terms state no
rate limit; the script still downloads sequentially with a short pause.

Usage:
  python fetch_pegelonline.py                 # mirror + build
  python fetch_pegelonline.py mirror          # download only
  python fetch_pegelonline.py build           # rebuild Parquet only
  python fetch_pegelonline.py mirror DONAU    # restrict to one waterway (testing)
"""
import csv # split each line on; while respecting the "-quoted fields
import re  # re.compile / .findall / .fullmatch - pull hrefs out of HTML, recognise DD.MM.YYYY folders
import sys # sys.argv (which stage to run), sys.exit (bail with a message)
import time # time.sleep - pause between requests, back-off between retries
from datetime import timedelta # add HH:MM to a date; hours=24 rolls to the next day for free
from io import StringIO # wrap the decoded text in a file-like object so csv.reader can consume it
from pathlib import Path #all filesystem paths — / joining, .rglob, .stem, .read_bytes, .write_bytes, .mkdir, .exists
from urllib.parse import unquote_plus, urljoin, urlparse 

import pandas as pd # the dataframe, pd.concat, pd.to_datetime, .drop_duplicates, .to_parquet
import requests # session, .get, .raise_for_status, exceptions

# Level 0 of the tree. Everything deeper is discovered by following links.
ROOT    = "https://pegelonline.wsv.de/webservices/files/Wasserstand+Rohdaten/"
# Separate REST endpoint that lists every gauge with its coordinates. The bulk
# download CSVs carry no lat/lon, so build() enriches pegel.parquet from here
# to make a map possible in Power BI.
STATIONS_JSON = "https://pegelonline.wsv.de/webservices/rest-api/v2/stations.json"
RAW     = Path("raw")        # untouched downloads land here (git-ignored)
DATA    = Path("data")       # parsed Parquet output
PAUSE   = 0.2                # seconds to wait after every HTTP request
TIMEOUT = 60                 # seconds before a request is considered dead
RETRIES = 3                  # attempts per URL on connection / 5xx errors

# Raw water levels are not quality-checked: a handful of gauges emit error codes
# (99999, 100000) or report on a different datum, giving values like 5 000 cm or
# 500 000 cm. Anything outside this band (in cm over gauge zero) is set to NaN in
# build(). ~0.05 % of readings, ~10 of ~740 gauges.
PLAUSIBLE_CM = (-500, 2500)

# A folder name that looks like a day, e.g. "08.09.2026".
DAY_RE  = re.compile(r"\d\d\.\d\d\.\d{4}")
# Pull the target out of any <a href="..."> on the page. The trailing "/?" lets
# an optional slash be dropped so links with and without it compare equal.
HREF_RE = re.compile(r'href="([^"?#]+?)/?"')

DATA.mkdir(exist_ok=True) # create data/ on import; exist_ok=True so it does not fail if the folder is already there

# One Session reused for every call: keeps a connection pool alive and sends the
# same headers each time. A descriptive User-Agent is basic courtesy for a bulk
# crawl - it tells the server operator who is hitting them.
S = requests.Session()
S.headers["User-Agent"] = (
    "german-water-levels/1.0 (personal learning project; github.com/vulevu228)"
)


def _get(url):
    """HTTP GET with a few patient retries on transient failures.

    Only network hiccups and 5xx (server-side) errors are retried. A 4xx such as
    404 is a real answer - it is raised immediately by raise_for_status().
    """
    for attempt in range(RETRIES):
        try:
            r = S.get(url, timeout=TIMEOUT)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} {url}")
            r.raise_for_status()          # turns any other 4xx/5xx into an exception
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == RETRIES - 1:    # last try failed -> let the caller deal with it
                raise
            time.sleep(2 * (attempt + 1))  # 2s, 4s, ... simple linear back-off
    raise RuntimeError("unreachable")


def children(url):
    """Return (name, url) for every entry exactly one level below `url`.

    The PEGELONLINE listing is a full HTML page with navigation, a logo, CSS
    links and so on - not a bare Apache index. The reliable filter is: keep a
    link only if its URL path equals the current path plus exactly one more
    segment. That excludes both the site chrome (/css/..., https://www.wsv.de)
    and any links that point deeper than one level.

    The same function is used at every depth (parameter -> waterway -> gauge ->
    day), because "a directory" behaves the same way at each level.
    """
    base = url if url.endswith("/") else url + "/"
    prefix = urlparse(base).path                     # e.g. /webservices/files/Wasserstand+Rohdaten/
    seen, out = set(), []
    for href in HREF_RE.findall(_get(url).text):
        # urljoin resolves "DONAU", "/webservices/.../DONAU" and a full URL the
        # same way; urlparse(...).path then drops the host and query string.
        path = urlparse(urljoin(base, href)).path
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):].strip("/")         # the part below the current folder
        if not rest or "/" in rest or rest in seen:  # "" = self link, "/" = deeper, dedupe
            continue
        seen.add(rest)
        # Keep the URL segment as-is (still percent/+ encoded so it stays a valid
        # URL); decode it only for the human-readable name.
        out.append((unquote_plus(rest), base + rest + "/"))
    return out


def _safe(name):
    """Make a string usable as a Windows/POSIX folder name."""
    for c in '<>:"/\\|?*':
        name = name.replace(c, "_")
    return name.strip() or "_"


def mirror(only=None):
    """Download every still-missing down.csv into raw/<waterway>/<uuid>/<day>.csv.

    `only` is an optional list of waterway names to restrict to (for testing).
    Three nested loops mirror the three directory levels below ROOT. The
    `dest.exists()` check is the whole resume mechanism: nothing is re-fetched,
    so a daily run only pulls the one new day per gauge.
    """
    RAW.mkdir(exist_ok=True)
    only = {o.upper() for o in only} if only else None
    n_new = n_skip = n_fail = 0

    waterways = children(ROOT)
    todo = [w for w in waterways if only is None or w[0].upper() in only]
    print(f"{len(todo)} waterways (of {len(waterways)})")

    for wi, (wname, wurl) in enumerate(todo, 1):
        stations = children(wurl)                    # gauge folders (their names are UUIDs)
        print(f"[{wi}/{len(todo)}] {wname}: {len(stations)} gauges")
        for sname, surl in stations:
            out_dir = RAW / _safe(wname) / sname
            for dname, durl in children(surl):       # day folders
                if not DAY_RE.fullmatch(dname):      # ignore anything that is not a date
                    continue
                dest = out_dir / f"{dname}.csv"
                if dest.exists():                    # already downloaded on an earlier run
                    n_skip += 1
                    continue
                try:
                    r = _get(durl + "down.csv")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)      # store the raw bytes, do not parse yet
                    n_new += 1
                except Exception as e:               # noqa: BLE001 - one bad file must not stop the run
                    n_fail += 1
                    print(f"  ! {wname}/{sname}/{dname}: {e}")
                time.sleep(PAUSE)
        print(f"    running total: {n_new} new, {n_skip} present, {n_fail} failed")

    print(f"\nmirror done: {n_new} new, {n_skip} present, {n_fail} failed -> {RAW.resolve()}")


def _num(s):
    """Parse a German-formatted number cell.

    '244'     -> 244.0
    '287,7'   -> 287.7      (comma is the decimal point)
    '1.234,5' -> 1234.5     (dot is the thousands separator)
    'XXX,XXX' -> NaN        (PEGELONLINE's marker for a missing value)
    """
    s = s.strip().strip('"')
    # Placeholder tokens are made only of X . , : and spaces (e.g. "XXX,XXX",
    # "XX.XX.XXXX", "XX:XX"). Treat anything like that as "no value".
    if not s or set(s) <= set("X.,: "):
        return float("nan")
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return float("nan")


COLUMNS = ["timestamp", "value_cm", "waterway", "station", "station_no",
           "pegel_uuid", "parameter", "unit", "pnp_m", "src_day"]
# low-cardinality text columns -> stored as 'category' to keep the frame (and the
# Parquet file) small: ~740 stations / ~90 waterways repeated across ~30M rows.
CAT_COLS = ["waterway", "station", "station_no", "pegel_uuid", "parameter",
            "unit", "src_day"]


def _parse_file(path):
    """Turn one down.csv into a list of row tuples in COLUMNS order.

    Returns [] for an empty / header-only file. build() collects these lists and
    builds ONE DataFrame at the end - much cheaper than making ~60k tiny frames
    and concatenating them.

    Layout of the file:
      line 0  : metadata header, 12 semicolon-separated fields
                date; provider; waterway; station; station_no; param; unit;
                <placeholder x3>; "PNP"; pnp_height
      line 1+ : "HH:MM";"value"   - the date is NOT here, it comes from line 0 /
                the file name; "24:00" means midnight of the following day.
    """
    text = path.read_bytes().decode("latin-1")       # the file is Latin-1, not UTF-8
    rows = [r for r in csv.reader(StringIO(text), delimiter=";", quotechar='"') if r]
    if len(rows) < 2:                                 # header only / empty -> nothing to do
        return []

    head = rows[0] + [""] * 12                        # pad so head[11] is always safe to read
    day = pd.Timestamp(pd.to_datetime(path.stem, format="%d.%m.%Y"))  # "08.09.2026" from the name

    # File-level metadata, identical on every row of this file. waterway falls
    # back to the folder name if the header field is blank.
    meta = (
        head[2] or path.parent.parent.name,          # waterway
        head[3],                                      # station
        head[4],                                      # station_no (string: may have leading zeros)
        path.parent.name,                            # pegel_uuid (the gauge folder)
        head[5] or "W_O",                            # parameter
        head[6] or "cm",                             # unit
        _num(head[11]),                              # pnp_m (gauge zero level, metres)
        path.stem,                                    # src_day ("08.09.2026")
    )

    out = []
    for r in rows[1:]:
        if len(r) < 2 or ":" not in r[0]:
            continue
        hh, mm = r[0].strip().strip('"').split(":")[:2]
        # timedelta(hours=24) rolls day+1 automatically, so "24:00" needs no special case.
        ts = day + timedelta(hours=int(hh), minutes=int(mm))
        out.append((ts, _num(r[1]), *meta))
    return out


def _station_meta():
    """Fetch the gauge catalogue (coordinates etc.) from the REST API.

    Returns a DataFrame keyed by station_no with columns latitude, longitude,
    river_km, agency, water. On any network error it returns an empty frame so
    build() still produces pegel.parquet, just without coordinates.
    """
    try:
        rows = _get(STATIONS_JSON).json()
    except Exception as e:                            # noqa: BLE001 - metadata is optional
        print(f"  ! station metadata unavailable ({e}) - pegel.parquet without coords")
        return pd.DataFrame(columns=["station_no"])

    meta = pd.DataFrame(
        {
            "station_no": r["number"],               # matches head[4] in the CSVs
            "latitude":   r.get("latitude"),
            "longitude":  r.get("longitude"),
            "river_km":   r.get("km"),
            "agency":     r.get("agency"),
            "water":      (r.get("water") or {}).get("longname"),
        }
        for r in rows
    )
    return meta.drop_duplicates("station_no")


WASSERSTAND_PARQUET = DATA / "wasserstand.parquet"


def build():
    """Parse raw/**/*.csv into data/wasserstand.parquet + data/pegel.parquet.

    Incremental: rows already in wasserstand.parquet are identified by
    (pegel_uuid, src_day); only raw files not yet represented are re-parsed and
    appended. A full rebuild happens automatically when the Parquet is missing
    or predates the src_day column. Delete the Parquet to force one.
    """
    all_files = sorted(RAW.rglob("*.csv"))           # recursive glob over the mirror tree
    if not all_files:
        sys.exit("no raw/**/*.csv found - run 'mirror' first")

    # Incremental only makes sense when the existing Parquet already carries
    # src_day. A legacy file without it triggers one clean full rebuild.
    old = None
    done = set()
    if WASSERSTAND_PARQUET.exists():
        prev = pd.read_parquet(WASSERSTAND_PARQUET)
        if "src_day" in prev.columns:
            old = prev
            done = set(zip(prev["pegel_uuid"], prev["src_day"]))
        else:
            print("existing Parquet predates src_day - rebuilding from scratch")
            del prev

    files = [f for f in all_files if (f.parent.name, f.stem) not in done]
    print(f"{len(all_files)} raw files, {len(files)} new to parse "
          f"({len(all_files) - len(files)} already in Parquet)")

    # Parse in batches: a Python list of ~30M row tuples costs several GB, but a
    # DataFrame with the repeated text columns as 'category' is far smaller. So
    # flush the buffer to a categorical frame every ~2M rows and free the list.
    def _to_frame(buf):
        df = pd.DataFrame(buf, columns=COLUMNS)
        for c in CAT_COLS:
            df[c] = df[c].astype("category")
        return df

    batches, buf, bad = [], [], 0
    for i, f in enumerate(files, 1):
        try:
            buf.extend(_parse_file(f))
        except Exception as e:                        # noqa: BLE001 - skip a corrupt file, keep going
            bad += 1
            print(f"  ! {f}: {e}")
        if len(buf) >= 2_000_000 or i == len(files):
            if buf:
                batches.append(_to_frame(buf))
                buf = []
        if i % 10000 == 0:
            print(f"  {i}/{len(files)}  ({sum(map(len, batches)):,} rows)")

    fresh = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame(columns=COLUMNS)
    parts = [p for p in (old, fresh) if p is not None and not p.empty]
    if not parts:
        sys.exit("nothing to do")
    big = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    # Null out physically impossible readings (error codes / wrong datum). The
    # row and its timestamp stay; only the value becomes NaN.
    lo, hi = PLAUSIBLE_CM
    outliers = ~big["value_cm"].between(lo, hi) & big["value_cm"].notna()
    big.loc[outliers, "value_cm"] = float("nan")

    big = (
        big.dropna(subset=["timestamp"])
        # day D's "24:00" row and day D+1's "00:00" row are the same instant;
        # drop_duplicates keeps the first and discards the copy.
        .drop_duplicates(["station_no", "parameter", "timestamp"])
        .sort_values(["waterway", "station", "timestamp"])
        .reset_index(drop=True)
    )
    big.to_parquet(WASSERSTAND_PARQUET, index=False)

    # One row per gauge - a small dimension table to join against in Power BI.
    # left-merge the REST coordinates on station_no so the map has lat/lon.
    dim = (
        big[["station_no", "station", "waterway", "pegel_uuid", "unit", "pnp_m"]]
        .drop_duplicates("station_no")
        .astype({"station_no": "string", "station": "string",     # drop 'category' for a clean
                 "waterway": "string", "pegel_uuid": "string", "unit": "string"})  # small table
        .merge(_station_meta(), on="station_no", how="left")
        .sort_values(["waterway", "station"])
        .reset_index(drop=True)
    )
    dim.to_parquet(DATA / "pegel.parquet", index=False)

    span = f"{big['timestamp'].min():%d.%m.%Y} - {big['timestamp'].max():%d.%m.%Y}"
    with_xy = int(dim["latitude"].notna().sum()) if "latitude" in dim else 0
    print(f"\n{len(big):,} readings  |  {len(dim)} gauges ({with_xy} with coords)  "
          f"|  {span}  |  {int(outliers.sum()):,} outliers nulled  |  {bad} broken files")
    print("->", (DATA / "wasserstand.parquet").resolve())


if __name__ == "__main__":
    # First positional arg picks the stage; anything after it is passed to
    # mirror() as a waterway filter.
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("all", "mirror"):
        mirror(only=sys.argv[2:] or None)
    if mode in ("all", "build"):
        build()
