"""What the blend must get right. No database, no network.

These are the rules from the spec that a reasonable-looking implementation
gets subtly wrong: preferring the mean, treating a veto as a low score, and
inventing a number for somebody the model did not score.
"""
import unittest

from matinee import blend


class Ranking(unittest.TestCase):
    IDS = {"devon", "gran"}

    def rank(self, picks):
        return blend.rank(picks, self.IDS)

    def test_floor_beats_mean(self):
        """The whole idea. A 70/70 film beats a 95/30 one, even though the
        second has the higher average — nobody wants to be the person who
        hated movie night."""
        out = self.rank([
            {"name": "Loved by one", "scores": {"devon": 95, "gran": 30}},
            {"name": "Fine for both", "scores": {"devon": 70, "gran": 70}},
        ])
        self.assertEqual([p["name"] for p in out], ["Fine for both", "Loved by one"])
        self.assertEqual(out[0]["floor"], 70)
        self.assertEqual(out[1]["floor"], 30)

    def test_mean_is_only_a_tiebreak(self):
        out = self.rank([
            {"name": "Flat", "scores": {"devon": 60, "gran": 60}},
            {"name": "Higher above the floor", "scores": {"devon": 90, "gran": 60}},
        ])
        self.assertEqual(out[0]["name"], "Higher above the floor")

    def test_the_floor_names_a_person(self):
        """'weakest fit: Gran, 62' — the number that decides whether you put it
        on, and it is useless without whose it is."""
        out = self.rank([{"name": "X", "scores": {"devon": 88, "gran": 62}}])
        self.assertEqual(out[0]["floor_user"], "gran")
        self.assertEqual(out[0]["floor"], 62)

    def test_a_missing_score_drops_the_pick(self):
        """Not a zero and not a pass. Defaulting either way puts a number on the
        card that nobody produced."""
        out = self.rank([
            {"name": "Half scored", "scores": {"devon": 90}},
            {"name": "Fully scored", "scores": {"devon": 50, "gran": 50}},
        ])
        self.assertEqual([p["name"] for p in out], ["Fully scored"])

    def test_scores_for_people_not_in_the_group_are_ignored(self):
        out = self.rank([{"name": "X", "scores": {"devon": 80, "gran": 70, "sam": 10}}])
        self.assertEqual(out[0]["floor"], 70)
        self.assertNotIn("sam", out[0]["scores"])

    def test_one_person_is_the_same_feature(self):
        """'Just me' and 'me and Gran' are not two modes."""
        out = blend.rank([{"name": "X", "scores": {"devon": 77}}], {"devon"})
        self.assertEqual(out[0]["floor"], 77)
        self.assertEqual(out[0]["floor_user"], "devon")


class Vetoes(unittest.TestCase):
    def test_a_veto_removes_rather_than_lowers(self):
        """Gran not liking horror is 'no horror', however much Devon would
        enjoy it — so it cannot be expressed as a score."""
        picks = [{"name": "Hereditary", "genres": ["Horror"], "scores": {}},
                 {"name": "Paddington", "genres": ["Family"], "scores": {}}]
        kept = blend.filter_vetoes(picks, [{"kind": "never_genre", "value": "Horror"}])
        self.assertEqual([p["name"] for p in kept], ["Paddington"])

    def test_genre_veto_is_case_insensitive(self):
        kept = blend.filter_vetoes(
            [{"name": "X", "genres": ["horror"], "scores": {}}],
            [{"kind": "never_genre", "value": "Horror"}])
        self.assertEqual(kept, [])

    def test_keyword_veto_reads_the_overview_too(self):
        kept = blend.filter_vetoes(
            [{"name": "A Quiet Film", "overview": "A story about war.", "scores": {}}],
            [{"kind": "never_keyword", "value": "war"}])
        self.assertEqual(kept, [])

    def test_year_and_runtime_bounds(self):
        picks = [{"name": "Old", "year": 1950, "scores": {}},
                 {"name": "New", "year": 2020, "scores": {}}]
        kept = blend.filter_vetoes(picks, [{"kind": "min_year", "value": "1990"}])
        self.assertEqual([p["name"] for p in kept], ["New"])

    def test_the_strictest_rule_in_the_group_wins(self):
        """Rules are unioned across the group: anybody's veto removes the title.
        Two people with different limits means the tighter one applies."""
        picks = [{"name": "Long", "runtime": 180, "scores": {}},
                 {"name": "Short", "runtime": 90, "scores": {}}]
        kept = blend.filter_vetoes(picks, [{"kind": "max_runtime", "value": "150"},
                                           {"kind": "max_runtime", "value": "100"}])
        self.assertEqual([p["name"] for p in kept], ["Short"])


