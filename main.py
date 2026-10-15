#!/usr/bin/env python3
"""main.py — YouTube Shorts video generation pipeline with Harness R1.

Uses either:
  - localhost:8080 (llama.cpp / qwen36) — default, free, local
  - MiniMax H3 API (https://api-minimax.ai/v1) — --model h3 flag

Video generation:
  - FFmpeg + Edge TTS — default, free
  - MiniMax H3 T2V API — --video-gen h3 flag (paid, ~$1/video)

Harness R1 Integration:
  - 4 hook points: episode_start, before_llm_call, after_llm_response, on_error
  - Failure packet collection for each LLM failure
  - Empty script prevention: <5 lines → reject + retry
  - Infinite loop prevention: MAX_RETRIES=3 strict limit
  - Response length validation: <50 chars → reject

H3 T2V Video Generation:
  - Generates video via MiniMax H3 T2V API
  - Supports text prompts from LLM-generated scripts
  - Auto-downloads video to local file
"""
import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import time

import env  # loads .env (README step: cp .env.example .env) before any getenv()

# ── Frame rendering dependencies ──
from PIL import Image, ImageDraw, ImageFont

# Import harness modules
from harness import (
    llm_chat_with_hooks,
    collect_failure,
    hooks,
    HookRegistry,
    FailurePacket,
    generate_video_with_h3,
    _detect_local_model,
)

# Import frame animation module (Turbo LoRA / MiniMax T2V workflow)
try:
    from animate_frames import build_animated_video
    ANIMATIONS_AVAILABLE = True
except ImportError:
    ANIMATIONS_AVAILABLE = False

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import asyncio
    import edge_tts
except ImportError:
    edge_tts = None
    asyncio = None

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
MIN_VIDEO_SECONDS = 15
MAX_RETRIES = 3
MIN_SCRIPT_LINES = 5
MIN_RESPONSE_LENGTH = 50
# Quality gate: 재미와 유익함 둘 다 확보해야 영상 제작 진행
PASS_MIN_ENT = 3   # entertainment 하한 (재미)
PASS_MIN_EDU = 3   # education 하한 (유익함)
PASS_MIN_TOTAL = 9  # 총점 하한 (ent>=3+edu>=3+ret>=1 최소값)
# 교차 검증: 평가자 체인 (독립 모델, 작성자 편향 방지)
# 1차: 독립 평가자 서버 (LLM_CPU_EVALUATOR_URL, 기본 127.0.0.1:8085)
# 2차: EVALUATOR_REMOTE_URL 백업 (설정한 경우에만 사용)
EVALUATOR_CPU_URL = os.getenv("LLM_CPU_EVALUATOR_URL", "http://127.0.0.1:8085/v1/chat/completions")
EVALUATOR_REMOTE_URL = os.getenv("EVALUATOR_REMOTE_URL", "")
EVALUATOR_REMOTE_TIMEOUT = 180
EVALUATOR_CPU_TIMEOUT = 60  # thinking OFF: ~7s measured (was 135s with thinking ON)
TTS_VOICE = "en-US-ChristopherNeural"

# ── Optional video factory (any 8GB GPU box): LTX-Video 2B T2V + SDXL-Turbo thumb ──
# Offloads animation/thumbnail generation to the second PC over HTTP (free, ~$0).
# The local 3090 stays LLM-only. Override the host/port via env if it moves.
FACTORY_URL = os.getenv("VIDEO_FACTORY_URL", "")
LTX_W = int(os.getenv("LTX_W", "512"))
LTX_H = int(os.getenv("LTX_H", "512"))
LTX_STEPS = int(os.getenv("LTX_STEPS", "24"))
LTX_FRAMES = int(os.getenv("LTX_FRAMES", "49"))  # ~2.0s @24fps, looped to narration length

LLM_TRACE_PATTERNS = (
    # numbered-line traces
    "Line 1", "Line 2", "Line 3", "Line 4",
    "Line 5", "Line 6", "Line 7", "Line 8",
    "Line 9", "Line 10", "Line 11", "Line 12",
    # self-referential reasoning (specific multi-word — bare "The "/"Every " etc.
    # were killing legit content lines that start with common English words)
    "I need", "I will", "I should", "I must",
    "I'll write", "I'll count", "I'll frame",
    "The user", "User explicitly", "This task", "This request",
    # instruction echoes (full phrases, not bare determiners)
    "Each sentence", "Each fact", "Each line", "Every line must",
    "All sentences", "All lines", "All 8",
    "Let's write", "Now I will", "Now, here",
    "Need to write", "Need to count",
    # process markers
    "Revised", "Draft", "Here's a thinking", "Word count",
    "A good hook", "The hook",  # writing-process reasoning openers
    "START OUTPUT:", "START ",
    "Thinking Process ", "Analyze ", "Deconstruct ",
    "Check:", "Still ", "Close. ",
)


def llm_chat(prompt: str, max_tokens: int = 300, temperature: float = 0.8, model_backend: str = "local") -> str:
    """Call the LLM (local llama.cpp or MiniMax H3 API) with hooks."""
    return llm_chat_with_hooks(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        stage="generic",
        model_backend=model_backend,
    )


def _is_meta_line(s: str) -> bool:
    """Check if a line is a meta/tracking line from the LLM."""
    for p in LLM_TRACE_PATTERNS:
        if s.startswith(p):
            return True
    # Word-count traces: "Task(1) managers2 with3 simple4" — 3+ embedded digits
    if len(re.findall(r'\d', s)) >= 3:
        return True
    return False


def extract_script_lines(text: str, num_lines: int = 5) -> str:
    """Extract actual content lines from a reasoning model's output.

    Strategy: scan ALL lines and filter meta/reasoning traces per line.
    (The old `text[-1000:]` window silently dropped the first ~4 lines of a
    legit 8-line script — that truncation fed short/partial scripts to the
    evaluator and tanked entertainment scores.)
    Only lines starting with capital letter, Hangul, or a digit are kept.
    """
    text = text.replace("\\n", "\n")

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    content_lines = []
    for l in lines:
        if len(l) < 10:
            continue
        # Must start with capital letter, Hangul, or a digit (numbered list)
        first = l[0]
        if not (first.isupper() or "\uac00" <= first <= "\ud7a3" or first.isdigit()):
            continue
        # Normalize: strip leading "1. ", "2.", etc. BEFORE meta checks so
        # legitimately numbered facts are not rejected by the digit-count rule.
        stripped = re.sub(r'^\d+\.?\s+', '', l)
        if _is_meta_line(l) or (stripped != l and _is_meta_line(stripped)):
            continue
        # Clean up markdown
        l = re.sub(r'\*\*([^*]+)\*\*', r'\1', l)
        l = re.sub(r'\*([^*]+)\*', r'\1', l)
        l = l.strip()
        # Skip lines that are still meta after cleanup
        if l and len(l) > 10:
            content_lines.append(l)

    while content_lines and (
        'word' in content_lines[-1].lower() or
        'good' in content_lines[-1].lower() or
        'sentence' in content_lines[-1].lower() or
        'feature' in content_lines[-1].lower()
    ):
        content_lines.pop()

    if content_lines:
        return '\n'.join(content_lines[:num_lines])
    # No clean lines found — return empty so the caller can detect garbage
    # output and retry/fail loudly. (Previously fell back to raw lines, which
    # let word-count traces like "Task(1) managers2 with3..." into videos.)
    return ''


def _load_research_brief(max_age_h=48) -> str | None:
    """Load output/research_brief.md if fresh (research_loop.py writes it per topic)."""
    p = os.path.join(OUTPUT_DIR, "research_brief.md")
    try:
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_h * 3600:
            return open(p, encoding="utf-8").read().strip()
    except OSError:
        pass
    return None


def _brief_block(brief: str | None) -> str:
    """Injectable facts section for script prompts (empty if no brief)."""
    if not brief:
        return ""
    return (
        f'\n<research facts — use these concrete details where they fit, '
        f'prefer specifics over generalities>\n{brief[:3000]}\n</research facts>\n'
    )


