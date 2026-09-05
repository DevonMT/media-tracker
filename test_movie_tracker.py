"""Tests for the movie tracker's recommendation filtering + UI.

Runs against an ISOLATED temp SQLite database (never the real Postgres
warehouse) and stubs all TMDB/Anthropic network calls, so it's fully offline and
side-effect free. No pytest required:

    cd "C:/Users/Devon/Claude Projects/movie-tracker"
    python -m unittest test_movie_tracker -v

Key trick: a SQLite backend is registered as sys.modules['db'] BEFORE the app is
imported, so `from db import get_conn, init_db` resolves to it instead of the
psycopg2/warehouse module.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Install the SQLite fake db BEFORE importing anything that does `from db import`.
import _sqlite_db  # noqa: E402
sys.modules["db"] = _sqlite_db

from streamlit.testing.v1 import AppTest  # noqa: E402
import tmdb  # noqa: E402  real module; network fns stubbed per-test
import recommend  # noqa: E402  real; binds the fake db.get_conn

DASHBOARD = os.path.join(HERE, "dashboard.py")


# ─────────────────────────── shared DB helpers ────────────────────────────────
class _DBTestCase(unittest.TestCase):
    """Base: fresh temp SQLite DB + default seed for each test."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="movietest_")
        os.close(fd)
        _sqlite_db.set_path(self.db_path)
        _sqlite_db.init_db()

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    # -- convenience writers -------------------------------------------------
    def add_media(self, title, mtype="movie", liked=1, rating=None, year=None):
        with _sqlite_db.get_conn() as c:
            c.execute(
                "INSERT INTO media (title, type, liked, rating, year) VALUES (?,?,?,?,?)",
                (title, mtype, liked, rating, year),
            )

    def add_exclusion(self, title, year=None, reason=None):
        with _sqlite_db.get_conn() as c:
            c.execute(
                "INSERT INTO exclusions (title, year, reason) VALUES (?,?,?)",
                (title, year, reason),
            )

    def add_watchlist(self, title, mtype="movie", year=None):
        with _sqlite_db.get_conn() as c:
            c.execute(
                "INSERT INTO saved_recommendations (title, type, year, status) "
                "VALUES (?,?,?, 'pending')",
                (title, mtype, year),
            )

    def set_active_platforms(self, names):
        """Activate exactly `names`; deactivate everything else."""
        with _sqlite_db.get_conn() as c:
            c.execute("UPDATE platforms SET active = 0")
            for n in names:
                c.execute("UPDATE platforms SET active = 1 WHERE name = ?", (n,))


# ─────────────────────── pure-logic: filter_recommendations ───────────────────
class TestFilterRecommendations(unittest.TestCase):
    CTX = {
        "avoid_titles": ["Ted Lasso", "Pirates of the Caribbean"],  # library ∪ watchlist
        "exclusions": [{"title": "Saw"}, {"title": "Red"}],
    }

    def _titles(self, recs, media_type="both"):
        return [r["title"] for r in recommend.filter_recommendations(recs, self.CTX, media_type)]

    def test_drops_excluded(self):
        recs = [{"title": "Red", "type": "movie"}, {"title": "Arrival", "type": "movie"}]
        self.assertEqual(self._titles(recs), ["Arrival"])

    def test_drops_library_and_watchlist_case_insensitive(self):
        recs = [{"title": "ted lasso", "type": "show"}, {"title": "Arrival", "type": "movie"}]
        self.assertEqual(self._titles(recs), ["Arrival"])

    def test_media_type_movie_only(self):
        recs = [{"title": "Arrival", "type": "movie"}, {"title": "Some Show", "type": "show"}]
        self.assertEqual(self._titles(recs, "movie"), ["Arrival"])

    def test_media_type_show_only(self):
        recs = [{"title": "Arrival", "type": "movie"}, {"title": "Some Show", "type": "show"}]
        self.assertEqual(self._titles(recs, "show"), ["Some Show"])

    def test_both_keeps_everything_valid(self):
        recs = [{"title": "Arrival", "type": "movie"}, {"title": "Some Show", "type": "show"}]
        self.assertEqual(self._titles(recs, "both"), ["Arrival", "Some Show"])


