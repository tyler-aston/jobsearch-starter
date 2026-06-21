#!/usr/bin/env python3
"""
watchlist_poller.py
-------------------
Poll a curated watchlist of company ATS boards (Greenhouse, Lever, Ashby) for
fresh senior PM / product-leadership roles. Free, no API keys, no paid actors,
no third-party scrapers. Pure Python standard library.

Run:
    python3 watchlist_poller.py

Reads:
    boards.json        list of {"ats": "...", "slug": "...", "name": "..."}
State (auto-created, do not delete):
    seen_jobs.json     job IDs observed in prior runs (drives change-detection)
Writes:
    new_roles.csv      net-new matching roles since last run, ranked

Tune the CONFIG block below.
"""

import csv
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import rank_roles   # Stage-A deterministic fit-ranker (free, stdlib) for the worklist

# ----------------------------- CONFIG --------------------------------------
MAX_AGE_DAYS = 3            # recency cut: drop anything posted older than this
# Kept short (early-applicant goal). Volume comes from MORE BOARDS + more fetchers,
# not a wider window. census.py measures inventory across age buckets on demand.
INCLUDE_ONSITE_NON_UTAH = False   # False = keep only Utah(any mode) + Remote(anywhere)
                                  # True  = also keep US on-site/hybrid roles
BOARDS_FILE = "boards.json"
SEEN_FILE = "seen_jobs.json"
OUT_FILE = "new_roles.csv"
DELETED_FILE = "deleted_jobs.json"   # durable tombstone of user-deleted roles

# Optional Google Sheet worklist (Apps Script web app). If both are set, the poller also
# pushes net-new roles to the sheet; the local CSV is always written as a backup either way.
WORKLIST_WEBHOOK_URL = os.environ.get("WORKLIST_WEBHOOK_URL")
WORKLIST_TOKEN = os.environ.get("WORKLIST_TOKEN")
REQUEST_TIMEOUT = 20
USER_AGENT = "watchlist-poller/1.0 (personal job search)"

# Aggregators / re-posters that hide the real employer -- drop entirely.
# "agent" is an offshore staffing agency (LatAm/PH placements), discovery-added 2026-06
# then removed from boards.json; denied here so its roles drop even if it ever resurfaces.
AGGREGATOR_DENY = {"jobgether", "agent"}

# Obvious non-US signals (a remote row with one of these AND no US token is dropped --
# see keep()). Kept fairly broad: a single contract posting fanned out across 25
# countries was padding the worklist as fake "remote_us" rows because the country
# wasn't listed here. The US-token escape in keep() protects genuine "Remote - US" rows.
NON_US_PAT = re.compile(
    r"\b(united kingdom|\buk\b|england|scotland|wales|london|manchester|"
    r"canada|toronto|ontario|vancouver|montreal|"
    r"germany|berlin|munich|france|paris|spain|madrid|barcelona|"
    r"netherlands|amsterdam|belgium|brussels|austria|vienna|switzerland|zurich|"
    r"ireland|dublin|portugal|lisbon|italy|rome|milan|greece|athens|cyprus|"
    r"sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki|"
    r"poland|warsaw|krakow|romania|bucharest|bulgaria|sofia|hungary|budapest|"
    r"czechia|czech|slovakia|slovenia|croatia|serbia|belgrade|"
    r"bosnia|herzegovina|montenegro|macedonia|albania|kosovo|moldova|"
    r"lithuania|latvia|estonia|ukraine|"
    r"tbilisi|batumi|kutaisi|"  # country Georgia, by city (bare 'georgia' = US-state collision)
    r"armenia|yerevan|azerbaijan|baku|kazakhstan|almaty|astana|uzbekistan|"
    r"kyrgyzstan|tajikistan|"
    r"turkmenistan|turkey|istanbul|"
    r"india|bangalore|bengaluru|mumbai|delhi|hyderabad|pune|pakistan|"
    r"singapore|malaysia|kuala lumpur|indonesia|jakarta|thailand|bangkok|"
    r"vietnam|philippines|manila|"
    r"china|shanghai|beijing|shenzhen|hong kong|taiwan|"
    r"japan|tokyo|korea|seoul|"
    r"australia|sydney|melbourne|new zealand|auckland|"
    r"brazil|sao paulo|(?<!new )mexico|argentina|colombia|chile|peru|"
    r"israel|tel aviv|uae|dubai|abu dhabi|saudi|egypt|cairo|"
    r"nigeria|lagos|kenya|nairobi|south africa|johannesburg|cape town)\b", re.I)

