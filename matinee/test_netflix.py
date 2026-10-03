"""Netflix import: parsing, strict matching, and the review-before-write flow.
No database, no network.

    python -m unittest matinee.test_netflix

The fixture is shaped like a real ViewingActivity.csv: one row per play,
episodes as "Show: Season N: Episode", trailers marked in Supplemental Video
Type, and a second profile in the same file.
"""
import os
import re
import unittest
from unittest import mock

os.environ.setdefault("GATEWAY_TOKEN", "")  # no gateway in tests

from fastapi.testclient import TestClient  # noqa: E402

from matinee import app as appmod, netflix  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "netflix_viewing_activity.csv")

CATALOG = {
    "the gray man": {"tmdb_id": 725201, "title": "The Gray Man", "type": "movie", "year": 2022, "genre": "Action/Thriller", "overview": "x"},
    "stranger things": {"tmdb_id": 66732, "title": "Stranger Things", "type": "show", "year": 2016, "genre": "Sci-Fi/Fantasy", "overview": "y"},
    "maid": {"tmdb_id": 97951, "title": "Maid", "type": "show", "year": 2021, "genre": "Drama", "overview": "z"},
    "dune part two": {"tmdb_id": 693134, "title": "Dune: Part Two", "type": "movie", "year": 2024, "genre": "Sci-Fi/Adventure", "overview": "d"},
    "bluey": {"tmdb_id": 82728, "title": "Bluey", "type": "show", "year": 2018, "genre": "Kids", "overview": "b"},
    # What find_match really does: "Up" is taken to be "Upgrade".
    "up": {"tmdb_id": 500664, "title": "Upgrade", "type": "movie", "year": 2018, "genre": "Action", "overview": "u"},
    "call the midwife": {"tmdb_id": 42009, "title": "Call the Midwife", "type": "show", "year": 2012, "genre": "Drama", "overview": "m"},
}


def fake_find_match(title, year, media_type):
    return CATALOG.get(netflix.tmdb._norm_for_match(title))


def read():
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


class Classify(unittest.TestCase):
    def test_a_season_marker_says_show_and_drops_the_episode(self):
        self.assertEqual(netflix.classify("Stranger Things: Season 4: Chapter One: The Hellfire Club"),
                         [("Stranger Things", "show")])
        self.assertEqual(netflix.classify("Maid: Limited Series: Start"), [("Maid", "show")])

    def test_part_two_at_the_end_is_a_film(self):
        """A film can be "Dune: Part Two"; only an episode name after the
        marker makes it a show."""
        self.assertEqual(netflix.classify("Dune: Part Two")[0], ("Dune: Part Two", "movie"))
        self.assertEqual(netflix.classify("Money Heist: Part 1: Episode 1"), [("Money Heist", "show")])

    def test_a_bare_subtitle_is_offered_both_ways_whole_title_first(self):
        self.assertEqual(netflix.classify("Bluey: Camping"),
                         [("Bluey: Camping", "movie"), ("Bluey", "show")])
        self.assertEqual(netflix.classify("Up"), [("Up", None)])


class Parse(unittest.TestCase):
    def test_trailers_hooks_and_flashes_are_not_watches(self):
        titles, _ = netflix.parse(read())
        self.assertNotIn("Some Trailer Name", titles)       # TRAILER row
        self.assertEqual(titles["The Gray Man"]["seconds"], 114 * 60 + 20)  # the 12s HOOK is not added
        self.assertNotIn("Some Trailer Name", titles)

    def test_episodes_fold_into_one_show(self):
        titles, _ = netflix.parse(read())
        st = titles["Stranger Things"]
        self.assertEqual(st["seconds"], 44 * 60 + 10 + 51 * 60 + 2)
        self.assertEqual(st["last"], "2026-09-29")

    def test_profiles_are_counted_and_can_be_filtered(self):
        titles, profiles = netflix.parse(read())
        self.assertEqual(set(profiles), {"Devon", "Gran"})
        self.assertIn("Call the Midwife", titles)
        only, _ = netflix.parse(read(), profile="devon")
        self.assertNotIn("Call the Midwife", only)

    def test_a_bom_is_tolerated_and_a_wrong_file_is_refused(self):
        titles, _ = netflix.parse("﻿" + read())
        self.assertIn("The Gray Man", titles)
        with self.assertRaises(ValueError):
            netflix.parse("Date,Amount\n2026-01-01,5\n")


