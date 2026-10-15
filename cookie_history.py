#!/usr/bin/env python3
"""cookie_history.py — pull watch history + watch-later via yt-dlp + browser cookies.

Zero OAuth, zero Google Console. Needs only a Netscape-format cookies.txt
exported from the VIEWING account's browser (Cookie-Editor extension).

Usage:
  python cookie_history.py --cookies kb/cookies/cookies.txt
  -> fetches youtube:history (capped) + youtube:watchlater
  -> writes kb/taste_profile.json (same schema as taste_profile.py)
  -> rewrites kb/wiki/10-areas/taste-profile.md + syncs the RAG store
"""
import argparse, json, os, re, sys
from datetime import datetime, timezone
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import knowledge_base as kb

YTDLP = os.path.join(BASE, ".venv", "bin", "yt-dlp")
PROFILE = os.path.join(BASE, "kb", "taste_profile.json")
WIKI_MD = os.path.join(BASE, "kb", "wiki", "10-areas", "taste-profile.md")
HISTORY_CAP = 1500   # watch-history items to consider
STOP = set("a an the to of in on for and or with your you it is are was were be "
           "this that as at by from how why what when where which can will do does "
           "i my we our us not no yes so if then than too very just like new part "
           "full episode hd 4k 1080p live official music video video".split())


def ytdlp_json(url, cookies, max_items):
    """Run yt-dlp --flat-playlist -J for a pseudo URL; return item list."""
    import subprocess
    r = subprocess.run(
        [YTDLP, "--cookies", cookies, "--flat-playlist",
         "--playlist-end", str(max_items), "-J", "--no-warnings",
         "--socket-timeout", "30", url],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp failed ({url}): {r.stderr[-500:]}")
    d = json.loads(r.stdout)
    return d.get("entries") or d.get("entries") or []


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def keywords_of(items):
    freq = Counter()
    for it in items:
        for w in re.findall(r"[A-Za-z]{3,}", (it.get("title") or "").lower()):
            if w not in STOP:
                freq[w] += 1
    return freq


def build(cookies):
    print("[1/2] fetching watch history (youtube:history) ...")
    hist = ytdlp_json("youtube:history", cookies, HISTORY_CAP)
    hist = [h for h in hist if h]
    print(f"      {len(hist)} items")

    print("[2/2] fetching watch-later (youtube:watchlater) ...")
    try:
        later = [w for w in ytdlp_json("youtube:watchlater", cookies, 300) if w]
    except Exception as e:
        print(f"      [warn] watch-later failed: {e}")
        later = []
    print(f"      {len(later)} items")

    channels = Counter(clean(h.get("channel") or h.get("uploader") or "") for h in hist)
    kws_all = keywords_of(hist)
    kws_later = keywords_of(later)
    later_ids = {w.get("id") for w in later}

    prof = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "cookies+yt-dlp",
        "historyCount": len(hist),
        "watchLaterCount": len(later),
        "topChannels": [{"channel": c, "count": n} for c, n in channels.most_common(25) if c],
        "topKeywords": [{"word": w, "count": n} for w, n in kws_all.most_common(40)],
        "watchLaterKeywords": [{"word": w, "count": n} for w, n in kws_later.most_common(20)],
        "recentTitles": [clean(h.get("title")) for h in hist[:40] if clean(h.get("title"))],
        "watchLaterTitles": [clean(w.get("title")) for w in later[:60] if clean(w.get("title"))],
    }
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    with open(PROFILE, "w") as f:
        json.dump(prof, f, indent=2, ensure_ascii=False)
    print("[OK] wrote", PROFILE)
    write_wiki(prof)
    kb.sync_wiki()
    return prof


def write_wiki(p):
    lines = ["# Taste Profile — Viewer Account",
             "<!-- kind: taste -->",
             "<!-- source: auto (viewing cookies + yt-dlp) -->",
             "<!-- tags: taste, viewer, preferences -->",
             "",
             f"(Auto-generated {p['updated']} via browser cookies. Do not hand-edit below.)",
             "", "## Top channels", ""]
    lines += [f"- {c['channel']} ({c['count']})" for c in p["topChannels"][:15]]
    lines += ["", "## Recurring title keywords", ""]
    lines.append(", ".join(f"{k['word']}({k['count']})" for k in p["topKeywords"][:25]) or "(none)")
    lines += ["", "## Watch-later keywords (strongest signal — intentional)", ""]
    lines.append(", ".join(f"{k['word']}({k['count']})" for k in p["watchLaterKeywords"][:15]) or "(none)")
    lines += ["", "## Recent history (sample)", ""]
    lines += [f"- {t}" for t in p["recentTitles"][:30]]
    lines += ["", "## Watch-later (sample)", ""]
    lines += [f"- {t}" for t in p["watchLaterTitles"][:30]]
    with open(WIKI_MD, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default=os.path.join(BASE, "kb", "cookies", "cookies.txt"))
    a = ap.parse_args()
    if not os.path.exists(a.cookies):
        print(f"cookie file not found: {a.cookies}")
        sys.exit(1)
    os.chmod(a.cookies, 0o600)
    build(a.cookies)