def _prompt_variants(topic: str, brief: str | None = None) -> list[str]:
    """The 3 few-shot prompt variants for script generation."""
    b = _brief_block(brief)
    return [
        (
            f'<role>You are a veteran YouTube Shorts writer who has produced 500+ viral shorts. '
            f'Your scripts make people laugh AND teach them something concrete they can use immediately.</role>\n'
            f'<task>Write an 8-line script about: {topic}</task>\n'
            f'{b}'
            f'<rules>\n'
            f'- Line 1 MUST be a hook that stops scrolling in under 3 seconds\n'
            f'- Every line is one sentence, 15-30 words\n'
            f'- Balance: at least 3 lines must be witty/funny (metaphor, punchline), and at least 3 other lines must deliver a concrete fact or action viewers can use\n'
            f'- No numbers anywhere, no labels, no bullets, no meta text, no reasoning\n'
            f'</rules>\n'
            f'<examples style reference — match this tone and energy>\n'
            f'"Your phone notification sound was engineered by the same people who built slot machines — because it does the exact same thing to your brain."\n'
            f'"That free app you love is checking your location 47 times an hour and selling every ping to the highest bidder, including the ad for pizza next door."\n'
            f'"You don\'t need another productivity app. You need one boring alarm that kills all notifications for two hours — and trust it like a dentist frowns at your teeth."\n'
            f'</examples>\n'
            f'<output>Exactly 8 lines, one per line. Nothing else.</output>\n'
        ),
        (
            f'<role>You are a viral Shorts scriptwriter. Every line must make the viewer laugh or gasp, AND give them something concrete to do differently tomorrow.</role>\n'
            f'<task>Write 8 lines about: {topic}</task>\n'
            f'{b}'
            f'<rules>One sentence per line (15-30 words). Line 1 is a hook. Balance: at least 3 witty/funny lines AND at least 3 concrete-fact/action lines. No numbers, labels, bullets, meta text.\n'
            f'Match this energy: "You\'ve been drinking water wrong your whole life — and the fix takes four seconds every morning."\n'
            f'"The app you call free is actually billing you in data, location pings, and thumb swipes — it just invoices in a currency you can\'t see." '
            f'"Your brain has no volume knob for notifications, so the loudest voice wins — right now that\'s your phone, not you."</rules>\n'
            f'<output>8 lines, one per line. Nothing else.</output>\n'
        ),
        (
            f'<role>You write YouTube Shorts scripts. Your specialty: making boring topics feel like a friend telling you the funniest true thing at 2 AM.</role>\n'
            f'<task>About: {topic}</task>\n'
            f'{b}'
            f'<rules>8 lines, one sentence each (15-30 words). Line 1 = hook. '
            f'Balance: at least 3 witty/funny lines AND at least 3 concrete-fact/action lines. No numbers/labels/bullets/meta text.\n'
            f'Style reference: "Your calendar has seventeen meetings and zero reasons — that\'s where deadlines get invented."\n'
            f'"The inbox-zero people brag about is just a trash can they refuse to open."\n'
            f'"You aren\'t distracted; you\'re being hunted by nine billion dollars of attention engineering."</rules>\n'
            f'<output>8 lines, one per line. Nothing else.</output>\n'
        ),
        (
            f'<role>You are a comedy writer for YouTube Shorts who happens to know the topic deeply. '
            f'You alternate: one joke line, then one hard fact that lands like a mic drop — repeat until you have eight lines.</role>\n'
            f'<task>Write 8 lines about: {topic}</task>\n'
            f'{b}'
            f'<rules>Line 1 = hook. Alternate funny line / concrete useful line. One sentence per line (15-30 words). '
            f'No numbers, labels, bullets, meta text.\n'
            f'Style reference: "Your alarm clock is the most honest app you own — it tells you the truth at 7 AM and doesn\'t care if you like it."\n'
            f'"The snooze button was invented by people who never wanted to live; it just buys you nine more minutes of pretending."\n'
            f'"Your phone battery percentage is the only relationship in your life that honestly reports how much time is left."</rules>\n'
            f'<output>8 lines, one per line. Nothing else.</output>\n'
        ),
    ]


def _candidate_lines(raw: str) -> list[str]:
    """Parse raw LLM output into clean content lines (JSON array or line extraction)."""
    lines = []
    try:
        bracket = raw.find('[')
        if bracket >= 0:
            arr = json.loads(raw[bracket:])
            if isinstance(arr, list):
                lines = [s.strip() for s in arr if isinstance(s, str) and len(s.strip()) > 3]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Fallback: strip START OUTPUT marker and extract clean lines
    if len(lines) < MIN_SCRIPT_LINES:
        result = raw.replace("START OUTPUT:", "", 1).strip()
        extracted = extract_script_lines(result, 12)
        if extracted:
            lines = [l for l in extracted.split("\n") if l.strip()]

    clean = [l for l in lines if not _is_meta_line(l)]
    # Drop a truncated trailing line (no terminal punctuation, short) —
    # happens when max_tokens cuts the last sentence mid-way.
    if clean and len(clean[-1]) < 80 and not clean[-1].rstrip().endswith((".", "!", "?")):
        clean = clean[:-1]
    return clean[:12]


def generate_script(topic: str, model_backend: str = "local") -> str:
    """Generate a YouTube Shorts script (8-12 lines for 40-60 second video).

    Harness R1: 빈 스크립트 방지 + MAX_RETRIES=3 엄격 적용.

    qwen38 같은 reasoning 모델은 content에 메타 텍스트(워드 카운트, Line N:,
    재검토 트래이스)를 섞어내는 경우가 있으므로, 여러 프롬프트 변형을 시도해
    가장 많은 clean line을 뽑아온 결과를 채택한다.
    """
    best_script = ""
    best_clean_count = 0
    last_raw = ""
    brief = _load_research_brief()

    for prompt in _prompt_variants(topic, brief):
        raw = llm_chat_with_hooks(
            prompt,
            max_tokens=1400,  # 8 lines × ~25 words + margin so line 8 is never truncated
            temperature=0.5,
            stage="generate_script",
            max_retries=MAX_RETRIES,
            min_response_length=MIN_RESPONSE_LENGTH,
            model_backend=model_backend,
        )
        last_raw = raw or ""
        if not raw:
            continue

        clean = _candidate_lines(raw)
        if len(clean) > best_clean_count:
            best_clean_count = len(clean)
            best_script = "\n".join(clean)
        if best_clean_count >= MIN_SCRIPT_LINES:
            break  # good enough — stop burning LLM calls

    if best_clean_count < MIN_SCRIPT_LINES:
        # LLM이 모든 변형에서 빈/불량 응답
        collect_failure(
            task=f"script generation about {topic}",
            stage="generate_script",
            attempt=len(_prompt_variants(topic)),
            prompt_snippet=_prompt_variants(topic)[0][:500],
            raw_response=last_raw,
            parsed_output=[],
            error_type="empty_all_retries",
            error_message=f"All prompt variants produced < {MIN_SCRIPT_LINES} clean lines (best: {best_clean_count})",
            expected="8+ plain-sentence lines with no meta text",
            actual_pattern=f"best variant yielded {best_clean_count} clean lines",
        )

    return best_script


def generate_best_script(topic: str, model_backend: str = "local", max_candidates: int = 4) -> tuple[str, dict]:
    """Best-of-N script generation with cross-validated scoring.

    The 27B author model is inconsistent — the same topic can yield a
    gate-passing script or a dry one on different runs (measured: 0/5 pass
    on single-shot, yet individual good samples exist). Instead of adopting
    the first candidate with enough lines, generate all prompt variants,
    score each candidate (local + remote 35B cross-check), and adopt the
    first one that passes the quality gate — or the highest-scoring one.

    Returns (script, score_dict). script may be '' if no usable candidate.
    """
    candidates = []
    brief = _load_research_brief()
    for prompt in _prompt_variants(topic, brief):
        raw = llm_chat_with_hooks(
            prompt,
            max_tokens=1400,
            temperature=0.7,  # slightly higher temp → more diverse samples
            stage="generate_script",
            max_retries=MAX_RETRIES,
            min_response_length=MIN_RESPONSE_LENGTH,
            model_backend=model_backend,
        )
        if not raw:
            continue
        clean = _candidate_lines(raw)
        if len(clean) >= MIN_SCRIPT_LINES:
            candidates.append("\n".join(clean))

    # Evaluate most-complete candidates first (line count as completeness proxy)
    candidates.sort(key=lambda s: -len(s.split("\n")))
    best_script = ""
    best_score = {"entertainment": 0, "education": 0, "retention": 0, "total": 0, "pass": False}

    def _rank(score: dict):
        # Selection must match the gate: min(ent, edu) is the binding constraint
        # (a candidate at ent=4/edu=2 total 9 LOSES to ent=3/edu=3 — the latter
        # can pass with a nudge, the former cannot). Total breaks ties.
        return (min(int(score.get("entertainment", 0)), int(score.get("education", 0))),
                int(score.get("total", 0)))

    for cand in candidates[:max_candidates]:
        score = evaluate_script(cand, topic, model_backend=model_backend)
        print(f"  [BEST-OF-N] candidate ({len(cand.split(chr(10)))} lines): ent={score.get('entertainment')} edu={score.get('education')} ret={score.get('retention')} total={score.get('total')}/15 pass={score.get('pass')}")
        if _rank(score) > _rank(best_score):
            best_script, best_score = cand, score
        if score.get("pass", False):
            break  # gate passed — stop burning LLM calls

    if not best_script:
        collect_failure(
            task=f"script generation about {topic}",
            stage="generate_best_script",
            attempt=len(_prompt_variants(topic)),
            prompt_snippet=_prompt_variants(topic)[0][:500],
            raw_response="",
            parsed_output=[],
            error_type="empty_all_retries",
            error_message=f"No candidate with >= {MIN_SCRIPT_LINES} clean lines",
            expected="8+ plain-sentence lines with no meta text",
            actual_pattern="all candidates below minimum line count",
        )

    return best_script, best_score


