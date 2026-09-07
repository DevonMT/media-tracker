# Movie tracker, overhauled: whose taste counts

Status: **proposed, nothing built.** Written 2026-09-07 from Devon's notes.

---

## What is wrong today, in one sentence

A row in `media` is a title AND somebody's opinion of it at the same time, so
there is exactly one opinion in the system and it belongs to nobody in
particular.

```sql
CREATE TABLE media (
  title TEXT, type TEXT, genre TEXT, year INTEGER,
  liked INTEGER, rating INTEGER, notes TEXT,   -- an opinion
  who   TEXT DEFAULT 'both'                    -- declared, never read or written
);
```

That `who` column is the tell. The idea was already reaching for this and had
nowhere to put it — a text field on a row that can only hold one opinion cannot
express "Devon gave it 4, Gran gave it 2".

Everything below follows from separating the two.

---

## 1. The split

```sql
-- A title. One row per film or show, ever, for everybody.
CREATE TABLE title (
  id        text PRIMARY KEY,
  tmdb_id   int UNIQUE,          -- the identity; see below
  kind      text CHECK (kind IN ('movie','show')),
  name      text NOT NULL,
  year      int,
  genres    text[],
  runtime   int,
  overview  text,
  poster    text
);

-- What ONE PERSON thinks of it. The thing that did not exist.
CREATE TABLE review (
  user_id   text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  title_id  text NOT NULL REFERENCES title(id)  ON DELETE CASCADE,
  rating    int CHECK (rating BETWEEN 1 AND 5),
  liked     boolean,
  notes     text,
  watched_at date,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, title_id)
);
```

**TMDB id is the identity, not the name.** Two people typing "The Thing" must
land on the same row or nothing blends — and there are two films called that.
Anything without a TMDB match gets a local id and is flagged, rather than
silently becoming a second copy of something.

**The primary key is (user, title)**, so one person holds at most one opinion of
a title. Changing your mind updates a row; it does not append a second verdict.

---

## 2. Two questions that look like one

This is the part Devon flagged as unclear, and the answer is that there are
genuinely two questions and the platform already answers one of them.

**"May this person use the app at all?"** — a platform grant. Already built,
already audited, and delegated granting means Devon can hand someone the app
without making them an administrator. Nothing new is needed.

**"Whose taste counts in MY recommendation?"** — not an access question. Devon
might give his grandmother the app without wanting her taste folded into his
solo picks; and someone with no account has no reviews to fold in at all.

So: **the platform grant is a prerequisite; the in-app connection is the
feature.** Reusing groups for the second would be a category error — a group
says "these people may reach this app", which is not "I want her opinion
counted", and the day those two need to differ the group would have to be
split into two groups meaning different things.

```sql
-- "You may count my reviews." Granted BY the person whose taste it is.
CREATE TABLE taste_share (
  owner_id   text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  viewer_id  text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  visibility text NOT NULL DEFAULT 'blend'
               CHECK (visibility IN ('blend','open')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, viewer_id)
);
```

**One-way, not mutual.** Gran letting Devon count her taste does not oblige
Devon to let Gran count his. Mutual-by-construction is a social network's answer
to a different problem, and here it would mean you cannot plan a film for
somebody without handing them your viewing history.

---

## 3. The privacy idea worth building around

**You can be counted without being read.**

`visibility = 'blend'` — the default — means: include me in a recommendation,
and do not show anyone my individual ratings. The output is *"this suits you
both"*, never *"Gran gave Alien 2 stars"*.

`visibility = 'open'` means: they may also browse my reviews. For Devon and his
grandmother, who watch together and talk about it, that is probably what they
both want — but it is a thing you turn on, not the starting position.

This matters more than it sounds. It is what lets someone join a movie night
without publishing their taste, and it costs the feature nothing, because a
blend never needed to expose the inputs to produce the output. **A system that
can recommend for a group without revealing the group's ratings is strictly
better than one that cannot**, and building it the other way round is a door
that cannot be closed later.

**What "blend" leaks anyway, stated honestly:** a determined person could infer
something about your taste by running blends with and without you and diffing
the results. That is inherent to blending, not a flaw in the storage — the
defence is that the people in your circle are people you invited.

---

## 4. Recommending for several people

The interesting problem, and the one the current single-profile prompt cannot
express at all.

**Optimise the floor, not the mean.** A film everybody likes at 70 beats one
that two people love at 95 and one endures at 30. The mean prefers the second;
nobody wants to be the person who hated movie night.

