"""Fix what the old shape let in: duplicates, and one title TMDB never matched.

    python -m matinee.repair            # dry run
    python -m matinee.repair --write    # do it

Three problems, and only two of them are duplicates. Lumping them together is
how the third gets destroyed:

  1. "Pirates of the Caribbean: Curse of the Black Pearl" is NOT a duplicate.
     There is no second row for that film -- TMDB simply missed it, because the
     stored name drops the "The". It needs a MATCH, not a merge, and merging it
     into another Pirates film would lose it.

  2. "Only Murders in the Building: Season 2" is the same show as "Only Murders
     in the Building". A season is not a separate title: `title` is one row per
     film or show, ever. Both rated 5, so nothing is in conflict.

  3. "Knives Out: Wake Up Dead" (2024) and "Wake Up Dead Man: A Knives Out
     Mystery" (2025) are one film entered twice under a wrong name and year.
     THE RATINGS DISAGREE -- 5 against 4 -- and the plain merge deliberately
     refuses to overwrite an existing verdict, so it would silently keep the 4.
     The 5 was entered later (old id 16 against 14), so by the same rule the
     migration used -- the latest entry is the current opinion -- the 5 wins,
     and it is set explicitly rather than left to the merge.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from matinee import store  # noqa: E402
import tmdb  # noqa: E402

# (local title name, the name TMDB knows it by, year) -- matched, not merged.
MATCH = [
    ("Pirates of the Caribbean: Curse of the Black Pearl",
     "Pirates of the Caribbean: The Curse of the Black Pearl", 2003, "movie"),
]

# (duplicate to fold away, the title it should have been, rating to keep or None)
MERGE = [
    ("Only Murders in the Building: Season 2", "Only Murders in the Building", None),
    ("Knives Out: Wake Up Dead", "Wake Up Dead Man: A Knives Out Mystery", 5),
]


def by_name(name):
    return store.q("SELECT * FROM title WHERE lower(name)=lower(%s)", (name,), one=True)


def main():
    write = "--write" in sys.argv
    did = 0

    for stored, real, year, kind in MATCH:
        t = by_name(stored)
        if not t:
            print("  already matched or gone: %s" % stored)
            continue
        if t["tmdb_id"]:
            print("  already has a tmdb id: %s" % stored)
            continue
        hit = tmdb.find_match(real, year, kind)
        if not hit:
            print("  STILL no TMDB match for %r -- left as a local title" % real)
            continue
        print("  MATCH  %-46s -> tmdb %s (%s)" % (stored[:44], hit["tmdb_id"], hit["title"]))
        did += 1
        if write:
            store.x(
                "UPDATE title SET tmdb_id=%s, name=%s, year=COALESCE(%s, year),"
                " runtime=COALESCE(%s, runtime), overview=COALESCE(%s, overview),"
                " poster=COALESCE(%s, poster) WHERE id=%s",
                (hit["tmdb_id"], hit["title"], hit.get("year"), hit.get("runtime"),
                 hit.get("overview"), hit.get("poster"), t["id"]))

    for dup_name, keep_name, keep_rating in MERGE:
        dup, keep = by_name(dup_name), by_name(keep_name)
        if not dup:
            print("  already merged or gone: %s" % dup_name)
            continue
        if not keep:
            print("  NO SURVIVOR for %r -- left alone rather than guessed at" % dup_name)
            continue
        print("  MERGE  %-46s -> %s" % (dup_name[:44], keep_name))
        if keep_rating is not None:
            print("         keeping rating %s: it was entered later, so it is the"
                  " current opinion" % keep_rating)
        did += 1
        if write:
            if keep_rating is not None:
                # Set BEFORE the merge. merge_titles refuses to overwrite an
                # existing verdict, which is right in general and wrong here:
                # this is one person's two entries for one film, not two people
                # disagreeing.
                for r in store.q("SELECT user_id FROM review WHERE title_id=%s", (dup["id"],)):
                    store.x("UPDATE review SET rating=%s, updated_at=now()"
                            " WHERE user_id=%s AND title_id=%s",
                            (keep_rating, r["user_id"], keep["id"]))
            store.merge_titles(keep["id"], dup["id"])

    print("")
    if not did:
        print("  nothing to do -- already repaired.")
    elif write:
        print("  %d change(s) written." % did)
    else:
        print("  %d change(s) pending. Re-run with --write." % did)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
