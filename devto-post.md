# I Built a YouTube Shorts Factory That Costs $0 Per Video — And It Improves Its Own Content

*Problem → stack → the two tricks nobody ships → cron → repo*

---

Everyone's "AI YouTube automation" is the same thing: a prompt in, a filler
Short out, posted 5× a day until the algorithm buries you.

I got tired of watching that, so I built a pipeline that **refuses to publish
bad work** and **gets better every run**. Everything runs on my own hardware.
Per-video cost: electricity.

## The problem

Local-LLM content pipelines fail in two boring ways:

1. **Quality drift.** A single prompt + single score produces "vague
   self-help aphorism" Shorts. No gate, so they all get uploaded.
2. **No memory.** Every run starts from zero. The pipeline never learns that
   the ice-cube-tray video outscored the "stay motivated" video 4:1.

Both are fixable with structure, not with a bigger model.

## The stack

| Layer | Tool | Cost |
|---|---|---|
| Script writer | Qwen 27B (llama.cpp, IQ4, local) | $0 |
| Quality judge | 35B MoE (llama.cpp, CPU, Q4) — optional | $0 |
| Narration | edge-TTS | $0 |
| Video | FFmpeg slides, or LTX-Video 2B B-roll (second box) | $0 |
| Upload | YouTube Data API v3 (OAuth) | $0 |
| Memory | SQLite FTS + embeddings + PARA wiki | $0 |

Total per video: **$0.00**. Total infra: hardware I already had.

## Trick 1: the dual-evaluator gate

One model writing and one model judging is cheap to fake — the writer
"agrees" with itself. So the gate is:

- generate **4 script variants** (different prompt framings)
- score each on **entertainment / education / retention** (1–5) by the writer
  *and* by an independent judge (a different model, CPU, thinking-mode off)
- average, flag any dimension where the two evaluators disagree by ≥ 2 points
  (bias detection)
- **ship only if entertainment ≥ 3 AND education ≥ 3**

A topic that fails produces no file. The upload cron simply finds nothing and
skips. One good Short a day beats five filler ones — the pipeline enforces
that instead of hoping for it.

## Trick 2: the second brain

A `kb/` directory is a real small knowledge base:

- **FTS + embeddings** over distilled notes (not raw captures)
- **PARA layout** — `00-projects / 10-areas / 20-resources / 30-archives`
- the research loop reads the web for the chosen topic, writes a fact brief
  that gets **injected into the script prompt**, and — here's the loop — if a
  finding is applicable to the *pipeline itself*, it's appended to
  `workflow-insights.md` as a pending change
- every shipped video records a **recipe** (topic + scores) to
  `topic-recipes.md`; the next day's topic-pick prompt reads the last
  successes as few-shot

Run 100 and the topic picker has seen its own hit rate. That's the part
nobody else ships.

## Bonus: taste-profiled topic picking

Topic selection is grounded in the *owner's* viewing account — subscriptions,
watch-later, recurring keywords (read-only OAuth, separate from the channel).
The model picks concrete, fun, useful topics the audience already queues up,
and the gate keeps the rest out.

## Run it hands-off

```cron
0 7 * * *       cd ~/AutoShorts && bash daily_generate.sh
0 8,12,16,20 * * *  cd ~/AutoShorts && .venv/bin/python3 upload_youtube.py --batch --status public
```

`daily_generate.sh`: pick topic (27B + taste + KB) → research → generate
(gate + B-roll) → record recipe. The batch uploader archives what it ships,
so nothing uploads twice.

## Getting it

Source is MIT — the full v2 pipeline (writer, gate, KB, research loop, taste
profile, cron scripts) is on Gumroad at launch pricing, and a free repo
follows. If you run local LLMs, the README walks you through llama.cpp +
OAuth in ~15 minutes.

**What I'd do differently:** I wish I'd built the knowledge base first. The
gate is table stakes; the memory is the moat.

*Comments welcome — what would you add before this starts producing on its
own?*
