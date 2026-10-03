"""Matinee: a watchlist, and what suits everyone watching.

Server-rendered HTML over plain requests, for the reason Runway went the same
way: Streamlit shipped a megabyte of JavaScript to draw a table, and a shared
review corpus has to be server-authoritative anyway — two people's reviews must
agree for a blend to mean anything.

IDENTITY COMES FROM THE GATEWAY. X-Platform-User is set unconditionally by
nginx from the platform's answer, and this app holds no login of its own.

That used to finish "nothing else can reach the container", which was a claim
about the network rather than a property of the app — and it was false. The
compose file published 8551 on every interface, Docker's rules sit in front of
UFW, and any device on the home network could send X-Platform-User and be
anybody: read their library, write their reviews, share their taste. So the
gateway now proves each request came through it with X-Gateway-Token, the same
check Mise and Keep make, and the port is no longer published.
"""
import hashlib
import hmac
import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from matinee import blend, netflix, store  # noqa: E402
import ai  # noqa: E402
import tmdb  # noqa: E402

app = FastAPI(title="Matinee")

GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")

# The container runs on UTC, so date.today() after 7pm on a Sunday said Monday.
# "The day we watched it" is the household's day.
HOME_TZ = ZoneInfo(os.environ.get("TZ") or "America/Chicago")
if not GATEWAY_TOKEN:
    # Said at boot, loudly, because the check below is skipped without it and a
    # missing variable would otherwise look exactly like a working gate.
    print("matinee: GATEWAY_TOKEN is unset - identity headers are TRUSTED "
          "FROM ANYONE who can reach this port", file=sys.stderr, flush=True)


@app.middleware("http")
async def through_the_gateway(request: Request, call_next):
    """Refuse anything that did not come through the gateway.

    Everything, not just the pages that read identity: X-Platform-Variant is
    trusted the same way, and a forged 'full' would put a lite user's
    recommendations on the metered key. /healthz and /static carry nobody's
    data. compare_digest so the refusal does not time how much of it matched."""
    path = request.url.path
    if GATEWAY_TOKEN and path != "/healthz" and not path.startswith("/static/"):
        sent = request.headers.get("x-gateway-token", "")
        if not hmac.compare_digest(sent.encode(), GATEWAY_TOKEN.encode()):
            return PlainTextResponse("Not through the gateway.", status_code=403)
    return await call_next(request)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def _asset_version():
    """A fingerprint of the stylesheets and scripts, stamped on their URLs.

    Without it a deploy could pair the new page with the browser's cached
    CSS: on 2026-09-25 the Kept rows came out with the old button styles
    until a hard reload. Hashed once at boot, since the files only change
    with a deploy, which restarts the process anyway."""
    h = hashlib.sha1()
    for root, _, files in sorted(os.walk(os.path.join(HERE, "static"))):
        for name in sorted(files):
            with open(os.path.join(root, name), "rb") as f:
                h.update(f.read())
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()

# The tier the grant carries. 'lite' is the library and reviews with no model
# calls — which is what lets this app be shared without moving Devon's own
# recommendations onto a metered key. Trusted because the gateway sets it
# unconditionally, exactly like the identity header.
#
# BLANK IS LITE. Matinee declares variants, so a blank tier is a grant with no
# tier recorded — the least capable, never the most. It used to default to
# "full", and only the broker's Max check stood between such a grant and a
# model call. Devon's rule: nobody but him reaches a model unless AI is
# deliberately turned on for them, which here is granting Full.
def variant(request):
    return (request.headers.get("x-platform-variant") or "lite").lower()


def me(request):
    """Who is asking. Refuses rather than guessing: an app that falls back to
    a default user when the header is missing is one bad proxy rule away from
    showing one person another person's library."""
    email = request.headers.get("x-platform-user")
    if not email:
        raise HTTPException(status_code=401, detail="No identity from the gateway.")
    who = store.person_by_email(email)
    if not who:
        raise HTTPException(status_code=403, detail="No account for %s." % email)
    return who


def is_admin(request):
    """In the platform's admin group. The gateway sets X-Platform-Groups
    unconditionally, like the identity header, so a client cannot claim it."""
    groups = (request.headers.get("x-platform-groups") or "").split(",")
    return "admin" in (g.strip() for g in groups)


def admin_only(request):
    """For the few writes that change something EVERYONE shares -- merging two
    titles, the household's platforms. Being signed in used to be enough, so
    any friend on a lite grant could merge titles or switch off Netflix."""
    who = me(request)
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Only an admin can change that.")
    return who