class Prompt(unittest.TestCase):
    def people(self):
        return [
            {"id": "devon", "label": "Devon",
             "profile": [{"name": "Knives Out", "year": 2019, "genres": ["Mystery"],
                          "rating": 5, "notes": None}]},
            {"id": "gran", "label": "Gran", "profile": []},
        ]

    def test_each_person_is_given_separately_with_their_id(self):
        """A merged profile would make the model average before we get the
        chance not to."""
        p = blend.build_prompt(self.people(), [], set(), [], 6, False)
        self.assertIn("(id: devon)", p)
        self.assertIn("(id: gran)", p)
        self.assertIn("Knives Out", p)

    def test_it_asks_for_per_person_scores_and_says_why(self):
        p = blend.build_prompt(self.people(), [], set(), [], 6, False)
        self.assertIn("SCORE EVERY PICK FOR EVERY PERSON SEPARATELY", p)
        self.assertIn("Do not average", p)

    def test_hard_rules_are_stated_as_hard(self):
        p = blend.build_prompt(self.people(), [{"kind": "never_genre", "value": "Horror"}],
                               set(), [], 6, False)
        self.assertIn("HARD RULES", p)
        self.assertIn("must not appear at all", p)

    def test_seen_titles_are_withheld_unless_rewatching(self):
        seen = {"knives out"}
        blocked = blend.build_prompt(self.people(), [], seen, [], 6, False)
        allowed = blend.build_prompt(self.people(), [], seen, [], 6, True)
        self.assertIn("do not suggest these", blocked)
        self.assertNotIn("do not suggest these", allowed)


class SeenInCode(unittest.TestCase):
    """The prompt asks; this makes sure."""

    def test_seen_dropped_whatever_the_spelling(self):
        picks = [{"name": "The Matrix"}, {"name": "Amélie"}, {"name": "Arrival"}]
        out = blend.drop_seen(picks, {"matrix", "amelie"})
        self.assertEqual([p["name"] for p in out], ["Arrival"])

    def test_sequel_is_not_the_original(self):
        out = blend.drop_seen([{"name": "Dune: Part Two"}], {"dune"})
        self.assertEqual(len(out), 1)

    def test_whole_seen_list_reaches_the_prompt(self):
        """It was cut at the first 120 alphabetically."""
        seen = {"title %03d" % i for i in range(200)} | {"zodiac"}
        text = blend.build_prompt([], [], seen, [], 6, False)
        self.assertIn("zodiac", text)


class FakeTmdb:
    def __init__(self, titles, fail=False):
        self.titles, self.fail = titles, fail

    def find_match(self, name, year, kind):
        if self.fail:
            raise OSError("tmdb down")
        return {"tmdb_id": name, "type": kind} if name in self.titles else None

    def get_watch_providers(self, tmdb_id, kind):
        return self.titles[tmdb_id]


class WhereToWatch(unittest.TestCase):
    PLATS = [{"name": "Netflix", "can_rent": False}, {"name": "Amazon Prime", "can_rent": False}]

    def test_streaming_on_our_service_kept_and_named(self):
        tm = FakeTmdb({"Arrival": {"flatrate": ["Amazon Prime Video"]}})
        out = blend.where_to_watch([{"name": "Arrival"}], self.PLATS, tm)
        self.assertEqual(out[0]["where"], ["Amazon Prime"])

    def test_invented_title_dropped(self):
        tm = FakeTmdb({})
        self.assertEqual(blend.where_to_watch([{"name": "Knives Out anthology"}], self.PLATS, tm), [])

    def test_not_on_our_services_dropped_unless_renting(self):
        tm = FakeTmdb({"Heat": {"flatrate": ["Max"], "rent": ["Apple TV"]}})
        self.assertEqual(blend.where_to_watch([{"name": "Heat"}], self.PLATS, tm), [])
        rent = self.PLATS + [{"name": "Digital Rental", "can_rent": True}]
        out = blend.where_to_watch([{"name": "Heat"}], rent, tm)
        self.assertEqual(out[0]["where"], ["Rent: Apple TV"])

    def test_tmdb_down_keeps_the_pick_unverified(self):
        out = blend.where_to_watch([{"name": "Arrival"}], self.PLATS, FakeTmdb({}, fail=True))
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["verified"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
