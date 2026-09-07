"""Matinee: a watchlist, and what suits everyone watching.

Server-rendered HTML over plain requests, for the reason Runway went the same
way: Streamlit shipped a megabyte of JavaScript to draw a table, and a shared
review corpus has to be server-authoritative anyway — two people's reviews must
agree for a blend to mean anything.

IDENTITY COMES FROM THE GATEWAY. X-Platform-User is set unconditionally by
nginx from the platform's answer, so a client cannot forge it and this app
holds no login of its own. Nothing else can reach the container.
"""
import os
import sys

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from matinee import blend, store  # noqa: E402
import ai  # noqa: E402
import tmdb  # noqa: E402

app = FastAPI(title="Matinee")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

# The tier the grant carries. 'lite' is the library and reviews with no model
# calls — which is what lets this app be shared without moving Devon's own
# recommendations onto a metered key. Trusted because the gateway sets it
# unconditionally, exactly like the identity header.
def variant(request):
    return (request.headers.get("x-platform-variant") or "full").lower()


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


def page(request, name, **ctx):
    who = ctx.pop("who", None) or me(request)
    # Request first: the (name, context) form is the old Starlette signature and
    # newer versions read the context dict as the template name.
    return templates.TemplateResponse(request, name, {
        "me": who, "variant": variant(request),
        "here": request.url.path, **ctx})


def _check_origin(request):
    """Same-origin form posts only. The session cookie is domain-wide, so
    without this another app on the estate could post here on your behalf."""
    origin = request.headers.get("origin")
    if origin and request.headers.get("host") not in origin:
        raise HTTPException(status_code=403, detail="Bad origin.")


# ── recommend ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    who = me(request)
    return page(request, "recommend.html", who=who,
                circle=store.circle(who["id"]), picks=[], note=None,
                chosen=[who["id"]], saved=store.saved(who["id"]))


@app.post("/recommend", response_class=HTMLResponse)
async def do_recommend(request: Request):
    _check_origin(request)
    who = me(request)
    form = await request.form()
    chosen = form.getlist("who") or [who["id"]]
    rewatch = form.get("rewatch") == "on"

    if variant(request) != "full":
        return page(request, "recommend.html", who=who, circle=store.circle(who["id"]),
                    picks=[], chosen=chosen, saved=store.saved(who["id"]),
                    note="Recommendations are part of the full version of Matinee.")

    try:
        out = blend.recommend(ai.ask_structured, who["id"], chosen,
                              allow_rewatch=rewatch)
    except ai.BrokerError as exc:
        # Degrade, never crash: the library and the reviews are still there.
        return page(request, "recommend.html", who=who, circle=store.circle(who["id"]),
                    picks=[], chosen=chosen, saved=store.saved(who["id"]),
                    note="Could not reach the recommender (%s)." % exc)

    labels = {p["id"]: p["label"] for p in out.get("people", [])}
    return page(request, "recommend.html", who=who, circle=store.circle(who["id"]),
                picks=out["picks"], note=out.get("note"), chosen=chosen,
                labels=labels, rewatch=rewatch, saved=store.saved(who["id"]))


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
    return RedirectResponse("/", status_code=303)


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
        store.set_review(who["id"], title_id, _int(form.get("rating")),
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
    if form.get("rating"):
        store.set_review(who["id"], tid, _int(form.get("rating")), True, None)
    return RedirectResponse("/title/%s" % tid, status_code=303)


@app.post("/merge")
async def merge(request: Request):
    """Fold a duplicate into the title it should have been.

    The old app had no identity, so the same film went in twice under different
    names and TMDB cannot match what was typed wrong. Reviews move across
    unless the person already has one on the survivor.
    """
    _check_origin(request)
    me(request)
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
    me(request)
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
