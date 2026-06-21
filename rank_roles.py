#!/usr/bin/env python3
"""Stage-A deterministic pre-ranker for the worklist (free, stdlib, no LLM).

Scores each role on three signals that are reliably readable from the worklist/title metadata
alone -- LEVEL (title -> seniority), LOCATION (Utah / remote / onsite), and FRESHNESS -- with the
weight on LEVEL and LOCATION and freshness as a light tiebreaker. It sorts the worklist so a fresh,
remote-or-Utah Director never sits buried under a stack of IC roles.

It deliberately does NOT score domain/role fit. We tried that (a keyword match on the title) and it
was wrong in both directions: it false-POSITIVED on "AI"/"Platform"/"Director" titles whose JDs were
actually deep-infra, devex, or regulated-domain roles Tyler can't clear, and false-NEGATIVED on a
genuine marketplace role whose "marketplace" signal lived in the JD body, not the title. The lesson
(2026-06-09): real fit is a JD-read judgment, not a title heuristic. So this score is a coarse
"is it the right LEVEL, can Tyler WORK there, and is it FRESH" pre-sort -- never a fit gate. The
tailor-application skill reads the full JD and makes the actual call.

LOCATION is coarse on purpose: it classifies Utah / remote / onsite / non-US from the location
string. It cannot see a JD's *state-restricted* remote (e.g. "remote, but only WA/OR/CA" excludes
Utah) -- that needs the JD read, which is where it's caught.

Use:
    import rank_roles; rank_roles.score_role(title, location, posted, company, comp)
    python3 rank_roles.py out/tyler_new_roles.csv        # writes *_ranked.csv + prints summary
"""
import csv, re, sys
from datetime import datetime, timezone

# ---- LEVEL: title -> seniority points (checked high-to-low; first match wins) ----
# Level word may be separated from "product manager" by infixes (Technical, AI, Sr.) --
# e.g. "Principal Technical Product Manager" -- so the PM-level patterns allow a bounded gap.
_GAP = r"[\w ,/&()+.-]{0,24}?"
LEVEL = [
    (re.compile(r"\bchief product officer\b|\bcpo\b", re.I), 44, "CPO"),
    (re.compile(r"\b(senior|sr\.?) director\b", re.I), 44, "Sr Director"),
    (re.compile(r"\b[se]?vp\b|\bvice president\b", re.I), 42, "VP"),
    (re.compile(r"\bhead of product\b", re.I), 42, "Head of Product"),
    (re.compile(r"\bdirector\b", re.I), 40, "Director"),
    (re.compile(rf"\bgroup\b{_GAP}\bproduct manager\b|\bgpm\b", re.I), 34, "Group PM"),
    (re.compile(rf"\bprincipal\b{_GAP}\bproduct manager\b", re.I), 34, "Principal PM"),
    (re.compile(rf"\bstaff\b{_GAP}\bproduct manager\b", re.I), 32, "Staff PM"),
    (re.compile(rf"\blead\b{_GAP}\bproduct manager\b", re.I), 32, "Lead PM"),
    (re.compile(rf"\b(senior|sr\.?)\b{_GAP}\bproduct manager\b", re.I), 28, "Senior PM"),
    (re.compile(r"\bproduct manager ii\b|\bpm ii\b", re.I), 18, "PM II (down-ranked)"),
    (re.compile(r"\bproduct (engineer|builder)\b", re.I), 28, "Product Eng/Builder (Sr-equiv)"),
    (re.compile(r"\bproduct lead\b", re.I), 24, "Product Lead"),
    (re.compile(r"\bproduct manager\b|\bproduct management\b", re.I), 12, "Plain PM (down-ranked)"),
]

# ---- LOCATION: Tyler is remote-or-Utah only (he declined onsite/relocation). Heavy weight. ----
# Patterns mirror watchlist_poller's geo classifiers (kept local so rank_roles stays dependency-free
# -- watchlist_poller imports rank_roles, so importing back would be a cycle).
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
US_TOKEN = re.compile(r"\b(us|usa|united states)\b", re.I)

LOC_UTAH, LOC_REMOTE, LOC_UNKNOWN, LOC_OUT = 40, 34, 12, 0


