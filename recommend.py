import os
import re
import ai
from dotenv import load_dotenv
from db import get_conn
import tmdb as tmdb_client

load_dotenv()

STAR_MAP = {1: "1/5", 2: "2/5", 3: "3/5", 4: "4/5", 5: "5/5"}

REC_TOOL = {
    "name": "submit_recommendations",
    "description": "Submit structured movie/show recommendations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":            {"type": "string"},
                        "year":             {"type": "integer"},
                        "type":             {"type": "string", "enum": ["movie", "show"]},
                        "platform":         {"type": "string", "description": "Where to watch, including rental cost if applicable"},
                        "overview":         {"type": "string", "description": "2-3 sentence plot summary"},
                        "cast":             {"type": "array", "items": {"type": "string"}, "description": "3-4 notable cast members"},
                        "vibe_match":       {"type": "string", "description": "Short tag e.g. mystery-comedy, feel-good adventure"},
                        "reason":           {"type": "string", "description": "2 sentences on why it fits their taste"},
                        "confidence":       {"type": "integer", "minimum": 0, "maximum": 100, "description": "0-100 confidence they will enjoy it"},
                        "sensitivity_flag": {"type": "boolean"},
                        "sensitivity_note": {"type": "string", "description": "One sentence if sensitivity_flag is true, else empty string"},
                    },
                    "required": ["title", "year", "type", "platform", "overview", "cast",
                                 "vibe_match", "reason", "confidence", "sensitivity_flag", "sensitivity_note"],
                },
            }
        },
        "required": ["recommendations"],
    },
}

RESCORE_TOOL = {
    "name": "submit_scores",
    "description": "Re-score a list of titles by taste match confidence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":      {"type": "string"},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                        "reason":     {"type": "string", "description": "One sentence on why this score changed or stayed the same"},
                    },
                    "required": ["title", "confidence", "reason"],
                },
            }
        },
        "required": ["scores"],
    },
}


def build_context() -> dict:
    with get_conn() as conn:
        media = conn.execute(
            "SELECT title, type, genre, year, liked, rating, notes FROM media "
            "ORDER BY liked DESC, rating DESC NULLS LAST, date_added DESC"
        ).fetchall()
        platforms = conn.execute("SELECT name, can_rent FROM platforms WHERE active = 1").fetchall()
        rent_budget = conn.execute("SELECT value FROM settings WHERE key = 'rent_budget'").fetchone()
        sens_row = conn.execute("SELECT value FROM settings WHERE key = 'sensitivities'").fetchone()
        exclusions = conn.execute(
            "SELECT title, year, reason FROM exclusions ORDER BY date_added DESC"
        ).fetchall()
        watchlist = conn.execute(
            "SELECT title FROM saved_recommendations WHERE status = 'pending'"
        ).fetchall()

    liked = [dict(r) for r in media if r["liked"]]
    disliked = [dict(r) for r in media if not r["liked"]]
    plats = [r["name"] for r in platforms]
    can_rent = any(r["can_rent"] for r in platforms)
    budget = float(rent_budget["value"]) if rent_budget else 5.0
    sensitivities = sens_row["value"].strip() if sens_row else ""

    # Titles to never re-surface: already in the library or on the watchlist.
    avoid_titles = sorted({r["title"] for r in media} | {r["title"] for r in watchlist})

    return {
        "liked": liked,
        "disliked": disliked,
        "platforms": plats,
        "can_rent": can_rent,
        "rent_budget": budget,
        "sensitivities": sensitivities,
        "exclusions": [dict(r) for r in exclusions],
        "avoid_titles": avoid_titles,
    }


def _liked_block(liked: list[dict]) -> str:
    lines = []
    for r in liked:
        rating_part = f", rated {STAR_MAP[r['rating']]}" if r.get("rating") else ""
        notes_part = f" — {r['notes']}" if r.get("notes") else ""
        lines.append(
            f"- {r['title']} ({r['type']}, {r['genre'] or 'unknown genre'}, {r['year'] or '?'}{rating_part}){notes_part}"
        )
    return "\n".join(lines)


