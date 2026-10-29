"""Recommending for several people at once.

The idea the old single-profile prompt could not express at all.

OPTIMISE THE FLOOR, NOT THE MEAN. A film everybody likes at 70 beats one that
two people love at 95 and one endures at 30. The mean prefers the second;
nobody wants to be the person who hated movie night. So the model is asked for
a per-person confidence, candidates rank by the MINIMUM across the group, and
the mean is only a tiebreak.

The floor is also what gets shown. "weakest fit: Gran, 62" is the number that
decides whether you put it on, and hiding it behind an average would be
presenting the one figure that does not answer the question.
"""
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from . import store

# Asked for beyond `want`, because the checks below remove some after the fact.
SPARE = 3

# What the model must return. Structured output only: the broker guarantees the
# shape or raises, so nothing here parses prose.
SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "year": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["movie", "show"]},
                    "genres": {"type": "array", "items": {"type": "string"}},
                    "overview": {"type": "string"},
                    "reason": {"type": "string"},
                    "platform": {"type": "string"},
                    # One number per person, tagged with the id we sent. Asking
                    # for a single score would make the model do the averaging,
                    # and the whole point is that we do not average. A list and
                    # not an object keyed by id: the native output path refuses
                    # a free-form object, and the ids are not known in advance.
                    "scores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "score": {"type": "integer"},
                            },
                            "required": ["id", "score"],
                        },
                    },
                },
                "required": ["name", "kind", "reason", "scores"],
            },
        }
    },
    "required": ["picks"],
}


def _profile_lines(rows):
    out = []
    for r in rows:
        g = "/".join(r["genres"] or [])
        note = (" — %s" % r["notes"]) if r.get("notes") else ""
        out.append("  %s (%s%s) rated %s/5%s"
                   % (r["name"], r["year"] or "?", (", " + g) if g else "", r["rating"], note))
    return "\n".join(out) if out else "  (nothing rated yet)"


def build_prompt(people, rules, seen, platforms, want, allow_rewatch):
    """One prompt, several people, per-person scores back.

    Every person's history is given SEPARATELY and labelled with their id. A
    merged profile would ask the model to average before we get a chance not
    to, which is the bug the floor rule exists to fix.
    """
    who = ", ".join(p["label"] for p in people)
    blocks = "\n\n".join(
        "%s (id: %s) — %d rated titles:\n%s"
        % (p["label"], p["id"], len(p["profile"]), _profile_lines(p["profile"]))
        for p in people)

    vetoes = []
    for r in rules:
        if r["kind"] == "never_genre":
            vetoes.append("never suggest anything in the genre %s" % r["value"])
        elif r["kind"] == "never_keyword":
            vetoes.append("never suggest anything involving %s" % r["value"])
        elif r["kind"] == "max_runtime":
            vetoes.append("nothing longer than %s minutes" % r["value"])
        elif r["kind"] == "min_year":
            vetoes.append("nothing made before %s" % r["value"])
    veto_text = ("\nHARD RULES — these are not preferences, and a title breaking "
                 "any of them must not appear at all:\n" +
                 "\n".join("  - " + v for v in vetoes)) if vetoes else ""

    avail = (("\nThey can watch on: %s." % ", ".join(platforms)) if platforms else "")
    seen_text = ""
    if seen and not allow_rewatch:
        # All of them. It was the first 120 alphabetically, so anything after
        # the cut-off was never mentioned at all; drop_seen() is the real guard.
        seen_text = ("\nAlready seen or dismissed by someone in the group — do not "
                     "suggest these:\n  " + ", ".join(sorted(seen)))

    return (
        "Recommend %d things to watch for this group: %s.\n\n"
        "%s\n"
        "%s%s%s\n\n"
        "SCORE EVERY PICK FOR EVERY PERSON SEPARATELY, 0-100, in `scores`, as "
        "one {id, score} entry per person using the id given above. Do not average them and do not return a single "
        "score: a film the whole group likes at 70 is better than one two people "
        "love at 95 and one person endures at 30, and only per-person numbers can "
        "express that.\n"
        "Be honest about the weak fit rather than flattering the group — an "
        "inflated low score is what puts on a film somebody hates.\n"
        "`reason` should say why it suits the group in one sentence, naming the "
        "tension if there is one."
        % (want, who, blocks, veto_text, avail, seen_text))


