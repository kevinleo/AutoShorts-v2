#!/usr/bin/env bash
# daily_generate.sh — 07:00 cron: auto-generate one Shorts video via the 2PC pipeline.
#
#   1) refresh taste profile if stale (weekly)      taste_profile.py
#   2) pick a topic: 27B + viewer taste + KB context (recent insights + past recipes)
#   3) research: web evidence -> output/research_brief.md (+ workflow insights -> KB)
#   4) generate: gate + B-roll (main.py injects the brief into prompts)
#   5) if gate passes: record the recipe (topic + scores) into the KB
#
# Safety: if output/final.mp4 is already pending (unuploaded), skip to avoid clobbering.
set -uo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BASE
cd "$BASE"
PY="${PYTHON:-$BASE/.venv/bin/python3}"
LOG="$BASE/gen.log"

echo "=== daily generate START $(date) ===" >> "$LOG"

# 1) skip if a video is already waiting for the upload cron
if [ -f output/final.mp4 ]; then
  echo "[SKIP] output/final.mp4 already pending upload — not regenerating." >> "$LOG"
  exit 0
fi

# 1b) refresh taste profile if it is missing or older than 7 days
"$PY" taste_profile.py --update-if-stale --max-age 604800 >> "$LOG" 2>&1 || \
  echo "[WARN] taste refresh failed (continuing with existing profile)" >> "$LOG"

# 2) pick a topic: 27B author, grounded in viewer taste + knowledge base context
# NOTE: stdout must reach $() — only stderr goes to the pick log.
TOPIC=$("$PY" - <<'PYEOF' 2>> "$BASE/.topic_pick.log"
import json, os, random, sys
BASE = os.environ["BASE"]
sys.path.insert(0, BASE)
from harness import llm_chat_with_hooks
import knowledge_base as kb

def _load(path):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""

# --- viewer taste (personal account: subscriptions + watch-later + playlists) ---
taste = ""
try:
    prof = json.load(open(os.path.join(BASE, "kb", "taste_profile.json")))
    subs = ", ".join(c["channel"] for c in prof.get("subscribedChannels", [])[:10])
    pls  = ", ".join(p["title"] for p in prof.get("playlists", [])[:10])
    chs  = ", ".join(c["channel"] for c in prof.get("topChannels", [])[:8])
    kws  = ", ".join(k["word"] for k in prof.get("topKeywords", [])[:12])
    wl   = ", ".join(k["word"] for k in prof.get("watchLaterKeywords", [])[:8])
    taste = (f"Subscribed channels: {subs or 'n/a'}\n"
             f"Their own playlists: {pls or 'n/a'}\n"
             f"Watched channels: {chs or 'n/a'}\n"
             f"Recurring keywords: {kws or 'n/a'}\n"
             f"Watch-later keywords (intentional): {wl or 'n/a'}")
except Exception as e:
    taste = f"(taste profile unavailable: {e})"

# --- knowledge base: recent workflow insights + past successful recipes ---
kb_ctx = ""
try:
    hits = kb.search("concrete fun useful topic that passed gate", k=4)
    recipes = "\n".join(f"- {r['title']}" for r in hits if r["kind"] in ("topic", "insight"))
    kb_ctx = recipes.strip() or "(no indexed recipes yet)"
except Exception as e:
    kb_ctx = f"(kb unavailable: {e})"

# --- ask the author model ---
try:
    raw = llm_chat_with_hooks(
        "Suggest ONE YouTube Shorts topic a viewer will want to share. It must be:\n"
        "- CONCRETE: about a specific daily situation, object, or action (phone, "
        "sleep, food, money, a habit, a tool, a setting) — NOT an abstract feeling.\n"
        "- FUNNY: has a 'wait, really?' hook that is fun to say out loud.\n"
        "- USEFUL: ends in something the viewer can actually DO tomorrow.\n"
        "AVOID self-help aphorisms, vague motivation, and generic 'be more "
        "productive' advice.\n\n"
        "This is the OWNER'S personal YouTube taste — lean toward what they "
        "actually watch and queue up, but keep it concrete and fun:\n" + taste + "\n\n"
        "Topics that already worked for this channel (do not repeat these):\n" + kb_ctx + "\n\n"
        "Reply with the topic only, one line, under 12 words, no quotes, no hashtags.\n"
        "Good example: 'why your phone battery dies by noon even on airplane mode'\n"
        "Bad example: 'the hidden reason you feel stuck in life'",
        max_tokens=80, temperature=0.9, stage="topic_pick",
        max_retries=1, min_response_length=8, model_backend="local",
    )
    t = (raw or "").strip().strip('"\'')
    if 12 <= len(t) <= 160 and "\n" not in t:
        print(t)
        sys.exit(0)
except Exception as e:
    print(f"[topic] LLM suggestion failed: {e}")

# fallback: rotate topics.txt without repeating the last 3 used
state = os.path.join(BASE, ".topic_state")
lines = [l.strip() for l in open(os.path.join(BASE, "topics.txt")) if l.strip()]
used = []
if os.path.exists(state):
    used = [l.strip() for l in open(state) if l.strip()][-3:]
pool = [t for t in lines if t not in used] or lines
pick = random.choice(pool)
with open(state, "a") as f:
    f.write(pick + "\n")
print(pick)
PYEOF
)