def _norm_title(t: str) -> str:
    """Normalize a title for set-membership comparison against the DB."""
    return " ".join((t or "").split()).strip().casefold()


def _norm_provider(s: str) -> str:
    """Normalize a platform/provider name so 'Disney+' == 'Disney Plus', etc."""
    return re.sub(r"[^a-z0-9]", "", (s or "").replace("+", " plus ").lower())


def filter_recommendations(recs: list[dict], ctx: dict, media_type: str = "both") -> list[dict]:
    """Drop anything that must never appear in the suggestion list: the wrong
    media type, or a title already excluded / in the library / on the watchlist.

    This is a hard, code-level guard — it does NOT trust the model to have honored
    the same constraints in the prompt."""
    blocked = {_norm_title(t) for t in ctx.get("avoid_titles", [])}
    blocked |= {_norm_title(e["title"]) for e in ctx.get("exclusions", [])}

    out = []
    for r in recs:
        rtype = (r.get("type") or "").lower()
        if media_type in ("movie", "show") and rtype != media_type:
            continue
        if _norm_title(r.get("title")) in blocked:
            continue
        out.append(r)
    return out


def platform_check(rec: dict, ctx: dict) -> tuple[bool, list[str] | None, bool]:
    """Verify a suggestion is real and streamable on an active platform via TMDB.

    Returns (available, matched_providers, verified):
      - verified=False  -> TMDB couldn't be reached; caller should keep the rec
                           rather than punish it for a network hiccup.
      - available=False -> title doesn't exist on TMDB (e.g. a hallucination) or
                           isn't on any active platform; caller should drop it.
    """
    mtype = (rec.get("type") or "movie").lower()
    try:
        match = tmdb_client.find_match(rec.get("title"), rec.get("year"), mtype)
    except Exception:
        return True, None, False  # network/API issue — don't drop on our account
    if not match:
        return False, None, True  # not a real title (or invented extra words)

    try:
        prov = tmdb_client.get_watch_providers(match["tmdb_id"], mtype)
    except Exception:
        return True, None, False

    flatrate = list(prov.get("flatrate", [])) + list(prov.get("free", [])) + list(prov.get("ads", []))
    rental = list(prov.get("rent", [])) + list(prov.get("buy", []))

    active_norm = {_norm_provider(a) for a in ctx.get("platforms", [])}
    matched = []
    for p in flatrate:
        pn = _norm_provider(p)
        if any(a and (a in pn or pn in a) for a in active_norm):
            matched.append(p)

    stream_ok = bool(matched)
    rent_ok = bool(ctx.get("can_rent")) and bool(rental)
    if not stream_ok and rent_ok:
        matched = sorted(set(rental))[:3]  # surface where it can be rented
    return (stream_ok or rent_ok), matched, True