# ─────────────────────── pure-logic: platform_check (TMDB) ────────────────────
class TestPlatformCheck(unittest.TestCase):
    def _run(self, rec, ctx, match, providers):
        with mock.patch.object(recommend.tmdb_client, "find_match", return_value=match), \
             mock.patch.object(recommend.tmdb_client, "get_watch_providers", return_value=providers):
            return recommend.platform_check(rec, ctx)

    def test_hallucination_dropped(self):
        # find_match returns None -> title doesn't exist on TMDB -> drop.
        with mock.patch.object(recommend.tmdb_client, "find_match", return_value=None):
            available, providers, verified = recommend.platform_check(
                {"title": "Knives Out anthology", "type": "movie"},
                {"platforms": ["Netflix"], "can_rent": False},
            )
        self.assertFalse(available)
        self.assertTrue(verified)

    def test_streaming_match(self):
        available, providers, verified = self._run(
            {"title": "Arrival", "type": "movie"},
            {"platforms": ["Netflix"], "can_rent": False},
            {"tmdb_id": 1}, {"flatrate": ["Netflix"]},
        )
        self.assertTrue(available)
        self.assertIn("Netflix", providers)

    def test_provider_name_normalization(self):
        # Active "Disney+" must match TMDB's "Disney Plus".
        available, providers, _ = self._run(
            {"title": "Encanto", "type": "movie"},
            {"platforms": ["Disney+"], "can_rent": False},
            {"tmdb_id": 2}, {"flatrate": ["Disney Plus"]},
        )
        self.assertTrue(available)

    def test_off_platform_dropped(self):
        # On Hulu only; active platform is Netflix, no rentals -> drop.
        available, providers, _ = self._run(
            {"title": "Hulu Only Film", "type": "movie"},
            {"platforms": ["Netflix"], "can_rent": False},
            {"tmdb_id": 3}, {"flatrate": ["Hulu"]},
        )
        self.assertFalse(available)

    def test_rental_available(self):
        # Not streaming anywhere active, but rentable and rentals are allowed.
        available, providers, _ = self._run(
            {"title": "Rentable Film", "type": "movie"},
            {"platforms": ["Netflix", "Digital Rental"], "can_rent": True},
            {"tmdb_id": 4}, {"rent": ["Apple TV"], "buy": ["Amazon Video"]},
        )
        self.assertTrue(available)
        self.assertTrue(providers)  # surfaces where to rent

    def test_network_error_keeps_rec(self):
        # TMDB raising -> fail open (don't punish a rec for a network hiccup).
        with mock.patch.object(recommend.tmdb_client, "find_match", side_effect=RuntimeError("boom")):
            available, providers, verified = recommend.platform_check(
                {"title": "Arrival", "type": "movie"},
                {"platforms": ["Netflix"], "can_rent": False},
            )
        self.assertTrue(available)
        self.assertFalse(verified)


# ───────────── end-to-end: get_recommendations pipeline (LLM stubbed) ─────────
class _FakeBlock:
    type = "tool_use"
    name = "submit_recommendations"

    def __init__(self, recs):
        self.input = {"recommendations": recs}


def _fake_find_match(title, year, mtype):
    key = (title or "").strip().lower()
    if "anthology" in key:  # hallucinated bundle
        return None
    ids = {"arrival": 1, "hulu only film": 2, "rentable film": 3}
    return {"tmdb_id": ids.get(key, 99), "title": title, "type": mtype, "year": year}


def _fake_providers(tmdb_id, mtype, region="US"):
    return {
        1: {"flatrate": ["Netflix"]},
        2: {"flatrate": ["Hulu"]},
        3: {"rent": ["Apple TV"], "buy": ["Amazon Video"]},
    }.get(tmdb_id, {})


