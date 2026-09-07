"""Carry the old library into the new shape.

Reads warehouse.movie.* and writes platform.*. NOTHING IS DELETED: the old
tables stay exactly as they are, so this can be run, inspected, and run again.
Idempotent by natural key -- tmdb id where there is one, name+year where there
is not -- so a second run updates rather than duplicating.

    python -m matinee.migrate            # dry run: says what it would do
    python -m matinee.migrate --write    # do it

THE ONE JUDGEMENT CALL, stated here because it is not recoverable later:
every old row says who='both'. Those ratings are joint verdicts, not one
person's. They are written as the owner's reviews because his is the only
account -- but marked source='joint', because flattening them to 'own' would
invent a precision the data never had, and the mark is what lets a second
person claim them later instead of starting from nothing.
"""
import os
import sys
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor, Json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tmdb  # noqa: E402


def connect(dbname):
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "postgres"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=dbname,
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


def owner_id(cur, email):
    cur.execute('SELECT id FROM "user" WHERE lower(email)=lower(%s)', (email,))
    row = cur.fetchone()
    if not row:
        sys.exit("no account for %s; the reviews need somebody to belong to" % email)
    return row[0]


def genres_of(genre):
    """'Mystery/Drama' was one text field. It was always a list."""
    if not genre:
        return []
    return [g.strip() for g in genre.replace(",", "/").split("/") if g.strip()]


def resolve(name, year, kind):
    """TMDB match, or None.

    Never guesses: an unmatched title is carried as a local one rather than
    attached to something that merely sounds like it, because a wrong tmdb_id
    silently merges two different films and there is no undoing that once
    reviews hang off it.
    """
    try:
        return tmdb.find_match(name, year, kind)
    except Exception as exc:  # no token, rate limit, network
        print("    ! tmdb lookup failed for %r: %s" % (name, exc))
        return None


