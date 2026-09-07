import json
import streamlit as st
from db import get_conn, init_db
from recommend import build_context, get_recommendations, rescore_saved, filter_recommendations
import tmdb as tmdb_client

try:
    from streamlit_theme import apply_theme  # vendored alongside this file
except Exception:  # pragma: no cover - theme is cosmetic
    def apply_theme(*_a, **_k):
        pass

init_db()

st.set_page_config(page_title="Movie Tracker", page_icon="🎬", layout="wide")
apply_theme()

# The way back to the hub. Streamlit gives an app no chrome of its own, so on
# movies.devondoes.dev this page was a dead end — reachable from devondoes.dev
# and with no route out except the browser's back button.
# The estate bar. Streamlit gives an app no chrome, so this is the same six
# elements every other app has, written out by hand — the shared stylesheet
# cannot be linked into a Streamlit page, but the markup contract can be met.
#
# target="_self" because Streamlit rewrites markdown links to open a new tab,
# and the way back to the hub is navigation, not a citation. Identity and sign
# out are left out until this app comes off Streamlit: they need a script, and
# Streamlit is the one place that is not worth fighting for them.
st.markdown(
    '<header class="ds-bar" style="display:flex;align-items:center;gap:8px;'
    'font-family:var(--font-mono);font-size:11.5px;letter-spacing:.04em">'
    '<a href="https://devondoes.dev/" target="_self" '
    'style="display:inline-flex;align-items:center;gap:7px;'
    'color:var(--color-text-muted);text-decoration:none">'
    '<span style="width:6px;height:6px;border-radius:50%;'
    'background:var(--color-accent);display:inline-block"></span>devondoes.dev</a>'
    '<span style="color:var(--color-text-faint)">/</span>'
    '<span style="color:var(--color-text)">matinee</span>'
    '</header>',
    unsafe_allow_html=True,
)
st.title("Matinee")

STARS = {None: "—", 1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★"}

def confidence_color(c: int) -> str:
    if c >= 90: return "🟢"
    if c >= 70: return "🟡"
    if c >= 50: return "🟠"
    return "🔴"

RATING_OPTS = ["No rating", "★☆☆☆☆ (1)", "★★☆☆☆ (2)", "★★★☆☆ (3)", "★★★★☆ (4)", "★★★★★ (5)"]


def _trigger(label: str, flag_key: str, btn_key: str) -> None:
    """Render a button that reveals an inline panel (guarded by flag_key)."""
    if not st.session_state.get(flag_key):
        if st.button(label, key=btn_key):
            st.session_state[flag_key] = True
            st.rerun()


def watched_trigger(uid: str) -> None:
    _trigger("✓ Mark watched", f"watch_{uid}", f"mw_{uid}")


def watched_body(uid: str, title: str, mtype: str, genre, year) -> bool:
    """Inline 'watched' panel with an optional rating. On save, writes the title
    into the Library (media) so it shapes future recs. Returns True once saved."""
    flag = f"watch_{uid}"
    if not st.session_state.get(flag):
        return False
    with st.container(border=True):
        st.caption(f"Watched **{title}** — rate it now, or log it without a rating.")
        liked = st.radio("Liked?", ["Yes", "No"], horizontal=True, key=f"wl_{uid}")
        rating_sel = st.select_slider("Rating", options=RATING_OPTS, value="No rating", key=f"wr_{uid}")
        rating_val = None if rating_sel == "No rating" else RATING_OPTS.index(rating_sel)
        c1, c2, c3 = st.columns(3)
        do_save   = c1.button("Save to Library", type="primary", key=f"ws_{uid}")
        do_norate = c2.button("Watched, no rating", key=f"wn_{uid}")
        do_cancel = c3.button("Cancel", key=f"wc_{uid}")

    def _close():
        for k in (flag, f"wl_{uid}", f"wr_{uid}"):
            st.session_state.pop(k, None)

    if do_cancel:
        _close()
        st.rerun()
    if do_save or do_norate:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO media (title, type, genre, year, liked, rating, notes) VALUES (?,?,?,?,?,?,?)",
                (title, mtype or "movie", genre or None,
                 int(year) if year else None, int(liked == "Yes"),
                 (None if do_norate else rating_val), None),
            )
        _close()
        return True
    return False


def exclude_trigger(uid: str) -> None:
    _trigger("🚫 Exclude", f"excl_{uid}", f"ex_{uid}")


