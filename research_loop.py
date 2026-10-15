#!/usr/bin/env python3
"""research_loop.py — research a topic, then split findings into:

  (a) TOPIC BRIEF   -> output/research_brief.md  (main.py injects it into the
                       script prompts: concrete facts, surprising numbers, hooks)
  (b) WORKFLOW INSIGHT -> if the research reveals something that improves OUR
                       pipeline (topic selection / script quality / production),
                       it is appended to kb/wiki/workflow-insights.md and indexed
                       into the RAG store, where daily_generate.sh pulls recent
                       insights into future prompts.  This is the "apply to
                       workflow" mechanism: KB -> prompt context -> behavior.

Web sources (no API keys): DuckDuckGo HTML search + Wikipedia REST API.
Analysis model: the local 27B author (harness.llm_chat_with_hooks).

Usage:
  python research_loop.py --topic "why your phone battery dies by noon"
  python research_loop.py --topic "..." --skip-web   # offline: LLM-knowledge only
"""
import argparse, html, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

import env  # loads .env (LLM endpoint overrides)

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import harness
import knowledge_base as kb

BRIEF_PATH = os.path.join(BASE, "output", "research_brief.md")
INSIGHTS_MD = os.path.join(BASE, "kb", "wiki", "10-areas", "workflow-insights.md")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0"
MAX_CHARS_PER_PAGE = 6000


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def ddg_search(query, n=5):
    """DuckDuckGo HTML endpoint -> [(title, url, snippet)]"""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    page = _get(url)
    out = []
    for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?class="result__snippet"[^>]*>(.*?)</a>',
            page, re.S):
        href, title, snip = m.group(1), m.group(2), m.group(3)
        if href.startswith("//duckduckgo.com/l/?"):
            uddg = re.search(r"uddg=([^&]+)", href)
            if uddg:
                href = urllib.parse.unquote(uddg.group(1))
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        title, snip = clean(title), clean(snip)
        if title and href.startswith("http"):
            out.append((title, href, snip))
        if len(out) >= n:
            break
    return out


