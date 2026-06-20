import json
import streamlit as st
from db import get_conn, init_db
from recommend import build_context, get_recommendations, rescore_saved
import tmdb as tmdb_client

init_db()

st.set_page_config(page_title="Movie Tracker", page_icon="🎬", layout="wide")
st.title("Movie & Show Tracker")

STARS = {None: "—", 1: "★☆☆☆☆", 2: "★★☆☆☆", 3: "★★★☆☆", 4: "★★★★☆", 5: "★★★★★"}

def confidence_color(c: int) -> str:
    if c >= 90: return "🟢"
    if c >= 70: return "🟡"
    if c >= 50: return "🟠"
    return "🔴"

tab_rec, tab_saved, tab_lib, tab_platforms, tab_settings = st.tabs(
    ["Get Recommendations", "Saved List", "Library", "Platforms", "Settings"]
)

# ── Recommendations ──────────────────────────────────────────────────────────
with tab_rec:
    st.header("Get Recommendations")

    if "rec_results" not in st.session_state:
        st.session_state.rec_results = []

    col_n, col_btn = st.columns([3, 1])
    n = col_n.slider("How many?", 1, 10, 5, label_visibility="collapsed")
    col_n.caption(f"Generate {n} recommendations")

    if col_btn.button("Generate", type="primary"):
        with st.spinner("Thinking…"):
            ctx = build_context()
            st.session_state.rec_results = get_recommendations(ctx, n)

    recs = st.session_state.rec_results
    if recs:
        # Check which titles are already saved
        with get_conn() as conn:
            saved_titles = {r["title"] for r in conn.execute(
                "SELECT title FROM saved_recommendations WHERE status = 'pending'"
            ).fetchall()}

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

                already_saved = rec["title"] in saved_titles
                if already_saved:
                    st.caption("✓ Already in saved list")
                else:
                    if st.button("Save to list", key=f"save_rec_{i}"):
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