def exclude_body(uid: str, title: str, year, mtype) -> bool:
    """Inline exclude-with-reason panel. On confirm, writes to the exclusions
    table (fed into future recommendation prompts). Returns True once saved."""
    flag = f"excl_{uid}"
    if not st.session_state.get(flag):
        return False
    with st.container(border=True):
        reason = st.text_input(
            "Why exclude? (used to steer future recommendations away from this)",
            key=f"exr_{uid}",
            placeholder="e.g. too gory · not into this franchise · already seen it elsewhere",
        )
        c1, c2 = st.columns(2)
        do_conf = c1.button("Confirm exclude", type="primary", key=f"exc_{uid}")
        do_cxl  = c2.button("Cancel", key=f"exx_{uid}")

    def _close():
        for k in (flag, f"exr_{uid}"):
            st.session_state.pop(k, None)

    if do_cxl:
        _close()
        st.rerun()
    if do_conf:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO exclusions (title, year, type, reason) VALUES (?,?,?,?)",
                (title, int(year) if year else None, mtype, reason.strip() or None),
            )
        _close()
        return True
    return False


def _fetch_tmdb(mid: int, title: str) -> None:
    """Callback: look up the title on TMDB, stash the top hit for review."""
    hits = tmdb_client.search(title)
    st.session_state[f"tmdb_fetch_{mid}"] = hits[0] if hits else {}


def _apply_tmdb(mid: int) -> None:
    """Callback: write the stashed TMDB hit onto the library row. Runs before the
    rerun, so it fires reliably even though the button lives inside an expander."""
    hit = st.session_state.get(f"tmdb_fetch_{mid}")
    if hit:
        with get_conn() as conn:
            conn.execute(
                "UPDATE media SET title=?, genre=?, year=?, type=? WHERE id=?",
                (hit["title"], hit.get("genre"), hit.get("year"), hit.get("type"), mid),
            )
    st.session_state[f"tmdb_fetch_{mid}"] = None


tab_rec, tab_saved, tab_lib, tab_excluded, tab_platforms, tab_settings = st.tabs(
    ["Get Recommendations", "Watchlist", "Library", "Excluded", "Platforms", "Settings"]
)

