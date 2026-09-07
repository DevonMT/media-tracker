"""Every query Matinee makes, in one place.

Server-authoritative and plain: two people's reviews have to be consistent for
a blend to mean anything, so there is no local cache and no merge to reason
about. Mise is local-first because it is one person's data needed offline in a
shop; this is other people's data used on a sofa with wifi, which is the
opposite on every count.
"""
import os
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor, Json


def _dsn():
    return dict(
        host=os.environ.get("PGHOST", "postgres"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("MATINEE_DB", "platform"),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )


_pool = None


def conn():
    """One long-lived connection, reopened if the server drops it."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.connect(**_dsn())
        _pool.autocommit = True
    return _pool


def q(sql, args=(), one=False):
    with conn().cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        if cur.description is None:
            return None
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with conn().cursor() as cur:
        cur.execute(sql, args)
        return cur.rowcount


def new_id():
    return str(uuid.uuid4())


# ── who you are, and who is in your circle ──────────────────────────────────

def person(user_id):
    return q('SELECT id, email, name FROM "user" WHERE id=%s', (user_id,), one=True)


def person_by_email(email):
    return q('SELECT id, email, name FROM "user" WHERE lower(email)=lower(%s)',
             (email,), one=True)


def circle(viewer_id):
    """Whose taste you may count, and how much of it you may see.

    Always includes you, first and unconditionally: "just me" is the same
    feature with one person selected, not a separate mode.

    Everyone else is here because THEY shared with you. A platform grant says
    "this person may reach the app", which is not "I want her opinion counted",
    so the grant is a prerequisite and this is the feature.
    """
    rows = q(
        """
        SELECT u.id, u.name, u.email, s.visibility,
               (SELECT count(*) FROM review r WHERE r.user_id = u.id) AS reviews
          FROM taste_share s
          JOIN "user" u ON u.id = s.owner_id
         WHERE s.viewer_id = %s
         ORDER BY lower(coalesce(u.name, u.email))
        """, (viewer_id,))
    me = person(viewer_id) or {"id": viewer_id, "name": None, "email": ""}
    mine = q("SELECT count(*) AS n FROM review WHERE user_id=%s", (viewer_id,), one=True)
    return [{**me, "visibility": "own", "reviews": mine["n"], "is_me": True}] + [
        {**r, "is_me": False} for r in rows]


def shared_by_me(owner_id):
    """Who you have let count your taste. One-way: this is not the same list as
    `circle`, and conflating them would mean you cannot plan a film for
    somebody without handing them your viewing history."""
    return q(
        """
        SELECT u.id, u.name, u.email, s.visibility, s.created_at
          FROM taste_share s JOIN "user" u ON u.id = s.viewer_id
         WHERE s.owner_id = %s ORDER BY lower(coalesce(u.name, u.email))
        """, (owner_id,))


def share(owner_id, viewer_id, visibility="blend"):
    return x("""INSERT INTO taste_share (owner_id, viewer_id, visibility)
                VALUES (%s,%s,%s)
                ON CONFLICT (owner_id, viewer_id)
                  DO UPDATE SET visibility = EXCLUDED.visibility""",
             (owner_id, viewer_id, visibility))


def unshare(owner_id, viewer_id):
    return x("DELETE FROM taste_share WHERE owner_id=%s AND viewer_id=%s",
             (owner_id, viewer_id))


def may_read(viewer_id, owner_id):
    """Whether individual ratings may be shown, as opposed to merely counted.

    The distinction the whole design turns on: 'blend' means include me and do
    not show anyone my ratings. A blend never needed to expose its inputs to
    produce an output, so this costs the feature nothing.
    """
    if viewer_id == owner_id:
        return True
    row = q("SELECT visibility FROM taste_share WHERE owner_id=%s AND viewer_id=%s",
            (owner_id, viewer_id), one=True)
    return bool(row and row["visibility"] == "open")


# ── the library ─────────────────────────────────────────────────────────────

def library(viewer_id, search=None):
    """Titles with your own verdict, and how many others have one.

    The count is deliberately a COUNT and not a list: it says "three people
    have an opinion" without saying whose or what, which is what 'blend'
    promises. Opening someone's ratings is a separate, permitted request.
    """
    where, args = "", [viewer_id, viewer_id]
    if search:
        where = " AND t.name ILIKE %s"
        args.append("%%%s%%" % search)
    return q(
        """
        SELECT t.*, r.rating, r.liked, r.notes, r.source,
               (SELECT count(*) FROM review o WHERE o.title_id = t.id) AS opinions,
               EXISTS (SELECT 1 FROM title_dismissed d
                        WHERE d.title_id = t.id AND d.user_id = %s) AS dismissed
          FROM title t
          LEFT JOIN review r ON r.title_id = t.id AND r.user_id = %s
         WHERE true""" + where + """
         ORDER BY (r.rating IS NULL), r.rating DESC, lower(t.name)
        """, tuple(args))


def title(title_id):
    return q("SELECT * FROM title WHERE id=%s", (title_id,), one=True)


def reviews_of(title_id, viewer_id):
    """Everyone's verdict on one title THAT YOU ARE ALLOWED TO SEE.

    Filtered in SQL rather than in the template, so a future caller cannot
    forget: only your own, and people who set their sharing to 'open'.
    """
    return q(
        """
        SELECT u.id, u.name, u.email, r.rating, r.liked, r.notes, r.source
          FROM review r JOIN "user" u ON u.id = r.user_id
         WHERE r.title_id = %s
           AND (r.user_id = %s
                OR EXISTS (SELECT 1 FROM taste_share s
                            WHERE s.owner_id = r.user_id AND s.viewer_id = %s
                              AND s.visibility = 'open'))
         ORDER BY (r.user_id <> %s), lower(coalesce(u.name, u.email))
        """, (title_id, viewer_id, viewer_id, viewer_id))


def upsert_title(tmdb_id, kind, name, year, genres=None, runtime=None,
                 overview=None, poster=None):
    """One row per film, ever. Matched on the TMDB id where there is one,
    because two people typing the same name must land on the same row."""
    if tmdb_id:
        found = q("SELECT id FROM title WHERE tmdb_id=%s", (tmdb_id,), one=True)
    else:
        found = q("""SELECT id FROM title WHERE tmdb_id IS NULL
                      AND lower(name)=lower(%s) AND year IS NOT DISTINCT FROM %s""",
                  (name, year), one=True)
    tid = found["id"] if found else new_id()
    x("""INSERT INTO title (id,tmdb_id,kind,name,year,genres,runtime,overview,poster)
         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
         ON CONFLICT (id) DO UPDATE SET
           name=EXCLUDED.name, year=EXCLUDED.year,
           genres=CASE WHEN EXCLUDED.genres = '{}' THEN title.genres ELSE EXCLUDED.genres END,
           runtime=COALESCE(EXCLUDED.runtime, title.runtime),
           overview=COALESCE(EXCLUDED.overview, title.overview),
           poster=COALESCE(EXCLUDED.poster, title.poster)""",
      (tid, tmdb_id, kind, name, year, genres or [], runtime, overview, poster))
    return tid


def set_review(user_id, title_id, rating=None, liked=None, notes=None):
    """Your opinion. One per title: changing your mind updates the row."""
    return x("""INSERT INTO review (user_id,title_id,rating,liked,notes,updated_at)
                VALUES (%s,%s,%s,%s,%s,now())
                ON CONFLICT (user_id,title_id) DO UPDATE SET
                  rating=EXCLUDED.rating, liked=EXCLUDED.liked,
                  notes=EXCLUDED.notes, updated_at=now()""",
             (user_id, title_id, rating, liked, notes))


def clear_review(user_id, title_id):
    return x("DELETE FROM review WHERE user_id=%s AND title_id=%s", (user_id, title_id))


def merge_titles(keep_id, drop_id):
    """Fold one title into another, keeping every opinion.

    Needed because the old shape let the same film in twice under different
    names, and TMDB cannot match what was typed wrong. Reviews move across
    unless the person already has one on the survivor, in which case theirs
    stands — a merge must never overwrite somebody's current verdict.
    """
    x("""UPDATE review r SET title_id=%s WHERE title_id=%s
          AND NOT EXISTS (SELECT 1 FROM review o
                           WHERE o.user_id=r.user_id AND o.title_id=%s)""",
      (keep_id, drop_id, keep_id))
    x("""UPDATE title_dismissed d SET title_id=%s WHERE title_id=%s
          AND NOT EXISTS (SELECT 1 FROM title_dismissed o
                           WHERE o.user_id=d.user_id AND o.title_id=%s)""",
      (keep_id, drop_id, keep_id))
    x("DELETE FROM title WHERE id=%s", (drop_id,))


# ── vetoes, dismissals, platforms ───────────────────────────────────────────

def rules(user_id):
    return q("SELECT kind, value FROM taste_rule WHERE user_id=%s ORDER BY kind, value",
             (user_id,))


def add_rule(user_id, kind, value):
    return x("""INSERT INTO taste_rule (user_id,kind,value) VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING""", (user_id, kind, value))


def drop_rule(user_id, kind, value):
    return x("DELETE FROM taste_rule WHERE user_id=%s AND kind=%s AND value=%s",
             (user_id, kind, value))


def rules_for(user_ids):
    """Everybody's vetoes, unioned. Anybody's veto removes the title, and it is
    never attributed: the recommendation simply does not contain horror and
    does not explain that somebody vetoed it."""
    if not user_ids:
        return []
    return q("SELECT DISTINCT kind, value FROM taste_rule WHERE user_id = ANY(%s)",
             (list(user_ids),))


def dismiss(user_id, title_id, reason=None):
    return x("""INSERT INTO title_dismissed (user_id,title_id,reason) VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING""", (user_id, title_id, reason))


def undismiss(user_id, title_id):
    return x("DELETE FROM title_dismissed WHERE user_id=%s AND title_id=%s",
             (user_id, title_id))


def seen_by(user_ids):
    """Titles anyone in the group has reviewed or dismissed, as a set of names.

    Names rather than ids because the model answers with titles, not rows.
    """
    if not user_ids:
        return set()
    rows = q("""SELECT DISTINCT lower(t.name) AS n FROM title t
                 WHERE EXISTS (SELECT 1 FROM review r
                                WHERE r.title_id=t.id AND r.user_id = ANY(%s))
                    OR EXISTS (SELECT 1 FROM title_dismissed d
                                WHERE d.title_id=t.id AND d.user_id = ANY(%s))""",
             (list(user_ids), list(user_ids)))
    return {r["n"] for r in rows}


def platforms(active_only=False):
    where = " WHERE active" if active_only else ""
    return q("SELECT * FROM watch_platform" + where + " ORDER BY lower(name)")


def set_platform(name, active, monthly_cost, can_rent):
    return x("""INSERT INTO watch_platform (id,name,active,monthly_cost,can_rent)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (name) DO UPDATE SET active=EXCLUDED.active,
                  monthly_cost=EXCLUDED.monthly_cost, can_rent=EXCLUDED.can_rent""",
             (new_id(), name, active, monthly_cost, can_rent))


# ── suggestions ─────────────────────────────────────────────────────────────

def save_suggestion(s, audience, floor_user, requested_by):
    return x("""INSERT INTO suggestion
                  (id,name,year,kind,platform,overview,reason,floor_score,
                   floor_user,scores,audience,requested_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
             (new_id(), s["name"], s.get("year"), s.get("kind"), s.get("platform"),
              s.get("overview"), s.get("reason"), s.get("floor"), floor_user,
              Json(s.get("scores") or {}), list(audience), requested_by))


def saved(user_id):
    return q("""SELECT * FROM suggestion WHERE requested_by=%s AND status='pending'
                 ORDER BY at DESC""", (user_id,))


def drop_suggestion(sid, user_id):
    return x("DELETE FROM suggestion WHERE id=%s AND requested_by=%s", (sid, user_id))


def taste_profile(user_id, limit=40):
    """What somebody likes, as the model needs to hear it.

    Only their OWN reviews — a blend asks the model per person, so mixing two
    people's histories into one profile is exactly the averaging the floor rule
    exists to avoid.
    """
    return q("""SELECT t.name, t.year, t.kind, t.genres, r.rating, r.notes
                  FROM review r JOIN title t ON t.id = r.title_id
                 WHERE r.user_id=%s AND r.rating IS NOT NULL
                 ORDER BY r.rating DESC, r.updated_at DESC LIMIT %s""",
             (user_id, limit))
