"""The no-AI "more like this" a Lite viewer gets. No database, no network.

    python -m unittest matinee.test_lite

Nothing here may reach a model: Devon's rule is that nobody else causes a
Claude call, and the test that matters is that a Lite POST never touches
ai.ask_structured.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("GATEWAY_TOKEN", "")  # no gateway in tests

from matinee import blend  # noqa: E402


def item(tid, name, year=2015, kind="movie", genres=None, votes=500, avg=7.5):
    return {"tmdb_id": tid, "name": name, "kind": kind, "year": year,
            "genres": genres or ["Drama"], "overview": "About %s." % name,
            "vote_average": avg, "vote_count": votes}


class FakeTmdb:
    """related[seed tmdb_id] -> items; providers[tmdb_id] -> provider dict."""

    def __init__(self, related, providers, down=False):
        self.rel, self.prov, self.down = related, providers, down
        self.finds = 0

    def related(self, tmdb_id, kind):
        if self.down:
            raise OSError("tmdb down")
        return self.rel.get(tmdb_id, [])

    def get_watch_providers(self, tmdb_id, kind):
        return self.prov.get(tmdb_id, {})

    def find_match(self, *a):  # a TMDB-sourced pick must not need a name lookup
        self.finds += 1
        raise AssertionError("find_match called")


SEEDS = [{"tmdb_id": 1, "kind": "movie", "name": "Arrival", "rating": 5},
         {"tmdb_id": 2, "kind": "movie", "name": "Sicario", "rating": 4}]
PLATS = [{"name": "Netflix", "can_rent": False}, {"name": "Amazon Prime", "can_rent": False}]
ON_NETFLIX = {"flatrate": ["Netflix"]}


class MoreLikeThis(unittest.TestCase):
    def setUp(self):
        st = mock.patch.object(blend, "store").start()
        st.seed_titles.return_value = SEEDS
        st.seen_by.return_value = set()
        st.seen_tmdb_ids.return_value = set()
        st.saved.return_value = []
        st.rules_for.return_value = []
        st.platforms.return_value = PLATS
        self.store = st
        self.addCleanup(mock.patch.stopall)

    def run_it(self, related, providers, **kw):
        tm = FakeTmdb(related, providers)
        out = blend.more_like_this("u1", tm, **kw)
        return out, tm

    def names(self, out):
        return [p["name"] for p in out["picks"]]

    def test_five_picks_on_active_services_each_with_a_reason(self):
        rel = {1: [item(10 + i, "Pick %d" % i) for i in range(8)]}
        out, tm = self.run_it(rel, {10 + i: ON_NETFLIX for i in range(8)})
        self.assertEqual(len(out["picks"]), 5)
        self.assertEqual(out["picks"][0]["where"], ["Netflix"])
        self.assertEqual(out["picks"][0]["reason"], "Because you liked Arrival.")
        self.assertEqual(tm.finds, 0)

    def test_seen_dismissed_and_kept_are_excluded(self):
        self.store.seen_by.return_value = {"seen one", "dismissed one"}
        self.store.saved.return_value = [{"name": "Kept One"}]
        self.store.seen_tmdb_ids.return_value = {13}
        rel = {1: [item(10, "Seen One"), item(11, "Dismissed One"), item(12, "Kept One"),
                   item(13, "Spelt Differently"), item(14, "Fresh")]}
        out, _ = self.run_it(rel, {i: ON_NETFLIX for i in range(10, 15)})
        self.assertEqual(self.names(out), ["Fresh"])

    def test_the_seeds_themselves_are_not_offered_back(self):
        rel = {1: [item(2, "Sicario"), item(10, "Fresh")]}
        out, _ = self.run_it(rel, {2: ON_NETFLIX, 10: ON_NETFLIX})
        self.assertEqual(self.names(out), ["Fresh"])

    def test_titles_off_every_active_service_are_dropped(self):
        rel = {1: [item(10, "On Max"), item(11, "On Netflix")]}
        out, _ = self.run_it(rel, {10: {"flatrate": ["Max"]}, 11: ON_NETFLIX})
        self.assertEqual(self.names(out), ["On Netflix"])

    def test_vetoes_apply(self):
        self.store.rules_for.return_value = [{"kind": "never_genre", "value": "Horror"}]
        rel = {1: [item(10, "Scary", genres=["Horror"]), item(11, "Calm")]}
        out, _ = self.run_it(rel, {10: ON_NETFLIX, 11: ON_NETFLIX})
        self.assertEqual(self.names(out), ["Calm"])

    def test_a_title_under_two_favourites_outranks_one_under_a_single(self):
        rel = {1: [item(10, "Single"), item(11, "Both")], 2: [item(11, "Both")]}
        out, _ = self.run_it(rel, {10: ON_NETFLIX, 11: ON_NETFLIX})
        self.assertEqual(self.names(out)[0], "Both")
        self.assertEqual(out["picks"][0]["reason"], "Because you liked Arrival and Sicario.")

    def test_barely_rated_titles_are_skipped(self):
        rel = {1: [item(10, "Obscure", votes=3), item(11, "Known")]}
        out, _ = self.run_it(rel, {10: ON_NETFLIX, 11: ON_NETFLIX})
        self.assertEqual(self.names(out), ["Known"])

    def test_later_batches_are_checked_when_the_first_is_all_off_service(self):
        n = blend.CHECK_BATCH + 3
        rel = {1: [item(100 + i, "T%02d" % i) for i in range(n)]}
        prov = {100 + i: (ON_NETFLIX if i >= blend.CHECK_BATCH else {}) for i in range(n)}
        out, _ = self.run_it(rel, prov)
        self.assertEqual(len(out["picks"]), 3)

    def test_nothing_rated_says_what_to_do_and_asks_tmdb_nothing(self):
        self.store.seed_titles.return_value = []
        out, tm = self.run_it({}, {})
        self.assertEqual(out["picks"], [])
        self.assertIn("Rate a few", out["note"])

    def test_tmdb_down_is_a_sentence_not_a_crash(self):
        out = blend.more_like_this("u1", FakeTmdb({}, {}, down=True))
        self.assertEqual(out["picks"], [])
        self.assertIn("TMDB", out["note"])

    def test_nothing_survives_says_so(self):
        out, _ = self.run_it({1: [item(10, "On Max")]}, {10: {"flatrate": ["Max"]}})
        self.assertEqual(out["picks"], [])
        self.assertIn("Nothing new", out["note"])


class LiteRoute(unittest.TestCase):
    """A Lite POST /recommend: picks come back and the model is never asked."""

    def post(self, variant):
        from fastapi.testclient import TestClient
        from matinee import app
        st = mock.patch.object(app, "store").start()
        st.person_by_email.return_value = {"id": "u1", "email": "d@x", "name": "D"}
        st.circle.return_value = [{"id": "u1", "name": "D", "email": "d@x",
                                   "is_me": True, "reviews": 6}]
        st.saved.return_value = []
        st.parties.return_value = []
        st.platforms.return_value = PLATS
        bl = mock.patch.object(app.blend, "store", st).start()
        st.seed_titles.return_value = SEEDS
        st.seen_by.return_value = set()
        st.seen_tmdb_ids.return_value = set()
        st.rules_for.return_value = []
        tm = FakeTmdb({1: [item(10 + i, "Pick %d" % i) for i in range(6)]},
                      {10 + i: ON_NETFLIX for i in range(6)})
        mock.patch.object(app, "tmdb", tm).start()
        ask = mock.patch.object(app.ai, "ask_structured",
                                side_effect=AssertionError("model called")).start()
        self.addCleanup(mock.patch.stopall)
        c = TestClient(app.app)
        headers = {"x-platform-user": "d@x",
                   "origin": "http://testserver"}
        if variant is not None:
            headers["x-platform-variant"] = variant
        r = c.post("/recommend", headers=headers, data={"who": ["u1"]})
        return r, ask

    def test_lite_gets_five_picks_without_a_model_call(self):
        r, ask = self.post("lite")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text.count('class="card"'), 5)
        self.assertIn("On Netflix", r.text)
        self.assertIn("Because you liked Arrival", r.text)
        self.assertNotIn("weakest fit", r.text)
        ask.assert_not_called()

    def test_a_blank_tier_is_lite_too(self):
        r, ask = self.post(None)
        self.assertEqual(r.text.count('class="card"'), 5)
        ask.assert_not_called()

    def test_lite_page_has_no_group_controls(self):
        r, _ = self.post("lite")
        self.assertNotIn('name="rewatch"', r.text)
        self.assertNotIn('type="checkbox" name="who"', r.text)


class BlendKeepsOtherPeoplesNotesPrivate(unittest.TestCase):
    """Sharing taste shares ratings. Another person's private review notes
    must never reach the model, where they could be quoted back in a pick's
    reason on the asker's screen. The asker's own notes still help."""

    def test_only_the_askers_notes_reach_the_prompt(self):
        self.addCleanup(mock.patch.stopall)
        st = mock.patch.object(blend, "store").start()
        st.circle.return_value = [{"id": "me", "name": "Me"}, {"id": "pal", "name": "Pal"}]
        st.taste_profile.side_effect = lambda uid: [{
            "name": "Arrival", "year": 2016, "kind": "movie", "genres": ["Drama"],
            "rating": 5, "notes": "MY-NOTE" if uid == "me" else "PALS-PRIVATE-NOTE"}]
        st.rules_for.return_value = []
        st.seen_by.return_value = set()
        st.platforms.return_value = []
        st.saved.return_value = []
        prompts = []

        def ask(prompt, schema):
            prompts.append(prompt)
            return {"picks": []}
        blend.recommend(ask, "me", ["me", "pal"])
        self.assertIn("MY-NOTE", prompts[0])
        self.assertNotIn("PALS-PRIVATE-NOTE", prompts[0])


class FullRouteNamesTheCaller(unittest.TestCase):
    """The broker runs Matinee in split mode: it refuses a call with no `user`,
    and puts only Devon (proved by the assertion) on his Max plan. So a Full
    POST must hand both gateway headers to the broker, unchanged."""

    def test_user_and_assertion_reach_the_broker(self):
        from fastapi.testclient import TestClient
        from matinee import app
        self.addCleanup(mock.patch.stopall)
        st = mock.patch.object(app, "store").start()
        st.person_by_email.return_value = {"id": "u2", "email": "friend@x", "name": "F"}
        st.circle.return_value = []
        st.saved.return_value = []
        st.parties.return_value = []

        def fake_recommend(ask, *a, **k):
            ask("prompt", {"type": "object"})
            return {"picks": [], "note": ""}
        mock.patch.object(app.blend, "recommend", side_effect=fake_recommend).start()
        post = mock.patch.object(app.ai.requests, "post").start()
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"data": {"picks": []}, "text": ""}
        TestClient(app.app).post("/recommend", data={"who": ["u2"]}, headers={
            "x-platform-user": "friend@x", "x-platform-variant": "full",
            "x-platform-assertion": "signed-token", "origin": "http://testserver"})
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["user"], "friend@x")
        self.assertEqual(sent["assertion"], "signed-token")
        self.assertEqual(sent["app_id"], "matinee")


if __name__ == "__main__":
    unittest.main(verbosity=1)