UTAH_PAT = re.compile(
    r"\b(utah|ut|lehi|draper|provo|orem|salt lake|sandy|south jordan|silicon slopes|"
    r"vineyard|american fork|pleasant grove|lindon|saratoga springs)\b", re.I)
REMOTE_PAT = re.compile(r"\bremote\b|work from home|\bwfh\b|anywhere", re.I)

# Title must match this (product-management / product-leadership function)...
TITLE_INCLUDE = re.compile(
    r"\bproduct manager\b|\bproduct management\b|\bproduct lead\b|"
    r"\bdirector[ ,/of-]+product\b|\bhead of product\b|"
    r"\b[se]?vp[ ,/of-]+product\b|\bchief product officer\b|"
    r"\bproduct manager ii\b|"
    # 2026-05-29: builder/engineer-flavored product roles. "AI Product Manager" already
    # matched via "product manager"; "AI/Founding Product Engineer" match "product engineer".
    r"\bproduct engineer\b|\bproduct builder\b",
    re.I)
# ...and must NOT match this (wrong function).
TITLE_EXCLUDE = re.compile(
    r"\bproduct marketing\b|\bprogram manager\b|\bproject manager\b|"
    r"\bproduct design|\bdesigner\b|\bux\b|\bproduct operations\b|\bproduct ops\b|"
    r"\bassociate\b|\bapm\b|\bintern\b|\bproduct owner\b",
    re.I)

# Fetch-time title prefilter. BambooHR and Workday skip non-matching titles BEFORE the
# per-job detail call (their list feeds have no usable date / are flooded by other roles),
# so the prefilter has to know which titles to keep AT FETCH TIME -- before per-profile
# filtering runs. These default to the PM taxonomy; run_profiles.py overrides them (and
# WORKDAY_SEARCH_TEXT below) with the union of the active profiles' title filters, so a
# non-PM friend gets full BambooHR/Workday coverage instead of silently zero. Greenhouse/
# Lever/Ashby ignore these -- they return everything and get filtered per-profile.
FETCH_INCLUDE = TITLE_INCLUDE
FETCH_EXCLUDE = TITLE_EXCLUDE
# ---------------------------------------------------------------------------


class Role:
    __slots__ = ("ats", "slug", "company", "job_id", "title", "location",
                 "remote", "comp", "posted", "url")

    def __init__(self, ats, slug, company, job_id, title, location,
                 remote, comp, posted, url):
        self.ats = ats
        self.slug = slug
        self.company = company or slug
        self.job_id = str(job_id)
        self.title = (title or "").strip()
        self.location = (location or "").strip()
        self.remote = bool(remote)
        self.comp = comp or ""
        self.posted = posted  # datetime|None
        self.url = url or ""

    @property
    def uid(self):
        return f"{self.ats}:{self.slug}:{self.job_id}"

    @property
    def is_utah(self):
        return bool(UTAH_PAT.search(self.location)) or bool(UTAH_PAT.search(self.title))

    @property
    def is_remote(self):
        return self.remote or bool(REMOTE_PAT.search(self.location)) or \
            bool(REMOTE_PAT.search(self.title))