def _score_map(raw):
    """The model's `scores` as {id: number}.

    The schema asks for a list of {id, score}; a plain {id: number} object is
    accepted too, since that is what rank() hands on and what is stored."""
    if isinstance(raw, dict):
        return raw
    out = {}
    for item in raw or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out.setdefault(item["id"], item.get("score"))
    return out


def rank(picks, ids):
    """Order by the weakest link, then the average.

    Anything the model failed to score for somebody in the group is dropped
    rather than defaulted: a missing score is not a zero and not a pass, and
    guessing either way would put a number on the card that nobody produced.
    """
    out = []
    for p in picks:
        scores = {k: v for k, v in _score_map(p.get("scores")).items()
                  if k in ids and isinstance(v, (int, float))
                  and not isinstance(v, bool)}
        if len(scores) != len(ids):
            continue
        floor_id = min(scores, key=lambda k: scores[k])
        p = dict(p)
        p["scores"] = {k: int(v) for k, v in scores.items()}
        p["floor"] = int(scores[floor_id])
        p["floor_user"] = floor_id
        p["mean"] = round(sum(scores.values()) / len(scores))
        out.append(p)
    out.sort(key=lambda p: (-p["floor"], -p["mean"]))
    return out


def title_key(name):
    """A title reduced to what identifies it: case, accents, trademark signs,
    punctuation and a leading article."""
    s = unicodedata.normalize("NFKD", (name or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[™®©]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return " ".join(s.split())


def drop_seen(picks, names):
    """Seen, dismissed or already kept: removed in code. The model is told too,
    but it is a model, and this is the part that does not depend on it."""
    keys = {title_key(n) for n in names}
    return [p for p in picks if title_key(p.get("name")) not in keys]


def _norm_provider(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").replace("+", " plus ").lower())


def availability(p, platforms, tmdb):
    """Where one title can be watched by the group, from TMDB.

    Returns (state, where): "streaming" (where = the group's services that
    carry it), "rent" (where = ["Rent: ...", "Buy: ..."], only if a service can rent),
    "gone" (TMDB knows it, but none of the group's services carry it),
    "missing" (TMDB has never heard of it) or "unknown" (TMDB unreachable).
    """
    names = {_norm_provider(x["name"]): x["name"] for x in platforms}
    can_rent = any(x.get("can_rent") for x in platforms)
    kind = "show" if p.get("kind") == "show" else "movie"
    try:
        # A pick that came from TMDB already has its id; only a model's pick
        # needs the name lookup.
        match = ({"tmdb_id": p["tmdb_id"], "type": kind} if p.get("tmdb_id")
                 else tmdb.find_match(p.get("name"), p.get("year"), kind))
        if not match:
            return "missing", []
        prov = tmdb.get_watch_providers(match["tmdb_id"], match.get("type") or kind)
    except Exception:  # noqa: BLE001 — unverified
        return "unknown", []
    streams = (prov.get("flatrate") or []) + (prov.get("free") or []) + (prov.get("ads") or [])
    where = sorted({names[k] for pn in map(_norm_provider, streams)
                    for k in names if k and (k in pn or pn in k)})
    if where:
        return "streaming", where
    lines = [label + ": " + ", ".join(sorted(set(prov.get(key) or []))[:2])
             for label, key in (("Rent", "rent"), ("Buy", "buy")) if prov.get(key)]
    if can_rent and lines:
        return "rent", lines
    return "gone", []


def where_to_watch(picks, platforms, tmdb, workers=6):
    """Check each pick against TMDB: does it exist, and can the group watch it?

    Drops a pick TMDB has never heard of (an invented title) and one on none of
    the group's services — unless a service allows renting and TMDB lists it
    for rent. Sets `where` to the services that carry it, which the card shows
    instead of the model's guess. If TMDB cannot be reached the pick is kept,
    unverified: a network hiccup is not the pick's fault, and an empty page
    would be worse than an unchecked card.
    """
    def check(p):
        state, where = availability(p, platforms, tmdb)
        if state == "unknown":
            return dict(p, where=[], verified=False)
        if state in ("streaming", "rent"):
            return dict(p, where=where, verified=True)
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for r in pool.map(check, picks) if r is not None]


# Kept picks are checked when the list is drawn, not when the pick was made:
# a service drops a title, or the model guessed wrong (Poker Face on Netflix,
# it is on Peacock). A day is plenty for "where do we watch this tonight", and
# the cache means a page load costs one TMDB call per pick once a day. Held in
# the process: a restart only means one more round of lookups.
FRESH_FOR = 24 * 3600
UNKNOWN_FOR = 10 * 60  # TMDB down: try again soon, but not on every page load
_seen = {}


def kept_availability(rows, platforms, tmdb, now=None, workers=6):
    """Each kept pick with `where` (services, or "Rent: ..."/"Buy: ...") and `gone` (True
    when TMDB lists it and no active service carries it). A pick TMDB cannot
    place, or cannot be reached for, gets neither: no line is better than a
    wrong one."""
    now = time.time() if now is None else now
    plats = tuple(sorted(x["name"] for x in platforms if x.get("name")))
    rent = any(x.get("can_rent") for x in platforms)

    def check(r):
        key = (title_key(r.get("name")), r.get("year"), r.get("kind"), plats, rent)
        hit = _seen.get(key)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
        state, where = availability(r, platforms, tmdb)
        _seen[key] = (now, UNKNOWN_FOR if state == "unknown" else FRESH_FOR, (state, where))
        return state, where

    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = list(pool.map(check, rows))
    return [dict(r, where=where if state in ("streaming", "rent") else [],
                 gone=state == "gone") for r, (state, where) in zip(rows, found)]


def filter_vetoes(picks, rules):
    """Applied BEFORE scoring matters, and never explained.

    A veto is not a low score: Gran not liking horror is "no horror", however
    much Devon would enjoy it. The model is told the rules too, but it is a
    model — this is the part that does not depend on it having complied.
    """
    never_genre = {r["value"].lower() for r in rules if r["kind"] == "never_genre"}
    never_kw = {r["value"].lower() for r in rules if r["kind"] == "never_keyword"}
    max_run = min([int(r["value"]) for r in rules if r["kind"] == "max_runtime"] or [0]) or None
    min_year = max([int(r["value"]) for r in rules if r["kind"] == "min_year"] or [0]) or None

    kept = []
    for p in picks:
        gen = {g.lower() for g in (p.get("genres") or [])}
        blob = ("%s %s %s" % (p.get("name", ""), p.get("overview", ""),
                              " ".join(p.get("genres") or []))).lower()
        if gen & never_genre:
            continue
        if any(k in blob for k in never_kw):
            continue
        if min_year and p.get("year") and p["year"] < min_year:
            continue
        if max_run and p.get("runtime") and p["runtime"] > max_run:
            continue
        kept.append(p)
    return kept


def recommend(ask_structured, viewer_id, audience_ids, want=6, allow_rewatch=False, tmdb=None):
    """The whole thing: gather, ask, filter, rank.

    `ask_structured` is injected so tests drive this without a network and
    without mocking the Anthropic SDK — the seam ai.py was built to have.
    """
    audience_ids = [i for i in audience_ids if i]
    if not audience_ids:
        return {"picks": [], "note": "Nobody selected."}

    known = {p["id"]: p for p in store.circle(viewer_id)}
    people = []
    for uid in audience_ids:
        who = known.get(uid)
        if not who:
            # Not in your circle: they never shared with you, so their taste is
            # not yours to count. Silently skipping would be worse than saying so.
            return {"picks": [], "note": "Somebody in that list has not shared their taste with you."}
        profile = store.taste_profile(uid)
        if uid != viewer_id:
            # Sharing taste shares ratings, not the private notes under them.
            # A note sent to the model can come back word for word in a pick's
            # `reason`, on the screen of whoever asked.
            profile = [dict(r, notes=None) for r in profile]
        people.append({
            "id": uid,
            "label": who.get("name") or who.get("email") or "Someone",
            "profile": profile,
        })

    rules = store.rules_for(audience_ids)
    seen = store.seen_by(audience_ids)
    plats = [p["name"] for p in store.platforms(active_only=True)]

    thin = [p["label"] for p in people if len(p["profile"]) < 5]

    prompt = build_prompt(people, rules, seen, plats, want + SPARE, allow_rewatch)
    got = ask_structured(prompt, SCHEMA)
    picks = filter_vetoes(got.get("picks") or [], rules)
    # Kept picks are on your list already; seen ones only when not rewatching.
    kept = [s["name"] for s in store.saved(viewer_id)]
    picks = drop_seen(picks, kept + ([] if allow_rewatch else list(seen)))
    if tmdb is not None:
        picks = where_to_watch(picks, store.platforms(active_only=True), tmdb)
    picks = rank(picks, set(audience_ids))[:want]

    note = None
    if thin:
        # Said rather than blocked. A floor score is only as good as its weakest
        # input, and hiding that the input is thin is worse than showing it.
        note = ("%s %s only a few titles rated, so their score is a guess more "
                "than a read." % (" and ".join(thin), "has" if len(thin) == 1 else "have"))
    return {"picks": picks, "note": note, "people": people}


# ── no-AI picks ─────────────────────────────────────────────────────────────
# The Lite version of Recommend. Nothing here calls a model: TMDB's own
# "recommendations" for the titles you rated highest, checked in code. Nobody
# but Devon causes a Claude call, so this is the whole feature for everyone else.

SEEDS = 5
MIN_VOTES = 50  # below this TMDB's rating of a title says little
CHECK_BATCH = 12  # providers are checked this many at a time, best first
CHECK_CAP = 36


def more_like_this(viewer_id, tmdb, want=5):
    """Picks for ONE person from TMDB's related titles for what they rated
    highest: not seen, dismissed or kept, within their vetoes, and on a service
    they have. Returns {"picks": [...], "note": ...} like recommend()."""
    seeds = store.seed_titles(viewer_id, SEEDS)
    if not seeds:
        return {"picks": [], "note": "Rate a few titles you liked (4 or 5) and Matinee can "
                                     "find more like them."}

    def fetch(seed):
        try:
            return seed, tmdb.related(seed["tmdb_id"], seed["kind"]), None
        except Exception as exc:  # noqa: BLE001
            return seed, [], exc

    with ThreadPoolExecutor(max_workers=SEEDS) as pool:
        fetched = list(pool.map(fetch, seeds))
    if all(err for _, _, err in fetched):
        return {"picks": [], "note": "Could not reach TMDB just now. Try again in a minute."}

    cand = {}
    for seed, items, _ in fetched:
        for pos, it in enumerate(items):
            if it.get("vote_count", 0) < MIN_VOTES:
                continue
            c = cand.setdefault(it["tmdb_id"], dict(it, score=0.0, because={}))
            # A title that turns up under several of your favourites, and high
            # in each list, is the better bet; your rating weights the seed.
            c["score"] += seed["rating"] / (1 + pos / 5)
            c["because"][seed["name"]] = seed["rating"]

    names = list(store.seen_by([viewer_id])) + [s["name"] for s in store.saved(viewer_id)]
    ids = store.seen_tmdb_ids(viewer_id) | {s["tmdb_id"] for s in seeds}
    ranked = [c for c in cand.values() if c["tmdb_id"] not in ids]
    ranked = drop_seen(ranked, names)
    ranked = filter_vetoes(ranked, store.rules_for([viewer_id]))
    ranked.sort(key=lambda c: (-c["score"], -c["vote_average"]))

    plats = store.platforms(active_only=True)
    picks = []
    for i in range(0, min(len(ranked), CHECK_CAP), CHECK_BATCH):
        for p in where_to_watch(ranked[i:i + CHECK_BATCH], plats, tmdb):
            liked = sorted(p["because"], key=lambda n: -p["because"][n])[:2]
            picks.append(dict(p, reason="Because you liked " + " and ".join(liked) + "."))
        if len(picks) >= want:
            break
    picks = picks[:want]
    return {"picks": picks,
            "note": None if picks else "Nothing new turned up on your services. "
                                       "Rate a few more titles and try again."}
