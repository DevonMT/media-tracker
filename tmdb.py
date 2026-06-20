import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.themoviedb.org/3"

MOVIE_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Sci-Fi", 53: "Thriller", 10752: "War", 37: "Western",
}
TV_GENRES = {
    10759: "Action/Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 10762: "Kids",
    9648: "Mystery", 10765: "Sci-Fi/Fantasy", 10768: "War/Politics", 37: "Western",
}


def _headers() -> dict:
    token = os.getenv("TMDB_API_TOKEN")
    return {"Authorization": f"Bearer {token}", "accept": "application/json"}


def _parse_item(item: dict) -> dict | None:
    media_type = item.get("media_type")
    if media_type not in ("movie", "tv"):
        return None
    genre_map = MOVIE_GENRES if media_type == "movie" else TV_GENRES
    genre_ids = item.get("genre_ids", [])
    genres = "/".join(genre_map[gid] for gid in genre_ids[:2] if gid in genre_map)
    title = item.get("title") or item.get("name", "Unknown")
    date = item.get("release_date") or item.get("first_air_date", "")
    year = int(date[:4]) if date and len(date) >= 4 else None
    overview = item.get("overview", "")
    return {
        "tmdb_id": item["id"],
        "title": title,
        "type": "movie" if media_type == "movie" else "show",
        "genre": genres or None,
        "year": year,
        "overview": overview[:180] + ("…" if len(overview) > 180 else ""),
    }


def search(query: str) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/search/multi",
        headers=_headers(),
        params={"query": query, "include_adult": False, "language": "en-US", "page": 1},
        timeout=8,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("results", [])[:7]:
        parsed = _parse_item(item)
        if parsed:
            results.append(parsed)
    return results


def get_cast_by_title(title: str, year: int | None, media_type: str) -> list[str]:
    hits = search(title)
    match = None
    for r in hits:
        if r["type"] == media_type:
            if year and r["year"] == year:
                match = r
                break
            if match is None:
                match = r
    if not match:
        return []
    endpoint = "movie" if media_type == "movie" else "tv"
    resp = requests.get(
        f"{BASE_URL}/{endpoint}/{match['tmdb_id']}/credits",
        headers=_headers(),
        timeout=8,
    )
    if resp.status_code != 200:
        return []
    return [m["name"] for m in resp.json().get("cast", [])[:4]]


def fetch_details(tmdb_id: int, media_type: str) -> dict | None:
    endpoint = "movie" if media_type == "movie" else "tv"
    resp = requests.get(
        f"{BASE_URL}/{endpoint}/{tmdb_id}",
        headers=_headers(),
        params={"language": "en-US"},
        timeout=8,
    )
    if resp.status_code != 200:
        return None
    item = resp.json()
    if media_type == "movie":
        genres = "/".join(g["name"] for g in item.get("genres", [])[:2])
        date = item.get("release_date", "")
    else:
        genres = "/".join(g["name"] for g in item.get("genres", [])[:2])
        date = item.get("first_air_date", "")
    year = int(date[:4]) if date and len(date) >= 4 else None
    title = item.get("title") or item.get("name", "Unknown")
    overview = item.get("overview", "")
    return {
        "tmdb_id": tmdb_id,
        "title": title,
        "type": media_type,
        "genre": genres or None,
        "year": year,
        "overview": overview[:180] + ("…" if len(overview) > 180 else ""),
    }
