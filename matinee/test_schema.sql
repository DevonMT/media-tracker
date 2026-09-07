-- What the shape must guarantee. Throwaway Postgres only.
CREATE FUNCTION expect(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF got IS NOT DISTINCT FROM want THEN RAISE NOTICE '  PASS  %', label;
  ELSE RAISE EXCEPTION 'FAIL  % : got % want %', label, got, want; END IF;
END $$;

INSERT INTO "user" VALUES ('devon','d@x','Devon'), ('gran','g@x','Gran'), ('sam','s@x','Sam');
INSERT INTO title (id,tmdb_id,kind,name,year) VALUES
  ('t1', 1234, 'movie','The Thing',1982),
  ('t2', 5678, 'movie','The Thing',2011),
  ('t3', NULL, 'show','Home Video Nobody Catalogued',1998);

-- 1. TWO PEOPLE, ONE TITLE, TWO OPINIONS. The thing the old shape could not do.
INSERT INTO review (user_id,title_id,rating) VALUES ('devon','t1',5), ('gran','t1',2);
SELECT expect('two people hold different opinions of one title',
  (SELECT count(*)::text FROM review WHERE title_id='t1'), '2');

-- 2. One person, one opinion. Changing your mind updates; it does not append.
INSERT INTO review (user_id,title_id,rating) VALUES ('devon','t1',3)
  ON CONFLICT (user_id,title_id) DO UPDATE SET rating=EXCLUDED.rating;
SELECT expect('a second verdict replaces the first',
  (SELECT rating::text FROM review WHERE user_id='devon' AND title_id='t1'), '3');
SELECT expect('...and did not create a row',
  (SELECT count(*)::text FROM review WHERE title_id='t1'), '2');

-- 3. Same name, different film. Identity is the tmdb id, not the name.
SELECT expect('two films called The Thing are two titles',
  (SELECT count(*)::text FROM title WHERE name='The Thing'), '2');
DO $$
BEGIN
  INSERT INTO title (id,tmdb_id,kind,name) VALUES ('dupe',1234,'movie','The Thing');
  RAISE EXCEPTION 'FAIL  a duplicate tmdb_id was accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE '  PASS  one row per tmdb id';
END $$;

-- 4. A title with no TMDB match is allowed, and more than one of them.
INSERT INTO title (id,tmdb_id,kind,name) VALUES ('t4',NULL,'show','Another Unmatched');
SELECT expect('unmatched titles are carried, not collapsed',
  (SELECT count(*)::text FROM title WHERE tmdb_id IS NULL), '2');

-- 5. Sharing is one-way and cannot point at yourself.
INSERT INTO taste_share (owner_id,viewer_id) VALUES ('gran','devon');
SELECT expect('gran shares with devon',
  (SELECT visibility FROM taste_share WHERE owner_id='gran' AND viewer_id='devon'), 'blend');
SELECT expect('...which does not make devon share back',
  (SELECT count(*)::text FROM taste_share WHERE owner_id='devon' AND viewer_id='gran'), '0');
DO $$
BEGIN
  INSERT INTO taste_share (owner_id,viewer_id) VALUES ('devon','devon');
  RAISE EXCEPTION 'FAIL  a self-share was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE '  PASS  you cannot share with yourself';
END $$;

-- 6. Blend is the DEFAULT. Opening your reviews is a thing you turn on.
SELECT expect('the default is blend, not open',
  (SELECT visibility FROM taste_share WHERE owner_id='gran'), 'blend');

-- 7. Vetoes are per person, and dismissals are too — the old app had one
--    global exclusions list, which cannot say "Devon has seen it, Gran has not".
INSERT INTO taste_rule VALUES ('gran','never_genre','Horror');
INSERT INTO title_dismissed (user_id,title_id) VALUES ('devon','t2');
SELECT expect('a dismissal belongs to one person',
  (SELECT count(*)::text FROM title_dismissed WHERE user_id='gran'), '0');

-- 8. Deleting a person takes their opinions, shares, rules and dismissals with
--    them, and leaves the titles alone — a title is not anybody's.
DELETE FROM "user" WHERE id='gran';
SELECT expect('deleting a person removes their reviews',
  (SELECT count(*)::text FROM review WHERE user_id='gran'), '0');
SELECT expect('...their shares',
  (SELECT count(*)::text FROM taste_share WHERE owner_id='gran'), '0');
SELECT expect('...and their vetoes',
  (SELECT count(*)::text FROM taste_rule WHERE user_id='gran'), '0');
SELECT expect('...but not the titles',
  (SELECT count(*)::text FROM title), '4');

-- 9. Ratings outside 1-5 are refused rather than stored and averaged.
DO $$
BEGIN
  INSERT INTO review (user_id,title_id,rating) VALUES ('sam','t1',9);
  RAISE EXCEPTION 'FAIL  a rating of 9 was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE '  PASS  ratings stay inside 1-5';
END $$;

-- 10. A suggestion records the FLOOR and who it belongs to, not just a mean.
INSERT INTO suggestion (id,name,floor_score,floor_user,scores,audience)
  VALUES ('s1','Alien',62,'devon','{"devon":62,"sam":88}','{devon,sam}');
SELECT expect('the floor is stored with the person it belongs to',
  (SELECT floor_user || ':' || floor_score FROM suggestion WHERE id='s1'), 'devon:62');