# ── Recommendations ──────────────────────────────────────────────────────────
with tab_rec:
    st.header("Get Recommendations")

    if "rec_results" not in st.session_state:
        st.session_state.rec_results = []

    # ── Configure, then generate ──────────────────────────────────────────────
    TYPE_CHOICES = {"Movies & shows": "both", "Movies only": "movie", "Shows only": "show"}
    with st.container(border=True):
        st.caption(
            "Suggestions never include titles you've excluded, already watched, or "
            "saved to your watchlist — and every one is verified as available on an "
            "active platform before it's shown."
        )
        cfg1, cfg2 = st.columns([2, 1])
        type_label = cfg1.radio("Show me", list(TYPE_CHOICES), horizontal=True, key="rec_type")
        media_type = TYPE_CHOICES[type_label]
        n = cfg2.slider("How many?", 1, 10, 5, key="rec_n")

        with get_conn() as conn:
            active_plats = [
                r["name"] for r in conn.execute(
                    "SELECT name FROM platforms WHERE active = 1 ORDER BY name"
                ).fetchall()
            ]
        if active_plats:
            st.caption("Filtering to your active platforms: " + ", ".join(active_plats))
        else:
            st.warning("No active platforms — turn some on in the Platforms tab, or suggestions won't be platform-filtered.")

        if st.button("Generate", type="primary", use_container_width=True):
            with st.spinner("Thinking… (verifying availability on your platforms)"):
                ctx = build_context()
                st.session_state.rec_results = get_recommendations(ctx, n, media_type=media_type)
            if not st.session_state.rec_results:
                st.warning(
                    "Nothing matched your filters and active platforms. Try widening "
                    "the type, enabling more platforms, or generating again."
                )

    recs = st.session_state.rec_results
    if recs:
        # Live guard: re-apply the hard filters on every rerun so a card vanishes
        # the moment it's excluded, watched, or added to the watchlist.
        live_ctx = build_context()
        visible = filter_recommendations(recs, live_ctx, "both")
        if len(visible) != len(recs):
            st.session_state.rec_results = visible
            recs = visible

        for i, rec in enumerate(recs):
            flag = rec.get("sensitivity_flag", False)
            prefix = "⚠️ " if flag else ""
            conf = rec.get("confidence", 0)
            dot = confidence_color(conf)

            with st.container(border=True):
                col_title, col_conf = st.columns([5, 1])
                col_title.markdown(f"### {prefix}{rec['title']} ({rec.get('year', '?')})")
                col_conf.metric("Confidence", f"{dot} {conf}%")

                cols = st.columns(3)
                cols[0].markdown(f"**Platform:** {rec.get('platform', '—')}")
                cols[1].markdown(f"**Type:** {rec.get('type', '—').capitalize()}")
                cols[2].markdown(f"**Vibe:** _{rec.get('vibe_match', '—')}_")

                cast = rec.get("cast", [])
                if cast:
                    st.markdown(f"**Cast:** {', '.join(cast)}")

                st.markdown(f"**Overview:** {rec.get('overview', '')}")
                st.markdown(f"**Why it fits:** {rec.get('reason', '')}")

                if flag and rec.get("sensitivity_note"):
                    st.warning(f"Sensitivity note: {rec['sensitivity_note']}")

                b1, b2, b3 = st.columns(3)
                with b1:
                    if st.button("＋ Add to watchlist", key=f"save_rec_{i}"):
                        with st.spinner("Fetching cast from TMDB…"):
                            tmdb_cast = tmdb_client.get_cast_by_title(
                                rec["title"], rec.get("year"), rec.get("type", "movie")
                            )
                        cast_to_store = tmdb_cast if tmdb_cast else rec.get("cast", [])
                        with get_conn() as conn:
                            conn.execute(
                                """INSERT INTO saved_recommendations
                                   (title, year, type, platform, overview, cast_list,
                                    vibe_match, reason, confidence, sensitivity_flag, sensitivity_note)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    rec["title"], rec.get("year"), rec.get("type"),
                                    rec.get("platform"), rec.get("overview"),
                                    json.dumps(cast_to_store),
                                    rec.get("vibe_match"), rec.get("reason"),
                                    conf, int(flag),
                                    rec.get("sensitivity_note") or "",
                                ),
                            )
                        st.success(f"Saved: {rec['title']}")
                        st.rerun()
                with b2:
                    watched_trigger(f"rec{i}")
                with b3:
                    exclude_trigger(f"rec{i}")

                if watched_body(f"rec{i}", rec["title"], rec.get("type", "movie"), None, rec.get("year")):
                    st.success(f"Added to Library: {rec['title']}")
                    st.rerun()
                if exclude_body(f"rec{i}", rec["title"], rec.get("year"), rec.get("type", "movie")):
                    st.success(f"Excluded: {rec['title']}")
                    st.rerun()

# ── Watchlist ─────────────────────────────────────────────────────────────────
with tab_saved:
    st.header("Watchlist")
    st.caption("Titles you've saved to watch. Mark one watched to move it into your "
               "Library (with an optional rating); exclude one to keep it out of future recommendations.")

    with get_conn() as conn:
        pending = conn.execute(
            "SELECT * FROM saved_recommendations WHERE status='pending' "
            "ORDER BY confidence DESC, date_saved DESC"
        ).fetchall()

    if not pending:
        st.info("Your watchlist is empty. Add titles from the Get Recommendations tab.")
    else:
        col_regen, _ = st.columns([2, 5])
        if col_regen.button("Regenerate confidence scores", type="secondary"):
            with st.spinner("Re-scoring…"):
                ctx = build_context()
                scores = rescore_saved([dict(r) for r in pending], ctx)
            score_map = {s["title"]: s for s in scores}
            with get_conn() as conn:
                for row in pending:
                    if row["title"] in score_map:
                        s = score_map[row["title"]]
                        conn.execute(
                            "UPDATE saved_recommendations SET confidence=?, reason=? WHERE id=?",
                            (s["confidence"], s["reason"], row["id"]),
                        )
            st.success("Scores updated.")
            st.rerun()

        st.divider()
        for row in pending:
            conf = row["confidence"] or 0
            dot = confidence_color(conf)
            flag = row["sensitivity_flag"]
            prefix = "⚠️ " if flag else ""
            uid = f"save{row['id']}"

            with st.container(border=True):
                col_t, col_c = st.columns([5, 1])
                col_t.markdown(f"### {prefix}{row['title']} ({row['year'] or '?'})")
                col_c.metric("Confidence", f"{dot} {conf}%")

                cols = st.columns(3)
                cols[0].markdown(f"**Platform:** {row['platform'] or '—'}")
                cols[1].markdown(f"**Type:** {(row['type'] or '').capitalize()}")
                cols[2].markdown(f"**Vibe:** _{row['vibe_match'] or '—'}_")

                cast = json.loads(row["cast_list"]) if row["cast_list"] else []
                if cast:
                    st.markdown(f"**Cast:** {', '.join(cast)}")
                if row["overview"]:
                    st.markdown(f"**Overview:** {row['overview']}")
                if row["reason"]:
                    st.markdown(f"**Why it fits:** {row['reason']}")
                if flag and row["sensitivity_note"]:
                    st.warning(f"Sensitivity note: {row['sensitivity_note']}")

                col_w, col_e, col_d = st.columns(3)
                with col_w:
                    watched_trigger(uid)
                with col_e:
                    exclude_trigger(uid)
                with col_d:
                    if st.button("Remove", key=f"del_saved_{row['id']}"):
                        with get_conn() as conn:
                            conn.execute("DELETE FROM saved_recommendations WHERE id=?", (row["id"],))
                        st.rerun()

                if watched_body(uid, row["title"], row["type"] or "movie", None, row["year"]):
                    with get_conn() as conn:
                        conn.execute("DELETE FROM saved_recommendations WHERE id=?", (row["id"],))
                    st.success(f"Moved to Library: {row['title']}")
                    st.rerun()
                if exclude_body(uid, row["title"], row["year"], row["type"]):
                    with get_conn() as conn:
                        conn.execute("DELETE FROM saved_recommendations WHERE id=?", (row["id"],))
                    st.success(f"Excluded: {row['title']}")
                    st.rerun()

# ── Excluded ──────────────────────────────────────────────────────────────────
with tab_excluded:
    st.header("Excluded")
    st.caption("Titles the recommender will never suggest again. Your reason is fed into "
               "future requests so it also steers away from similar picks.")

    with get_conn() as conn:
        excluded = conn.execute(
            "SELECT * FROM exclusions ORDER BY date_added DESC, id DESC"
        ).fetchall()

    if not excluded:
        st.info("Nothing excluded yet. Use 🚫 Exclude on a recommendation or watchlist item.")
    else:
        for row in excluded:
            with st.container(border=True):
                c1, c2 = st.columns([6, 1])
                yr = f" ({row['year']})" if row["year"] else ""
                reason = f" — _{row['reason']}_" if row["reason"] else ""
                c1.markdown(f"**{row['title']}**{yr}{reason}")
                if c2.button("Un-exclude", key=f"unexcl_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute("DELETE FROM exclusions WHERE id=?", (row["id"],))
                    st.rerun()

    st.divider()
    st.subheader("Add an exclusion manually")
    with st.form("add_exclusion"):
        c1, c2 = st.columns([3, 1])
        ex_title = c1.text_input("Title")
        ex_year  = c2.number_input("Year", min_value=1900, max_value=2030, value=2024, step=1)
        ex_reason = st.text_input("Reason (optional)", placeholder="why avoid this / similar titles")
        if st.form_submit_button("Add exclusion") and ex_title.strip():
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO exclusions (title, year, reason) VALUES (?,?,?)",
                    (ex_title.strip(), int(ex_year), ex_reason.strip() or None),
                )
            st.rerun()

# ── Library ──────────────────────────────────────────────────────────────────
with tab_lib:
    st.header("My Library")

    for key, default in [("tmdb_results", []), ("tmdb_selected", None), ("last_query", "")]:
        if key not in st.session_state:
            st.session_state[key] = default

    with st.expander("Add new entry", expanded=False):
        rating_opts = ["No rating", "★☆☆☆☆ (1)", "★★☆☆☆ (2)", "★★★☆☆ (3)", "★★★★☆ (4)", "★★★★★ (5)"]

        # Form state — held in session_state so TMDB search can prefill the
        # fields. (A widget's value= is ignored once its key already exists, so
        # prefilling has to write session_state before the widget is drawn.)
        for k, d in [("add_title", ""), ("add_type", "movie"), ("add_genre", ""), ("add_year", 2024)]:
            st.session_state.setdefault(k, d)

        st.caption("Type the details in yourself, or search TMDB below to auto-fill them.")

        # ── Optional TMDB search to prefill the fields ──
        # In a form so pressing Enter in the box submits the search.
        with st.form("tmdb_search_form"):
            col_q, col_btn = st.columns([4, 1])
            col_q.text_input("Search TMDB", key="add_query", label_visibility="collapsed",
                             placeholder="Search TMDB… e.g. Knives Out — then press Enter")
            do_search = col_btn.form_submit_button("Search")
        if do_search:
            q = st.session_state.add_query.strip()
            with st.spinner("Searching…"):
                st.session_state.tmdb_results = tmdb_client.search(q) if q else []
            st.session_state.pop("add_pick", None)

        results = st.session_state.tmdb_results
        if results:
            opts = [
                f"{r['title']} ({r['year'] or '?'}) · {r['type']} · {r['genre'] or 'unknown genre'}"
                for r in results
            ]
            if st.session_state.get("add_pick", 0) >= len(opts):
                st.session_state.add_pick = 0
            pick = st.radio("Which one?", list(range(len(opts))),
                            format_func=lambda i: opts[i], key="add_pick")
            sel = results[pick]
            if sel.get("overview"):
                st.caption(sel["overview"])
            if st.button("⬇ Use this — fill the fields below"):
                st.session_state.add_title = sel["title"]
                st.session_state.add_type  = sel["type"]
                st.session_state.add_genre = sel["genre"] or ""
                st.session_state.add_year  = max(1900, min(2030, int(sel["year"] or 2024)))
                st.session_state.tmdb_results = []
                st.session_state.pop("add_pick", None)
                st.rerun()

        st.divider()

        # ── The entry itself — always editable, whether typed or prefilled ──
        col1, col2 = st.columns(2)
        col1.text_input("Title", key="add_title", placeholder="Required")
        col2.selectbox("Type", ["movie", "show"], key="add_type")
        col3, col4 = st.columns(2)
        col3.text_input("Genre", key="add_genre")
        col4.number_input("Year", min_value=1900, max_value=2030, step=1, key="add_year")
        liked_sel  = st.radio("Liked?", ["Yes", "No"], horizontal=True, key="add_liked")
        rating_sel = st.select_slider("Rating", options=rating_opts, value="No rating", key="add_rating")
        rating_val = None if rating_sel == "No rating" else rating_opts.index(rating_sel)
        notes = st.text_area("Notes (optional)", key="add_notes")

        if st.button("Add to library", type="primary", key="btn_add"):
            title = st.session_state.add_title.strip()
            if not title:
                st.warning("Enter a title first — type one in, or search TMDB and click “Use this”.")
            else:
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO media (title, type, genre, year, liked, rating, notes) VALUES (?,?,?,?,?,?,?)",
                        (title, st.session_state.add_type, st.session_state.add_genre or None,
                         int(st.session_state.add_year), int(liked_sel == "Yes"), rating_val, notes or None),
                    )
                st.success(f"Added: {title}")
                for k in ("add_title", "add_type", "add_genre", "add_year", "add_query",
                          "add_notes", "add_rating", "add_liked", "add_pick", "tmdb_results"):
                    st.session_state.pop(k, None)
                st.rerun()

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM media ORDER BY liked DESC, rating DESC, date_added DESC"
        ).fetchall()

    if rows:
        filter_type = st.selectbox("Filter", ["All", "Movies", "Shows", "Liked", "Disliked", "Unrated"])
        filtered = [r for r in rows if (
            filter_type == "All"
            or (filter_type == "Movies"   and r["type"] == "movie")
            or (filter_type == "Shows"    and r["type"] == "show")
            or (filter_type == "Liked"    and r["liked"])
            or (filter_type == "Disliked" and not r["liked"])
            or (filter_type == "Unrated"  and r["rating"] is None)
        )]

        for row in filtered:
            liked_icon = "👍" if row["liked"] else "👎"
            star_str   = STARS.get(row["rating"], "—")
            label = f"{liked_icon} {star_str} **{row['title']}** ({row['type']}, {row['year'] or '?'}) — {row['genre'] or 'no genre'}"
            with st.expander(label):
                if row["notes"]:
                    st.write(f"Notes: {row['notes']}")

                rating_opts_inner = ["No rating", "★☆☆☆☆ (1)", "★★☆☆☆ (2)", "★★★☆☆ (3)", "★★★★☆ (4)", "★★★★★ (5)"]
                current_idx = 0 if row["rating"] is None else row["rating"]
                new_rating_sel = st.select_slider(
                    "Update rating", options=rating_opts_inner,
                    value=rating_opts_inner[current_idx], key=f"rate_{row['id']}"
                )
                new_rating_val = None if new_rating_sel == "No rating" else rating_opts_inner.index(new_rating_sel)
                if st.button("Save rating", key=f"save_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute("UPDATE media SET rating = ? WHERE id = ?", (new_rating_val, row["id"]))
                    st.rerun()

                fetch_key = f"tmdb_fetch_{row['id']}"

                col_fetch, col_del = st.columns(2)
                col_fetch.button("Fetch from TMDB", key=f"fetch_{row['id']}",
                                 on_click=_fetch_tmdb, args=(row["id"], row["title"]))
                if col_del.button("Remove", key=f"del_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute("DELETE FROM media WHERE id = ?", (row["id"],))
                    st.rerun()

                fetched = st.session_state.get(fetch_key)
                if fetched:
                    st.info(
                        f"**{fetched['title']}** ({fetched['year']}) — {fetched['genre'] or 'no genre'}  \n"
                        f"_{fetched.get('overview', '')}_"
                    )
                    st.button("Apply this info", key=f"apply_{row['id']}",
                              on_click=_apply_tmdb, args=(row["id"],))
                elif fetched == {}:
                    st.warning("No TMDB match found.")
    else:
        st.info("No entries yet.")

# ── Platforms ─────────────────────────────────────────────────────────────────
with tab_platforms:
    st.header("Streaming Platforms")

    with get_conn() as conn:
        platforms = conn.execute("SELECT * FROM platforms ORDER BY active DESC, name").fetchall()

    for p in platforms:
        col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
        col1.write(f"**{p['name']}**")
        col2.write(f"${p['monthly_cost']:.2f}/mo" if p['monthly_cost'] else "—")
        col3.write("Rents" if p["can_rent"] else "Streams only")
        active = col4.toggle("Active", value=bool(p["active"]), key=f"plat_{p['id']}")
        if active != bool(p["active"]):
            with get_conn() as conn:
                conn.execute("UPDATE platforms SET active = ? WHERE id = ?", (int(active), p["id"]))
            st.rerun()

    st.divider()
    st.subheader("Add platform")
    with st.form("add_platform"):
        col1, col2, col3, col4 = st.columns(4)
        pname = col1.text_input("Name")
        pcost = col2.number_input("Monthly cost", min_value=0.0, value=0.0, step=0.01)
        prent = col3.checkbox("Can rent?")
        pactive = col4.checkbox("Active?", value=True)
        if st.form_submit_button("Add") and pname:
            with get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO platforms (name, active, monthly_cost, can_rent) VALUES (?,?,?,?)",
                    (pname, int(pactive), pcost, int(prent)),
                )
            st.rerun()

# ── Settings ──────────────────────────────────────────────────────────────────
with tab_settings:
    st.header("Settings")

    with get_conn() as conn:
        rent_row = conn.execute("SELECT value FROM settings WHERE key = 'rent_budget'").fetchone()
        sens_row = conn.execute("SELECT value FROM settings WHERE key = 'sensitivities'").fetchone()
    rent_budget   = float(rent_row["value"]) if rent_row else 5.0
    sensitivities = sens_row["value"] if sens_row else ""

    new_budget = st.number_input(
        "Max rental budget per title ($)",
        min_value=0.0, max_value=50.0, value=rent_budget, step=0.50,
    )
    st.subheader("Content sensitivities")
    st.caption(
        "Recommendations still appear for these — they get a ⚠️ warning instead of being excluded. "
        "One per line."
    )
    new_sens = st.text_area(
        "Sensitivities", value=sensitivities, height=100,
        placeholder="excessive gore\nstrong language\ndark/disturbing themes\n..."
    )
    if st.button("Save settings", type="primary"):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO settings (key,value) VALUES ('rent_budget',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_budget),),
            )
            conn.execute(
                "INSERT INTO settings (key,value) VALUES ('sensitivities',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (new_sens.strip(),),
            )
        st.success("Saved.")