So the model is asked for a **per-person confidence**, and candidates rank by
the **minimum** across the group, with the mean only as a tiebreak. The floor is
also what gets shown: *"weakest fit: Gran, 62"* is the number that decides
whether you put it on.

**Vetoes are not low scores.** Gran not liking horror is not "horror scores
lower for Gran" — it is *no horror*, however much Devon would enjoy it. Hard
constraints belong outside the scoring, applied as a filter before it, and each
person owns their own:

```sql
CREATE TABLE taste_rule (
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  kind    text NOT NULL CHECK (kind IN ('never_genre','never_keyword','max_runtime','min_year')),
  value   text NOT NULL,
  PRIMARY KEY (user_id, kind, value)
);
```

Applied as a union across whoever is in the blend: anybody's veto removes the
title. A rule is a person's own, and is never shown to the others as theirs —
the recommendation simply does not contain horror, and does not explain that
somebody vetoed it.

**Already-seen is per person and asymmetric.** A title Devon has seen and Gran
has not is a candidate for Gran, a rewatch for Devon, and for a blend it is a
judgement call — so it is a mode, not a rule: *exclude anything anyone has
seen* / *allow rewatches*. The current app's `exclusions` table collapses this
into one global answer.

---

## 5. What carries over from Mise, and what must not

**Carries over:**

- The **platform session and grant model** — no second login, no key to paste.
- The **variant idea**, if it earns it: a `full` tier with AI recommendations and
  a `lite` tier that is just the shared library and reviews. Same reason as
  Mise: the broker refuses the Claude Max path for an app reachable by
  non-admins, so anyone sharing this app has to think about who can spend money.
  **This one is not optional if Gran gets an account** — see §7.
- **Layout follows width, interaction follows pointer.** An iPad is 1180px and
  still a touch device.
- The **token system, both themes, and the toggle**; `useDialog` for modal
  semantics; `usePointer`.
- **Deterministic ids for anything the app seeds**, which is the bug that made
  Mise grow duplicate lists.

**Must NOT carry over: local-first sync.**

Mise is local-first because it is one person's data, needed offline, in a shop.
A shared review corpus is the opposite on every count: it is *other people's*
data, it must be server-authoritative because two people's reviews have to be
consistent for a blend to mean anything, and it is used on a sofa with wifi.
Porting Dexie and last-write-wins here would buy an offline mode nobody needs
and pay for it with merge semantics nobody can reason about.

**Server-authoritative, plain requests.** And off Streamlit, for the reason
finances went: it shipped 1156 KB to draw a table.

---

## 6. Shape of the thing

One screen carries the whole idea:

> **Recommend for:** `[Devon ✓] [Gran ✓] [+ someone]`

Toggling people re-runs the blend. Devon alone is the same feature with one
person selected, so "just me" and "me and Gran" are not two modes — which is
exactly how Devon described wanting to use it.

- **Phone:** the picker is a row of chips under the header; results are a single
  column of cards; the floor score sits on each card.
- **Desktop:** the picker moves to a sidebar with everyone in your circle
  listed; results become a multi-column grid — the same width/pointer split as
  Mise, and the sidebar is where Mise's list rail proved itself.

---

## 7. What has to be decided before building

1. **Does Gran get her own account?** The whole design assumes yes — reviews
   belong to a `user_id`. The alternative is "profiles" owned by Devon, which is
   simpler and privacy-free (he would hold her data) and cannot ever grow into
   someone reviewing from their own phone. **This is the load-bearing decision;
   everything else is downstream of it.**
2. **If yes: variants first.** A non-admin with access flips this app off the
   Max subscription and onto a metered key. That is exactly the trap already
   documented for Mise, and it fires the moment Gran is granted access.
3. **Migrating the existing library.** Today's rows become titles plus reviews
   attributed to *someone* — presumably Devon, since `who` was never populated.
   Rows with no TMDB match need a decision: match them by hand, or carry them as
   local titles.
4. **Does a blend need everyone present to have reviewed enough?** A person with
   four ratings will drag a blend toward noise. Minimum-reviews threshold, or
   weight by confidence, or say so in the UI.

## Order of work

1. Split `title` / `review`, migrate the existing library, keep today's
   single-user behaviour working.
2. Variants + grants, before anyone else has an account.
3. `taste_share` and the circle UI, default `blend`.
4. The floor-based multi-person recommendation and vetoes.
5. Off Streamlit, with the Mise design system.
