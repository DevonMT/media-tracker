"""A NUL byte in any input is a 400, not a 500. No database, no network.

    python -m unittest matinee.test_nonul

Postgres cannot store U+0000, so POST /title/{id}/review with
notes=before%00after raised "A string literal cannot contain NUL (0x00)
characters" inside the handler (devondoes queue #612, finding L-13).
"""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("GATEWAY_TOKEN", "")  # no gateway in tests

from fastapi.testclient import TestClient  # noqa: E402

from matinee import app as appmod, nonul  # noqa: E402

CSV = b"Profile Name,Start Time,Duration,Title\nA,2026-01-01 10:00:00,1:00:00,Maid\n"


class RefuseNul(unittest.TestCase):
    def setUp(self):
        self.store = mock.patch.object(appmod, "store").start()
        self.store.person_by_email.return_value = {"id": "u1", "email": "d@x", "name": "Devon"}
        self.addCleanup(mock.patch.stopall)
        # raise_server_exceptions=False: a handler that blows up shows as the 500 it is.
        self.client = TestClient(appmod.app, raise_server_exceptions=False)
        self.h = {"x-platform-user": "d@x", "host": "testserver"}

    def post(self, path="/title/t1/review", **kw):
        kw["headers"] = {**self.h, **kw.get("headers", {})}
        return self.client.post(path, follow_redirects=False, **kw)

    def refused(self, r):
        self.assertEqual(r.status_code, 400, r.text[:200])
        self.assertIn("NUL", r.text)
        self.store.set_review.assert_not_called()

    def test_the_repro_notes_with_a_percent_encoded_nul(self):
        # Sent as the raw string, exactly as the finding wrote it.
        r = self.post(content=b"notes=before%00after",
                      headers={"content-type": "application/x-www-form-urlencoded"})
        self.refused(r)

    def test_a_form_field_with_a_nul(self):
        self.refused(self.post(data={"notes": "before\x00after"}))

    def test_a_nul_in_a_field_name(self):
        self.refused(self.post(data={"no\x00tes": "x"}))

    def test_the_query_string(self):
        self.refused(self.post(path="/title/t1/review?x=a%00b", data={"notes": "ok"}))
        r = self.client.get("/add?q=a%00b", headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_the_path(self):
        r = self.client.get("/title/a%00b", headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_json_whatever_the_content_type_says(self):
        body = json.dumps({"notes": "before\x00after"}).encode()       # \u0000 escape
        for ct in ("application/json", "text/plain", "application/x-www-form-urlencoded",
                   "application/octet-stream", ""):
            with self.subTest(content_type=ct):
                self.refused(self.post(content=body, headers={"content-type": ct}))

    def test_a_json_escape_inside_a_form_field(self):
        # /import/confirm carries each row as a JSON string in a form field.
        r = self.post("/import/confirm", data={"pick": json.dumps({"tmdb_id": 1, "name": "a\x00b"})})
        self.assertEqual(r.status_code, 400)
        self.store.upsert_title.assert_not_called()

    def test_json_nested_past_the_depth_limit(self):
        n = nonul.MAX_DEPTH + 5
        deep = ("[" * n + "]" * n).encode()
        self.refused(self.post(content=deep, headers={"content-type": "application/json"}))
        absurd = ("[" * 200000 + "]" * 200000).encode()                 # the parser itself gives up
        self.refused(self.post(content=absurd, headers={"content-type": "text/plain"}))

    def test_json_within_the_limit_passes_the_layer(self):
        r = self.post(content=b'{"a": [[1]]}', headers={"content-type": "application/json"})
        self.assertNotEqual(r.status_code, 400)

    def test_a_nul_in_a_multipart_text_field(self):
        r = self.post("/import", data={"profile": "a\x00b"},
                      files={"file": ("v.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 400)

    def test_a_nul_in_an_uploaded_file_is_the_importers_business(self):
        with mock.patch.object(appmod.netflix.tmdb, "find_match", return_value=None):
            r = self.post("/import", files={"file": ("v.csv", "x,y\n1,2\n".encode("utf-16"), "text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Title", r.text)                                  # the importer's sentence

    def test_ordinary_input_still_gets_through(self):
        # Percent signs, a lone backslash, an escaped backslash before "u0000".
        r = self.post(data={"rating": "4", "notes": "100% good, a back\\slash, \\\\u0000 is text\n\ttab"})
        self.assertEqual(r.status_code, 303)
        self.store.set_review.assert_called_once()

    def test_a_percent_sign_followed_by_00_typed_as_text_is_not_a_nul(self):
        r = self.post(data={"notes": "%00"})                            # the characters %, 0, 0
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.store.set_review.call_args.args[-1], "%00")

    def test_a_refused_request_that_skipped_the_gateway_is_still_a_403(self):
        with mock.patch.object(appmod, "GATEWAY_TOKEN", "secret"):
            r = self.post(data={"notes": "before\x00after"})
        self.assertEqual(r.status_code, 403)

    def test_oversize_body_is_413_not_buffered_forever(self):
        r = self.post(content=b"a=" + b"x" * (nonul.MAX_BODY + 1),
                      headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 413)


if __name__ == "__main__":
    unittest.main()
