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

from . import store

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
        sample = sorted(seen)[:120]
        seen_text = ("\nAlready seen or dismissed by someone in the group — do not "
                     "suggest these:\n  " + ", ".join(sample))

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


def recommend(ask_structured, viewer_id, audience_ids, want=6, allow_rewatch=False):
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

    prompt = build_prompt(people, rules, seen, plats, want, allow_rewatch)
    got = ask_structured(prompt, SCHEMA)
    picks = rank(filter_vetoes(got.get("picks") or [], rules), set(audience_ids))

    note = None
    if thin:
        # Said rather than blocked. A floor score is only as good as its weakest
        # input, and hiding that the input is thin is worse than showing it.
        note = ("%s %s only a few titles rated, so their score is a guess more "
                "than a read." % (" and ".join(thin), "has" if len(thin) == 1 else "have"))
    return {"picks": picks, "note": note, "people": people}