class Match(unittest.TestCase):
    def setUp(self):
        mock.patch.object(netflix.tmdb, "find_match", side_effect=fake_find_match).start()
        self.addCleanup(mock.patch.stopall)

    def run_match(self):
        return netflix.match(netflix.parse(read())[0])

    def test_matched_titles_are_deduped_per_tmdb_id(self):
        matched, _, _ = self.run_match()
        names = [m["name"] for m in matched]
        self.assertEqual(names.count("Bluey"), 1)            # two episodes, one show
        for want in ("The Gray Man", "Stranger Things", "Maid", "Dune: Part Two", "Bluey"):
            self.assertIn(want, names)

    def test_a_loose_tmdb_hit_is_not_trusted(self):
        """"Up" came back as "Upgrade". Marking Upgrade as seen would hide a
        film nobody watched, so it is listed for review instead."""
        matched, unmatched, _ = self.run_match()
        self.assertNotIn("Upgrade", [m["name"] for m in matched])
        self.assertIn("Zzyzx Road Obscure Thing", [u["name"] for u in unmatched])

    def test_under_five_minutes_is_not_a_watch_matched_or_not(self):
        # "Up" is 60 seconds over two plays and "Barely Watched Film" is 3
        # minutes: previews that ran on, listed nowhere.
        matched, unmatched, _ = self.run_match()
        listed = [m["name"] for m in matched] + [u["name"] for u in unmatched]
        self.assertNotIn("Up", listed)
        self.assertNotIn("Barely Watched Film", listed)

    def test_tmdb_down_means_unmatched_with_a_reason_not_a_crash(self):
        with mock.patch.object(netflix.tmdb, "find_match", side_effect=ConnectionError("down")):
            matched, unmatched, _ = self.run_match()
        self.assertEqual(matched, [])
        self.assertTrue(unmatched and all(u["reason"] == "TMDB unavailable" for u in unmatched))

    def test_seconds_must_clear_the_bar_after_folding(self):
        # Two 4-minute episodes of a seasonless show are 8 minutes: a watch.
        text = ("Profile Name,Start Time,Duration,Title,Supplemental Video Type\n"
                "A,2026-01-01 10:00:00,0:04:00,Bluey: Camping,\n"
                "A,2026-01-02 10:00:00,0:04:00,Bluey: Hotel,\n")
        matched, _, _ = netflix.match(netflix.parse(text)[0])
        self.assertEqual([m["name"] for m in matched], ["Bluey"])


class Flow(unittest.TestCase):
    """Preview writes nothing; confirm files only what stayed ticked."""

    def setUp(self):
        self.store = mock.patch.object(appmod, "store").start()
        self.store.person_by_email.return_value = {"id": "u1", "email": "d@x", "name": "Devon"}
        self.store.reviewed_tmdb_ids.return_value = {66732}   # already own Stranger Things
        self.store.title_by_name.return_value = None
        self.store.upsert_title.side_effect = lambda *a, **k: "t-%s" % (a[0] or a[2])
        mock.patch.object(netflix.tmdb, "find_match", side_effect=fake_find_match).start()
        self.addCleanup(mock.patch.stopall)
        self.client = TestClient(appmod.app)
        self.h = {"x-platform-user": "d@x", "host": "testserver"}

    def upload(self, **data):
        return self.client.post("/import", headers=self.h, data=data,
                                files={"file": ("ViewingActivity.csv", read().encode(), "text/csv")})

    def test_preview_lists_both_groups_and_writes_nothing(self):
        r = self.upload()
        self.assertEqual(r.status_code, 200)
        self.assertIn("The Gray Man", r.text)
        self.assertIn("Zzyzx Road Obscure Thing", r.text)
        self.assertIn("already in your library", r.text)      # Stranger Things, left alone
        self.assertNotIn("Upgrade", r.text)
        self.store.upsert_title.assert_not_called()
        self.store.ensure_review.assert_not_called()

    def test_a_non_netflix_file_gets_a_sentence(self):
        r = self.client.post("/import", headers=self.h, data={},
                             files={"file": ("x.csv", b"Date,Amount\n1,2\n", "text/csv")})
        self.assertIn("no Title column", r.text)

    def test_confirm_files_the_ticked_as_unrated_seen(self):
        pre = self.upload().text
        picks = re.findall(r'name="pick" value="(\d+)"', pre)
        self.assertGreaterEqual(len(picks), 4)
        form = {"pick": picks}
        for m in re.finditer(r'name="((?:tmdb_id|name|kind|year|genres|overview|last)_\d+)" value="([^"]*)"', pre):
            form[m.group(1)] = m.group(2).replace("&#39;", "'").replace("&amp;", "&")
        r = self.client.post("/import/confirm", headers=self.h, data=form)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.ensure_review.call_count, len(picks))
        # Unrated: only ensure_review, never set_review, so no rating is invented
        # and an existing one is never overwritten.
        self.store.set_review.assert_not_called()
        gray = [c for c in self.store.upsert_title.call_args_list if c.args[0] == 725201][0]
        self.assertEqual(gray.args[1:4], ("movie", "The Gray Man", 2022))
        dated = [c for c in self.store.ensure_review.call_args_list if c.args[1] == "t-725201"][0]
        self.assertEqual(str(dated.args[2]), "2026-09-30")     # the last watched day

    def test_unmatched_are_filed_only_when_ticked(self):
        r = self.client.post("/import/confirm", headers=self.h, data={})
        self.assertIn("Filed 0 as seen", r.text)
        self.store.ensure_review.assert_not_called()
        r = self.client.post("/import/confirm", headers=self.h,
                             data={"local": "0", "lname_0": "Zzyzx Road Obscure Thing", "lkind_0": "movie"})
        self.assertIn("without a TMDB match", r.text)
        self.store.upsert_title.assert_called_once_with(None, "movie", "Zzyzx Road Obscure Thing", None)
        self.store.ensure_review.assert_called_once_with("u1", "t-Zzyzx Road Obscure Thing")

    def test_a_forged_origin_is_refused(self):
        r = self.client.post("/import/confirm", headers={**self.h, "origin": "https://evil.example"}, data={})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
