# AutoShorts v2 — Self-Improving YouTube Shorts Factory

Turn one topic into a **published, quality-gated YouTube Short** — automatically,
with **$0 per video**. Local LLM (llama.cpp) writes, cross-checks, and scores;
free edge-TTS narrates; FFmpeg (or optional LTX B-roll) builds; OAuth uploads.
The pipeline also **learns from its own history** — a knowledge base of what
worked feeds the next topic pick.

No per-video API fees. No subscription. Your hardware does the work.

---

## What's different in v2

| | v1 | **v2** |
|---|---|---|
| Script | 1 prompt, 1 attempt | **best-of-N** (4 prompt variants) |
| Quality | single score | **dual evaluator cross-check** (27B writer + 35B judge) with disagreement detection |
| Gate | none | **ent >= 3 AND edu >= 3** or no video is produced |
| Topics | static list / web search | **taste profile** (your own watch history) + **knowledge base** (FTS + embeddings + PARA wiki) |
| Research | none | web-evidence **brief injected into the script prompt** + workflow insights auto-filed to the KB |
| Visuals | FFmpeg slides | optional **LTX-Video B-roll** with per-line concrete visual prompts (LLM-converted) |

## Requirements

- Python 3.10+, FFmpeg
- A local LLM server (OpenAI-compatible; llama.cpp recommended) — **one model is
  enough**: it acts as writer AND judge. A second independent judge (e.g. a 35B
  MoE on CPU) improves reliability and is optional.
- ~10 GB disk for llama.cpp + model (IQ4 quant is fine for 27B-class models)
- YouTube OAuth (upload) — 5-minute setup below

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then edit: point LLM_AUTHOR_URL at your llama.cpp
```

### 1. Start a local LLM (llama.cpp)

```bash
llama-server -m your-model-q4.gguf --host 127.0.0.1 --port 8888 -np 1
# .env: LLM_AUTHOR_URL=http://127.0.0.1:8888/v1
```

### 2. YouTube OAuth (5 min)

1. Google Cloud Console → create project → enable **YouTube Data API v3**
2. OAuth consent screen → External → add yourself as a test user
3. Credentials → Create OAuth client ID (Desktop) → download `credentials.json`
4. Run:
   ```bash
   .venv/bin/python3 -c "
   import json, urllib.request, urllib.parse
   from pathlib import Path
   c = json.load(open('credentials.json'))['installed']
   u = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
       'client_id': c['client_id'], 'redirect_uri': c['installed']['redirect_uris'][0],
       'response_type': 'code', 'scope': 'https://www.googleapis.com/auth/youtube.upload',
       'access_type': 'offline', 'prompt': 'consent'})
   print(u)
   "
   ```
5. Open the URL, consent, copy the `code=...` from the redirect (page may error — that's fine)
6. Exchange it:
   ```bash
   .venv/bin/python3 -c "
   import json, urllib.request, urllib.parse
   c = json.load(open('credentials.json'))['installed']
   d = urllib.parse.urlencode({'client_id': c['client_id'], 'client_secret': c['client_secret'],
       'code': 'PASTE_CODE', 'grant_type': 'authorization_code',
       'redirect_uri': c['installed']['redirect_uris'][0]}).encode()
   r = json.loads(urllib.request.urlopen(urllib.request.Request(
       'https://oauth2.googleapis.com/token', data=d)).read())
   json.dump(r, open('token.json','w'))
   print('token.json written' if r.get('refresh_token') else 'MISSING refresh_token — re-run with prompt=consent')
   "
   ```
   Put `token.json` in this directory. Token refresh is handled automatically.

### 3. Optional extras (all degrade gracefully if absent)

- **Second judge** — `LLM_CPU_EVALUATOR_URL` (e.g. 35B MoE on CPU, Q4). Without it,
  the author model self-scores.
- **Animated B-roll** — `VIDEO_FACTORY_URL` pointing at a T2V factory
  (LTX-Video 2B + `/t2v` + `/thumb` + `/health`). Without it, `--video-gen ltx`
  auto-falls-back to FFmpeg slides.
- **Taste profile** — `taste_profile.py` + a read-only OAuth for the *viewing*
  account (separate from the channel). Drives topic selection toward what your
  viewers actually watch.
- **Knowledge base** — `kb/` ships with the PARA wiki skeleton; it fills itself
  as the pipeline runs (recipes, insights).
- **KB embeddings** — a second llama.cpp server for vectors, so search is hybrid
  (BM25 + cosine). Optional; without it search is BM25 keyword-only.
  ```bash
  llama-server -m nomic-embed-text-v1.5.Q8_0.gguf --embeddings --port 8086
  # .env: EMBED_URL=http://127.0.0.1:8086/v1/embeddings
  ```

## Usage

```bash
# One video, full pipeline (script → gate → video → upload)
.venv/bin/python3 main.py --topic "why your ice cube tray cracks from the bottom" \
    --shorts --status public --model local --video-gen ltx

# Upload what the pipeline left in output/
.venv/bin/python3 upload_youtube.py --batch --status public

# Or the full daily loop (topic pick → research → generate → record)
bash daily_generate.sh
```

### Cron (one video/day, hands-off)

```cron
0 7 * * *  cd /path/to/AutoShorts && bash daily_generate.sh
0 8,12,16,20 * * *  cd /path/to/AutoShorts && .venv/bin/python3 upload_youtube.py --batch --status public
```

The batch uploader archives uploaded videos to `output/archive/YYYYMMDD/`, so a
video is never uploaded twice.

## Quality gate

A video is only produced when the script passes **entertainment >= 3 AND
education >= 3** (out of 5) — scored by the writer model and, if configured, an
independent judge, averaged. Failed topics produce no file; the upload cron
simply skips. This is by design: one good Short beats three filler ones.

## Repo layout

```
main.py              pipeline (script → gate → video → metadata)
harness.py           LLM client: multi-backend, failure packets, meta-text filters
env.py               loads .env into the environment at startup
knowledge_base.py    FTS + embedding search over kb/ (second brain)
research_loop.py     web evidence → research brief + workflow insights
taste_profile.py     viewer watch-history → taste profile (OAuth)
cookie_history.py    same profile, no OAuth: browser cookies + yt-dlp
viewer_oauth.py      read-only OAuth for the viewing account
oauth_listen.py      catches the OAuth redirect code on localhost
animate_frames.py    optional T2V frame animation (paid MiniMax fallback)
evaluate.py          standalone script scorer (dev/debug helper)
daily_generate.sh    07:00 cron: topic → research → generate → record
upload_youtube.py    OAuth upload, batch/archive mode, --delete
topics.txt           fallback topic list (used when the LLM pick fails)
kb/wiki/             PARA wiki (00-projects / 10-areas / 20-resources / 30-archives)
```

## License

MIT — see LICENSE.
