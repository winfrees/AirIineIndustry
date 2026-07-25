"""
CHANNEL DISCOVERY — stop guessing URLs, go and find them.
========================================================

The first live probe run proved the approach and found the fare data
(DB1B Market/Coupon downloaded and parsed cleanly, mean fare $323.77) but 404'd
on every guessed T-100 filename. T-100 is not optional — it is the only source
of SEATS and departures, so without it there is no load factor, no de-censoring,
no seat window, and no monthly seasonality. See docs/route-data-design.md.

The DB1B success is the clue: the working URL was

    /PREZIP/Origin_and_Destination_Survey_DB1BMarket_2024_2.zip

i.e. PREZIP mirrors the DOWNLOAD UI's table name plus the period, not the
internal RawDataTable name. So rather than guess a fourth time, this module
enumerates and reports:

  1. NAME SWEEP     HEAD a generated matrix of plausible PREZIP filenames and
                    report every one that answers 200
  2. PAGE SCRAPE    pull the TranStats index/table pages and extract every
                    .zip href, every literal PREZIP/... string, and the real
                    form field names — which is what a correct DownLoad_Table
                    POST needs
  3. ARCGIS         resolve the geodata.bts.gov T-100 mirror through the public
                    ArcGIS search API, which unlike PREZIP is a documented,
                    stable interface

Everything found is REPORTED rather than silently adopted, with one exception:
if the name sweep finds a working URL, probe.py will use it immediately so a
single run can both discover and validate the full chain.

Scraping here is deliberately shallow — a fixed list of entry points, one level
deep, no recursion.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
import urllib.error
import urllib.request

from airlinesim.btsdata.download import USER_AGENT

PREZIP_BASE = "https://transtats.bts.gov/PREZIP/"

# Entry points that plausibly reference the real T-100 download. Kept explicit
# so this never turns into a crawler.
SCRAPE_PAGES = (
    PREZIP_BASE,
    "https://www.transtats.bts.gov/DataIndex.asp",
    "https://www.transtats.bts.gov/DatabaseInfo.asp?QO_VQ=EEE",
    "https://www.transtats.bts.gov/Tables.asp?QO_VQ=EEE",
    "https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FIM",
    "https://www.bts.gov/topics/airlines-and-airports/airline-data-downloads",
)

# Base names to sweep. The first group is what we already tried (all 404);
# the rest follow the naming convention the DB1B and On-Time files actually use
# — the human-facing table name, underscored.
PREZIP_BASES = (
    "T_T100D_SEGMENT_ALL_CARRIER",
    "T_T100_SEGMENT_ALL_CARRIER",
    "T_T100D_SEGMENT_US_CARRIER_ONLY",
    "T_T100D_MARKET_ALL_CARRIER",
    "T_100_Domestic_Segment",
    "T_100_Domestic_Segment_All_Carriers",
    "T_100_Domestic_Segment_(All_Carriers)",
    "T_100_Domestic_Segment_All_Carriers_1990_present",
    "T_100_Segment_All_Carriers",
    "Air_Carrier_Statistics_T_100_Domestic_Segment",
    "T100_Domestic_Segment",
    "T100D_SEGMENT",
    "T_100_Domestic_Market",
)

# Period suffix shapes. On-Time uses "_1987_present_{year}_{month}", DB1B uses
# "_{year}_{quarter}", and some tables are published whole with no period.
PERIOD_FORMS = (
    "",
    "_{year}",
    "_{year}_{month}",
    "_1990_present_{year}_{month}",
    "_All_Years",
)

ARCGIS_SEARCH = ("https://www.arcgis.com/sharing/rest/search"
                 "?q=T-100%20Domestic%20Market%20and%20Segment&f=json&num=10")


@dataclass
class UrlResult:
    url: str
    status: int = 0
    length: int = 0
    content_type: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        # A zip that exists; an HTML body at a .zip URL is a soft 404.
        return self.status == 200 and "html" not in self.content_type.lower()


@dataclass
class PageResult:
    url: str
    status: int = 0
    error: str = ""
    title: str = ""
    zip_links: list = field(default_factory=list)
    prezip_mentions: list = field(default_factory=list)
    form_fields: list = field(default_factory=list)
    t100_options: list = field(default_factory=list)


@dataclass
class DiscoveryReport:
    swept: list = field(default_factory=list)     # UrlResult
    hits: list = field(default_factory=list)      # UrlResult that answered 200
    pages: list = field(default_factory=list)     # PageResult
    arcgis: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


# ------------------------------------------------------------
# 1. name sweep
# ------------------------------------------------------------

def _probe_url(url: str, timeout: float = 25.0) -> UrlResult:
    """HEAD a URL, falling back to a 1-byte ranged GET where HEAD is refused."""
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        req = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return UrlResult(url, resp.status,
                                 int(resp.headers.get("Content-Length") or 0),
                                 resp.headers.get("Content-Type") or "")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 405, 501) and method == "HEAD":
                continue          # server dislikes HEAD; try the ranged GET
            return UrlResult(url, exc.code, 0, "", f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return UrlResult(url, 0, 0, "", f"{type(exc).__name__}: {exc}")
    return UrlResult(url, 0, 0, "", "HEAD and ranged GET both refused")


def candidate_names(year: int, month: int) -> list:
    seen, out = set(), []
    for base in PREZIP_BASES:
        for form in PERIOD_FORMS:
            name = base + form.format(year=year, month=month)
            url = f"{PREZIP_BASE}{name}.zip"
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def sweep_prezip(year: int, month: int, workers: int = 8) -> list:
    urls = candidate_names(year, month)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_probe_url, urls))


# ------------------------------------------------------------
# 2. page scrape
# ------------------------------------------------------------

class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.zip_links, self.form_fields, self.options = [], [], []
        self.title, self._in_title = "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href", "").lower().endswith(".zip"):
            self.zip_links.append(a["href"])
        elif tag in ("input", "select", "textarea"):
            name = a.get("name")
            if name:
                val = (a.get("value") or "")[:80]
                self.form_fields.append(f"{tag}:{name}={val}" if val else f"{tag}:{name}")
        elif tag == "option":
            label = a.get("value") or ""
            if label:
                self.options.append(label[:120])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()[:120]


_PREZIP_RE = re.compile(r"PREZIP/[A-Za-z0-9_.()\-]+\.zip", re.I)


def scrape_page(url: str, timeout: float = 40.0, cap: int = 4 * 1024 * 1024) -> PageResult:
    res = PageResult(url=url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res.status = resp.status
            body = resp.read(cap).decode("latin-1", "replace")
    except urllib.error.HTTPError as exc:
        res.status, res.error = exc.code, f"HTTP {exc.code}"
        return res
    except (urllib.error.URLError, OSError) as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        return res

    ex = _Extractor()
    try:
        ex.feed(body)
    except Exception as exc:  # noqa: BLE001 — malformed markup must not abort discovery
        res.error = f"parse: {type(exc).__name__}: {exc}"
    res.title = ex.title
    res.zip_links = ex.zip_links[:40]
    res.prezip_mentions = sorted(set(_PREZIP_RE.findall(body)))[:40]
    res.form_fields = ex.form_fields[:60]
    # Anything mentioning T-100/T100 is the interesting subset of a long
    # <option> list of table names.
    res.t100_options = [o for o in ex.options if "t100" in o.lower()
                        or "t_100" in o.lower()][:40]
    return res


def scrape_pages(pages=SCRAPE_PAGES, workers: int = 4) -> list:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(scrape_page, pages))


# ------------------------------------------------------------
# 3. ArcGIS mirror
# ------------------------------------------------------------

def arcgis_mirror(timeout: float = 40.0) -> dict:
    """
    Resolve the T-100 feature service through the public ArcGIS search API and
    report its query endpoint and field names. A documented REST interface is a
    far better long-term channel than an undocumented zip directory, even though
    the mirror is a curated subset of the full table.
    """
    out = {"items": [], "fields": [], "query_url": "", "error": ""}
    req = urllib.request.Request(ARCGIS_SEARCH, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    for item in (data.get("results") or [])[:10]:
        out["items"].append({"title": item.get("title"), "type": item.get("type"),
                             "url": item.get("url") or "", "id": item.get("id")})

    service = next((i["url"] for i in out["items"]
                    if i["url"] and "FeatureServer" in i["url"]), "")
    if not service:
        out["error"] = out["error"] or "no FeatureServer item in search results"
        return out

    layer = f"{service.rstrip('/')}/0"
    out["query_url"] = f"{layer}/query"
    try:
        req = urllib.request.Request(f"{layer}?f=json",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            meta = json.loads(resp.read().decode("utf-8", "replace"))
        out["fields"] = [f.get("name") for f in (meta.get("fields") or [])][:80]
        out["layer_name"] = meta.get("name", "")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"layer metadata: {type(exc).__name__}: {exc}"
    return out


# ------------------------------------------------------------
# orchestration
# ------------------------------------------------------------

def discover_t100(year: int, month: int, do_sweep=True, do_pages=True,
                  do_arcgis=True) -> DiscoveryReport:
    rep = DiscoveryReport()
    if do_sweep:
        rep.swept = sweep_prezip(year, month)
        rep.hits = [r for r in rep.swept if r.ok]
        rep.notes.append(f"swept {len(rep.swept)} PREZIP names, "
                         f"{len(rep.hits)} answered 200")
    if do_pages:
        rep.pages = scrape_pages()
        found = sorted({m for p in rep.pages for m in p.prezip_mentions})
        if found:
            rep.notes.append(f"pages referenced {len(found)} PREZIP file(s)")
            # A referenced name we didn't sweep is the most valuable output here.
            for mention in found:
                url = f"https://transtats.bts.gov/{mention}"
                if not any(r.url == url for r in rep.swept):
                    res = _probe_url(url)
                    rep.swept.append(res)
                    if res.ok:
                        rep.hits.append(res)
    if do_arcgis:
        rep.arcgis = arcgis_mirror()
    return rep
