# Project — 2-PC Shorts Pipeline
<!-- kind: note -->
<!-- para: projects -->
<!-- source: pipeline docs -->
<!-- tags: project, pipeline, architecture -->

Active project: $0/day automated YouTube Shorts (1 or 2 PCs, no paid APIs).

## Architecture
- **Main PC (LLM box)**
  - GPU: LLM author ONLY (27B-class, port auto-detected 8888/8080-8083)
  - CPU (optional): independent evaluator server on :8085 — cross-checks the gate
  - CPU (optional): embedder server on :8086 (`llama-server --embeddings`) for
    hybrid KB search; BM25-only when absent
  - upload: `upload_youtube.py --batch` (08/12/16/20 cron, respects meta status)
- **Second PC (optional, video box)**
  - video factory :8090 — LTX-Video 2B `/t2v` + SDXL-Turbo `/thumb`
  - `--video-gen ltx` auto-falls back to FFmpeg slides when it is unreachable
- **Second brain (this store)**
  - `kb/kb.db`: SQLite FTS5 (BM25) + 768-d embeddings (hybrid 0.5/0.5)
  - `kb/wiki/`: PARA folders (00-projects 10-areas 20-resources 30-archives)
  - CODE flow: Capture (history/web raw) → Organize (PARA) → Distill (LLM briefs/insights)
    → Express (RAG into topic/script prompts)

## Daily loop (07:00 cron → daily_generate.sh)
1. taste refresh if >7d (taste_profile.py)
2. topic pick: author LLM + taste + KB context (insights + past recipes)
3. research_loop.py: web (DDG+Wikipedia) → research_brief.md; workflow insights → KB
4. main.py: best-of-N scripts w/ brief injected → gate (ent>=3 AND edu>=3, 2 evaluators)
   → B-roll (factory) or FFmpeg slides → final.mp4 only if the gate passes
5. recipe recorded to 20-resources/topic-recipes.md + KB

## Standing rules
- gate ent>=3 AND edu>=3 NEVER lowered
- topics: concrete + funny + useful, no self-help aphorisms
- the LLM GPU is never used for video rendering
- private/unlisted meta status is never force-published by cron

## Accounts (two, kept separate)
- **Channel (upload):** `token.json` in the repo dir
  (scope: `youtube.upload`) — created by you via README step 2, never touched by cron.
- **Viewer (taste):** `viewer_token.json` (scope: `youtube.readonly` ONLY — no upload
  rights). Watch history + watch-later → `taste_profile.py` → topic selection.
  Optional: skip it and the pipeline picks topics from the KB + topics.txt only.
