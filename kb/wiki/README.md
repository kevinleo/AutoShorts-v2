# Second Brain — YouTube Shorts Pipeline
<!-- kind: note -->
<!-- para: areas -->
<!-- tags: meta, second-brain, para -->

Personal second brain (PARA + CODE) for the Shorts pipeline.

- **Capture** — raw inputs: viewing history (`kb/taste_profile.json`), web research
  (per-video briefs), pipeline logs.
- **Organize** — PARA folders below (this directory is indexed into the RAG store
  by `knowledge_base.py sync`):
  - `00-projects/`  active work (the pipeline itself)
  - `10-areas/`     ongoing (viewer taste, workflow quality/insights)
  - `20-resources/` evergreen (topic recipes, techniques, facts)
  - `30-archives/`  dated/finished (old recipes, dead leads)
- **Distill** — the 27B reduces raw captures into atomic notes (briefs, insights);
  never trust a raw capture, always distill.
- **Express** — `knowledge_base.py search` pulls notes into the daily topic prompt
  and (via research_brief.md) the script prompt.

Query examples:
```
python knowledge_base.py tree
python knowledge_base.py stats
python knowledge_base.py search "what kind of hooks worked" 5
```
