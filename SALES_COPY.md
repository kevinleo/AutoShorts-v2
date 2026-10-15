# Gumroad Listing — AutoShorts v2

## Title
AutoShorts v2 — Self-Improving YouTube Shorts Factory (topic → published Short, $0/video)

## Price
$99 (launch: $49 for first 20 buyers)

## 초기비용 $0
- Gumroad: 리스팅 무료 (판매 시 수수료 10% + 결제비 ~3%)
- GitHub: 무료 공개 리포 (마케팅)
- Dev.to: 무료 블로그 (SEO 유입)
- Fiverr: 계정 무료, 판매 시 20%
→ 전부 "파는 순간"에만 비용. 선결제 없음.

## Description (English)

**One topic in. A quality-gated, published YouTube Short out. $0 per video.**

AutoShorts v2 is a complete Python pipeline that picks, researches, writes,
scores, builds, and uploads YouTube Shorts using a FREE local LLM. No API fees,
no subscription — your own hardware does the work.

**What's inside**
- **Best-of-N script generation** — 4 prompt variants, keep the best
- **Dual-evaluator quality gate** — writer model + independent judge cross-check;
  a video only ships when entertainment ≥ 3 AND education ≥ 3 (no filler)
- **Research loop** — pulls real web evidence into a brief, injects it into the
  script prompt, and files reusable insights back to the knowledge base
- **Second brain** — SQLite FTS + embeddings + PARA wiki that remembers what
  worked and steers the next topic
- **Taste profile** — point it at your viewing account's watch history and the
  pipeline picks topics *your* audience actually watches
- **Animated B-roll (optional)** — per-line LLM-converted visual prompts to an
  LTX-Video T2V factory; graceful fallback to FFmpeg slides
- **Cron-ready** — `daily_generate.sh` + batch uploader with archive-to-avoid-
  duplicates. One video a day, hands-off, or 3–5 with multiple cron slots

**Requirements**
- Python 3.10+, FFmpeg, a local LLM server (llama.cpp; one 27B-class model is
  enough — it writes AND judges)
- YouTube OAuth (5-minute setup, full walkthrough in README)

**What you get**
- Full source (MIT), `requirements.txt` (6 deps), `.env.example`
- Step-by-step README: llama.cpp setup, OAuth, cron
- Working daily-loop script + knowledge-base skeleton
- The exact quality-gate + cross-check logic that keeps filler off your channel

## Description (Korean)

**토픽 한 줄 넣으면, 품질 게이트 통과한 숏츠가 공개됩니다. 영상당 $0.**

AutoShorts v2는 로컬 LLM(llama.cpp)으로 주제 조사 → 스크립트 작성 → 이중 품질
검증 → 영상 생성 → YouTube 업로드까지 자동화하는 완전체 파이프로입니다. API
비용 0, 구독료 0 — 내 하드웨어가 일합니다.

- best-of-N 스크립트(4변형) + **이중 평가자 교차 검증**(ent≥3 AND edu≥3 게이트,
  미달 시 영상 미생성 — 쉐어 넘버 방지)
- 웹 근거 리서치 브리프가 스크립트 프롬프트에 주입 + 유효한 인사이트는 지식
  베이스(FTS+임베딩+PARA 위키)로 자동 기록 → 다음 주제 선정에 활용 (자기 개선)
- 시청 계정 시청기록 기반 취향 프로필 → 내 시청자가 진짜 보는 주제를 골라줌
- 옵션: LTX-Video B-roll(라인별 구체 비주얼 프롬프트 자동 변환) / FFmpeg 슬라이드 폴백
- 크론 즉시 구동: 일 1개(또는 3~5개) 완전 손놓기, 중복 업로드 방지 아카이브

요구사항: Python 3.10+, FFmpeg, 로컬 LLM 1개(27B 클래스, 작성+평가 겸용 가능),
YouTube OAuth(5분, README 가이드 포함).

## FAQ

**Do I need a big GPU?** A 27B-class model at IQ4 runs on 16 GB VRAM (or slower
on CPU). One model is enough; a second independent judge improves gate
reliability but is optional.

**Do I need a second PC for video?** No — FFmpeg slides work out of the box.
The LTX-Video B-roll factory is an optional upgrade.

**Will it upload to my channel?** Yes — standard YouTube OAuth, tokens are yours,
never leave your machine.

## Tags
youtube, shorts, automation, llm, llama.cpp, ai, content-creation, self-hosted,
no-api-cost, python
