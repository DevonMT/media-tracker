"""Netflix's viewing-activity CSV, turned into titles Matinee can file as seen.

No model anywhere: parsing and TMDB matching are plain code, so this is open to
everybody and costs nothing metered.

WHAT THE FILE IS. One row per PLAY, not per title: every episode, every
resumed film, every autoplayed trailer. Netflix writes a series as
"Show: Season 2: Episode Title" and a film as its bare name, and marks trailers,
hooks and recaps in "Supplemental Video Type". So the work is to drop the
non-views, fold the plays into titles, and then decide what each title is.

MATCHING IS DELIBERATELY STRICT. The file carries no year, and tmdb.find_match
takes "Up" to be "Upgrade" when it has to. A wrong match here would mark the
wrong film as seen and hide it from every recommendation, so only an exact
(normalised) name is accepted; everything else is listed for a person to look
at, never guessed.
"""
import csv
import io
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import tmdb

# Plays shorter than this, summed per title, are a preview that ran on, not a
# watch. Trailers and hooks are already gone by Supplemental Video Type.
# Two bars because episodes of a show with no season marker only fold into one
# title AFTER TMDB names it: PRE keeps the lookups to things worth looking up,
# MIN is applied to the folded total.
PRE_SECONDS = 60
MIN_SECONDS = 300

# More than this many distinct titles is a lookup per title, so cap it and say so.
MAX_TITLES = 400

# A segment that says "this is a season/series of a show", not an episode name.
STRONG = re.compile(r"^(season\s*\d*|series\s*\d*|limited series|collection|volume\s*\d+|book\s*\d+)\b", re.I)
# "Part 2" is only a show marker when an episode name follows it: a film can be
# "Dune: Part Two" and end right there.
WEAK = re.compile(r"^(part\s*\d+|chapter\s*\d+|episode\s*\d+)\b", re.I)


def _seconds(text):
    try:
        h, m, s = (int(p) for p in (text or "").strip().split(":"))
    except ValueError:
        return 0
    return h * 3600 + m * 60 + s


def classify(title):
    """What a viewing-activity title names, as a list of (name, kind) to try in
    order. kind is "show", "movie" or None for either.

    A season marker settles it. Without one, "A: B" is a film with a subtitle
    or an episode of a show with no seasons, and only TMDB can say which, so
    both are offered and the whole title goes first."""
    segs = [s.strip() for s in title.split(": ")]
    for i in range(1, len(segs)):
        if STRONG.match(segs[i]) or (WEAK.match(segs[i]) and i < len(segs) - 1):
            return [(": ".join(segs[:i]), "show")]
    if len(segs) > 1:
        return [(title.strip(), "movie"), (segs[0], "show")]
    return [(title.strip(), None)]


def parse(text, profile=None):
    """Plays folded into titles.

    Returns (titles, profiles). `titles` maps the show or film as Netflix wrote
    it (episodes collapsed to the show name where a season marker says so) to
    {"candidates", "seconds", "last"}; `profiles` counts rows per profile so the
    page can say when a household file holds more than one person.
    Raises ValueError when this is not a viewing-activity file."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "Title" not in reader.fieldnames:
        raise ValueError("That does not look like Netflix's viewing activity: "
                         "there is no Title column.")
    titles, profiles = OrderedDict(), {}
    for row in reader:
        name = (row.get("Profile Name") or "").strip()
        profiles[name] = profiles.get(name, 0) + 1
        if profile and name.lower() != profile.strip().lower():
            continue
        if (row.get("Supplemental Video Type") or "").strip():
            continue  # trailer, hook, recap, promo
        raw = (row.get("Title") or "").strip()
        if not raw:
            continue
        cands = classify(raw)
        key = cands[0][0] if cands[0][1] == "show" else raw
        t = titles.setdefault(key, {"candidates": cands, "seconds": 0, "last": None})
        t["seconds"] += _seconds(row.get("Duration"))
        day = (row.get("Start Time") or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and (t["last"] is None or day > t["last"]):
            t["last"] = day
    return {k: v for k, v in titles.items() if v["seconds"] >= PRE_SECONDS}, profiles


def _lookup(candidates):
    """The first candidate TMDB names exactly, else None. Raises if TMDB is down."""
    for name, kind in candidates:
        hit = tmdb.find_match(name, None, kind or "movie")
        if hit and tmdb._norm_for_match(hit["title"]) == tmdb._norm_for_match(name):
            return hit
    return None


def match(titles, workers=6):
    """Split parsed titles into (matched, unmatched, truncated).

    matched: one entry per TMDB id (a show and its episodes are one title).
    unmatched: [{"name", "kind", "reason"}], reason "no match" or "TMDB unavailable"."""
    items = list(titles.items())
    truncated = max(0, len(items) - MAX_TITLES)
    items = items[:MAX_TITLES]

    def one(item):
        key, t = item
        try:
            return key, t, _lookup(t["candidates"]), None
        except Exception:  # noqa: BLE001 - any TMDB failure is "could not check"
            return key, t, None, "TMDB unavailable"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, items))

    by_id, unmatched = OrderedDict(), []
    for key, t, hit, err in results:
        if not hit:
            if t["seconds"] >= MIN_SECONDS:
                show = t["candidates"][0][1] == "show"
                unmatched.append({"name": key, "kind": "show" if show else "movie",
                                  "reason": err or "no match"})
            continue
        m = by_id.get(hit["tmdb_id"])
        if m:
            m["seconds"] += t["seconds"]
            m["last"] = max(filter(None, [m["last"], t["last"]]), default=None)
            continue
        by_id[hit["tmdb_id"]] = {
            "tmdb_id": hit["tmdb_id"], "name": hit["title"], "kind": hit["type"],
            "year": hit["year"],
            "genres": hit["genre"].split("/") if hit.get("genre") else [],
            "overview": hit.get("overview") or None,
            "seconds": t["seconds"], "last": t["last"]}
    matched = [m for m in by_id.values() if m["seconds"] >= MIN_SECONDS]
    return matched, unmatched, truncated