def _get_json(url, post=False, retries=1, pause=0.15, body=None):
    # Only Ashby throttles rapid sequential requests, so only fetch_ashby opts into
    # the full 1.5s spacing (pass pause=1.5). Everything else uses a light default
    # pause -- with ~1,350 boards fetched sequentially, a blanket 1.5s/request was
    # ~34 min of pure sleep and was inflating the daily run toward the 90-min cap.
    import time
    for attempt in range(retries + 1):
        time.sleep(pause)  # space requests so the ATS never throttles us
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            if post or body is not None:
                req.method = "POST"
                req.data = json.dumps(body).encode("utf-8") if body is not None else b"{}"
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries:
                raise
            time.sleep(3)

def _parse_dt(val):
    """Accept ISO strings (with/without Z) and epoch-ms ints. Return aware UTC dt."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val / 1000.0, tz=timezone.utc)
    s = str(val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(val)[:19], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


# ----------------------------- FETCHERS ------------------------------------
def fetch_greenhouse(slug, name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _get_json(url)
    roles = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        # Greenhouse has no remote boolean, so location.name alone misses remote roles
        # posted with an HQ city (e.g. offices say "US Remote" but loc says "United
        # States"). Fold in offices[] name+location for both the remote signal and a
        # cleaner location string (offices carry the full "City, State, Country").
        # offices[] informs the REMOTE SIGNAL only -- NOT the location string. A
        # company's HQ country must not be appended to the role's location, or a US-HQ
        # company's foreign role (loc "Bulgaria", office "Boca Raton, US") would inject a
        # "United States" token and wrongly survive the non-US filter in keep().
        offices = j.get("offices") or []
        off_blob = " ".join(((o.get("name") or "") + " " + (o.get("location") or ""))
                            for o in offices)
        remote = bool(REMOTE_PAT.search(loc + " " + off_blob))
        # If loc.name is blank, fall back to the office location for the role's location.
        if not loc and offices:
            loc = (offices[0].get("location") or offices[0].get("name") or "").strip()
        # Greenhouse list endpoint exposes updated_at; first_published when present.
        posted = _parse_dt(j.get("first_published") or j.get("updated_at"))
        roles.append(Role("greenhouse", slug, name, j.get("id"), j.get("title"),
                          loc, remote, "", posted, j.get("absolute_url")))
    return roles


def fetch_lever(slug, name):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get_json(url)
    roles = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        remote = (j.get("workplaceType") == "remote") or REMOTE_PAT.search(loc)
        roles.append(Role("lever", slug, name, j.get("id"), j.get("text"),
                          loc, remote, "", _parse_dt(j.get("createdAt")),
                          j.get("hostedUrl")))
    return roles


def fetch_ashby(slug, name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = _get_json(url, pause=1.5)  # Ashby throttles rapid requests -- keep full spacing
    company = data.get("name") or name
    roles = []
    for j in data.get("jobs", []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        roles.append(Role("ashby", slug, company, j.get("id") or j.get("jobUrl"),
                          j.get("title"), j.get("location", ""),
                          j.get("isRemote", False), comp,
                          _parse_dt(j.get("publishedAt")),
                          j.get("jobUrl") or j.get("applyUrl")))
    return roles


def fetch_bamboohr(slug, name):
    # BambooHR's public list feed has NO posted date, so we title-prefilter on the cheap
    # list first, then hit the per-job detail endpoint (which has datePosted) only for the
    # handful of survivors -- keeping it to ~0-3 detail calls per board instead of N.
    data = _get_json(f"https://{slug}.bamboohr.com/careers/list")
    roles = []
    for j in data.get("result", []):
        title = j.get("jobOpeningName") or ""
        if not FETCH_INCLUDE.search(title) or FETCH_EXCLUDE.search(title):
            continue   # cheap prefilter -- avoid a detail request for off-function roles
        loc_obj = j.get("location") or {}
        loc = ", ".join(p for p in (loc_obj.get("city"), loc_obj.get("state")) if p)
        job_id = j.get("id")
        posted, url = None, f"https://{slug}.bamboohr.com/careers/{job_id}"
        try:
            detail = _get_json(f"https://{slug}.bamboohr.com/careers/{job_id}/detail")
            jo = (detail.get("result") or {}).get("jobOpening") or {}
            posted = _parse_dt(jo.get("datePosted"))
            url = jo.get("jobOpeningShareUrl") or url
            d_loc = jo.get("location") or {}
            loc = ", ".join(p for p in (d_loc.get("city"), d_loc.get("state")) if p) or loc
        except Exception:
            continue   # detail fetch failed -> no date -> would be dropped anyway; skip
        remote = bool(j.get("isRemote")) or bool(REMOTE_PAT.search(loc))
        roles.append(Role("bamboohr", slug, name, job_id, title, loc,
                          remote, "", posted, url))
    return roles


def _workday_age_days(text):
    """Workday list feed gives relative strings ('Posted Today', 'Posted 3 Days Ago',
    'Posted 30+ Days Ago'). Return an approximate age in days, or None if unparseable."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return int(m.group(1)) * 30
    return None


