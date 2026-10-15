#!/usr/bin/env python3
"""taste_profile.py — profile the user's viewing tastes from their PERSONAL account.

Reads (youtube.readonly, viewer_token.json — NOT the channel token):
  - watchHistory  (last up to 2000 videos, with dates)
  - watch-later   (playlistItems of playlistId=WL)

Produces:
  kb/taste_profile.json  — structured: top channels, keywords, durations, recency
  kb/wiki/taste-profile.md — human+LLM readable summary (auto section replaced)
  (then: knowledge_base.py sync indexes it into the RAG store)

Usage:
  python taste_profile.py --check          # token works?
  python taste_profile.py --update         # refresh profile now
  python taste_profile.py --update-if-stale --max-age 604800   # 7 days (cron use)
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import env  # loads .env (VIEWER_TOKEN_FILE)
import knowledge_base as kb

# Read-only OAuth token of the VIEWING account (never the channel upload token).
TOKEN_FILE = os.environ.get("VIEWER_TOKEN_FILE",
                            os.path.join(BASE, "viewer_token.json"))
PROFILE = os.path.join(BASE, "kb", "taste_profile.json")
WIKI_MD = os.path.join(BASE, "kb", "wiki", "10-areas", "taste-profile.md")
API = "https://www.googleapis.com/youtube/v3/"
HISTORY_LIMIT = 2000
DETAIL_BATCH = 50

STOP = set("a an the to of in on for and or with your you it is are was were be "
           "this that as at by from how why what when where which can will do does "
           "i my we our us not no yes so if then than too very just like new".split())


def token():
    if not os.path.exists(TOKEN_FILE):
        print(f"[SKIP] no viewer token at {TOKEN_FILE} — the taste profile needs the "
              "read-only viewer OAuth (README: viewer_oauth.py). Set VIEWER_TOKEN_FILE "
              "in .env, or skip taste-based topic picking.")
        sys.exit(0)
    d = json.load(open(TOKEN_FILE))
    return d["access_token"]


def refresh_access():
    d = json.load(open(TOKEN_FILE))
    data = urllib.parse.urlencode({
        "refresh_token": d["refresh_token"],
        "client_id": d["client_id"],
        "client_secret": d["client_secret"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    d["access_token"] = r["access_token"]
    d["token"] = r["access_token"]
    with open(TOKEN_FILE, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(TOKEN_FILE, 0o600)
    return d["access_token"]


def api(path, at, **params):
    params = {k: v for k, v in params.items() if v}
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {at}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            at = refresh_access()
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {at}"})
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        raise


def check():
    at = token()
    d = api("watchHistory", at, part="snippet", maxResults=1)
    n = d.get("pageInfo", {}).get("totalResults", 0)
    print(f"[OK] viewer account — watch history total = {n} items")
    return True


def fetch_subscriptions(at, limit=100):
    """Subscribed channels — strongest intentional taste signal."""
    chans, token_ = [], None
    while len(chans) < limit:
        d = api("subscriptions", at, part="snippet", mine="true",
                maxResults=50, pageToken=token_)
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            sn = it.get("snippet", {})
            chans.append({
                "title": sn.get("title", ""),
                "description": (sn.get("description") or "")[:200],
            })
        token_ = d.get("nextPageToken")
        if not token_:
            break
    return chans


def fetch_playlists(at, limit=30):
    """User's own playlists (WL + custom). Custom playlist TITLES are taste signals."""
    pls, token_ = [], None
    while len(pls) < limit:
        d = api("playlists", at, part="snippet,contentDetails", mine="true",
                maxResults=50, pageToken=token_)
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            sn = it.get("snippet", {})
            cd = it.get("contentDetails", {}) or {}
            pid = it.get("id")
            if isinstance(pid, dict):
                pid = pid.get("playlist", "")
            pls.append({
                "id": pid or "",
                "title": sn.get("title", ""),
                "count": int(cd.get("itemCount", 0) or 0),
            })
        token_ = d.get("nextPageToken")
        if not token_:
            break
    return pls


def fetch_playlist_items(at, playlist_id, limit=100):
    vids, token_ = [], None
    while len(vids) < limit and playlist_id:
        d = api("playlistItems", at, part="snippet", playlistId=playlist_id,
                maxResults=50, pageToken=token_)
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            sn = it["snippet"]
            vids.append({
                "videoId": sn.get("resourceId", {}).get("videoId"),
                "title": sn.get("title", ""),
                "channel": sn.get("videoOwnerText", ""),
                "time": sn.get("addedAt"),
                "playlist": playlist_id,
            })
        token_ = d.get("nextPageToken")
        if not token_:
            break
    return vids


