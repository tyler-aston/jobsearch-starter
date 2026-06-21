#!/usr/bin/env python3
"""Multi-user runner. Polls the shared board list ONCE, then applies each person's
own filters (titles / geo / recency) and writes each person their own worklist
(CSV always; their own Google Sheet if configured).

Why poll once, filter many: fetching 460 boards is the slow, rate-limited part.
We fetch every board a single time, then run each profile's filter over the same
in-memory role set. Adding a 10th friend costs ~no extra network time.

Profiles live in profiles/*.json (files starting with "_" are ignored — e.g.
the example template). Per-profile state/output:
    state/<name>_seen.json        change-detection (commit this across runs)
    state/<name>_deleted.json     durable tombstone of user-deleted roles
    out/<name>_new_roles.csv      the worklist (durable; never truncated)

Each profile's Google Sheet secrets are read from the environment by suffix:
    WORKLIST_WEBHOOK_URL_<SHEET_ENV>  +  WORKLIST_TOKEN_<SHEET_ENV>
(e.g. sheet_env="ME" -> WORKLIST_WEBHOOK_URL_ME). Missing -> CSV only.

Usage:
    python3 run_profiles.py                 # all profiles
    python3 run_profiles.py me alex         # only named profiles
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import watchlist_poller as wp   # reuse fetchers, Role, _role_row, write_worklist, sheet push

PROFILE_DIR = "profiles"
STATE_DIR = "state"
OUT_DIR = "out"


def load_profiles(only=None):
    profiles = []
    for path in sorted(glob.glob(os.path.join(PROFILE_DIR, "*.json"))):
        base = os.path.basename(path)
        if base.startswith("_"):
            continue   # template / disabled
        p = json.load(open(path))
        name = p.get("name") or os.path.splitext(base)[0]
        p["name"] = name
        if only and name not in only:
            continue
        profiles.append(p)
    return profiles


_MATCH_NOTHING = re.compile(r"(?!)")   # never matches -- a "no excludes" / permissive sentinel


def compile_filters(p):
    """Return (title_include, title_exclude, geo) for a profile, inheriting the
    poller's defaults when a field is null/absent.

    Exclude inheritance is deliberate and asymmetric: a profile that customizes
    title_include but leaves title_exclude null gets NO excludes -- NOT the PM exclude.
    The PM exclude drops designer/ux/product-design/associate/owner titles, which ARE a
    non-PM friend's targets, so silently inheriting it would zero out e.g. a Design search
    across every board. Only an all-default profile (Tyler) inherits the PM exclude."""
    ti = p.get("title_include")
    te = p.get("title_exclude")
    inc = re.compile(ti, re.I) if ti else wp.TITLE_INCLUDE
    if te:
        exc = re.compile(te, re.I)        # explicit exclude -> use it
    elif ti:
        exc = _MATCH_NOTHING              # custom include, no exclude given -> exclude nothing
    else:
        exc = wp.TITLE_EXCLUDE           # all-default (PM) profile -> inherit the PM exclude
    geo = p.get("geo") or {}
    geo = {
        "keep_utah": geo.get("keep_utah", True),
        "keep_remote_us": geo.get("keep_remote_us", True),
        "keep_onsite_other": geo.get("keep_onsite_other", False),
    }
    return inc, exc, geo


def geo_pass(role, geo):
    """Profile-aware version of the poller's keep() geo logic."""
    if role.is_utah:
        return geo["keep_utah"]
    if role.is_remote:
        # drop clearly non-US remote unless it also says US
        if wp.NON_US_PAT.search(role.location) and not re.search(
                r"\b(us|usa|united states)\b", role.location, re.I):
            return False
        return geo["keep_remote_us"]
    return geo["keep_onsite_other"] and not wp.NON_US_PAT.search(role.location)