class TestGetRecommendationsPipeline(_DBTestCase):
    def test_full_filter_and_verify(self):
        self.add_media("Ted Lasso", mtype="show", liked=1)   # library
        self.add_exclusion("Saw")                            # excluded
        self.set_active_platforms(["Netflix", "Digital Rental"])  # can_rent via Digital Rental

        canned = [
            {"title": "Arrival", "type": "movie", "year": 2016},           # keep (Netflix)
            {"title": "Saw", "type": "movie", "year": 2004},               # drop (excluded)
            {"title": "Ted Lasso", "type": "show", "year": 2020},          # drop (library + wrong type)
            {"title": "Knives Out anthology", "type": "movie", "year": 0}, # drop (hallucination)
            {"title": "Hulu Only Film", "type": "movie", "year": 2021},    # drop (off-platform)
            {"title": "Rentable Film", "type": "movie", "year": 2019},     # keep (rental)
        ]
        # normalize canned recs to include all required-ish keys the code reads
        for r in canned:
            r.setdefault("platform", "guessed")

        ctx = recommend.build_context()
        # The app's only AI seam is ai.ask_structured, so that is what a test
        # should stand in for. Mocking the Anthropic SDK would now be mocking
        # something this app does not import.
        with mock.patch.object(recommend.ai, "ask_structured",
                               return_value={"recommendations": canned}), \
             mock.patch.object(recommend.tmdb_client, "find_match", side_effect=_fake_find_match), \
             mock.patch.object(recommend.tmdb_client, "get_watch_providers", side_effect=_fake_providers):
            out = recommend.get_recommendations(ctx, n=2, media_type="movie")

        self.assertEqual([r["title"] for r in out], ["Arrival", "Rentable Film"])
        # platform field is overwritten with the VERIFIED provider(s), not the guess.
        arrival = next(r for r in out if r["title"] == "Arrival")
        self.assertIn("Netflix", arrival["platform"])
        self.assertNotEqual(arrival["platform"], "guessed")

    def test_truncated_reply_returns_empty_not_crash(self):
        # A max_tokens-truncated reply is still a valid object, just missing the
        # "recommendations" key. Must degrade to [] rather than KeyError.
        ctx = recommend.build_context()
        with mock.patch.object(recommend.ai, "ask_structured", return_value={}):
            out = recommend.get_recommendations(ctx, n=3, media_type="both")
        self.assertEqual(out, [])

    def test_broker_unavailable_degrades_instead_of_crashing(self):
        # The broker refuses for reasons nobody can fix mid-session: budget
        # spent, subscription not permitted, mini rebooting. None of those
        # should take the app down -- an empty list is a worse evening, not a
        # broken app.
        ctx = recommend.build_context()
        with mock.patch.object(recommend.ai, "ask_structured",
                               side_effect=recommend.ai.BrokerError("budget_exceeded")):
            out = recommend.get_recommendations(ctx, n=3, media_type="both")
        self.assertEqual(out, [])

    def test_rescore_survives_a_broker_failure(self):
        # Returning [] here means "no new scores", which leaves the saved
        # watchlist untouched. Raising would blank the page.
        with mock.patch.object(recommend.ai, "ask_structured",
                               side_effect=recommend.ai.BrokerError("down")):
            self.assertEqual(recommend.rescore_saved([], {"liked": []}), [])