def fetch_history(at, limit=HISTORY_LIMIT):
    """Watch history — only if the API endpoint is available (404 in some envs)."""
    vids, token_ = [], None
    while len(vids) < limit:
        d = api("watchHistory", at, part="snippet,player", maxResults=500,
                pageToken=token_,
                order="date")
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            sn = it.get("snippet", {})
            vids.append({
                "videoId": sn.get("videoId"),
                "title": sn.get("videoTitle", ""),
                "channel": sn.get("channelTitle", ""),
                "time": sn.get("publishedAt"),
            })
        token_ = d.get("nextPageToken")
        if not token_:
            break
    return vids


def fetch_watch_later(at, limit=200):
    return fetch_playlist_items(at, "WL", limit)


def iso8601_dur(s):
    if not s:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return None
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mi * 60 + sec


def enrich_details(at, vids, cap=400):
    """Add duration for the most recent `cap` unique videos (50/call)."""
    ids = []
    for v in vids:
        if v["videoId"] and v["videoId"] not in ids:
            ids.append(v["videoId"])
        if len(ids) >= cap:
            break
    dur = {}
    for i in range(0, len(ids), DETAIL_BATCH):
        chunk = ids[i:i + DETAIL_BATCH]
        try:
            d = api("videos", at, part="contentDetails", id=",".join(chunk))
            for it in d.get("items", []):
                dur[it["id"]] = iso8601_dur(it.get("contentDetails", {}).get("duration"))
        except Exception as e:
            print("  [warn] details batch failed:", e)
    for v in vids:
        v["duration"] = dur.get(v["videoId"])
    return vids


def keywords_of(vids):
    freq = {}
    for v in vids:
        for w in re.findall(r"[A-Za-z]{3,}", v["title"].lower()):
            if w in STOP:
                continue
            freq[w] = freq.get(w, 0) + 1
    return sorted(freq.items(), key=lambda x: -x[1])


def build_profile():
    at = token()
    notes = []

    print("[1/5] subscriptions (channels you follow) ...")
    try:
        subs = fetch_subscriptions(at)
    except Exception as e:
        subs, notes = [], [f"subscriptions: {e}"]
    print(f"      {len(subs)} channels")

    print("[2/5] your playlists (WL + custom) ...")
    try:
        playlists = fetch_playlists(at)
    except Exception as e:
        playlists, notes = [], [f"playlists: {e}"]
    print(f"      {len(playlists)} playlists")

    # pull items from non-WL custom playlists (titles = taste) + WL
    vids = []
    for pl in playlists:
        if pl["id"] == "WL" or pl["title"].lower() in ("watch later", "나중에 볼 동영상", "나중에 볼 동영상들"):
            continue
        try:
            vids += fetch_playlist_items(at, pl["id"], limit=60)
        except Exception as e:
            notes.append(f"playlistItems({pl['title'][:20]}): {e}")
    later = []
    try:
        # WL is a system playlist — find it by title from the mine list
        wl_id = next((p["id"] for p in playlists
                      if p["title"].lower() in ("watch later", "나중에 볼 동영상", "나중에 볼 동영상들")), "WL")
        later = fetch_playlist_items(at, wl_id, limit=200)
    except Exception as e:
        notes.append(f"watch-later: {e}")
    print(f"      {len(later)} watch-later, {len(vids)} custom-playlist items")

    print("[3/5] watch history (if endpoint available) ...")
    try:
        hist = fetch_history(at, limit=HISTORY_LIMIT)
    except Exception as e:
        hist, notes = [], [f"watchHistory: {e} (endpoint unavailable here?)"]
    print(f"      {len(hist)} items")

    # enrich durations for what we can
    for v in vids + later:
        v.setdefault("inWatchLater", v.get("playlist") == "WL")
    durs = []
    for v in (hist or vids or []):
        if v.get("videoId"):
            try:
                enrich_details(at, [v], cap=50)
            except Exception:
                pass
            if v.get("duration"):
                durs.append(v["duration"])
    durs.sort()
    dur_median = durs[len(durs) // 2] if durs else None

    # channel tallies: subscriptions (weight 2) + video channels (weight 1)
    ch = {}
    for s in subs:
        ch[s["title"]] = ch.get(s["title"], 0) + 2
    for v in vids + later + hist:
        c = v.get("channel")
        if c:
            ch[c] = ch.get(c, 0) + 1
    top_ch = sorted(ch.items(), key=lambda x: -x[1])[:25]

    # keywords from playlist titles + video titles (intentional signals first)
    pl_titles = " ".join(p["title"] for p in playlists)
    all_titles = pl_titles + " " + " ".join(
        (v.get("title") or "") for v in vids + later)
    if hist:
        all_titles += " " + " ".join((v.get("title") or "") for v in hist)
    kws = keywords_of([{"title": all_titles}])[:40]
    wl_kws = keywords_of(later)[:20]
    sub_kws = keywords_of([{"title": " ".join(s["title"] + " " + s["description"] for s in subs)}])[:20]

    prof = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "historyCount": len(hist),
        "watchLaterCount": len(later),
        "customPlaylistItemCount": len(vids),
        "playlistCount": len(playlists),
        "subscribedChannels": [{"channel": s["title"], "about": s["description"]}
                               for s in subs],
        "playlists": [{"title": p["title"], "count": p["count"]} for p in playlists],
        "topChannels": [{"channel": c, "count": n} for c, n in top_ch],
        "topKeywords": [{"word": w, "count": n} for w, n in kws],
        "watchLaterKeywords": [{"word": w, "count": n} for w, n in wl_kws],
        "subscriptionKeywords": [{"word": w, "count": n} for w, n in sub_kws],
        "durationMedianSec": dur_median,
        "recentTitles": [v["title"] for v in (hist or vids)[:40]],
        "watchLaterTitles": [v["title"] for v in later[:60]],
        "notes": notes,
    }
    os.makedirs(os.path.dirname(PROFILE), exist_ok=True)
    with open(PROFILE, "w") as f:
        json.dump(prof, f, indent=2, ensure_ascii=False)
    print("[OK] wrote", PROFILE)
    write_wiki(prof)
    n = kb.sync_wiki()
    print(f"[OK] wiki synced ({n} changed)")
    return prof


