"""Which title a watched pick becomes. No database, no network.

    python -m unittest matinee.test_watched

The review is only as useful as the row it lands on: a twin of a film already
in the library splits its opinions in two, and a TMDB id attached on a guess
puts somebody's verdict on a different film.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("GATEWAY_TOKEN", "test")  # quiet the boot warning

from matinee import app  # noqa: E402


def pick(name="Up", year=2009, kind="movie", overview="A house, balloons."):
    return {"name": name, "year": year, "kind": kind, "overview": overview,
            "title_id": None}


def hit(title="Up", year=2009, kind="movie", tmdb_id=14160, genre="Animation/Family"):
    return {"tmdb_id": tmdb_id, "title": title, "type": kind, "year": year,
            "genre": genre, "overview": "From TMDB."}


class TitleFor(unittest.TestCase):
    def setUp(self):
        self.store = mock.patch.object(app, "store").start()
        self.find = mock.patch.object(app.tmdb, "find_match").start()
        self.store.title_by_name.return_value = None
        self.store.upsert_title.return_value = "new-id"
        self.addCleanup(mock.patch.stopall)

    def upserted(self):
        return self.store.upsert_title.call_args.args

    def test_an_existing_title_gets_the_review_not_a_twin(self):
        self.store.title_by_name.return_value = "lib-id"
        self.assertEqual(app._title_for(pick()), "lib-id")
        self.find.assert_not_called()
        self.store.upsert_title.assert_not_called()

    def test_tmdb_match_with_the_same_year_is_attached(self):
        self.find.return_value = hit()
        app._title_for(pick())
        tmdb_id, kind, name, year, genres = self.upserted()[:5]
        self.assertEqual((tmdb_id, kind, name, year), (14160, "movie", "Up", 2009))
        self.assertEqual(genres, ["Animation", "Family"])

    def test_a_different_year_is_a_different_film(self):
        """find_match takes "Up" to be "Upgrade" (2018) if nothing better turns
        up. The year is what says no."""
        self.find.return_value = hit(title="Upgrade", year=2018, tmdb_id=500664)
        app._title_for(pick())
        self.assertIsNone(self.upserted()[0])
        self.assertEqual(self.upserted()[2], "Up")

    def test_with_no_year_only_the_exact_name_is_trusted(self):
        self.find.return_value = hit(title="Upgrade", year=2018)
        app._title_for(pick(year=None))
        self.assertIsNone(self.upserted()[0])

        self.find.return_value = hit(title="Up!", year=2009)
        app._title_for(pick(year=None))
        self.assertEqual(self.upserted()[0], 14160)

    def test_tmdb_down_still_keeps_the_review(self):
        self.find.side_effect = ConnectionError("no route")
        self.assertEqual(app._title_for(pick()), "new-id")
        self.assertEqual(self.upserted()[:4], (None, "movie", "Up", 2009))

    def test_old_app_tv_kind_is_a_show(self):
        self.find.return_value = None
        app._title_for(pick(name="Shrinking", year=2023, kind="tv"))
        self.assertEqual(self.upserted()[1], "show")


class Rating(unittest.TestCase):
    def test_only_one_to_five(self):
        self.assertEqual([app._rating(v) for v in ("1", "5", "0", "6", "9", "", None, "x")],
                         [1, 5, None, None, None, None, None, None])


class ActOnAResult(unittest.TestCase):
    """A result card answers in place: the picks exist only in the page that
    drew them, so a redirect would throw the other five away. Without the
    page's script, a plain form post still gets somewhere sensible."""

    CARD = {"name": "Paddington 2", "year": "2017", "kind": "movie", "overview": "A bear."}
    FETCH = {"X-Requested-With": "fetch"}

    def setUp(self):
        from fastapi.testclient import TestClient
        self.store = mock.patch.object(app, "store").start()
        mock.patch.object(app, "me", return_value={"id": "devon"}).start()
        mock.patch.object(app, "_title_for", return_value="tid").start()
        self.addCleanup(mock.patch.stopall)
        self.store.saved.return_value = [{"id": "s1", "name": "Paddington 2",
                                          "year": 2017, "floor_score": 81}]
        self.client = TestClient(app.app, headers={"X-Gateway-Token": "test"})

    def post(self, url, data, **headers):
        return self.client.post(url, data=data, headers=headers, follow_redirects=False)

    def test_seen_it_saves_your_review_and_answers_in_place(self):
        r = self.post("/pick/seen", {**self.CARD, "rating": "2", "notes": " Too twee. "},
                      **self.FETCH)
        self.assertEqual(r.status_code, 204)
        self.store.set_review.assert_called_once_with("devon", "tid", 2, False, "Too twee.")

    def test_without_the_script_it_redirects_to_the_saved_note(self):
        r = self.post("/pick/seen", {**self.CARD, "rating": "5"})
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/?watched=tid"))

    def test_not_for_me_dismisses_it_for_you(self):
        r = self.post("/pick/dismiss", self.CARD, **self.FETCH)
        self.assertEqual(r.status_code, 204)
        self.store.dismiss.assert_called_once_with("devon", "tid", "recommendation")

    def test_keep_in_place_sends_back_the_kept_list(self):
        r = self.post("/keep", {**self.CARD, "floor": "81", "floor_user": "devon"}, **self.FETCH)
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="kept"', r.text)
        self.assertIn("Paddington 2", r.text)

    def test_another_site_cannot_act_on_your_behalf(self):
        r = self.post("/pick/dismiss", self.CARD, Origin="https://evil.example", **self.FETCH)
        self.assertEqual(r.status_code, 403)
        self.store.dismiss.assert_not_called()


if __name__ == "__main__":
    unittest.main()