def page(request, name, **ctx):
    who = ctx.pop("who", None) or me(request)
    # Request first: the (name, context) form is the old Starlette signature and
    # newer versions read the context dict as the template name.
    return templates.TemplateResponse(request, name, {
        "me": who, "variant": variant(request), "is_admin": is_admin(request),
        "here": request.url.path, **ctx})


def _check_origin(request):
    """Same-origin form posts only. The session cookie is domain-wide, so
    without this another app on the estate could post here on your behalf."""
    origin = request.headers.get("origin")
    if origin and request.headers.get("host") not in origin:
        raise HTTPException(status_code=403, detail="Bad origin.")


# ── recommend ───────────────────────────────────────────────────────────────

def _kept(user_id):
    """The Kept list with where each pick can be watched now (TMDB, cached a
    day), because the line saved with it is only what the model guessed at pick
    time. Never fails the page: with TMDB out the rows just carry no line."""
    rows = store.saved(user_id)
    if not rows:
        return rows
    try:
        return blend.kept_availability(rows, store.platforms(active_only=True), tmdb)
    except Exception as exc:  # noqa: BLE001
        print("matinee: kept availability failed: %s" % exc, file=sys.stderr, flush=True)
        return rows


@app.get("/", response_class=HTMLResponse)
def home(request: Request, watched: str | None = None):
    who = me(request)
    return page(request, "recommend.html", who=who,
                circle=store.circle(who["id"]), picks=[], note=None,
                chosen=[who["id"]], parties=store.parties(who["id"]),
                saved=_kept(who["id"]),
                just_watched=store.title(watched) if watched else None)