# ──────────────────────────── UI tests via AppTest ────────────────────────────
class TestDashboardUI(_DBTestCase):
    def setUp(self):
        super().setUp()
        # Keep every TMDB network call out of the UI runs.
        for fn, ret in [("search", []), ("get_cast_by_title", []),
                        ("get_watch_providers", {}), ("find_match", None)]:
            p = mock.patch.object(tmdb, fn, return_value=ret)
            p.start()
            self.addCleanup(p.stop)

    def test_app_loads_without_error(self):
        at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
        # at.exception is an (empty) ElementList when nothing was raised.
        self.assertEqual(len(at.exception), 0, list(at.exception))

    def test_config_panel_present(self):
        at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
        show_me = [r for r in at.radio if r.label == "Show me"]
        self.assertTrue(show_me, "expected a 'Show me' media-type radio")
        self.assertEqual(show_me[0].options, ["Movies & shows", "Movies only", "Shows only"])
        self.assertTrue([b for b in at.button if b.label == "Generate"],
                        "expected a Generate button")

    def test_generate_live_filters_excluded(self):
        self.add_exclusion("Saw")

        def fake_get_recs(ctx, n, media_type="both", **_kw):
            # Deliberately return an excluded title; the UI live-guard must drop it.
            return [
                {"title": "Saw", "type": "movie", "year": 2004, "confidence": 80,
                 "platform": "Netflix", "overview": "", "reason": "", "vibe_match": "",
                 "cast": [], "sensitivity_flag": False, "sensitivity_note": ""},
                {"title": "Arrival", "type": "movie", "year": 2016, "confidence": 90,
                 "platform": "Netflix", "overview": "", "reason": "", "vibe_match": "",
                 "cast": [], "sensitivity_flag": False, "sensitivity_note": ""},
            ]

        with mock.patch.object(recommend, "get_recommendations", side_effect=fake_get_recs):
            at = AppTest.from_file(DASHBOARD, default_timeout=30).run()
            gen = next(b for b in at.button if b.label == "Generate")
            at = gen.click().run()

        # Rec cards render the title as an "### {title} ({year})" heading. Match on
        # that so we don't get a false positive from the Excluded tab, which also
        # lists "Saw" (in bold) as a legitimately excluded title.
        rendered = "\n".join(m.value for m in at.markdown)
        self.assertIn("### Arrival", rendered)
        self.assertNotIn("### Saw", rendered)


# ───────────────────── opt-in LIVE TMDB smoke test ────────────────────────────
# These hit the real TMDB API, so they're skipped by default. Enable with:
#     RUN_LIVE_TMDB=1 python -m unittest test_movie_tracker -v   (bash)
#     $env:RUN_LIVE_TMDB=1; python -m unittest test_movie_tracker -v   (PowerShell)
# Requires TMDB_API_TOKEN (tmdb.py already loads it from .env on import).
_LIVE_ON = bool(os.environ.get("RUN_LIVE_TMDB")) and bool(os.getenv("TMDB_API_TOKEN"))


@unittest.skipUnless(_LIVE_ON, "set RUN_LIVE_TMDB=1 (and TMDB_API_TOKEN) to run live TMDB tests")
class TestTMDBLive(unittest.TestCase):
    """Confirms the real TMDB responses still parse and the search fix holds.
    Complements the stubbed tests, which can't catch an upstream API change."""

    def test_search_surfaces_red_2010(self):
        # The whole reason for filtering people BEFORE the result cap: the 2010
        # film "Red" (Bruce Willis / Helen Mirren) must be reachable in search.
        results = tmdb.search("Red")
        self.assertTrue(results, "TMDB returned no results for 'Red'")
        hit = next(
            (r for r in results
             if r["type"] == "movie" and r["year"] == 2010 and r["title"].strip().lower() == "red"),
            None,
        )
        self.assertIsNotNone(hit, f"'Red' (2010) not found in: {[(r['title'], r['year']) for r in results]}")

    def test_find_match_real_title(self):
        match = tmdb.find_match("Red", 2010, "movie")
        self.assertIsNotNone(match)
        self.assertIn("tmdb_id", match)
        self.assertEqual(match["year"], 2010)

    def test_find_match_rejects_hallucination(self):
        # A live multi-search for this may surface the real "Knives Out"; the
        # find_match guard must still reject it (extra invented word).
        self.assertIsNone(tmdb.find_match("Knives Out anthology", None, "movie"))

    def test_watch_providers_shape(self):
        match = tmdb.find_match("Red", 2010, "movie")
        self.assertIsNotNone(match)
        prov = tmdb.get_watch_providers(match["tmdb_id"], "movie")
        self.assertIsInstance(prov, dict)
        # Every expected key is present and maps to a list of provider-name strings.
        for key in ("flatrate", "rent", "buy", "free", "ads"):
            self.assertIn(key, prov)
            self.assertIsInstance(prov[key], list)
            self.assertTrue(all(isinstance(name, str) for name in prov[key]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