WORKDAY_SEARCH_TEXT = "Product Manager"   # server-side narrows to product roles (see below).
# run_profiles.py overrides this with the profile's "workday_search_text" so a non-PM friend
# narrows Workday to THEIR function (e.g. "Software Engineer") instead of getting zero results.


def fetch_workday(slug, name):
    # slug encodes the per-tenant config as "tenant/dc/site" (e.g. "wgu/wd5/External"),
    # because Workday needs all three and none are guessable.
    tenant, dc, site = slug.split("/", 2)
    base = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    public = f"https://{tenant}.{dc}.myworkdayjobs.com/{site}"
    # We query CXS with searchText="Product Manager" so Workday narrows to product roles
    # server-side. This is the difference between finding 0 and finding the PM roles at a
    # high-volume tenant: with searchText="" the feed is dominated by the day's non-PM
    # postings, and at a mega-board (Autodesk: thousands of openings) the sparse PM roles
    # never fit inside the page cap -- the old code returned 0 for exactly this reason.
    # TRADE-OFF: searchText results are RELEVANCE-sorted, not date-descending, so we can NO
    # LONGER early-stop once a page goes old (a recent PM role can sit below an old one). We
    # still apply the cheap relative-age skip BEFORE the detail fetch, so detail requests
    # stay bounded to recent roles; the narrower result set keeps small boards cheap (they
    # break on offset>=total in 1-2 pages). MAX_PAGES caps the rare >240-PM mega-board.
    PAGE, MAX_PAGES = 20, 12
    age_limit = MAX_AGE_DAYS + 1   # lenient buffer; keep() does the precise cut
    roles, offset, seen_ids = [], 0, set()
    for _ in range(MAX_PAGES):
        data = _get_json(f"{base}/jobs", body={"appliedFacets": {}, "limit": PAGE,
                                               "offset": offset,
                                               "searchText": WORKDAY_SEARCH_TEXT})
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            age = _workday_age_days(j.get("postedOn", ""))
            if age is not None and age > age_limit:
                continue                 # older than window -> cheap skip, no detail fetch
            title = j.get("title", "")
            if not FETCH_INCLUDE.search(title) or FETCH_EXCLUDE.search(title):
                continue                 # cheap prefilter -- avoid a detail request
            ep = j.get("externalPath", "")
            if ep in seen_ids:
                continue
            seen_ids.add(ep)
            try:
                info = (_get_json(f"{base}{ep}") or {}).get("jobPostingInfo") or {}
                posted = _parse_dt(info.get("startDate"))
                locs = [info.get("location") or ""] + (info.get("additionalLocations") or [])
                loc = "; ".join(l for l in locs if l) or j.get("locationsText", "")
                url = info.get("externalUrl") or f"{public}{ep}"
                remote = ("remote" in (info.get("remoteType") or "").lower()
                          or bool(REMOTE_PAT.search(loc)))
                job_id = info.get("jobReqId") or (j.get("bulletFields") or [ep])[0]
            except Exception:
                continue                 # detail failed -> no date -> would be dropped anyway
            roles.append(Role("workday", slug, name, job_id, title, loc,
                              remote, "", posted, url))
        offset += PAGE
        if offset >= data.get("total", 0):
            break                        # exhausted the PM-matching result set
    return roles


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "bamboohr": fetch_bamboohr, "workday": fetch_workday}


