# Taste Profile — Viewer Account
<!-- kind: taste -->
<!-- source: auto (subscriptions + watch-later + custom playlists) -->
<!-- tags: taste, viewer, preferences -->

(Empty skeleton. `python taste_profile.py --update` fills this page from YOUR
viewing account — subscriptions, playlists, watch history, watch-later keywords —
and `knowledge_base.py sync` indexes it so the topic picker reads it.
Everything below is generated on your machine and never leaves it.)

## Subscribed channels (strongest taste signal)

(none yet)

## Your playlists (curated interests)

(none yet)

## Top video channels (subscribed + watched)

(none yet)

## Recurring keywords

(none)

## Watch-later keywords (intentional)

(none)

## Notes

- No viewer account? Skip it: `daily_generate.sh` continues with KB + topics.txt
  (it logs `[SKIP] no viewer token ...` and keeps going).
- Cookie-only alternative (no Google Cloud Console): `python cookie_history.py
  --cookies kb/cookies/cookies.txt`.