# ── Saved List ────────────────────────────────────────────────────────────────
with tab_saved:
    st.header("Saved List")

    with get_conn() as conn:
        saved = conn.execute(
            "SELECT * FROM saved_recommendations ORDER BY confidence DESC, date_saved DESC"
        ).fetchall()

    pending = [r for r in saved if r["status"] == "pending"]
    done    = [r for r in saved if r["status"] != "pending"]

    if pending:
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

                col_w, col_s, col_d = st.columns(3)
                if col_w.button("Mark watched", key=f"watched_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE saved_recommendations SET status='watched' WHERE id=?",
                            (row["id"],),
                        )
                    st.rerun()
                if col_s.button("Skip", key=f"skip_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE saved_recommendations SET status='skipped' WHERE id=?",
                            (row["id"],),
                        )
                    st.rerun()
                if col_d.button("Remove", key=f"del_saved_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute("DELETE FROM saved_recommendations WHERE id=?", (row["id"],))
                    st.rerun()
    else:
        st.info("No saved recommendations yet. Generate some on the Get Recommendations tab.")

    if done:
        with st.expander(f"Watched / Skipped ({len(done)})"):
            for row in done:
                st.markdown(f"- **{row['title']}** — _{row['status']}_")
                col_restore, col_remove = st.columns([1, 4])
                if col_restore.button("Restore", key=f"restore_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE saved_recommendations SET status='pending' WHERE id=?",
                            (row["id"],),
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

        st.markdown("**Step 1 — Search TMDB**")
        col_q, col_btn = st.columns([4, 1])
        query = col_q.text_input("Title", label_visibility="collapsed", placeholder="e.g. Knives Out")
        if col_btn.button("Search", type="primary"):
            if query.strip():
                with st.spinner("Searching…"):
                    st.session_state.tmdb_results = tmdb_client.search(query.strip())
                st.session_state.tmdb_selected = None
                st.session_state.last_query = query.strip()

        if st.session_state.tmdb_results:
            st.markdown("**Step 2 — Which one?**")
            options = [
                f"{r['title']} ({r['year'] or '?'}) — {r['type']} — {r['genre'] or 'unknown genre'}"
                for r in st.session_state.tmdb_results
            ] + ["None of these — enter manually"]
            choice = st.radio("Results", options, label_visibility="collapsed")
            choice_idx = options.index(choice)
            if choice_idx < len(st.session_state.tmdb_results):
                sel = st.session_state.tmdb_results[choice_idx]
                if sel.get("overview"):
                    st.caption(sel["overview"])
                st.session_state.tmdb_selected = sel
            else:
                st.session_state.tmdb_selected = None

        if st.session_state.tmdb_results:
            st.markdown("**Step 3 — Confirm & add**")

        sel = st.session_state.tmdb_selected
        f_title = sel["title"] if sel else (st.session_state.last_query or "")
        f_type  = sel["type"]  if sel else "movie"
        f_genre = (sel["genre"] or "") if sel else ""
        f_year  = (sel["year"] or 2024) if sel else 2024

        col1, col2 = st.columns(2)
        inp_title = col1.text_input("Title", value=f_title, key="inp_title")
        inp_type  = col2.selectbox("Type", ["movie", "show"],
                                   index=0 if f_type == "movie" else 1, key="inp_type")
        col3, col4 = st.columns(2)
        inp_genre = col3.text_input("Genre", value=f_genre, key="inp_genre")
        inp_year  = col4.number_input("Year", min_value=1900, max_value=2030,
                                      value=int(f_year), step=1, key="inp_year")
        liked_sel  = st.radio("Liked?", ["Yes", "No"], horizontal=True)
        rating_sel = st.select_slider("Rating", options=rating_opts, value="No rating", key="inp_rating")
        rating_val = None if rating_sel == "No rating" else rating_opts.index(rating_sel)
        notes = st.text_area("Notes (optional)", key="inp_notes")

        if st.button("Add to library", type="primary", key="btn_add"):
            if inp_title.strip():
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO media (title, type, genre, year, liked, rating, notes) VALUES (?,?,?,?,?,?,?)",
                        (inp_title.strip(), inp_type, inp_genre or None, int(inp_year),
                         int(liked_sel == "Yes"), rating_val, notes or None),
                    )
                st.success(f"Added: {inp_title.strip()}")
                for k in ("tmdb_results", "tmdb_selected", "last_query"):
                    st.session_state[k] = [] if k == "tmdb_results" else None if k == "tmdb_selected" else ""
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
                if fetch_key not in st.session_state:
                    st.session_state[fetch_key] = None

                col_fetch, col_del = st.columns(2)
                if col_fetch.button("Fetch from TMDB", key=f"fetch_{row['id']}"):
                    with st.spinner("Looking up…"):
                        hits = tmdb_client.search(row["title"])
                    st.session_state[fetch_key] = hits[0] if hits else {}

                fetched = st.session_state.get(fetch_key)
                if fetched is not None:
                    if fetched:
                        st.info(
                            f"**{fetched['title']}** ({fetched['year']}) — {fetched['genre'] or 'no genre'}  \n"
                            f"_{fetched.get('overview', '')}_"
                        )
                        if st.button("Apply this info", key=f"apply_{row['id']}"):
                            with get_conn() as conn:
                                conn.execute(
                                    "UPDATE media SET title=?, genre=?, year=?, type=? WHERE id=?",
                                    (fetched["title"], fetched["genre"], fetched["year"],
                                     fetched["type"], row["id"]),
                                )
                            st.session_state[fetch_key] = None
                            st.rerun()
                    else:
                        st.warning("No TMDB match found.")

                if col_del.button("Remove", key=f"del_{row['id']}"):
                    with get_conn() as conn:
                        conn.execute("DELETE FROM media WHERE id = ?", (row["id"],))
                    st.rerun()
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