def fetch_prefilter(plist):
    """Compute the fetch-time title prefilter for a set of profiles sharing one boards file.

    BambooHR/Workday skip off-function titles BEFORE the per-job detail call (see
    watchlist_poller.FETCH_INCLUDE), so the prefilter must keep every title ANY active
    profile wants. For a friend's repo there's exactly one profile, so this is just their
    own filter; for a multi-profile repo it's the permissive union.

    Returns (fetch_include, fetch_exclude, workday_search_text)."""
    incs, excs, searches = [], [], []
    for p in plist:
        inc, exc, _ = compile_filters(p)
        incs.append(inc)
        excs.append(exc)
        # Normalize THEN fall back: a whitespace-only value must collapse to the PM default,
        # not to an empty searchText (which would un-narrow Workday and flood mega-tenants).
        searches.append((p.get("workday_search_text") or "").strip() or wp.WORKDAY_SEARCH_TEXT)
    inc_pats = list(dict.fromkeys(c.pattern for c in incs))
    fetch_inc = incs[0] if len(inc_pats) == 1 else re.compile("|".join(f"(?:{p})" for p in inc_pats), re.I)
    # Only safe to exclude at fetch time if EVERY profile excludes the same thing; otherwise
    # one profile's exclude could hide a title another profile wants -> stay permissive.
    exc_pats = list(dict.fromkeys(c.pattern for c in excs))
    fetch_exc = excs[0] if len(exc_pats) == 1 else _MATCH_NOTHING
    # Workday's server-side searchText can't be a regex. One distinct term -> use it;
    # mixed-function multi-profile repo -> fall back to the PM default.
    terms = list(dict.fromkeys(searches))
    workday_search = terms[0] if len(terms) == 1 else wp.WORKDAY_SEARCH_TEXT
    return fetch_inc, fetch_exc, workday_search


def fetch_all_boards(boards_file, fetch_inc, fetch_exc, workday_search):
    """Fetch every board ONCE, with the given fetch-time prefilter. Returns (roles, failures)."""
    wp.FETCH_INCLUDE = fetch_inc
    wp.FETCH_EXCLUDE = fetch_exc
    wp.WORKDAY_SEARCH_TEXT = workday_search
    boards = json.load(open(boards_file))
    roles, failures = [], []
    for b in boards:
        ats, slug = b.get("ats"), b.get("slug")
        fetcher = wp.FETCHERS.get(ats)
        if not fetcher:
            failures.append(f"{ats}:{slug} (unknown ats)")
            continue
        try:
            roles.extend(fetcher(slug, b.get("name", slug)) or [])
        except Exception as e:
            failures.append(f"{ats}:{slug} ({type(e).__name__})")
    return roles, failures