def _location(location, title=""):
    """Coarse geo points + label. Utah and remote are the whole game for Tyler; onsite-non-Utah and
    non-US are zeroed (he can't/won't take them). Reads title too, since '... (Remote)' is common."""
    loc = location or ""
    blob = f"{loc} {title or ''}"
    if NON_US_PAT.search(loc) and not US_TOKEN.search(loc):
        return LOC_OUT, "non-US"
    if UTAH_PAT.search(loc):
        return LOC_UTAH, "Utah"
    if REMOTE_PAT.search(blob):
        return LOC_REMOTE, "remote"
    if US_TOKEN.fullmatch(loc.strip()):
        # Bare "US"/"USA"/"United States" carries no city/state info — it's often a fully-remote
        # posting that just didn't say "remote" in the location field, so treat it as unknown
        # rather than zeroing it into Tier C (the JD read is the real judge).
        return LOC_UNKNOWN, "loc? (bare US)"
    if not loc.strip():
        return LOC_UNKNOWN, "loc?"
    return LOC_OUT, "onsite non-UT"


# ---- Title-level disqualifiers (JD-level ones are the skill's job) ----
DISQUALIFY = re.compile(r"\bcontract(or)?\b|\btemporary\b|\bpart-?time\b", re.I)


def _age_days(posted):
    if not posted:
        return None
    s = str(posted).strip().replace("Z", "+00:00")
    for parse in (lambda: datetime.fromisoformat(s),
                  lambda: datetime.strptime(s[:10], "%Y-%m-%d")):
        try:
            dt = parse()
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days
        except ValueError:
            continue
    return None


def _freshness(age):
    # Lightest of the three signals -- a tiebreaker, not a driver.
    if age is None: return 2
    if age <= 1: return 10
    if age <= 2: return 8
    if age <= 3: return 6
    if age <= 7: return 3
    if age <= 14: return 1
    return 0


def score_role(title, location="", posted=None, company="", comp=""):
    lvl, lvl_label = 0, "non-PM"
    for pat, pts, label in LEVEL:
        if pat.search(title or ""):
            lvl, lvl_label = pts, label; break
    loc, loc_label = _location(location, title)
    age = _age_days(posted)
    fresh = _freshness(age)
    disq = bool(DISQUALIFY.search(title or ""))
    total = lvl + loc + fresh
    # Tier is a coarse LEVEL band (gated by workability), NOT a fit gate: leadership -> A,
    # senior IC -> B, below-Senior-PM / can't-work-there (loc 0) / contract-temp -> C. The
    # numeric score (which weights location + freshness) orders roles within a tier.
    if disq or loc == 0 or lvl < 28:
        tier = "C"
    elif lvl >= 40:
        tier = "A"
    else:
        tier = "B"
    why = f"{lvl_label}(+{lvl}) | {loc_label}(+{loc}) | fresh(+{fresh})"
    if disq: why += " | DISQ:contract/temp"
    return {"fit_tier": tier, "fit_score": total, "fit_why": why,
            "alt": lvl, "loc": loc, "loc_label": loc_label, "fresh": fresh, "age_days": age}


def _pick(row, *names):
    for n in names:
        for k in row:
            if k and k.lower() == n: return row[k]
    return ""


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "new_roles.csv"
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        s = score_role(_pick(r, "title"), _pick(r, "location"),
                       _pick(r, "posted", "posted_at", "date"),
                       _pick(r, "company"), _pick(r, "comp"))
        r["fit_tier"], r["fit_score"], r["fit_why"] = s["fit_tier"], s["fit_score"], s["fit_why"]
    order = {"A": 0, "B": 1, "C": 2}
    rows.sort(key=lambda r: (order[r["fit_tier"]], -int(r["fit_score"])))
    out = path.rsplit(".", 1)[0] + "_ranked.csv"
    fields = list(rows[0].keys()) if rows else []
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    from collections import Counter
    print(f"{len(rows)} roles -> {out}   tiers: {dict(Counter(r['fit_tier'] for r in rows))}\n")
    for tier in ("A", "B", "C"):
        sub = [r for r in rows if r["fit_tier"] == tier]
        print(f"=== {tier} ({len(sub)}) ===")
        for r in sub[:12]:
            print(f"  [{r['fit_score']:>3}] {_pick(r,'company')[:18]:<18} {_pick(r,'title')[:42]:<42} {_pick(r,'location')[:16]:<16} | {r['fit_why']}")
        print()


if __name__ == "__main__":
    main()