# ----------------------------- FILTER --------------------------------------
def keep(role, cutoff):
    if role.slug.lower() in AGGREGATOR_DENY or role.company.lower() in AGGREGATOR_DENY:
        return False
    if not TITLE_INCLUDE.search(role.title):
        return False
    if TITLE_EXCLUDE.search(role.title):
        return False
    if role.posted is not None and role.posted < cutoff:   # recency, enforced HERE
        return False
    # undated roles (posted is None) are NO LONGER dropped: include + flag with a
    # blank posted date so a missing ATS date stops silently discarding real roles.
    # They rank last within their tier (rank_key uses ts=0 for undated).
    if role.is_utah:
        return True
    if role.is_remote:
        # drop clearly non-US remote unless it also says US
        if NON_US_PAT.search(role.location) and not re.search(r"\b(us|usa|united states)\b",
                                                              role.location, re.I):
            return False
        return True
    return INCLUDE_ONSITE_NON_UTAH and not NON_US_PAT.search(role.location)


# NOTE: `tier` is the GEO bucket (utah/remote/onsite). `fit_tier` (A/B/C) is the
# Stage-A FIT ranking from rank_roles -- distinct axis, deliberately not overloaded.
WORKLIST_FIELDS = ["status", "uid", "fit_tier", "fit_score", "fit_why",
                   "tier", "company", "title", "location",
                   "comp", "posted", "apply_url"]


def _role_row(r):
    tier = "utah" if r.is_utah else ("remote" if r.is_remote else "onsite")
    posted = r.posted.date().isoformat() if r.posted else ""
    fit = rank_roles.score_role(r.title, r.location, r.posted, r.company, r.comp)
    return {"status": "", "uid": r.uid,
            "fit_tier": fit["fit_tier"], "fit_score": fit["fit_score"], "fit_why": fit["fit_why"],
            "tier": tier, "company": r.company,
            "title": r.title, "location": r.location, "comp": r.comp,
            "posted": posted, "apply_url": r.url}


