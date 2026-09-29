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
                    # One number per person, keyed by the id we sent. Asking for
                    # a single score would make the model do the averaging, and
                    # the whole point is that we do not average.
                    "scores": {"type": "object"},
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
        "SCORE EVERY PICK FOR EVERY PERSON SEPARATELY, 0-100, in `scores`, keyed "
        "by the id given above. Do not average them and do not return a single "
        "score: a film the whole group likes at 70 is better than one two people "
        "love at 95 and one person endures at 30, and only per-person numbers can "
        "express that.\n"
        "Be honest about the weak fit rather than flattering the group — an "
        "inflated low score is what puts on a film somebody hates.\n"
        "`reason` should say why it suits the group in one sentence, naming the "
        "tension if there is one."
        % (want, who, blocks, veto_text, avail, seen_text))


def rank(picks, ids):
    """Order by the weakest link, then the average.

    Anything the model failed to score for somebody in the group is dropped
    rather than defaulted: a missing score is not a zero and not a pass, and
    guessing either way would put a number on the card that nobody produced.
    """
    out = []
    for p in picks:
        scores = {k: v for k, v in (p.get("scores") or {}).items()
                  if k in ids and isinstance(v, (int, float))}
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


def where_to_watch(picks, platforms, tmdb, workers=6):
    """Check each pick against TMDB: does it exist, and can the group watch it?

    Drops a pick TMDB has never heard of (an invented title) and one on none of
    the group's services — unless a service allows renting and TMDB lists it
    for rent. Sets `where` to the services that carry it, which the card shows
    instead of the model's guess. If TMDB cannot be reached the pick is kept,
    unverified: a network hiccup is not the pick's fault, and an empty page
    would be worse than an unchecked card.
    """
    names = {_norm_provider(p["name"]): p["name"] for p in platforms}
    can_rent = any(p.get("can_rent") for p in platforms)

    def check(p):
        kind = "show" if p.get("kind") == "show" else "movie"
        try:
            match = tmdb.find_match(p.get("name"), p.get("year"), kind)
            if not match:
                return None
            prov = tmdb.get_watch_providers(match["tmdb_id"], match.get("type") or kind)
        except Exception:  # noqa: BLE001 — unverified, kept
            return dict(p, where=[], verified=False)
        streams = (prov.get("flatrate") or []) + (prov.get("free") or []) + (prov.get("ads") or [])
        where = sorted({names[k] for pn in map(_norm_provider, streams)
                        for k in names if k and (k in pn or pn in k)})
        if where:
            return dict(p, where=where, verified=True)
        rentable = sorted(set((prov.get("rent") or []) + (prov.get("buy") or [])))
        if can_rent and rentable:
            return dict(p, where=["Rent: " + ", ".join(rentable[:2])], verified=True)
        return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for r in pool.map(check, picks) if r is not None]


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
        people.append({
            "id": uid,
            "label": who.get("name") or who.get("email") or "Someone",
            "profile": store.taste_profile(uid),
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