def get_recommendations(
    ctx: dict, n: int = 5, media_type: str = "both", verify_platforms: bool = True
) -> list[dict]:
    disliked_block = (
        "\n".join(f"- {r['title']}" for r in ctx["disliked"]) if ctx["disliked"] else "None"
    )
    platform_block = ", ".join(ctx["platforms"]) if ctx["platforms"] else "No platforms active"
    rent_note = (
        f"Digital rentals are acceptable up to ${ctx['rent_budget']:.2f} per title."
        if ctx["can_rent"]
        else "Streaming only — do not recommend rentals or purchases."
    )

    if media_type == "movie":
        type_block = "ONLY recommend movies. Do NOT recommend TV shows."
    elif media_type == "show":
        type_block = "ONLY recommend TV shows. Do NOT recommend movies."
    else:
        type_block = "You may recommend either movies or TV shows."

    sensitivity_block = ""
    if ctx["sensitivities"]:
        sensitivity_block = f"""
Content sensitivities (do NOT exclude for these — include the title but set sensitivity_flag=true and write a one-sentence sensitivity_note):
{ctx['sensitivities']}
"""

    exclusion_block = ""
    if ctx.get("exclusions"):
        lines = []
        for e in ctx["exclusions"]:
            yr = f" ({e['year']})" if e.get("year") else ""
            why = f" — reason: {e['reason']}" if e.get("reason") else ""
            lines.append(f"- {e['title']}{yr}{why}")
        exclusion_block = f"""
HARD EXCLUSIONS — the viewer explicitly rejected these. NEVER recommend them, and avoid closely similar titles for the stated reason (e.g. same sub-genre, tone, or franchise the reason points to):
{chr(10).join(lines)}
"""

    avoid_block = ""
    if ctx.get("avoid_titles"):
        avoid_block = f"""
Already in their library or on their watchlist — do NOT recommend any of these again:
{', '.join(ctx['avoid_titles'])}
"""

    # Over-generate: the hard filters below will remove some, so ask for a buffer
    # and trim back to exactly `n` afterward.
    ask_n = min(n + 6, 15)

    prompt = f"""You are a movie and TV recommendation assistant.

Based on the following viewing history (higher ratings = stronger signal):

{_liked_block(ctx['liked'])}

Did NOT enjoy:
{disliked_block}

MEDIA TYPE: {type_block}

STRICT PLATFORM RULE: Only recommend titles available on one of these platforms: {platform_block}. {rent_note} Do not recommend anything on a platform not in that list. If uncertain whether a title is on a listed platform, skip it.

ONLY recommend real titles that actually exist. Do NOT invent titles, box sets, "anthology" collections, or bundles.

Recommendations may come from any era — classic films, cult favorites, and older shows are equally valid as recent releases. Do not bias toward titles from the last few years.
{avoid_block}{exclusion_block}{sensitivity_block}
Return {ask_n} recommendations. For confidence: 90-100 = near-certain they'll love it, 70-89 = strong match, 50-69 = decent match, below 50 = uncertain."""

    try:
        # Each rec is verbose (overview + cast + reason), so the over-generated
        # ask_n needs headroom — too small a budget truncates the reply, which
        # comes back missing the "recommendations" key entirely.
        out = ai.ask_structured(prompt, REC_TOOL["input_schema"],
                                max_tokens=min(8000, 1024 + ask_n * 400))
    except ai.BrokerError as exc:
        # No recommendations is a worse evening, not a broken app.
        print(f"[recommend] broker unavailable: {exc}")
        return []
    # Still defensive: a truncated reply can be a valid object with the key
    # missing. Degrade to no results rather than raising KeyError.
    raw = out.get("recommendations") or []

    # Hard filter #1: type / excluded / library / watchlist.
    recs = filter_recommendations(raw, ctx, media_type)

    # Hard filter #2: verify each survivor really exists and is on an active
    # platform (also removes hallucinations). Skip only if there are no active
    # platforms to check against. Verify lazily so we stop once we have `n`.
    if not (verify_platforms and ctx.get("platforms")):
        return recs[:n]

    final = []
    for r in recs:
        available, providers, _verified = platform_check(r, ctx)
        if not available:
            continue
        if providers:
            r["platform"] = ", ".join(providers)
        final.append(r)
        if len(final) >= n:
            break
    return final


def rescore_saved(saved: list[dict], ctx: dict) -> list[dict]:
    titles_block = "\n".join(f"- {r['title']} ({r['type']}, {r['year'] or '?'})" for r in saved)
    prompt = f"""You are re-scoring a saved watchlist based on updated viewing history.

Current taste profile (higher ratings = stronger signal):
{_liked_block(ctx['liked'])}

Re-score each of the following titles with a confidence (0-100) that the viewer will enjoy it, based purely on taste match. Ignore platform availability — that's already confirmed.

Titles to score:
{titles_block}"""

    try:
        out = ai.ask_structured(prompt, RESCORE_TOOL["input_schema"], max_tokens=1500)
    except ai.BrokerError as exc:
        # Keep the existing scores rather than blanking the watchlist.
        print(f"[rescore] broker unavailable: {exc}")
        return []
    return out.get("scores") or []