def write_worklist(out_file, new_roles, deleted_file=DELETED_FILE):
    """Persistent worklist: never truncate. The durable tombstone (deleted_file) is the single
    AUTHORITATIVE record of retired roles -- a uid in it is dropped from the worklist and never
    re-added, surviving seen_jobs.json resets and Sheet rebuilds. It is fed from two places:
    a CSV row marked status='delete', and (via run_profiles) the roles the user retires in the
    Google Sheet. Append net-new roles deduped by uid. Returns (num_appended, worklist_total)."""
    tombstone = set(json.load(open(deleted_file))) if os.path.exists(deleted_file) else set()
    tomb_before = len(tombstone)

    existing_rows, kept_uids = [], set()
    if os.path.exists(out_file):
        with open(out_file, newline="") as f:
            for row in csv.DictReader(f):
                if (row.get("status") or "").strip().lower() == "delete":
                    if row.get("uid"):
                        tombstone.add(row["uid"])   # retire permanently
                    continue
                if not any((v or "").strip() for v in row.values()):
                    continue   # skip blank lines
                if (row.get("uid") or "") in tombstone:
                    continue   # tombstone is authoritative -> drop already-retired rows
                norm = {k: (row.get(k) or "") for k in WORKLIST_FIELDS}
                existing_rows.append(norm)
                if norm["uid"]:
                    kept_uids.add(norm["uid"])

    appended = [_role_row(r) for r in new_roles
                if r.uid not in kept_uids and r.uid not in tombstone]

    merged = existing_rows + appended

    # HYGIENE 1 -- geo self-heal: drop rows whose location is clearly non-US with no US token
    # (and not Utah). The durable worklist accumulates rows classified before NON_US_PAT was
    # tightened; this purges those stale artifacts (e.g. a "Mexico City" row) on every write,
    # matching keep()'s live geo rule. Rows with a US token (e.g. "Remote - US; Toronto") stay.
    def _clearly_non_us(loc):
        loc = loc or ""
        return (bool(NON_US_PAT.search(loc))
                and not re.search(r"\b(us|usa|united states)\b", loc, re.I)
                and not UTAH_PAT.search(loc))
    merged = [r for r in merged if not _clearly_non_us(r.get("location", ""))]

    # HYGIENE 2 -- cross-run dedup: reposts get a new job_id (new uid) so the durable worklist
    # accumulates the same role many times (e.g. Jerry x4). Collapse by (company, normalized
    # title), keeping the freshest by posted date. Trade-off: two genuinely distinct roles that
    # share an identical title at one company would collapse to one -- rare vs. the repost noise.
    def _dkey(r):
        company = (r.get("company") or "").strip().lower()
        title = re.sub(r"[^a-z0-9]+", " ", (r.get("title") or "").lower()).strip()
        return (company, title)

    def _posted_ts(r):
        try:
            return datetime.strptime((r.get("posted") or "")[:10], "%Y-%m-%d")
        except ValueError:
            return datetime.min

    best_row = {}
    for r in merged:
        k = _dkey(r)
        if k not in best_row or _posted_ts(r) > _posted_ts(best_row[k]):
            best_row[k] = r
    merged = list(best_row.values())

    # Sort the full worklist by FIT (Stage-A): tier A->B->C, then highest score first,
    # so a fresh, well-fit Director never sits buried under a stack of IC roles. Old CSV
    # rows predating fit-ranking lack these fields -> default them so the writer is back-compat
    # (untiered rows fall to the bottom). _FIT_ORDER maps unknown tiers there too.
    _FIT_ORDER = {"A": 0, "B": 1, "C": 2}

    def _fit_sort_key(row):
        tier = (row.get("fit_tier") or "").strip().upper()
        try:
            score = int(row.get("fit_score") or 0)
        except (TypeError, ValueError):
            score = 0
        return (_FIT_ORDER.get(tier, 3), -score)

    rows = sorted(merged, key=_fit_sort_key)

    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WORKLIST_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    if len(tombstone) != tomb_before:
        json.dump(sorted(tombstone), open(deleted_file, "w"))
    return len(appended), len(rows)


def push_to_worklist_sheet(new_roles, url, token):
    """POST net-new roles to the Google Sheet Apps Script web app. The script handles
    append/dedup/delete-tombstone server-side and returns {added,total,deleted}. Never
    raises -- on any failure returns None so the run continues (CSV backup already written)."""
    payload = {"token": token, "roles": [_role_row(r) for r in new_roles]}
    try:
        # _get_json sends a JSON POST body and follows Apps Script's 302 redirect to the
        # googleusercontent result; pause=0 (no ATS throttle to dodge here).
        resp = _get_json(url, body=payload, pause=0)
        if not isinstance(resp, dict) or "added" not in resp:
            print(f"  ! worklist sheet: unexpected response {resp!r} (kept CSV backup)")
            return None
        return resp
    except Exception as e:
        print(f"  ! worklist sheet push failed: {type(e).__name__} (kept CSV backup)")
        return None