def write_wiki(p):
    head = """# Taste Profile — Viewer Account
<!-- kind: taste -->
<!-- source: auto (subscriptions + watch-later + custom playlists) -->
<!-- tags: taste, viewer, preferences -->

(Auto-generated by taste_profile.py — updated %s. Do not hand-edit below.)

""" % p["updated"]
    lines = [head, "## Subscribed channels (strongest taste signal)", ""]
    for s in p["subscribedChannels"]:
        about = (s.get("about") or "").replace("\n", " ").strip()
        lines.append(f"- **{s['channel']}** — {about[:80]}")
    lines += ["", "## Your playlists (curated interests)", ""]
    lines += [f"- {pl['title']} ({pl['count']} items)" for pl in p["playlists"]]
    lines += ["", "## Top video channels (subscribed + watched)", ""]
    lines += [f"- {c['channel']} ({c['count']})" for c in p["topChannels"][:15]]
    lines += ["", "## Recurring keywords", ""]
    lines.append(", ".join(f"{k['word']}({k['count']})" for k in p["topKeywords"][:25]) or "(none)")
    lines += ["", "## Watch-later keywords (intentional)", ""]
    lines.append(", ".join(f"{k['word']}({k['count']})" for k in p["watchLaterKeywords"][:15]) or "(none)")
    if p.get("durationMedianSec"):
        m, s = divmod(p["durationMedianSec"], 60)
        lines += ["", f"## Typical video length: ~{m}m{s:02d}s (median)", ""]
    lines += ["", "## Recent / saved video titles (sample)", ""]
    lines += [f"- {t}" for t in (p["watchLaterTitles"] + p["recentTitles"])[:30]]
    if p.get("notes"):
        lines += ["", "## Notes", ""]
        lines += [f"- {n}" for n in p["notes"]]
    open(WIKI_MD, "w").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--check" in args:
        check()
    elif "--update" in args:
        build_profile()
    elif "--update-if-stale" in args:
        max_age = 604800
        if "--max-age" in args:
            i = args.index("--max-age")
            max_age = int(args[i + 1])
        if os.path.exists(PROFILE) and (time.time() - os.path.getmtime(PROFILE)) < max_age:
            print(f"[SKIP] taste profile fresh (age < {max_age}s)")
        else:
            build_profile()
    else:
        print(__doc__)