def run_profile(p, all_roles, boards_failures):
    name = p["name"]
    inc, exc, geo = compile_filters(p)
    # Footgun guard: a friend who set a non-PM title_include but left workday_search_text on the
    # product default gets ~0 Workday roles silently (Workday narrows server-side and can't take a
    # regex). Surface it in the run log so a green run isn't mistaken for full coverage.
    wst = (p.get("workday_search_text") or "").strip()
    if p.get("title_include") and wst.lower() in ("", "product manager"):
        print(f"[{name}] note: custom title_include but workday_search_text is "
              f"'{wst or 'Product Manager'}' -> Workday boards return ~0. Set it to your function "
              f"(e.g. 'Software Engineer'); see the README recipe.")
    # Recency window precedence: MAX_AGE_DAYS env var (one-off override for the whole
    # run) > the profile's "max_age_days" > the poller default. Lets you do a quick
    # `MAX_AGE_DAYS=21 python3 run_profiles.py` backlog peek without editing/committing.
    max_age = p.get("max_age_days", wp.MAX_AGE_DAYS)
    env_age = os.environ.get("MAX_AGE_DAYS")
    if env_age:
        try:
            max_age = int(env_age)
        except ValueError:
            pass  # ignore a non-numeric override, keep the profile value
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)

    seen_file = os.path.join(STATE_DIR, f"{name}_seen.json")
    del_file = os.path.join(STATE_DIR, f"{name}_deleted.json")
    out_file = os.path.join(OUT_DIR, f"{name}_new_roles.csv")
    seen = set(json.load(open(seen_file))) if os.path.exists(seen_file) else set()

    # Resolve this profile's Sheet secrets up front: we need them BEFORE write_worklist to pull
    # the user's Sheet-side retires into the tombstone, and again after to push net-new roles.
    env = p.get("sheet_env")
    env = env.strip() if isinstance(env, str) and env.strip().lower() not in ("none", "null") else None
    url = os.environ.get(f"WORKLIST_WEBHOOK_URL_{env}") if env else None
    token = os.environ.get(f"WORKLIST_TOKEN_{env}") if env else None

    # Close the Sheet->engine loop: the user triages by typing DELETE in the Sheet; merge those
    # retires into the durable git tombstone so write_worklist drops them from the worklist for
    # good (survives Sheet rebuilds and seen resets). Best-effort -- never blocks the run.
    if url and token:
        sheet_deleted = wp.fetch_sheet_deleted(url, token)
        if sheet_deleted:
            tomb = set(json.load(open(del_file))) if os.path.exists(del_file) else set()
            if not sheet_deleted <= tomb:
                json.dump(sorted(tomb | sheet_deleted), open(del_file, "w"))

    all_ids, matches = set(), []
    for r in all_roles:
        all_ids.add(r.uid)
        if r.uid in seen:
            continue
        if r.company.lower() in wp.AGGREGATOR_DENY or r.slug.lower() in wp.AGGREGATOR_DENY:
            continue
        if not inc.search(r.title) or exc.search(r.title):
            continue
        if r.posted is not None and r.posted < cutoff:
            continue   # undated roles pass (flagged with blank date), matching poller behavior
        if not geo_pass(r, geo):
            continue
        matches.append(r)

    # dedup across ATS by (company, title), keep freshest
    best = {}
    for r in sorted(matches, key=lambda x: x.posted or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True):
        best.setdefault((r.company.lower(), r.title.lower()), r)
    new_roles = sorted(best.values(), key=wp.rank_key)

    n_added, n_total = wp.write_worklist(out_file, new_roles, del_file)

    seen |= all_ids
    json.dump(sorted(seen), open(seen_file, "w"))

    line = f"[{name}] new: {n_added}  worklist total: {n_total}  -> {out_file}"

    # Optional per-profile Google Sheet (secrets resolved above).
    if env:
        if url and token:
            resp = wp.push_to_worklist_sheet(new_roles, url, token)
            if resp:
                line += f"  | Sheet added {resp['added']} (total {resp['total']})"
            else:
                line += "  | Sheet push failed (CSV kept)"
        else:
            line += f"  | Sheet skipped (set WORKLIST_*_{env})"
    return line


def main():
    only = set(sys.argv[1:]) or None
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    profiles = load_profiles(only)
    if not profiles:
        sys.exit("No profiles found in profiles/*.json (files starting with _ are ignored).")

    # Boards file: usually shared. If profiles use different board files, fetch each once.
    by_boards = {}
    for p in profiles:
        by_boards.setdefault(p.get("boards_file", wp.BOARDS_FILE), []).append(p)

    print(f"Running {len(profiles)} profile(s): {', '.join(p['name'] for p in profiles)}")
    for boards_file, plist in by_boards.items():
        fetch_inc, fetch_exc, workday_search = fetch_prefilter(plist)
        print(f"\nFetching boards from {boards_file} (once for {len(plist)} profile(s); "
              f"Workday search='{workday_search}')...")
        all_roles, failures = fetch_all_boards(boards_file, fetch_inc, fetch_exc, workday_search)
        print(f"  fetched {len(all_roles)} roles | failed boards: {len(failures)}")
        if failures:
            print("  failures:", ", ".join(failures[:20]) + (" ..." if len(failures) > 20 else ""))
        for p in plist:
            print(run_profile(p, all_roles, failures))
    print("\nDONE")


if __name__ == "__main__":
    main()