@app.post("/recommend", response_class=HTMLResponse)
async def do_recommend(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    chosen = form.getlist("who") or [who["id"]]
    rewatch = form.get("rewatch") == "on"

    if variant(request) != "full":
        return page(request, "recommend.html", who=who, circle=store.circle(who["id"]),
                    picks=[], chosen=chosen, parties=store.parties(who["id"]),
                    saved=_kept(who["id"]),
                    note="Recommendations are part of the full version of Matinee.")

    try:
        out = blend.recommend(ai.ask_structured, who["id"], chosen,
                              allow_rewatch=rewatch, tmdb=tmdb)
    except ai.BrokerError as exc:
        # Degrade, never crash: the library and the reviews are still there.
        # The full reason goes to the container log; the page gets a sentence,
        # because the exception names internal hosts, ports and budgets.
        print("matinee: recommend failed [%s/%s] for %s: %s"
              % (exc.kind, exc.code, who["id"], exc), file=sys.stderr, flush=True)
        return page(request, "recommend.html", who=who, circle=store.circle(who["id"]),
                    picks=[], chosen=chosen, parties=store.parties(who["id"]),
                    saved=_kept(who["id"]),
                    note=exc.public_message())

    labels = {p["id"]: p["label"] for p in out.get("people", [])}
    return page(request, "recommend.html", who=who, circle=store.circle(who["id"]),
                picks=out["picks"], note=out.get("note"), chosen=chosen,
                labels=labels, rewatch=rewatch, parties=store.parties(who["id"]),
                saved=_kept(who["id"]))


@app.post("/keep")
async def keep(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    store.save_suggestion(
        {"name": form["name"], "year": _int(form.get("year")), "kind": form.get("kind"),
         "platform": form.get("platform"), "overview": form.get("overview"),
         "reason": form.get("reason"), "floor": _int(form.get("floor")),
         "scores": {}},
        audience=form.getlist("audience") or [who["id"]],
        floor_user=form.get("floor_user") or who["id"], requested_by=who["id"])
    if _in_place(request):
        # The Kept list, redrawn, so the pick appears there without a reload.
        return templates.TemplateResponse(request, "_kept.html",
                                          {"saved": _kept(who["id"])})
    return RedirectResponse("/", status_code=303)


def _in_place(request):
    """The results page acts on a card without leaving it.

    A recommendation lives only in the response that drew it, so a redirect
    after "Keep" used to throw away the other five picks. The page's script
    asks with this header and takes a 204; a form posted without JavaScript
    still gets the redirect."""
    return request.headers.get("x-requested-with") == "fetch"


def _done(request, url):
    if _in_place(request):
        return Response(status_code=204)
    return RedirectResponse(url, status_code=303)


def _pick_from(form):
    """The title fields a result card posts back, shaped like a suggestion so
    _title_for can place it."""
    return {"name": form["name"], "year": _int(form.get("year")),
            "kind": form.get("kind"), "overview": form.get("overview") or None,
            "title_id": None}


@app.post("/pick/seen")
async def seen_pick(request: Request):
    """Already seen one it suggested: say what you thought, good or bad.

    Either way it leaves the results, and the review is what keeps it out of
    the next ones. No watched date, because "some time ago" is not a day."""
    _check_origin(request)
    who = me(request)
    form = await request.form()
    tid = _title_for(_pick_from(form))
    store.set_review(who["id"], tid, _rating(form.get("rating")),
                     form.get("liked") == "on",
                     (form.get("notes") or "").strip() or None)
    return _done(request, "/?watched=%s" % tid)


@app.post("/pick/dismiss")
async def dismiss_pick(request: Request):
    """Not for me, without having watched it. Dismissals are part of what the
    recommender is told everyone has seen, so it stops coming back."""
    _check_origin(request)
    who = me(request)
    form = await request.form()
    store.dismiss(who["id"], _title_for(_pick_from(form)), "recommendation")
    return _done(request, "/")


@app.post("/keep/{sid}/watched")
async def watched_kept(sid: str, request: Request):
    """You watched a kept pick: it becomes a title with your review on it.

    Only YOUR review. The pick may have been for you and Gran, but her opinion
    is hers to give — writing it for her would put words in the blend that
    nobody said."""
    _check_origin(request)
    who = me(request)
    s = store.suggestion(sid, who["id"])
    if not s:
        raise HTTPException(status_code=404, detail="No such pick.")
    form = await request.form()
    tid = s["title_id"] or _title_for(s)
    store.set_review(who["id"], tid, _rating(form.get("rating")),
                     form.get("liked") == "on",
                     (form.get("notes") or "").strip() or None,
                     watched_at=datetime.now(HOME_TZ).date())
    store.mark_watched(sid, who["id"], tid)
    return RedirectResponse("/?watched=%s" % tid, status_code=303)


def _title_for(s):
    """The library row a kept pick becomes.

    An existing title first, so a film already in the library gets a review
    rather than a twin. Then TMDB, accepted only when it is plainly the same
    thing — `find_match` takes "Up" to be "Upgrade" if it has to, and nothing is
    attached to a TMDB id on a guess. Then a local title, so TMDB being down
    never loses what somebody just told us.
    """
    kind = "show" if s["kind"] in ("show", "tv") else "movie"
    tid = store.title_by_name(s["name"], s["year"])
    if tid:
        return tid
    hit = None
    try:
        hit = tmdb.find_match(s["name"], s["year"], kind)
    except Exception as exc:
        print("matinee: TMDB lookup for %r failed: %s" % (s["name"], exc),
              file=sys.stderr, flush=True)
    if hit and (hit["year"] == s["year"] if s["year"]
                else tmdb._norm_for_match(hit["title"]) == tmdb._norm_for_match(s["name"])):
        return store.upsert_title(
            hit["tmdb_id"], hit["type"], hit["title"], hit["year"],
            hit["genre"].split("/") if hit.get("genre") else [],
            None, hit.get("overview") or s["overview"], None)
    return store.upsert_title(None, kind, s["name"], s["year"], [], None,
                              s["overview"], None)


@app.post("/keep/{sid}/drop")
def drop_kept(sid: str, request: Request):
    _check_origin(request)
    store.drop_suggestion(sid, me(request)["id"])
    return RedirectResponse("/", status_code=303)


# ── library ─────────────────────────────────────────────────────────────────

@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str | None = None):
    who = me(request)
    return page(request, "library.html", who=who,
                titles=store.library(who["id"], q), q=q or "")


@app.get("/title/{title_id}", response_class=HTMLResponse)
def one_title(title_id: str, request: Request):
    who = me(request)
    t = store.title(title_id)
    if not t:
        raise HTTPException(status_code=404, detail="No such title.")
    return page(request, "title.html", who=who, t=t,
                reviews=store.reviews_of(title_id, who["id"]),
                # How many opinions exist, against how many you may read. The
                # gap is the privacy promise made visible rather than hidden.
                total=store.q("SELECT count(*) AS n FROM review WHERE title_id=%s",
                              (title_id,), one=True)["n"])


@app.post("/title/{title_id}/review")
async def review(title_id: str, request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    if form.get("clear"):
        store.clear_review(who["id"], title_id)
    else:
        store.set_review(who["id"], title_id, _rating(form.get("rating")),
                         form.get("liked") == "on", form.get("notes") or None)
    return RedirectResponse("/title/%s" % title_id, status_code=303)


@app.post("/title/{title_id}/dismiss")
def dismiss(title_id: str, request: Request):
    _check_origin(request)
    store.dismiss(me(request)["id"], title_id)
    return RedirectResponse("/library", status_code=303)


@app.post("/title/{title_id}/undismiss")
def undismiss(title_id: str, request: Request):
    _check_origin(request)
    store.undismiss(me(request)["id"], title_id)
    return RedirectResponse("/library", status_code=303)


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request, q: str | None = None):
    who = me(request)
    hits = []
    if q:
        try:
            hits = tmdb.search(q, limit=10)
        except Exception as exc:
            return page(request, "add.html", who=who, q=q, hits=[],
                        note="TMDB search failed (%s). You can still add it by hand." % exc)
    return page(request, "add.html", who=who, q=q or "", hits=hits, note=None)


@app.post("/add")
async def add(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    tid = store.upsert_title(
        _int(form.get("tmdb_id")), form.get("kind") or "movie", form["name"],
        _int(form.get("year")), (form.get("genres") or "").split(",") if form.get("genres") else [],
        _int(form.get("runtime")), form.get("overview") or None, form.get("poster") or None)
    rating = _rating(form.get("rating"))
    if rating is not None:
        # Liked follows the score. It was always True here, so adding a film
        # at 1/5 filed it as one you liked.
        store.set_review(who["id"], tid, rating, rating >= 4, None)
    else:
        store.ensure_review(who["id"], tid)
    return RedirectResponse("/title/%s" % tid, status_code=303)


# ── import ──────────────────────────────────────────────────────────────────

MAX_UPLOAD = 5 * 1024 * 1024   # a few years of viewing activity is well under 1 MB
MAX_IMPORT = 1000
# One form field per row (the row's data rides in the checkbox value), so a
# confirm posts about as many fields as it has rows. Starlette refuses more
# than 1000 by default, which is below what a long history legitimately sends.
IMPORT_FIELDS = 2 * MAX_IMPORT + 50


def _row(value):
    """A ticked row's JSON back into a dict. The value came from a form, so
    anything that is not an object is dropped rather than trusted."""
    try:
        row = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def _more(remaining, next_offset, profile):
    """What the next-batch upload form needs, or None when the file is done."""
    if not remaining:
        return None
    return {"offset": next_offset, "remaining": remaining, "profile": profile}


def _import_page(request, who, **ctx):
    ctx.setdefault("preview", None)
    ctx.setdefault("done", None)
    ctx.setdefault("note", None)
    ctx.setdefault("more", None)
    return page(request, "import.html", who=who, **ctx)


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    return _import_page(request, me(request))


@app.post("/import", response_class=HTMLResponse)
async def import_preview(request: Request):
    """Read a Netflix viewing-activity CSV and SHOW what would be filed.

    Nothing is written here. Matching is plain code (no model, so no gate), but
    marking the wrong film as seen hides it from every future pick, so the
    person looks first and /import/confirm writes only what stays ticked."""
    _check_origin(request)
    who = me(request)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _import_page(request, who, note="Choose the ViewingActivity.csv file.")
    raw = await upload.read(MAX_UPLOAD + 1)
    if not raw or len(raw) > MAX_UPLOAD:
        return _import_page(request, who, note="That file is empty or too large to be a viewing history.")
    profile = (form.get("profile") or "").strip()
    offset = max(0, _int(form.get("offset")) or 0)
    try:
        titles, profiles = netflix.parse(raw.decode("utf-8-sig", errors="replace"),
                                         profile or None)
        matched, unmatched, remaining = netflix.match(titles, offset=offset)
    except ValueError as exc:
        return _import_page(request, who, note=str(exc))
    have = store.reviewed_tmdb_ids(who["id"], [m["tmdb_id"] for m in matched])
    fresh = [m for m in matched if m["tmdb_id"] not in have]
    return _import_page(request, who, preview={
        "matched": fresh, "already": len(matched) - len(fresh), "unmatched": unmatched,
        "batch": ((offset + 1, min(offset + netflix.MAX_TITLES, len(titles)), len(titles))
                  if remaining or offset else None),
        "profiles": profiles if len(profiles) > 1 else None, "profile": profile},
        more=_more(remaining, offset + netflix.MAX_TITLES, profile))


@app.post("/import/confirm", response_class=HTMLResponse)
async def import_confirm(request: Request):
    """File what the person left ticked, as seen and unrated.

    ensure_review never touches a rating you already gave, and an unrated review
    is what recommend() counts as seen (store.seen_by). The unmatched ones are
    unticked by default: ticked, each becomes a local title with no TMDB id."""
    _check_origin(request)
    who = me(request)
    form = await request.form(max_fields=IMPORT_FIELDS)
    added = local = 0
    for value in form.getlist("pick")[:MAX_IMPORT]:
        row = _row(value)
        tmdb_id, name = _int(row.get("tmdb_id")), str(row.get("name") or "").strip()
        if not tmdb_id or not name:
            continue
        try:
            seen_on = date.fromisoformat(str(row.get("last") or ""))
        except ValueError:
            seen_on = None
        genres = row.get("genres") if isinstance(row.get("genres"), list) else []
        tid = store.upsert_title(
            tmdb_id, "show" if row.get("kind") == "show" else "movie", name,
            _int(row.get("year")), [str(g) for g in genres if g],
            None, row.get("overview") or None, None)
        store.ensure_review(who["id"], tid, seen_on)
        added += 1
    for value in form.getlist("local")[:MAX_IMPORT]:
        row = _row(value)
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        kind = "show" if row.get("kind") == "show" else "movie"
        tid = store.title_by_name(name) or store.upsert_title(None, kind, name, None)
        store.ensure_review(who["id"], tid)
        local += 1
    return _import_page(request, who, done={"added": added, "local": local},
                        more=_more(_int(form.get("remaining")) or 0,
                                   max(0, _int(form.get("next_offset")) or 0),
                                   (form.get("profile") or "").strip()))


@app.post("/merge")
async def merge(request: Request):
    """Fold a duplicate into the title it should have been.

    The old app had no identity, so the same film went in twice under different
    names and TMDB cannot match what was typed wrong. Reviews move across
    unless the person already has one on the survivor.
    """
    _check_origin(request)
    admin_only(request)
    form = await request.form()
    keep, drop = form["keep"], form["drop"]
    if keep == drop:
        raise HTTPException(status_code=400, detail="Those are the same title.")
    store.merge_titles(keep, drop)
    return RedirectResponse("/library", status_code=303)


# ── circle ──────────────────────────────────────────────────────────────────

def _circle_ctx(request, who, note=None):
    return dict(who=who,
                counted=[p for p in store.circle(who["id"]) if not p["is_me"]],
                shared=store.shared_by_me(who["id"]),
                friends=store.friends_not_yet_shared(who["id"]),
                note=note)


@app.get("/circle", response_class=HTMLResponse)
def circle(request: Request):
    who = me(request)
    return page(request, "circle.html", **_circle_ctx(request, who))


@app.post("/circle/pick")
async def share_with_friend(request: Request):
    """Share with somebody chosen from your friends.

    The platform's directory doing its job: no address to type and no chance of
    picking the wrong one. Their id is checked against your friends rather than
    trusted from the form — a hidden field is a suggestion, not a fact.
    """
    _check_origin(request)
    who = me(request)
    form = await request.form()
    wanted = form.get("id")
    if wanted not in {f["id"] for f in store.friends_not_yet_shared(who["id"])}:
        return page(request, "circle.html",
                    **_circle_ctx(request, who,
                                  "That is not somebody you are connected to."))
    store.share(who["id"], wanted, form.get("visibility") or "blend")
    return RedirectResponse("/circle", status_code=303)


@app.post("/circle")
async def do_share(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    other = store.person_by_email((form.get("email") or "").strip())
    if not other:
        return page(request, "circle.html", **_circle_ctx(request, who,
            "Nobody on devondoes.dev has that address. They need an account "
            "before you can share with them."))
    if other["id"] == who["id"]:
        return page(request, "circle.html", **_circle_ctx(request, who,
            "Your own taste is always counted."))
    store.share(who["id"], other["id"], form.get("visibility") or "blend")
    return RedirectResponse("/circle", status_code=303)


@app.post("/circle/{viewer_id}/stop")
def stop_share(viewer_id: str, request: Request):
    _check_origin(request)
    store.unshare(me(request)["id"], viewer_id)
    return RedirectResponse("/circle", status_code=303)


# ── settings ────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    who = me(request)
    return page(request, "settings.html", who=who, rules=store.rules(who["id"]),
                platforms=store.platforms())


@app.post("/settings/rule")
async def add_rule(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    value = (form.get("value") or "").strip()
    if value:
        store.add_rule(who["id"], form["kind"], value)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/rule/drop")
async def drop_rule(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    store.drop_rule(who["id"], form["kind"], form["value"])
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/platform")
async def set_platform(request: Request):
    _check_origin(request)
    admin_only(request)
    form = await request.form()
    store.set_platform(form["name"], form.get("active") == "on",
                       float(form.get("monthly_cost") or 0),
                       form.get("can_rent") == "on")
    return RedirectResponse("/settings", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rating(v):
    """1-5 or nothing. The column's CHECK would turn a hand-edited 7 into a 500."""
    n = _int(v)
    return n if n is not None and 1 <= n <= 5 else None