if [ -z "$TOPIC" ]; then
  echo "[ERROR] could not pick a topic — aborting." >> "$LOG"
  exit 1
fi
echo "[TOPIC] $TOPIC" >> "$LOG"

# 3) research: web evidence -> research_brief.md (injected into script prompts by main.py)
#    + any workflow-applicable finding gets appended to the KB insights (self-improvement loop)
"$PY" research_loop.py --topic "$TOPIC" >> "$LOG" 2>&1 || \
  echo "[WARN] research loop failed — generating without brief" >> "$LOG"

# 4) generate (gate + B-roll). main.py writes output/final.mp4 only if the gate passes.
"$PY" main.py \
  --topic "$TOPIC" \
  --shorts --status public --model local --video-gen ltx >> "$LOG" 2>&1
rc=$?

# 5) if the video passed the gate, record the recipe into the knowledge base
if [ -f output/final.mp4 ] && [ -f output/meta.json ]; then
  TOPIC="$TOPIC" "$PY" - <<'PYEOF' >> "$LOG" 2>&1 || echo "[WARN] recipe record failed" >> "$LOG"
import json, os, sys, datetime
BASE = os.environ["BASE"]
sys.path.insert(0, BASE)
import knowledge_base as kb
meta = json.load(open(os.path.join(BASE, "output", "meta.json")))
s = meta.get("scores", meta.get("score", {}))
topic = os.environ["TOPIC"]
scores = f"ent={s.get('entertainment')} edu={s.get('education')} ret={s.get('retention')} total={s.get('total')}/15"
date = datetime.date.today().isoformat()
entry = (f"\n- **topic:** {topic}\n"
         f"  - scores: {scores} ({date})\n"
         f"  - why it worked: pending — note the hook/format that worked\n")
path = os.path.join(BASE, "kb", "wiki", "20-resources", "topic-recipes.md")
with open(path, "a") as f:
    f.write(entry)
kb.add("topic", f"{date} {topic}", f"scores: {scores}\ntopic: {topic}",
       source="pipeline", tags="topic,recipe")
kb.sync_wiki()
print(f"[OK] recipe recorded: {topic} ({scores})")
PYEOF
fi

if [ -f output/final.mp4 ]; then
  echo "[OK] video ready for 08:00 upload cron." >> "$LOG"
else
  echo "[WARN] no final.mp4 (gate failed or generation error) rc=$rc — upload cron will skip." >> "$LOG"
fi
echo "=== daily generate DONE $(date) rc=$rc ===" >> "$LOG"
exit $rc
