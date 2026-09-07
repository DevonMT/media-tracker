-- Matinee: a title, and what each person thinks of it.
--
-- The old shape made a row in `media` a title AND somebody's opinion at once,
-- so there was exactly one opinion in the system and it belonged to nobody in
-- particular. The `who` column was the tell: it existed, it said 'both' on
-- every row, and nothing ever read it — because a text field on a row that
-- holds one opinion cannot express "Devon gave it 4, Gran gave it 2".
--
-- These tables live in the PLATFORM database, not the warehouse, because
-- reviews reference "user"(id) and Postgres has no cross-database foreign key.
-- It is also where Mise and Drill already keep their per-user state, so this
-- stops being the one app whose data sits somewhere else.

-- A title. One row per film or show, ever, for everybody.
CREATE TABLE IF NOT EXISTS title (
  id        text PRIMARY KEY,
  -- The identity. Two people typing "The Thing" must land on the same row or
  -- nothing blends, and there are two films called that. NULL is allowed and
  -- means "no TMDB match" — carried as a local title rather than silently
  -- becoming a second copy of something.
  tmdb_id   int UNIQUE,
  kind      text NOT NULL CHECK (kind IN ('movie','show')),
  name      text NOT NULL,
  year      int,
  genres    text[] NOT NULL DEFAULT '{}',
  runtime   int,
  overview  text,
  poster    text,
  added_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS title_name ON title (lower(name));

-- What ONE PERSON thinks of it. The thing that did not exist.
CREATE TABLE IF NOT EXISTS review (
  user_id    text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  title_id   text NOT NULL REFERENCES title(id)  ON DELETE CASCADE,
  rating     int CHECK (rating BETWEEN 1 AND 5),
  liked      boolean,
  notes      text,
  watched_at date,
  -- 'joint' marks the reviews carried over from the old app, where every row
  -- said who='both'. They are Devon's for now because his is the only account,
  -- but they were never his alone, and flattening that to 'own' would invent a
  -- precision the data never had. It is also what lets a second person claim
  -- them later instead of starting from nothing.
  source     text NOT NULL DEFAULT 'own' CHECK (source IN ('own','joint')),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, title_id)
);
CREATE INDEX IF NOT EXISTS review_title ON review (title_id);

-- "You may count my reviews." Granted BY the person whose taste it is.
--
-- Not a platform group. A group says "these people may reach this app", which
-- is not "I want her opinion counted", and the day those two need to differ the
-- group would have to be split into two groups meaning different things.
CREATE TABLE IF NOT EXISTS taste_share (
  owner_id   text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  viewer_id  text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  -- 'blend': count me, do not show anyone my individual ratings.
  -- 'open':  they may also browse my reviews.
  visibility text NOT NULL DEFAULT 'blend' CHECK (visibility IN ('blend','open')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, viewer_id),
  CHECK (owner_id <> viewer_id)
);

-- Hard constraints, applied BEFORE scoring. A veto is not a low score: Gran not
-- liking horror is "no horror", however much Devon would enjoy it.
--
-- Applied as a union across whoever is in the blend — anybody's veto removes
-- the title — and never shown to the others as theirs. The recommendation
-- simply does not contain horror and does not explain why.
CREATE TABLE IF NOT EXISTS taste_rule (
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  kind    text NOT NULL CHECK (kind IN ('never_genre','never_keyword','max_runtime','min_year')),
  value   text NOT NULL,
  PRIMARY KEY (user_id, kind, value)
);

-- Seen-and-dismissed, per person. The old app had one global list, which
-- cannot express "Devon has seen it, Gran has not".
CREATE TABLE IF NOT EXISTS title_dismissed (
  user_id  text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  title_id text NOT NULL REFERENCES title(id) ON DELETE CASCADE,
  reason   text,
  at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, title_id)
);

-- Where you can watch things. Shared, not per-person: a subscription is a fact
-- about the household, not a taste.
CREATE TABLE IF NOT EXISTS watch_platform (
  id           text PRIMARY KEY,
  name         text NOT NULL UNIQUE,
  active       boolean NOT NULL DEFAULT true,
  monthly_cost numeric(8,2) NOT NULL DEFAULT 0,
  can_rent     boolean NOT NULL DEFAULT false
);

-- A recommendation somebody kept. Attributed, because "for Devon and Gran" is
-- part of what it was.
CREATE TABLE IF NOT EXISTS suggestion (
  id          text PRIMARY KEY,
  title_id    text REFERENCES title(id) ON DELETE SET NULL,
  name        text NOT NULL,
  year        int,
  kind        text,
  platform    text,
  overview    text,
  reason      text,
  -- The number that decides whether you put it on: the LOWEST per-person
  -- confidence in the blend, not the average.
  floor_score int,
  floor_user  text REFERENCES "user"(id) ON DELETE SET NULL,
  scores      jsonb NOT NULL DEFAULT '{}',
  audience    text[] NOT NULL DEFAULT '{}',
  requested_by text REFERENCES "user"(id) ON DELETE SET NULL,
  status      text NOT NULL DEFAULT 'pending',
  at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS matinee_setting (
  user_id text NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  key     text NOT NULL,
  value   text,
  PRIMARY KEY (user_id, key)
);