def wikipedia_search(query, n=3):
    try:
        d = json.loads(_get("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "opensearch", "search": query, "limit": n, "format": "json"})))
        return [(d[1][i], f"https://en.wikipedia.org/wiki/{urllib.parse.quote(d[2][i].replace(' ', '_'))}")
                for i in range(len(d[1]))]
    except Exception:
        return []


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", s))


def fetch_text(url):
    try:
        if "wikipedia.org" in url:
            title = url.rsplit("/", 1)[-1]
            d = json.loads(_get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"))
            return d.get("extract", "")[:MAX_CHARS_PER_PAGE]
        return strip_html(_get(url))[:MAX_CHARS_PER_PAGE]
    except Exception:
        return ""


def gather_evidence(topic, skip_web=False):
    """Return (evidence_text, sources)"""
    sources, chunks = [], []
    if skip_web:
        return "(offline mode — no web sources; rely on your own knowledge and flag uncertainty.)", sources
    queries = [
        f"{topic}",
        f"{topic} surprising fact",
        f"{topic} study research",
    ]
    seen = set()
    for q in queries:
        for title, url, snip in ddg_search(q, n=4):
            if url in seen or "wikipedia.org" in url:
                continue
            seen.add(url)
            sources.append({"title": title, "url": url, "snippet": snip})
    # Wikipedia for the core concept
    for title, url in wikipedia_search(topic.split("why ")[:1][-1] if topic.startswith("why ") else topic, n=2):
        if url not in seen:
            seen.add(url)
            sources.append({"title": title, "url": url, "snippet": "wikipedia"})
    # fetch up to 3 non-wiki pages + 1 wiki page
    fetched = 0
    for s in sources:
        if fetched >= 4:
            break
        text = fetch_text(s["url"])
        if text and len(text) > 200:
            chunks.append(f"### Source: {s['title']}\n{s['url']}\n{text}")
            fetched += 1
    ev = "\n\n".join(chunks)
    if not ev:
        ev = "(web fetch failed or no usable results — rely on your own knowledge and flag uncertainty.)"
    return ev[:18000], sources


def llm_brief(topic, evidence):
    prompt = (
        f"You are preparing a YouTube Shorts script about: {topic}\n\n"
        f"Below are web research sources. Extract the GOLD: concrete, verifiable, "
        f"surprising facts a short can actually use (numbers, mechanisms, named studies, "
        f"counterintuitive details). Prefer specifics over generalities.\n\n"
        f"RESEARCH SOURCES:\n{evidence}\n\n"
        "Output EXACTLY this JSON (no other text):\n"
        '{"hook": "one scroll-stopping hook sentence using the best surprising fact",\n'
        ' "facts": ["5-8 concrete usable facts, one sentence each, with the number/name where known"],\n'
        ' "payoff": "the concrete do-it-tomorrow takeaway",\n'
        ' "sources": ["up to 3 urls actually used"]}'
    )
    raw = harness.llm_chat_with_hooks(prompt, max_tokens=700, temperature=0.3,
                                      stage="research_brief", model_backend="local")
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return {k: d.get(k) for k in ("hook", "facts", "payoff", "sources")}
    except json.JSONDecodeError:
        return None


def llm_insight(topic, brief):
    prompt = (
        "You are auditing our YouTube Shorts production pipeline. The pipeline: "
        "(1) pick a concrete, fun, practical topic, (2) write an 8-line script "
        "(3 witty + 3 useful lines minimum), (3) gate: entertainment>=3 AND "
        "education>=3 out of 5 from two LLM evaluators, (4) generate B-roll video, "
        "caption overlay, TTS, upload.\n\n"
        f"Topic researched today: {topic}\n"
        f"Research brief: {json.dumps(brief or {}, ensure_ascii=False)}\n\n"
        "Does this research contain anything that would IMPROVE OUR PIPELINE itself "
        "(not just this video)? For example: a better way to choose topics, a script "
        "structure that reliably scores higher on education, a hook format that stops "
        "scrolls, a production trick, a factual domain we should cover more.\n\n"
        "Only answer yes if there is a SPECIFIC, actionable change with clear benefit. "
        "Output EXACTLY this JSON (no other text):\n"
        '{"applies": true or false,\n'
        ' "title": "short title of the insight",\n'
        ' "finding": "1-3 sentences, what was learned and from where (url if any)",\n'
        ' "apply_to": "one of: topic-selection | script-writing | gate | production",\n'
        ' "concrete_change": "the exact change to make in the named part of the pipeline"}'
    )
    raw = harness.llm_chat_with_hooks(prompt, max_tokens=500, temperature=0.2,
                                      stage="research_insight", model_backend="local")
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def write_brief(topic, brief, sources):
    os.makedirs(os.path.dirname(BRIEF_PATH), exist_ok=True)
    lines = [f"# Research Brief — {topic}",
             f"<!-- generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->",
             ""]
    if brief:
        if brief.get("hook"):
            lines += [f"**Hook:** {brief['hook']}", ""]
        if brief.get("facts"):
            lines += ["**Usable facts:**"]
            lines += [f"- {f}" for f in brief["facts"]]
            lines.append("")
        if brief.get("payoff"):
            lines += [f"**Payoff:** {brief['payoff']}", ""]
    if sources:
        lines += ["**Sources:**"]
        lines += [f"- {s['url']} ({s['title']})" for s in sources[:6]]
    open(BRIEF_PATH, "w").write("\n".join(lines) + "\n")
    print(f"[OK] brief -> {BRIEF_PATH}")


def apply_insight(insight):
    """Append to the wiki insights doc + index into the RAG store."""
    date = datetime.now().strftime("%Y-%m-%d")
    entry = (f"\n## {date} — {insight.get('title', 'insight')}\n"
             f"- **Finding:** {insight.get('finding', '')}\n"
             f"- **Apply to:** {insight.get('apply_to', 'unknown')}\n"
             f"- **Concrete change:** {insight.get('concrete_change', '')}\n"
             f"- **Status:** pending-review\n")
    with open(INSIGHTS_MD, "a") as f:
        f.write(entry)
    kb.add("insight", f"{date} {insight.get('title', '')}",
           f"apply_to={insight.get('apply_to','')}\nfinding: {insight.get('finding','')}\nchange: {insight.get('concrete_change','')}",
           source="research_loop", tags="workflow,insight,research")
    kb.sync_wiki()
    print(f"[OK] insight applied to KB -> {insight.get('apply_to')}")
    print("     (visible in future prompts via knowledge_base.py search; "
          "code changes need user sign-off)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--skip-web", action="store_true")
    a = ap.parse_args()

    print("[1/4] gathering web evidence ...")
    evidence, sources = gather_evidence(a.topic, a.skip_web)
    print(f"      {len(sources)} candidate sources")

    print("[2/4] extracting topic brief (27B) ...")
    brief = llm_brief(a.topic, evidence)
    if brief:
        write_brief(a.topic, brief, sources)
    else:
        print("      [warn] brief extraction failed — continuing without")

    print("[3/4] checking for workflow insight (27B) ...")
    insight = llm_insight(a.topic, brief)
    if insight and insight.get("applies"):
        print("[4/4] applying insight to knowledge base ...")
        apply_insight(insight)
    else:
        print("[4/4] no workflow-applicable insight — nothing to apply")


if __name__ == "__main__":
    main()