def main():
    write = "--write" in sys.argv
    me = os.environ.get("MATINEE_OWNER", "devon.troedel@gmail.com")

    old = connect(os.environ.get("PGDATABASE", "warehouse"))
    new = connect("platform")
    oc = old.cursor(cursor_factory=RealDictCursor)
    nc = new.cursor()

    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as fh:
        nc.execute(fh.read())

    user = owner_id(nc, me)
    print("  reviews will belong to %s" % me)

    oc.execute("SELECT * FROM movie.media ORDER BY id")
    media = oc.fetchall()
    print("  %d titles to carry" % len(media))

    # Several old rows can be the same title: there was no identity, so
    # re-rating something APPENDED instead of updating. Knives Out is in there
    # three times at 2, 4 and 5. The new shape holds one opinion per person per
    # title, so the collision has to be resolved, and the principled answer is
    # that the LATEST entry is the current opinion -- somebody changing their
    # mind, recorded the only way the old app allowed. Every collision is
    # reported, because a silent merge is how you lose a rating and never know.
    seen = {}
    ratings_for = {}

    matched = 0
    unmatched = 0
    for m in media:
        hit = resolve(m["title"], m["year"], m["type"])
        tmdb_id = hit.get("tmdb_id") if hit else None
        if tmdb_id:
            matched += 1
            nc.execute("SELECT id FROM title WHERE tmdb_id=%s", (tmdb_id,))
        else:
            unmatched += 1
            nc.execute(
                "SELECT id FROM title WHERE tmdb_id IS NULL AND lower(name)=lower(%s)"
                " AND year IS NOT DISTINCT FROM %s", (m["title"], m["year"]))
        found = nc.fetchone()
        key = tmdb_id if tmdb_id else ("local", m["title"].lower(), m["year"])
        tid = seen.get(key) or (found[0] if found else str(uuid.uuid4()))
        seen[key] = tid
        ratings_for.setdefault(key, []).append((m["title"], m["rating"]))

        if write:
            nc.execute(
                "INSERT INTO title (id,tmdb_id,kind,name,year,genres,runtime,overview,poster)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET"
                "   name=EXCLUDED.name, year=EXCLUDED.year, genres=EXCLUDED.genres,"
                "   runtime=COALESCE(EXCLUDED.runtime, title.runtime),"
                "   overview=COALESCE(EXCLUDED.overview, title.overview),"
                "   poster=COALESCE(EXCLUDED.poster, title.poster)",
                (tid, tmdb_id, m["type"], (hit or {}).get("title") or m["title"],
                 (hit or {}).get("year") or m["year"], genres_of(m["genre"]),
                 (hit or {}).get("runtime"), (hit or {}).get("overview"),
                 (hit or {}).get("poster")))
            nc.execute(
                "INSERT INTO review (user_id,title_id,rating,liked,notes,source)"
                " VALUES (%s,%s,%s,%s,%s,'joint')"
                " ON CONFLICT (user_id,title_id) DO UPDATE SET"
                "   rating=EXCLUDED.rating, liked=EXCLUDED.liked, notes=EXCLUDED.notes",
                (user, tid, m["rating"], bool(m["liked"]), m["notes"]))
        print("    %-46s %s" % (m["title"][:44],
                                ("tmdb %s" % tmdb_id) if tmdb_id else "LOCAL (no match)"))

    # The old exclusions list was global. There is one account, so it becomes
    # his -- it could not have meant anything else. From here it is per person,
    # which is what lets "Devon has seen it, Gran has not" be expressible.
    oc.execute("SELECT * FROM movie.exclusions")
    dismissed = 0
    for e in oc.fetchall():
        nc.execute("SELECT id FROM title WHERE lower(name)=lower(%s)", (e["title"],))
        row = nc.fetchone()
        if row:
            tid = row[0]
        else:
            # A dismissal for something never in the library. Dropping it would
            # hand the title straight back on the next recommendation, which is
            # the one thing a dismissal exists to prevent -- so the title is
            # created for it.
            hit = resolve(e["title"], e["year"], e["type"] or "movie")
            tid = str(uuid.uuid4())
            print("    dismissed, not in the library -- creating the title: %s" % e["title"])
            if write:
                nc.execute(
                    "INSERT INTO title (id,tmdb_id,kind,name,year,overview,poster)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (tmdb_id) DO UPDATE SET name=EXCLUDED.name"
                    " RETURNING id",
                    (tid, (hit or {}).get("tmdb_id"), e["type"] or "movie",
                     (hit or {}).get("title") or e["title"],
                     (hit or {}).get("year") or e["year"],
                     (hit or {}).get("overview"), (hit or {}).get("poster")))
                got = nc.fetchone()
                if got:
                    tid = got[0]
        dismissed += 1
        if write:
            nc.execute("INSERT INTO title_dismissed (user_id,title_id,reason)"
                       " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                       (user, tid, e["reason"]))

    oc.execute("SELECT * FROM movie.platforms")
    plats = oc.fetchall()
    for p in plats:
        if write:
            nc.execute("INSERT INTO watch_platform (id,name,active,monthly_cost,can_rent)"
                       " VALUES (%s,%s,%s,%s,%s)"
                       " ON CONFLICT (name) DO UPDATE SET"
                       "   active=EXCLUDED.active, monthly_cost=EXCLUDED.monthly_cost,"
                       "   can_rent=EXCLUDED.can_rent",
                       (str(uuid.uuid4()), p["name"], bool(p["active"]),
                        p["monthly_cost"] or 0, bool(p["can_rent"])))

    oc.execute("SELECT * FROM movie.settings")
    setts = oc.fetchall()
    for s in setts:
        if write:
            nc.execute("INSERT INTO matinee_setting (user_id,key,value) VALUES (%s,%s,%s)"
                       " ON CONFLICT (user_id,key) DO UPDATE SET value=EXCLUDED.value",
                       (user, s["key"], s["value"]))

    oc.execute("SELECT * FROM movie.saved_recommendations")
    saved = oc.fetchall()
    for s in saved:
        if write:
            nc.execute(
                "INSERT INTO suggestion (id,name,year,kind,platform,overview,reason,"
                " floor_score,floor_user,scores,audience,requested_by,status)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                ("legacy-%s" % s["id"], s["title"], s["year"], s["type"], s["platform"],
                 s["overview"], s["reason"], s["confidence"], user,
                 Json({user: s["confidence"]} if s["confidence"] else {}),
                 [user], user, s["status"] or "pending"))

    print("")
    dupes = {k: v for k, v in ratings_for.items() if len(v) > 1}
    if dupes:
        print("  MERGED -- entered more than once, because the old shape had no")
        print("  identity and re-rating APPENDED instead of updating. The last")
        print("  entry is kept as the current opinion; every rating is named so")
        print("  none disappears silently:")
        for rows in dupes.values():
            name = rows[0][0]
            had = ", ".join(str(r) for _, r in rows)
            print("    %-40s rated %s  -> kept %s" % (name[:38], had, rows[-1][1]))
        print("")
    print("  titles:     %d matched to TMDB, %d carried as local" % (matched, unmatched))
    print("  dismissed:  %d" % dismissed)
    print("  also:       %d platforms, %d settings, %d saved suggestions"
          % (len(plats), len(setts), len(saved)))
    if write:
        new.commit()
        print("  WRITTEN. The old warehouse.movie tables are untouched.")
    else:
        new.rollback()
        print("  dry run -- nothing written. Re-run with --write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
