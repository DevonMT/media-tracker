"""Who may change what everyone shares. No database, no network.

    python -m unittest matinee.test_access
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("GATEWAY_TOKEN", "test")  # quiet the boot warning

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from matinee import app  # noqa: E402


def req(groups=None, user="d@x"):
    headers = [(b"x-platform-user", user.encode())]
    if groups is not None:
        headers.append((b"x-platform-groups", groups.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


class AdminOnly(unittest.TestCase):
    def setUp(self):
        self.store = mock.patch.object(app, "store").start()
        self.store.person_by_email.return_value = {"id": "u1", "email": "d@x"}
        self.addCleanup(mock.patch.stopall)

    def test_an_admin_may(self):
        self.assertEqual(app.admin_only(req("friends,admin"))["id"], "u1")

    def test_a_signed_in_friend_may_not(self):
        with self.assertRaises(HTTPException) as e:
            app.admin_only(req("friends"))
        self.assertEqual(e.exception.status_code, 403)

    def test_no_groups_header_is_not_admin(self):
        with self.assertRaises(HTTPException):
            app.admin_only(req(None))

    def test_a_group_merely_containing_the_word_is_not_admin(self):
        self.assertFalse(app.is_admin(req("administrators-of-nothing,notadmin")))


class Rating(unittest.TestCase):
    def test_out_of_range_is_nothing_not_a_500(self):
        self.assertEqual([app._rating(v) for v in ("7", "0", "abc", "", "3")], [None, None, None, None, 3])


if __name__ == "__main__":
    unittest.main()