def fetch_sheet_deleted(url, token):
    """Return the set of uids the user has retired in the Google Sheet -- both the durable
    Deleted tab and any worklist row currently marked status='delete'. This closes the
    Sheet->engine loop: the user triages by typing DELETE in the Sheet, and run_profiles
    merges these into the git-side tombstone so they drop from the worklist permanently.
    Never raises -- returns an empty set on any failure so a run is never blocked."""
    try:
        resp = _get_json(url, body={"token": token, "action": "deleted"}, pause=0)
        if isinstance(resp, dict) and isinstance(resp.get("deleted"), list):
            return {u for u in resp["deleted"] if u}
        print(f"  ! sheet-deleted: unexpected response {resp!r}")
    except Exception as e:
        print(f"  ! sheet-deleted fetch failed: {type(e).__name__}")
    return set()


def rank_key(role):
    tier = 0 if role.is_utah else (1 if role.is_remote else 2)
    ts = role.posted.timestamp() if role.posted else 0
    return (tier, -ts)


# ----------------------------- MAIN ----------------------------------------
def main():
    if not os.path.exists(BOARDS_FILE):
        sys.exit(f"Missing {BOARDS_FILE}. Create it (see the sample in this folder).")
    boards = json.load(open(BOARDS_FILE))
    seen = set(json.load(open(SEEN_FILE))) if os.path.exists(SEEN_FILE) else set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    all_ids, matches, failures = set(), [], []
    undated_skipped = 0   # passed title filters but dropped only for missing date
    for b in boards:
        ats, slug = b.get("ats"), b.get("slug")
        fetcher = FETCHERS.get(ats)
        if not fetcher:
            failures.append(f"{ats}:{slug} (unknown ats)")
            continue
        try:
            roles = fetcher(slug, b.get("name", slug))
        except Exception as e:
            failures.append(f"{ats}:{slug} ({type(e).__name__})")
            continue
        for r in roles:
            all_ids.add(r.uid)
            if r.uid in seen:
                continue
            if keep(r, cutoff):
                matches.append(r)
            elif (r.posted is None
                  and r.slug.lower() not in AGGREGATOR_DENY
                  and r.company.lower() not in AGGREGATOR_DENY
                  and TITLE_INCLUDE.search(r.title)
                  and not TITLE_EXCLUDE.search(r.title)):
                undated_skipped += 1   # would have matched but for the missing date

    # dedup across ATS by (company, title); keep the freshest
    best = {}
    for r in sorted(matches, key=lambda x: x.posted or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True):
        best.setdefault((r.company.lower(), r.title.lower()), r)
    new_roles = sorted(best.values(), key=rank_key)

    n_added, n_total = write_worklist(OUT_FILE, new_roles, DELETED_FILE)   # local backup, always

    seen |= all_ids
    json.dump(sorted(seen), open(SEEN_FILE, "w"))

    print(f"Boards polled: {len(boards)}  |  failed: {len(failures)}")
    if failures:
        print("  Failed boards (check slug/ATS):", ", ".join(failures))
    if undated_skipped:
        print(f"Skipped {undated_skipped} title-matching role(s) with no posted date.")
    print(f"Added to worklist: {n_added}  |  worklist total: {n_total}  ->  {OUT_FILE}")

    # NOTE: this single-user entrypoint writes a LOCAL CSV only and deliberately does NOT push
    # to any Google Sheet. run_profiles.py is the canonical (multi-user) pipeline and the only
    # thing that should write a shared sheet -- a second pusher here caused worklist/sheet drift
    # (orphan rows that belonged to no profile's state). Use `python3 run_profiles.py` for the
    # real run; this main() remains as a quick local smoke test of the fetch+filter+rank path.
    for r in new_roles:
        tier = "UT" if r.is_utah else ("RM" if r.is_remote else "ON")
        date = r.posted.date().isoformat() if r.posted else "?"
        print(f"  [{tier}] {date}  {r.company:<22} {r.title[:48]:<48} {r.url}")


if __name__ == "__main__":
    main()
