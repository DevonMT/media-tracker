import os
import anthropic
from dotenv import load_dotenv
from db import get_conn

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

    liked = [dict(r) for r in media if r["liked"]]
    disliked = [dict(r) for r in media if not r["liked"]]
    plats = [r["name"] for r in platforms]
    can_rent = any(r["can_rent"] for r in platforms)
    budget = float(rent_budget["value"]) if rent_budget else 5.0
    sensitivities = sens_row["value"].strip() if sens_row else ""

    return {
        "liked": liked,
        "disliked": disliked,
        "platforms": plats,
        "can_rent": can_rent,
        "rent_budget": budget,
        "sensitivities": sensitivities,
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


def get_recommendations(ctx: dict, n: int = 5) -> list[dict]:
    disliked_block = (
        "\n".join(f"- {r['title']}" for r in ctx["disliked"]) if ctx["disliked"] else "None"
    )
    platform_block = ", ".join(ctx["platforms"]) if ctx["platforms"] else "No platforms active"
    rent_note = (
        f"Digital rentals are acceptable up to ${ctx['rent_budget']:.2f} per title."
        if ctx["can_rent"]
        else "Streaming only — do not recommend rentals or purchases."
    )
    sensitivity_block = ""
    if ctx["sensitivities"]:
        sensitivity_block = f"""
Content sensitivities (do NOT exclude for these — include the title but set sensitivity_flag=true and write a one-sentence sensitivity_note):
{ctx['sensitivities']}
"""

    prompt = f"""You are a movie and TV recommendation assistant.

Based on the following viewing history (higher ratings = stronger signal):

{_liked_block(ctx['liked'])}

Did NOT enjoy:
{disliked_block}

STRICT PLATFORM RULE: Only recommend titles available on one of these platforms: {platform_block}. {rent_note} Do not recommend anything on a platform not in that list. If uncertain whether a title is on a listed platform, skip it.

Recommendations may come from any era — classic films, cult favorites, and older shows are equally valid as recent releases. Do not bias toward titles from the last few years.
{sensitivity_block}
Return exactly {n} recommendations. For confidence: 90-100 = near-certain they'll love it, 70-89 = strong match, 50-69 = decent match, below 50 = uncertain."""

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=3000,
        tools=[REC_TOOL],
        tool_choice={"type": "tool", "name": "submit_recommendations"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_recommendations":
            return block.input["recommendations"]
    return []


def rescore_saved(saved: list[dict], ctx: dict) -> list[dict]:
    titles_block = "\n".join(f"- {r['title']} ({r['type']}, {r['year'] or '?'})" for r in saved)
    prompt = f"""You are re-scoring a saved watchlist based on updated viewing history.

Current taste profile (higher ratings = stronger signal):
{_liked_block(ctx['liked'])}

Re-score each of the following titles with a confidence (0-100) that the viewer will enjoy it, based purely on taste match. Ignore platform availability — that's already confirmed.

Titles to score:
{titles_block}"""

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1500,
        tools=[RESCORE_TOOL],
        tool_choice={"type": "tool", "name": "submit_scores"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_scores":
            return block.input["scores"]
    return []