def _post_evaluator(url: str, model: str, prompt: str, timeout: int, max_tokens: int = 4096) -> dict | None:
    """POST one evaluator endpoint, parse the score JSON. Returns dict or None.

    The 35B evaluators are thinking models — without enable_thinking=False the
    whole token budget goes into reasoning_content and the score JSON never
    appears (measured: 135s + empty content vs 6.5s + clean JSON with it off).
    """
    import requests

    try:
        r = requests.post(
            url,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        txt = (msg.get("content") or "").strip()
        if not txt:
            rc = msg.get("reasoning_content") or ""
            i, j = rc.rfind("{"), rc.rfind("}")
            txt = rc[i:j + 1] if (i != -1 and j > i) else rc
        try:
            return json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            match = re.search(r'\{[^{}]*"entertainment"[^{}]*\}', txt, re.S)
            if match:
                try:
                    return json.loads(match.group())
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception as e:
        print(f"  [WARN] 평가자 {url} 호출 실패: {e}")
    return None


def _detect_cpu_evaluator_model() -> str:
    from urllib.request import urlopen
    try:
        r = urlopen(EVALUATOR_CPU_URL.replace("/chat/completions", "/models"), timeout=5)
        data = json.loads(r.read())
        models = data.get("data") or data.get("models") or []
        if models:
            name = models[0].get("id") or models[0].get("name") or ""
            if name:
                return name
    except Exception:
        pass
    return "35b-cpu"


def _remote_evaluate(prompt: str) -> tuple[dict | None, bool]:
    """Cross-check with an independent 35B evaluator (not the author model).

    평가자 체인:
      1차: 독립 평가자 서버 (LLM_CPU_EVALUATOR_URL)
      2차: EVALUATOR_REMOTE_URL 백업 (설정한 경우에만)
    Returns (result_dict_or_None, reliable_bool).
    """
    result = _post_evaluator(EVALUATOR_CPU_URL, _detect_cpu_evaluator_model(), prompt,
                             EVALUATOR_CPU_TIMEOUT, max_tokens=600)
    if result is not None:
        return result, True
    if EVALUATOR_REMOTE_URL:  # unset by default — calling "" just logs a URL error
        result = _post_evaluator(EVALUATOR_REMOTE_URL, "remote-evaluator", prompt,
                                 EVALUATOR_REMOTE_TIMEOUT, max_tokens=4096)
        if result is not None:
            return result, True
    return None, False


def evaluate_script(script: str, topic: str, model_backend: str = "local") -> dict:
    """Evaluate script quality on entertainment, education, and retention.

    재미(entertainment)와 유익함(education)이 핵심 기준 — 둘 다 3점 이상이어야 pass.
    교차 검증: 로컬 작성자 모델 + 원격 35B 평가자 각각 점수 매기고 평균.
    두 평가자가 어느 차원에서 2점 이상 갈리면 [DISAGREE] 경고 (편향 감지).
    """
    prompt = textwrap.dedent(f"""
    You are a strict YouTube Shorts editor. Evaluate this script about: {topic}

    SCRIPT:
    {script}

    Score each dimension 1-5 using these anchors (be strict — most scripts deserve 2-3, not 4-5):
    - entertainment: 1=dry lecture, 2=facts only no personality, 3=engaging hook+voice but not funny, 4=witty lines a viewer would quote, 5=laugh-out-loud AND quotable
    - education: 1=nothing concrete, 2=vague wisdom, 3=one solid usable fact or action, 4=several concrete takeaways, 5=actionable and surprising
    - retention: 1=skippable from line 2, 3=decent pacing, 5=impossible to skip

    Output ONLY this JSON (no extra text, no reasoning):
    {{"entertainment": 3, "education": 3, "retention": 3, "total": 9}}
    """)

    def _score_once():
        raw = llm_chat_with_hooks(
            prompt,
            max_tokens=200,
            temperature=0.2,  # low temp for consistent scoring
            stage="evaluate_script",
            max_retries=1,
            min_response_length=10,  # score JSON is short (~45 chars); 50 would always reject
            model_backend=model_backend,
        )
        if raw:
            try:
                return json.loads(raw), True
            except (json.JSONDecodeError, TypeError):
                match = re.search(r'\{[^{}]*"entertainment"[^{}]*\}', raw)
                if match:
                    try:
                        return json.loads(match.group()), True
                    except (json.JSONDecodeError, TypeError):
                        pass
        return None, False

    result, reliable = _score_once()
    if not reliable:
        # 평가가 신뢰성 없으면 한 번 더 시도 — 기본값으로 조용히 대체하지 않음
        result, reliable = _score_once()

    # ── 교차 검증: 원격 35B 평가자 (독립 모델) ──
    remote_result, remote_reliable = _remote_evaluate(prompt)
    if not remote_reliable:
        print("  [WARN] 교차 검증 불가 — 로컬 점수만 사용")

    local_scores = None
    if reliable and result is not None:
        local_scores = {k: int(result.get(k, 0)) for k in ("entertainment", "education", "retention")}
    else:
        # 로컬 평가 실패 → 보수적 점수 (기존 동작 유지)
        local_scores = {"entertainment": 2, "education": 2, "retention": 2}
        print("  [WARN] 로컬 평가 신뢰성 없음 — 보수적 점수 사용")

    if remote_reliable and remote_result is not None:
        remote_scores = {k: int(remote_result.get(k, 0)) for k in ("entertainment", "education", "retention")}
        # 두 평가자 평균 (자기 평가 편향 방지)
        merged = {k: round((local_scores[k] + remote_scores[k]) / 2) for k in local_scores}
        print(f"  [CROSS-CHECK] 로컬={local_scores} 원격35B={remote_scores} → 평균={merged}")
        # 편향 감지: 어느 차원에서 2점 이상 갈림
        disagree = [k for k in local_scores if abs(local_scores[k] - remote_scores[k]) >= 2]
        if disagree:
            print(f"  [DISAGREE] 평가자 의견 차이 {disagree} (로컬={local_scores}, 원격={remote_scores}) — 평균 점수 사용")
        result = merged
    else:
        result = local_scores

    ent = int(result.get("entertainment", 0))
    edu = int(result.get("education", 0))
    ret = int(result.get("retention", 0))
    total = ent + edu + ret
    result["entertainment"], result["education"], result["retention"] = ent, edu, ret
    result["total"] = total
    # 재미와 유익함 둘 다 확보해야 통과 (총점만 높은 건 안 됨)
    result["pass"] = (ent >= PASS_MIN_ENT and edu >= PASS_MIN_EDU and total >= PASS_MIN_TOTAL)
    result["reliable"] = reliable or remote_reliable

    return result


def generate_title(topic: str, model_backend: str = "local") -> str:
    """Generate a YouTube Shorts title."""
    prompt = (
        f"Write a YouTube Shorts title about: {topic}\n"
        f"Under 50 characters. Just the title text. No explanation, no thinking, no quotes. Just output the title.\n"
    )
    raw = llm_chat_with_hooks(
        prompt,
        max_tokens=80,
        temperature=0.5,
        stage="generate_title",
        max_retries=2,
        min_response_length=10,
        model_backend=model_backend,
    )
    if not raw:
        return f"{topic} Shorts"
    # qwen38-style models often mix reasoning into content; pick the best
    # candidate line instead of blindly taking the first.
    candidates = []
    for l in raw.split("\n"):
        t = l.strip().strip('"\'')
        if not t or len(t) > 60:
            continue
        if _is_meta_line(t):
            continue
        # Skip lines that are clearly reasoning/instruction echoes
        if re.search(r'(?i)(respond|thinking|explanation|no quotes|under \d+ char|the user|user explicitly)', t):
            continue
        candidates.append(t)
    title = candidates[-1] if candidates else (topic[:42].strip() + " — Quick Facts")
    # Remove meta artifacts
    title = re.sub(r"Here's a thinking process:", "", title).strip()
    title = re.sub(r"(?i)(Each|All|The|This|Every|These|Let's|Now,|Count:|Need|ies|I need|I will|I should|I must)\b", "", title).strip()
    title = re.sub(r"^\d+\.\s+", "", title).strip()
    title = re.sub(r"[^\w\s\-!?.,\uac00-\ud7a3\u2014]", " ", title).strip()
    title = re.sub(r"\s+", " ", title)  # collapse double spaces from char stripping
    meta_phrases = ["Here's a thinking", "Each sentence", "All sentences", "The request", "Each fact", "Each line", "START"]
    for mp in meta_phrases:
        title = re.sub(rf"(?i)^{re.escape(mp)}\s+", "", title).strip()
    title = title[:100].strip()
    if not title or len(title) < 3:
        title = topic[:60]
    return title


def generate_tags(topic: str, model_backend: str = "local") -> list:
    """Generate YouTube tags."""
    prompt = (
        f"Give me 3 comma-separated YouTube tags for: {topic}\n"
        f"Only tags, no quotes, no numbers.\n"
    )
    raw = llm_chat_with_hooks(
        prompt,
        max_tokens=60,
        temperature=0.8,
        stage="generate_tags",
        max_retries=1,
        min_response_length=MIN_RESPONSE_LENGTH,
        model_backend=model_backend,
    )
    if not raw:
        return ["ytshorts", "tech", "tutorial"]
    # Pick only plausible tags: short, no spaces-heavy reasoning lines
    echo_re = re.compile(
        r'(?i)^(only|no |give me|comma|the user|user explicitly|respond|thinking)'
        r'|\b(no quotes|no numbers|comma-separated|just tags)\b'
    )
    tags = []
    for l in raw.split("\n"):
        t = l.strip().strip('"\'').rstrip(".,;")
        if not t or len(t) > 25:
            continue
        if _is_meta_line(t):
            continue
        if echo_re.search(t):
            continue
        tags.append(t.lower())
    # Also accept comma-separated form from a single line (same filters apply)
    if not tags:
        for l in raw.split("\n"):
            parts = [p.strip().strip('"\'').rstrip(".,;")
                     for p in l.replace("،", ",").split(",")]
            parts = [p for p in parts
                     if 1 < len(p) <= 25 and not _is_meta_line(p) and not echo_re.search(p)]
            if 2 <= len(parts) <= 5:
                tags.extend(p.lower() for p in parts)
    tags = list(dict.fromkeys(tags))  # dedupe, keep order
    if len(tags) < 3:
        fallback = ["ytshorts", "tech", "tutorial"]
        tags.extend(fallback[: 3 - len(tags)])
    return tags[:3]


# ── Visual theme (PIL frame rendering) ──
THEME_ACCENT = (255, 209, 102)   # gold — hook line + progress bar
PALETTES = [                      # (top, bottom) gradient pairs, cycled per segment
    ((13, 16, 42), (48, 30, 88)),    # navy → purple
    ((10, 20, 46), (20, 52, 96)),    # navy → deep blue
    ((16, 12, 44), (74, 30, 84)),    # dark violet → magenta
    ((10, 24, 40), (18, 66, 74)),    # navy → teal
]


def _make_background(video_w: int, video_h: int, index: int = 0) -> "Image.Image":
    """Gradient background + soft radial glow behind the text zone.

    Built at low resolution (27x48) and upscaled with bilinear — fast enough
    for per-segment rendering (~ms), and the palette shifts subtly per segment
    so the video feels alive instead of a flat card.
    """
    from PIL import Image
    top_c, bot_c = PALETTES[index % len(PALETTES)]
    sw, sh = 27, 48
    grad = Image.new("RGB", (sw, sh))
    px = grad.load()
    for y in range(sh):
        t = y / (sh - 1)
        c = tuple(int(top_c[k] + (bot_c[k] - top_c[k]) * t) for k in range(3))
        for x in range(sw):
            px[x, y] = c
    grad = grad.resize((video_w, video_h), Image.BILINEAR)

    # Soft radial glow centered on the text zone (~45% height)
    glow = Image.new("L", (sw, sh), 0)
    gpx = glow.load()
    cx, cy = sw / 2, sh * 0.45
    maxr = max(sw, sh) * 0.8
    for y in range(sh):
        for x in range(sw):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            gpx[x, y] = int(max(0.0, 1 - d / maxr) ** 2 * 70)
    glow = glow.resize((video_w, video_h), Image.BILINEAR)

    white = Image.new("RGB", (video_w, video_h), (255, 255, 255))
    return Image.composite(white, grad, glow)


def _render_frame(line: str, frame_path: str, video_w: int = 1080, video_h: int = 1920,
                  index: int = 0, total: int = 1) -> None:
    """Render a text frame with PIL — proper word wrap + center alignment.

    drawtext can't wrap long lines (text overflows both edges and gets cut),
    so we rasterize the frame with Pillow: gradient background, wrapped text,
    centered. The hook line (index 0) is rendered in gold with an underline;
    a progress bar at the bottom shows position in the script.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = _make_background(video_w, video_h, index)
    draw = ImageDraw.Draw(img)
    is_hook = (index == 0)
    text_color = THEME_ACCENT if is_hook else (255, 255, 255)

    font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    if not os.path.exists(font_path):
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    # Hook line gets a larger font range
    size_range = range(84, 36, -4) if is_hook else range(72, 36, -4)
    for font_size in size_range:
        font = ImageFont.truetype(font_path, font_size)
        # wrap text to fit within 90% of frame width
        max_width = int(video_w * 0.88)
        words = line.split()
        wrapped = []
        cur = ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=font) <= max_width:
                cur = test
            else:
                if cur:
                    wrapped.append(cur)
                cur = w
        if cur:
            wrapped.append(cur)

        line_h = font_size * 1.35
        total_h = line_h * len(wrapped)
        # text block must fit vertically too (max 60% of frame height)
        if len(wrapped) <= 6 and total_h <= video_h * 0.6:
            break

    x_center = video_w / 2
    y_start = (video_h - line_h * len(wrapped)) / 2 - video_h * 0.03
    for i, wl in enumerate(wrapped):
        tw = draw.textlength(wl, font=font)
        x = x_center - tw / 2
        y = y_start + i * line_h
        # outline effect: draw slightly offset dark copies first
        for dx, dy in ((-2, -2), (2, -2), (-2, 2), (2, 2)):
            draw.text((x + dx, y + dy), wl, font=font, fill=(0, 0, 0))
        draw.text((x, y), wl, font=font, fill=text_color)

    # Hook underline bar below the text block
    if is_hook:
        bar_w = min(int(video_w * 0.34), int(max(draw.textlength(wl, font=font) for wl in wrapped)))
        bx = x_center - bar_w / 2
        by = y_start + line_h * len(wrapped) + 26
        draw.rounded_rectangle([bx, by, bx + bar_w, by + 12], radius=6, fill=THEME_ACCENT)

    # Progress bar at the bottom (position in script)
    bar_y = video_h - 96
    track_w = int(video_w * 0.72)
    track_x = (video_w - track_w) // 2
    draw.rounded_rectangle([track_x, bar_y, track_x + track_w, bar_y + 14], radius=7, fill=(58, 62, 92))
    frac = (index + 1) / max(total, 1)
    fill_w = max(14, int(track_w * frac))
    draw.rounded_rectangle([track_x, bar_y, track_x + fill_w, bar_y + 14], radius=7, fill=THEME_ACCENT)

    # Three small dots top-left — active dot highlights the current segment
    for k in range(3):
        dx0 = 64 + k * 30
        col = THEME_ACCENT if k == (index % 3) else (92, 97, 132)
        draw.ellipse([dx0, 72, dx0 + 14, 86], fill=col)

    img.save(frame_path)


def escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext filter."""
    # Use a simple approach: keep only alnum, space, and safe punctuation
    text = re.sub(r'[^a-zA-Z0-9\s\-!?.,]', '', text)
    text = text[:100]
    text = text.strip()
    text = text.replace("'", "\\'")
    return text


def _tts_line(text: str, out_path: str) -> float:
    """Generate TTS audio for one line via edge-tts. Returns duration in seconds."""
    async def _run():
        c = edge_tts.Communicate(text, TTS_VOICE)
        await c.save(out_path)
    asyncio.run(_run())
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", out_path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return 3.0


def _render_caption_overlay(line: str, frame_path: str, video_w: int = 1080, video_h: int = 1920,
                            index: int = 0, total: int = 1) -> None:
    """Render a TRANSPARENT caption PNG (RGBA) with wrapped text, for overlay on a
    video background. Reuses the word-wrap + sizing logic of _render_frame so captions
    stay crisp and consistent; no solid background (alpha=0) so the T2V video shows
    through. Hook line is gold with an underline; a thin progress bar sits at the bottom.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (video_w, video_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    is_hook = (index == 0)
    text_color = (255, 209, 102, 255) if is_hook else (255, 255, 255, 255)

    font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
    if not os.path.exists(font_path):
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    size_range = range(84, 36, -4) if is_hook else range(72, 36, -4)
    wrapped = [line]
    font = None
    line_h = 0
    for font_size in size_range:
        font = ImageFont.truetype(font_path, font_size)
        max_width = int(video_w * 0.88)
        words = line.split()
        wrapped = []
        cur = ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=font) <= max_width:
                cur = test
            else:
                if cur:
                    wrapped.append(cur)
                cur = w
        if cur:
            wrapped.append(cur)
        line_h = font_size * 1.35
        total_h = line_h * len(wrapped)
        if len(wrapped) <= 6 and total_h <= video_h * 0.6:
            break

    x_center = video_w / 2
    y_start = (video_h - line_h * len(wrapped)) / 2 - video_h * 0.03
    for i, wl in enumerate(wrapped):
        tw = draw.textlength(wl, font=font)
        x = x_center - tw / 2
        y = y_start + i * line_h
        # dark outline for legibility over the video
        for dx, dy in ((-2, -2), (2, -2), (-2, 2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)):
            draw.text((x + dx, y + dy), wl, font=font, fill=(0, 0, 0, 220))
        draw.text((x, y), wl, font=font, fill=text_color)

    if is_hook:
        bar_w = min(int(video_w * 0.34), int(max(draw.textlength(wl, font=font) for wl in wrapped)))
        bx = x_center - bar_w / 2
        by = y_start + line_h * len(wrapped) + 26
        draw.rounded_rectangle([bx, by, bx + bar_w, by + 12], radius=6, fill=(255, 209, 102, 255))

    # progress bar
    track_w = int(video_w * 0.72)
    track_x = (video_w - track_w) // 2
    bar_y = video_h - 96
    draw.rounded_rectangle([track_x, bar_y, track_x + track_w, bar_y + 14], radius=7, fill=(255, 255, 255, 60))
    frac = (index + 1) / max(total, 1)
    fill_w = max(14, int(track_w * frac))
    draw.rounded_rectangle([track_x, bar_y, track_x + fill_w, bar_y + 14], radius=7, fill=(255, 209, 102, 200))

    img.save(frame_path)


def _factory_t2v(prompt: str, out_path: str, seed: int = 42) -> bool:
    """Request one animated B-roll clip from the video factory /t2v endpoint.
    Returns True on success (clip written to out_path), False otherwise.
    """
    import base64
    import requests
    try:
        r = requests.post(
            f"{FACTORY_URL}/t2v",
            json={"prompt": prompt, "width": LTX_W, "height": LTX_H,
                  "frames": LTX_FRAMES, "steps": LTX_STEPS, "seed": seed},
            timeout=300,
        )
        d = r.json()
        if not d.get("ok") or not d.get("mp4_base64"):
            print(f"    [FACTORY] t2v failed: {d.get('error')}")
            return False
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(d["mp4_base64"]))
        return True
    except Exception as e:
        print(f"    [FACTORY] t2v error: {e}")
        return False


def _visual_prompt(line: str, topic: str, model_backend: str) -> str:
    """Convert one abstract script line into a concrete T2V visual prompt (local LLM, free).
    Returns the original line if conversion fails — same behavior as before.
    """
    prompt = (
        "You write text-to-video prompts for B-roll behind narrated captions.\n"
        f"Video topic: {topic}\n"
        f"Narration line: {line}\n"
        "Rewrite this as ONE concrete visual scene a text-to-video model can render: "
        "specific physical objects, action, lighting, camera angle. No people talking, "
        "no text, no metaphors-as-objects. Under 40 words. Output only the visual prompt."
    )
    try:
        raw = llm_chat_with_hooks(
            prompt, max_tokens=80, temperature=0.5, stage="broll_visual_prompt",
            max_retries=1, min_response_length=8, model_backend=model_backend,
        )
        t = (raw or "").strip().strip('"\'')
        # drop the last line-free, single-line, non-meta result
        for l in reversed(t.split("\n")):
            l = l.strip().strip('"\'')
            if 15 <= len(l) <= 300 and not _is_meta_line(l):
                return l
        if 15 <= len(t) <= 300:
            return t
    except Exception as e:
        print(f"    [WARN] visual prompt conversion failed line: {e}")
    return line[:200]


def _factory_health() -> dict:
    import requests
    try:
        return requests.get(f"{FACTORY_URL}/health", timeout=10).json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _build_video_ltx(lines: list, output_path: str, topic: str = "", model_backend: str = "local") -> str:
    """Build the Shorts video using LTX-Video 2B (video factory) as animated B-roll backgrounds.

    For each script line:
      1. edge-tts narration (duration drives the segment length)
      2. one LTX-Video 2B clip (factory /t2v) as a full-bleed blurred background
      3. a transparent caption overlay (wrapped text) composited on top
      4. mux narration + video into a segment
    Segments are concatenated. The local 3090 is never touched (LLM-only).
    Falls back to the plain local FFmpeg path if the factory is unreachable.
    """
    import base64
    health = _factory_health()
    if not health.get("ok"):
        print(f"  [FACTORY] unreachable ({health.get('error')}) — falling back to local FFmpeg")
        return _build_video_local(lines, output_path)

    fps = 30
    video_w, video_h = 1080, 1920
    pad = 0.4
    min_segment = (MIN_VIDEO_SECONDS + len(lines) - 1) / len(lines)

    clips_dir = os.path.join(OUTPUT_DIR, "ltx_clips")
    cap_dir = os.path.join(OUTPUT_DIR, "ltx_captions")
    audio_dir = os.path.join(OUTPUT_DIR, "tts_audio")
    for d in (clips_dir, cap_dir, audio_dir):
        os.makedirs(d, exist_ok=True)

    segments = []
    seg_files = []
    list_file = os.path.join(OUTPUT_DIR, "segments_list.txt")
    tts_failures = 0
    t2v_failures = 0
    try:
        for i, line in enumerate(lines):
            # 1) narration
            audio_path = os.path.join(audio_dir, f"line_{i:04d}.mp3")
            if edge_tts is not None:
                try:
                    dur = _tts_line(line, audio_path)
                except Exception as e:
                    print(f"    [WARN] TTS failed line {i+1}: {e} — silence")
                    tts_failures += 1
                    dur = 3.0
                    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                                    "-i", f"anullsrc=r=22050:cl=mono:d={dur}",
                                    "-c:a", "libmp3lame", audio_path],
                                   check=True, capture_output=True)
            else:
                dur = 3.0
                subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                                "-i", f"anullsrc=r=22050:cl=mono:d={dur}",
                                "-c:a", "libmp3lame", audio_path],
                               check=True, capture_output=True)
            seg_dur = max(dur + pad, min_segment)

            # 2) animated B-roll from the factory (concrete visual prompt per line)
            clip_path = os.path.join(clips_dir, f"clip_{i:04d}.mp4")
            scene_prompt = _visual_prompt(line, topic, model_backend)
            if scene_prompt != line[:200]:
                print(f"    [BROLL] line {i+1} visual: {scene_prompt[:80]}...")
            ok = _factory_t2v(scene_prompt, clip_path, seed=42 + i)
            if not ok:
                t2v_failures += 1
                # degrade to a still frame for this segment
                still_path = os.path.join(clips_dir, f"still_{i:04d}.png")
                _render_frame(line, still_path, video_w, video_h, index=i, total=len(lines))
                bg_input = "-loop", "1", "-i", still_path
                bg_filter = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=12:1,eq=brightness=-0.12"
            else:
                bg_input = "-stream_loop", "-1", "-i", clip_path
                bg_filter = ("scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                             "boxblur=18:2,eq=brightness=-0.16:saturation=0.85")

            # 3) caption overlay
            cap_path = os.path.join(cap_dir, f"cap_{i:04d}.png")
            _render_caption_overlay(line, cap_path, video_w, video_h, index=i, total=len(lines))

            # 4) mux: looped blurred video background + caption overlay + narration
            seg_path = os.path.join(OUTPUT_DIR, f"seg_{i:04d}.mp4")
            cmd = ["ffmpeg", "-y"] + list(bg_input) + ["-i", cap_path, "-i", audio_path,
                "-filter_complex", f"[0:v]{bg_filter}[bg];[bg][1:v]overlay=0:0,format=yuv420p[v]",
                "-map", "[v]", "-map", "2:a", "-t", f"{seg_dur:.3f}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22", "-r", str(fps),
                "-c:a", "aac", "-b:a", "128k", "-shortest", seg_path]
            subprocess.run(cmd, check=True, capture_output=True)

            # verify segment has video+audio
            pv = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", seg_path],
                                capture_output=True, text=True)
            pa = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", seg_path],
                                capture_output=True, text=True)
            if "video" not in pv.stdout or "audio" not in pa.stdout:
                raise RuntimeError(f"LTX SEGMENT FAIL {i+1}: missing video/audio track")
            seg_files.append(seg_path)
            segments.append(seg_path)

        if tts_failures == len(lines):
            raise RuntimeError("LTX TTS FAIL: all lines failed")
        if t2v_failures:
            print(f"  [FACTORY] {t2v_failures}/{len(lines)} segments used still-frame fallback")

        with open(list_file, "w") as f:
            for fp in seg_files:
                f.write(f"file '{fp}'\n")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
                         "-c:a", "aac", "-b:a", "128k", output_path],
                        check=True, capture_output=True)
    finally:
        for d in (clips_dir, cap_dir, audio_dir):
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    os.remove(os.path.join(d, fn))
                os.rmdir(d)
        if os.path.exists(list_file):
            os.remove(list_file)
        for p in seg_files:
            if os.path.exists(p):
                os.remove(p)

    ok, iss = _verify_video(output_path)
    if not ok:
        raise RuntimeError(f"Video verification failed: {'; '.join(iss)}")
    print("  [VIDEO VERIFY] OK — LTX B-roll video valid")
    return output_path


def build_video(
    script: str,
    output_path: str,
    video_gen_model: str = "local",
    video_anim: str = "none",
    topic: str = "",
    model_backend: str = "local",
):
    """Create an MP4 from a script using ffmpeg or H3 T2V.

    Local mode: per-line edge-tts narration + drawtext frames.
    Each segment's duration matches its narration (with a small pad), so the
    final video length equals the total audio length — no dead air, no cutoff.

    Frame animation (Turbo LoRA / MiniMax T2V workflow):
        When video_gen_model == "h3" and video_anim in ("hook", "full"),
        each PIL frame is sent to MiniMax H3 T2V API for animation.

    Args:
        script: Script lines to display on screen.
        output_path: Output video file path.
        video_gen_model: "local" (FFmpeg) or "h3" (MiniMax T2V API).
        video_anim: "none" (default), "hook" (first line), "full" (every segment).

    Returns:
        Path to generated video.
    """
    lines = [l.strip() for l in script.split("\n") if l.strip()]
    if not lines:
        raise ValueError("Empty script — cannot build video")

    if video_gen_model == "h3" and video_anim != "none":
        # Turbo LoRA / MiniMax T2V frame animation
        if not ANIMATIONS_AVAILABLE:
            print("  [WARN] animate_frames module not found — falling back to H3 T2V")
        else:
            frames_dir = os.path.join(OUTPUT_DIR, "frames")
            return build_animated_video(
                script=script,
                frames_dir=frames_dir,
                output_path=output_path,
                mode=video_anim,
            )
    if video_gen_model == "h3":
        return _build_video_h3_t2v(lines, output_path)

    if video_gen_model == "ltx":
        return _build_video_ltx(lines, output_path, topic=topic, model_backend=model_backend)

    # Local FFmpeg pipeline with TTS narration
    return _build_video_local(lines, output_path)


def _build_video_local(lines: list, output_path: str) -> str:
    """Local FFmpeg pipeline with TTS narration — per-line still frames.

    Each segment's duration matches its narration (with a small pad), so the
    final video length equals the total audio length — no dead air, no cutoff.
    """
    fps = 30
    video_w, video_h = 1080, 1920
    pad = 0.4  # small pause between lines
    min_segment = (MIN_VIDEO_SECONDS + len(lines) - 1) / len(lines)

    frames_dir = os.path.join(OUTPUT_DIR, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    audio_dir = os.path.join(OUTPUT_DIR, "tts_audio")
    os.makedirs(audio_dir, exist_ok=True)

    segments = []  # (frame_path, audio_path, duration)
    seg_files = []
    list_file = os.path.join(OUTPUT_DIR, "segments_list.txt")
    tts_failures = 0
    try:
        for i, line in enumerate(lines):
            frame_path = os.path.join(frames_dir, f"frame_{i:04d}.png")
            _render_frame(line, frame_path, video_w, video_h, index=i, total=len(lines))

            # ── 단계 검증 [FRAME] — 파일 존재 + 해상도 확인 (잘림/빈 프레임 방지) ──
            if not os.path.exists(frame_path) or os.path.getsize(frame_path) < 10_000:
                raise RuntimeError(f"FRAME FAIL line {i+1}: 파일 없음 또는 빈 프레임 ({frame_path})")
            from PIL import Image as _Img
            with _Img.open(frame_path) as _im:
                if _im.size != (video_w, video_h):
                    raise RuntimeError(f"FRAME FAIL line {i+1}: 해상도 {_im.size} != {(video_w, video_h)}")

            audio_path = os.path.join(audio_dir, f"line_{i:04d}.mp3")
            if edge_tts is not None:
                try:
                    dur = _tts_line(line, audio_path)
                except Exception as e:
                    print(f"  [WARN] TTS failed for line {i+1}: {e} — using silence")
                    tts_failures += 1
                    dur = 3.0
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=22050:cl=mono:d={dur}",
                         "-c:a", "libmp3lame", audio_path],
                        check=True, capture_output=True,
                    )
            else:
                dur = 3.0
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=22050:cl=mono:d={dur}",
                     "-c:a", "libmp3lame", audio_path],
                    check=True, capture_output=True,
                )

            # ── 단계 검증 [TTS] — 오디오 파일 존재 + 길이 확인 (빈/잘린 오디오 방지) ──
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 500:
                raise RuntimeError(f"TTS FAIL line {i+1}: 오디오 파일 없음 또는 비어있음 ({audio_path})")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", audio_path],
                capture_output=True, text=True,
            )
            try:
                audio_dur = float(probe.stdout.strip())
            except ValueError:
                raise RuntimeError(f"TTS FAIL line {i+1}: ffprobe 길이 읽기 실패 ({probe.stderr[:80]})")
            if audio_dur < 0.5:
                raise RuntimeError(f"TTS FAIL line {i+1}: 오디오 길이 {audio_dur:.2f}s — 빈/잘림 의심")

            seg_dur = max(dur + pad, min_segment)
            segments.append((frame_path, audio_path, seg_dur))

        # ── 단계 검증 [TTS 전체] — 전 라인 실패하면 영상 자체가 의미 없음 ──
        if tts_failures == len(lines):
            raise RuntimeError(f"TTS FAIL: 모든 라인 ({len(lines)}개) 합성 실패 — 영상 제작 중단")
        elif tts_failures > 0:
            print(f"  [WARN] TTS 실패 {tts_failures}/{len(lines)} 라인 (침묵으로 대체됨)")

        total_duration = sum(s[2] for s in segments)

        # Build each segment: still frame + its narration, padded to seg_dur
        seg_files = []
        for i, (frame_path, audio_path, seg_dur) in enumerate(segments):
            seg_path = os.path.join(OUTPUT_DIR, f"seg_{i:04d}.mp4")
            subprocess.run(
                ["ffmpeg", "-y",
                 "-loop", "1", "-i", frame_path,
                 "-i", audio_path,
                 "-t", f"{seg_dur:.3f}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
                 "-r", str(fps),
                 "-c:a", "aac", "-b:a", "128k",
                 "-shortest",
                 seg_path],
                check=True, capture_output=True,
            )

            # ── 단계 검증 [SEGMENT] — video+audio 트랙 + 길이 확인 (오디오 전용 함정 방지) ──
            probe_v = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", seg_path],
                capture_output=True, text=True)
            probe_a = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", seg_path],
                capture_output=True, text=True)
            if "video" not in probe_v.stdout:
                raise RuntimeError(f"SEGMENT FAIL {i+1}: video 트랙 없음 ({seg_path})")
            if "audio" not in probe_a.stdout:
                raise RuntimeError(f"SEGMENT FAIL {i+1}: audio 트랙 없음 ({seg_path})")
            probe_d = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", seg_path],
                capture_output=True, text=True)
            try:
                seg_actual = float(probe_d.stdout.strip())
            except ValueError:
                raise RuntimeError(f"SEGMENT FAIL {i+1}: 길이 읽기 실패")
            if abs(seg_actual - seg_dur) > 0.5:
                raise RuntimeError(f"SEGMENT FAIL {i+1}: 길이 {seg_actual:.2f}s != 예상 {seg_dur:.2f}s")

            seg_files.append(seg_path)

        # Concat all segments
        list_file = os.path.join(OUTPUT_DIR, "segments_list.txt")
        with open(list_file, "w") as f:
            for fp in seg_files:
                f.write(f"file '{fp}'\n")

        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "22",
             "-c:a", "aac", "-b:a", "128k",
             output_path],
            check=True, capture_output=True,
        )
    finally:
        for d in (frames_dir, audio_dir):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    os.remove(os.path.join(d, f))
                os.rmdir(d)
        if os.path.exists(list_file):
            os.remove(list_file)
        for i in range(len(segments)):
            p = os.path.join(OUTPUT_DIR, f"seg_{i:04d}.mp4")
            if os.path.exists(p):
                os.remove(p)

    # ── Post-build verification (final MP4 only) ──
    ok, iss = _verify_video(output_path)
    if not ok:
        print(f"  [VIDEO VERIFY] ISSUES ({len(iss)}):")
        for x in iss:
            print(f"    - {x}")
        # Hard-fail: video may be unwatchable
        raise RuntimeError(f"Video verification failed: {'; '.join(iss)}")
    else:
        print(f"  [VIDEO VERIFY] OK — video+audio tracks, resolution, codec, duration all valid")

    return output_path


def _verify_video(path: str) -> tuple[bool, list[str]]:
    """Verify generated video has valid video+audio tracks and correct resolution.

    Returns (ok: bool, issues: list[str]).
    """
    issues = []

    # 1. File size sanity
    sz = os.path.getsize(path)
    if sz < 1000:
        issues.append(f"File too small: {sz} bytes")
        return False, issues

    # 2. ffprobe: check streams
    try:
        probe_v = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_type,codec_name,width,height,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=15)
        probe_a = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
             "-of", "json", path],
            capture_output=True, text=True, timeout=15)
        if probe_v.returncode != 0 or probe_a.returncode != 0:
            issues.append(f"ffprobe failed: {probe_v.stderr[:200]}")
            return False, issues
        data_v = json.loads(probe_v.stdout)
        data_a = json.loads(probe_a.stdout)
    except (json.JSONDecodeError, OSError) as e:
        issues.append(f"ffprobe error: {e}")
        return False, issues

    streams_v = data_v.get("streams", [])
    streams_a = data_a.get("streams", [])
    video_stream = streams_v[0] if streams_v else None
    audio_stream = streams_a[0] if streams_a else None

    if not video_stream:
        issues.append("NO VIDEO TRACK — only audio (audio-only mp4 passes returncode 0)")
    else:
        w = video_stream.get("width", 0)
        h = video_stream.get("height", 0)
        codec = video_stream.get("codec_name", "")
        if codec not in ("h264", "hevc"):
            issues.append(f"Video codec '{codec}' — expected h264/hevc")
        if h > w:  # vertical (Shorts)
            expected = (1080, 1920) if w == 1080 else (720, 1280)
            if (w, h) != expected:
                issues.append(f"Unusual vertical resolution {w}x{h} (expected {expected})")
        else:
            issues.append(f"Horizontal video {w}x{h} — should be vertical (Shorts)")

    if not audio_stream:
        issues.append("NO AUDIO TRACK")
    else:
        sr = int(audio_stream.get("sample_rate", 0))
        ch = audio_stream.get("channels", 0)
        if sr not in (11025, 16000, 22050, 24000, 32000, 44100, 48000):
            issues.append(f"Audio sample rate {sr} — unexpected")
        if ch not in (1, 2):
            issues.append(f"Audio channels {ch} — unexpected")

    # 3. Duration sanity
    try:
        dur_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10)
        dur = float(dur_probe.stdout.strip()) if dur_probe.stdout.strip() else 0
        if dur < 3:
            issues.append(f"Video duration {dur:.1f}s — too short")
    except Exception as e:
        issues.append(f"Duration check failed: {e}")

    return len(issues) == 0, issues


def _build_video_h3_t2v(lines: list, output_path: str):
    """Build video using MiniMax H3 T2V API."""
    prompt = " ".join(lines)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    try:
        video_path = generate_video_with_h3(
            prompt=prompt,
            duration=30,
            model="video-v2",
        )
    except RuntimeError as e:
        print(f"  [ERROR] H3 T2V failed: {e}")
        raise RuntimeError(
            f"MiniMax T2V API generation failed: {e}\n"
            "Set MINIMAX_H3_API_KEY environment variable for API access."
        ) from e

    # Copy to output location
    if video_path != output_path:
        os.rename(video_path, output_path)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="YouTube Shorts pipeline")
    parser.add_argument("--topic", required=True, help="Video topic")
    parser.add_argument("--shorts", action="store_true", help="Generate vertical Shorts format")
    parser.add_argument("--status", default="public", choices=["public", "unlisted", "private"])
    parser.add_argument(
        "--model",
        choices=["local", "h3"],
        default="local",
        help="LLM backend: local (llama.cpp qwen36) or h3 (MiniMax H3 API). Default: local",
    )
    parser.add_argument(
        "--video-gen",
        choices=["local", "h3", "ltx"],
        default="local",
        help="Video generation: local (FFmpeg + Edge TTS) or h3 (MiniMax T2V API) "
             "or ltx (LTX-Video 2B B-roll via a video factory on a second box, free). Default: local",
    )
    parser.add_argument(
        "--video-anim",
        choices=["none", "hook", "full"],
        default="none",
        help="Frame animation mode: 'none' (default), 'hook' (first line only), 'full' (every segment). Requires --video-gen h3.",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="Skip video generation, export script + metadata as JSON only",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # [1/5] Generate script (best-of-N: 3개 변형 생성 → 교차 검증 점수화 → 최고/통과 후보 채택)
    print(f"[1/5] Generating script about: {args.topic} (LLM: {'H3 API' if args.model == 'h3' else f'local {_detect_local_model()}'}) — best-of-N")
    script, score = generate_best_script(args.topic, model_backend=args.model)
    if not script or not script.strip():
        print(f"[ERROR] Script generation failed, empty script. Checking LLM connection...")
        # Quick health check
        try:
            resp = llm_chat_with_hooks(
                "Reply with exactly the word OK", max_tokens=10, temperature=0.1,
                stage="health_check", max_retries=1, min_response_length=1,
                model_backend=args.model,
            )
            if resp and "ok" in str(resp).lower():
                print(f"  LLM health check OK, response: {resp[:50]}")
            else:
                print(f"  LLM health check FAILED: {resp[:50]}")
        except Exception as e:
            print(f"  LLM connection error: {e}")
        return
    print(f"  Script ({len(script)} chars):")
    for i, line in enumerate(script.split("\n"), 1):
        print(f"    {i}. {line}")

    # [2/5] Quality gate check (scoring already done inside generate_best_script)
    print(f"[2/5] Quality gate: entertainment={score.get('entertainment', '?')}, "
          f"education={score.get('education', '?')}, "
          f"retention={score.get('retention', '?')}, "
          f"total={score.get('total', '?')}/15")
    print(f"  Pass: {score.get('pass', False)}")

    if not score.get("pass", False) and script.strip():
        # Identify the weak dimension(s) so the retry prompt can target them
        _ent = int(score.get("entertainment", 0)); _edu = int(score.get("education", 0))
        weak = []
        if _ent < PASS_MIN_ENT:
            weak.append("FUN — add wit, punchlines, surprising metaphors")
        if _edu < PASS_MIN_EDU:
            weak.append("USEFUL — add concrete facts and actions the viewer can use tomorrow")
        weak_text = " and ".join(weak) if weak else "both dimensions"
        for attempt in range(1, MAX_RETRIES):
            prompt_retry = textwrap.dedent(f"""
            Previous script about {args.topic} scored total={score.get('total', 0)}/15 (entertainment={_ent}, education={_edu}). Need entertainment>={PASS_MIN_ENT} AND education>={PASS_MIN_EDU}.

            The weak dimension(s): {weak_text}. Fix those specifically while keeping the strong ones.

            Rewrite it to be FUN and USEFUL — make each line punchy, witty, or surprising. Start with a strong hook.

            Write about: {args.topic}

            <role>You are a viral Shorts scriptwriter. Every line must make the viewer laugh or gasp, AND give them something concrete to do differently tomorrow.</role>
            <rules>Write exactly 8 lines. One sentence per line (15-30 words). Line 1 is a hook that stops scrolling. Balance: at least 3 witty/funny lines AND at least 3 concrete-fact/action lines. No labels, no bullets, no numbers, no meta text.</rules>
            <examples style reference — match this energy>
            "You've been drinking water wrong your whole life — and the fix takes four seconds every morning."
            "The app you call free is actually billing you in data, location pings, and thumb swipes — it just invoices in a currency you can't see."
            "Your brain has no volume knob for notifications, so the loudest voice wins — right now that's your phone, not you."
            </examples>

            Just the lines:
            """)
            raw = llm_chat_with_hooks(
                prompt_retry,
                max_tokens=1000,
                temperature=0.9,
                stage="generate_script_retry",
                max_retries=1,
                min_response_length=MIN_RESPONSE_LENGTH,
                model_backend=args.model,
            )
            if raw:
                new_script = extract_script_lines(raw, 12)
                new_clean = [l for l in new_script.split("\n") if l.strip() and not _is_meta_line(l)]
                cur_clean = [l for l in script.split("\n") if l.strip() and not _is_meta_line(l)]
                # 리트리는 반드시 평가 — 줄 수가 같아도 점수가 더 좋으면 교체 (교차 검증 포함)
                new_score = evaluate_script(new_script, args.topic, model_backend=args.model)
                print(f"  Retry {attempt}: score={new_score.get('total', '?')}/15, pass={new_score.get('pass', False)}")
                if new_score.get("pass", False):
                    script, score = new_script, new_score
                    break
                # 점수가 기존보다 좋으면 교체 — 게이트와 같은 기준으로 (min(ent,edu) 우선, total 타이브레이커)
                def _rank(s):
                    return (min(int(s.get("entertainment", 0)), int(s.get("education", 0))),
                            int(s.get("total", 0)))
                if _rank(new_score) > _rank(score):
                    script, score = new_script, new_score
                    print(f"  Retry {attempt}: better score ({new_score.get('total')}/{score.get('total')}) — replaced")
                else:
                    print(f"  Retry {attempt}: no improvement (lines {len(new_clean)} vs {len(cur_clean)}, score same/lower) — keeping previous script")

    print(f"  Final score: {score.get('total', '?')}/15 (ent={score.get('entertainment')}, edu={score.get('education')})")

    # ── Quality gate: 재미+유익함 미달이면 영상 제작 중단 ──
    if not score.get("pass", False):
        print(f"  [GATE] FAIL — entertainment={score.get('entertainment')} (min {PASS_MIN_ENT}), "
              f"education={score.get('education')} (min {PASS_MIN_EDU}). "
              f"영상 제작 중단: 재미/유익함 기준 미달.")
        collect_failure(
            task=f"script quality gate for {args.topic}",
            stage="quality_gate",
            attempt=MAX_RETRIES,
            prompt_snippet=script[:500],
            raw_response=json.dumps(score),
            parsed_output=score,
            error_type="quality_gate_fail",
            error_message=f"ent={score.get('entertainment')} edu={score.get('education')} total={score.get('total')}/15 — below gate (ent>={PASS_MIN_ENT}, edu>={PASS_MIN_EDU})",
            expected=f"entertainment>={PASS_MIN_ENT} AND education>={PASS_MIN_EDU}",
            actual_pattern="retries exhausted, script quality insufficient",
        )
        # 메타데이터만 저장 (영상 없음) — 업로드 크론은 final.mp4 없으면 건너뜀
        meta = {
            "title": args.topic[:60],
            "description": f"Check out this {args.topic} tip! #ytshorts",
            "tags": ["ytshorts", "tech", "tutorial"],
            "status": args.status,
            "script": script,
            "score": score,
            "gate_failed": True,
        }
        meta_path = os.path.join(OUTPUT_DIR, "meta.json")
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        print(f"  Metadata saved (no video): {meta_path}")
        return

    # [3/5] Generate title
    print(f"[3/5] Generating title")
    title = generate_title(args.topic, model_backend=args.model)
    # Clean title — strip meta artifacts
    title = re.sub(r"(?i)Here's a thinking process:", "", title).strip()
    title = re.sub(r"(?i)(Each|All|The|This|Every|These|Let's|Now,|Count:|Need|ies|I need|I will|I should|I must)\b", "", title).strip()
    title = re.sub(r"[^\w\s\-!?.,\uac00-\ud7a3\u2014]", " ", title).strip()
    title = re.sub(r"\s+", " ", title)  # collapse double spaces from char stripping
    # Remove any remaining meta phrases
    meta_phrases = ["Here's a thinking", "Each sentence", "All sentences", "The request", "Each fact", "Each line"]
    for mp in meta_phrases:
        title = re.sub(rf"(?i)^{re.escape(mp)}\s+", "", title).strip()
    title = title[:100].strip()
    if not title or len(title) < 3:
        title = args.topic[:60]
    print(f"  Title: {title}")

    # [4/5] Build video
    if args.no_video:
        print(f"[4/5] Skipping video generation (--no-video)")
        video_path = None
    else:
        num_lines = len([l for l in script.split("\n") if l.strip()])
        tts_note = "edge-tts narration" if edge_tts is not None else "silence (edge-tts not installed)"
        _vg_label = {'h3': 'H3 T2V', 'ltx': 'LTX video factory'}.get(args.video_gen, 'FFmpeg')
        print(f"[4/5] Building video ({num_lines} lines, {tts_note}) — method: {_vg_label}" +
              (f" (anim: {args.video_anim})" if args.video_anim != "none" else "") + ")")
        video_path = build_video(
            script, os.path.join(OUTPUT_DIR, "final.mp4"),
            video_gen_model=args.video_gen,
            video_anim=args.video_anim,
            topic=args.topic,
            model_backend=args.model,
        )
        print(f"  Video: {video_path}")

    # [5/5] Tags + metadata
    tags = generate_tags(args.topic, model_backend=args.model)
    # Filter out meta lines from tags
    meta_tags = ["Here's a thinking", "Each sentence", "All sentences", "The request", "Each fact", "Each line", "Line 1", "Line 2", "Line 3"]
    tags = [t for t in tags if not any(m.lower() in t.lower() for m in meta_tags) and len(t) > 2]
    if len(tags) < 3:
        fallback = ["ytshorts", "tech", "tutorial"]
        tags.extend(fallback[: 3 - len(tags)])
    tags = tags[:5]  # YouTube max 500 chars total for tags
    print(f"[5/5] Tags: {tags}")

    # Build description within YouTube's 5000 char limit
    desc_intro = f"Check out this {args.topic} tip! #ytshorts #tech #tutorial"
    script_text = script[:4000]  # leave room for intro
    description = f"{desc_intro}\n\n{script_text}"
    if len(description) > 5000:
        description = description[:4997] + "..."

    meta = {
        "title": title,
        "description": description,
        "tags": tags,
        "status": args.status,
        "script": script,
        "score": score,
    }
    meta_path = os.path.join(OUTPUT_DIR, "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"  Metadata saved to {meta_path}")

    # Verify video
    if video_path:
        video_size = os.path.getsize(video_path)
        ffprobe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(ffprobe.stdout.strip()) if ffprobe.stdout.strip() else 0
        print(f"  Video verified: {video_size:,} bytes, {duration:.1f}s")

        if duration < MIN_VIDEO_SECONDS:
            print(f"  [WARN] Video shorter than {MIN_VIDEO_SECONDS}s")

    print("Pipeline complete. Run upload_youtube.py to upload.")


if __name__ == '__main__':
    main()
