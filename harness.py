#!/usr/bin/env python3
"""harness.py — Harness R1: Agent wrapper with 4 hook points + failure packet collection.

Hook Points:
  1. hook_episode_start(stage, context) — 각 스테이지 시작 전 주의사항/제약조건 주입
  2. hook_before_llm_call(prompt, stage, attempt) — LLM 호출 직전 컨텍스트 주입
  3. hook_after_llm_response(response, stage, context) — 응답 수신 후 검증/가드레일
  4. hook_on_error(error, stage, context) — 에러 시 복구 지시

Failure Packet:
  LLM 실패 패턴을 JSON로 수집 → 추후 패치 코드 생성에 사용.

Backend:
  - local:  your own OpenAI-compatible local server (llama.cpp), auto-detected port
  - h3:     https://api-minimax.ai/v1 (MiniMax H3 via OpenAI-compatible API, optional/paid)
"""
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Any, Optional

import env  # loads .env before the endpoint defaults below are read

# ── Paths ────────────────────────────────────────────────────────────────────
PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
FAILURE_LOG_DIR = os.path.join(PIPELINE_DIR, "failure_logs")
os.makedirs(FAILURE_LOG_DIR, exist_ok=True)

# ── Local model auto-detection ───────────────────────────────────────────────
_LOCAL_MODEL_CACHE = None
_AUTHOR_BASE_CACHE = None

# Author LLM server: 3090 VRAM (llama.cpp 27B). Historically localhost:8888, but the
# user may restart it on a different port during a model switch (it defaults to 8080
# when started without --port), so we auto-detect the live port instead of hardcoding.
LOCAL_AUTHOR_URL = os.getenv("LLM_AUTHOR_URL", "http://127.0.0.1:8888/v1")
# Candidate local author ports to scan (8085 is the CPU evaluator — excluded).
_AUTHOR_CANDIDATE_PORTS = (8888, 8080, 8081, 8082, 8083)
# Optional backup evaluator server (used only when set in .env / env)
NPU_EVALUATOR_URL = os.getenv("NPU_EVALUATOR_URL", "")
# CPU evaluator: 35B-A3B MoE Q4 on 3990X (localhost:8085, llama.cpp CPU)
CPU_EVALUATOR_URL = os.getenv("LLM_CPU_EVALUATOR_URL", "http://127.0.0.1:8085/v1")


def _query_models(base_url: str, timeout: int = 5) -> str:
    try:
        from urllib.request import urlopen
        r = urlopen(f"{base_url}/models", timeout=timeout)
        data = json.loads(r.read())
        # Handle both OpenAI-style {"data":[...]} and llama.cpp-style {"models":[...]}
        models = data.get("data") or data.get("models") or []
        if models:
            m0 = models[0]
            name = m0.get("id") or m0.get("name") or m0.get("model") or ""
            if name:
                return name
    except Exception:
        pass
    return ""


def _detect_author_base() -> str:
    """Return the live author LLM base URL (http://host:port/v1), auto-detecting port.

    The 27B author server on the 3090 is user-managed and may be restarted on a
    different port during a model switch (llama-server defaults to 8080 when
    started without --port). Scan candidate localhost ports and return the first
    that serves a model. Cached for the process lifetime.
    """
    global _AUTHOR_BASE_CACHE
    if _AUTHOR_BASE_CACHE:
        return _AUTHOR_BASE_CACHE
    for port in _AUTHOR_CANDIDATE_PORTS:
        base = f"http://127.0.0.1:{port}/v1"
        if _query_models(base):
            _AUTHOR_BASE_CACHE = base
            return base
    # Nothing on candidate ports — fall back to the configured URL (may still work,
    # e.g. if the user set LLM_AUTHOR_URL explicitly to a non-candidate port).
    _AUTHOR_BASE_CACHE = LOCAL_AUTHOR_URL
    return _AUTHOR_BASE_CACHE


def _detect_local_model() -> str:
    """Return the model name actually served by the author LLM server.

    Primary: 3090 VRAM server (auto-detected localhost port, 27B author).
    Fallback: the backup evaluator server (NPU_EVALUATOR_URL) if set.
    Cached after first successful detection.
    """
    global _LOCAL_MODEL_CACHE
    if _LOCAL_MODEL_CACHE:
        return _LOCAL_MODEL_CACHE
    name = _query_models(_detect_author_base()) or _query_models(NPU_EVALUATOR_URL)
    if name:
        _LOCAL_MODEL_CACHE = name
        return name
    return "qwen38"  # fallback alias (matches the 3090 server's --alias)


# ── Backend Config ──────────────────────────────────────────────────────────
# Backend-specific settings — override via env vars for h3 mode.
#   MINIMAX_API_KEY  : H3 text gen key (required for h3 backend)
#   MINIMAX_H3_MODEL : H3 model ID (default: h3)
#   H3_T2V_API_KEY   : H3 T2V key (for video generation)

_H3_TEXT_API_KEY = os.getenv("MINIMAX_API_KEY", "")
_H3_TEXT_MODEL = os.getenv("H3_MODEL", "h3")
_H3_T2V_API_KEY = os.getenv("MINIMAX_H3_API_KEY", os.getenv("MINIMAX_API_KEY", ""))
_H3_T2V_MODEL = os.getenv("H3_T2V_MODEL", "video-v2")


# ── Data Classes ─────────────────────────────────────────────────────────────
@dataclass
class FailurePacket:
    """실패 패cket — LLM이 실패한 패턴을 구조화한 데이터."""
    task: str  # 작업 설명 (예: "script generation about topic")
    stage: str  # 실패한 스테이지 (예: "generate_script")
    attempt: int  # 몇 번째 시도인지
    prompt_snippet: str  # 프롬프트 앞부분 (500자 제한)
    raw_response: str  # LLM의 원시 응답
    parsed_output: Any  # 파싱 후 결과 (없면 빈 list)
    error_type: str  # 에러 유형 (예: "empty_script", "json_parse_fail", "too_short")
    error_message: str  # 인간이 읽을 수 있는 에러 메시지
    expected: str  # 기대했던 출력 형식
    actual_pattern: str  # 실제 발견된 패턴 요약 (100자)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json_file(self, filename: str = None) -> str:
        """JSON 파일로 저장."""
        if filename is None:
            ts = int(self.timestamp)
            filename = f"{self.stage}_{ts}.json"
        path = os.path.join(FAILURE_LOG_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return path


# ── Hook Registry ────────────────────────────────────────────────────────────
class HookRegistry:
    """4가지 hook point를 관리하는 registry.

    각 hook은 콜백 함수를 등록할 수 있고, 순차적으로 실행됩니다.
    """

    def __init__(self):
        self._hooks = {
            "episode_start": [],
            "before_llm_call": [],
            "after_llm_response": [],
            "on_error": [],
        }

    def register(self, hook_name: str, callback: Callable):
        """hook에 콜백 함수 등록."""
        if hook_name not in self._hooks:
            raise ValueError(f"Unknown hook: {hook_name}. Available: {list(self._hooks.keys())}")
        self._hooks[hook_name].append(callback)

    def fire(self, hook_name: str, **kwargs):
        """hook 실행. 모든 कॉл백의 결과를 list로 반환."""
        results = []
        for cb in self._hooks.get(hook_name, []):
            try:
                result = cb(**kwargs)
                if result is not None:
                    results.append(result)
            except Exception as e:
                # hook 실패가 전체 파이프라인을 죽이지 않음
                pass
        return results


# 전 глобальный instance
hooks = HookRegistry()


# ── Default Hooks (기본 동작) ────────────────────────────────────────────────
def _default_on_episode_start(stage: str, context: dict = None) -> str:
    """에피소드 시작점 — 스테이지별주의사항/제약조건 주입."""
    constraints = {
        "generate_script": (
            "JSON array of strings. Each line 15-30 단어. "
            "메타 텍스트 금지 ('Each sentence...', 'Line 1.' 등). "
            "오직 JSON만 출력."
        ),
        "evaluate_script": "JSON only. {\"entertainment\": N, ...}",
        "generate_title": "60자 이하. 끝문장 부호 금지.",
        "generate_tags": "3개 comma-separated. 숫자/따옴표 금지.",
    }
    return f"[HARNESS] Stage: {stage}. Constraint: {constraints.get(stage, '')}"


def _default_on_before_llm_call(prompt: str, stage: str, attempt: int = 1) -> str:
    """LLM 호출 직전 — 시도 횟수 정보 주입."""
    if attempt > 1:
        return f"  (Retry attempt {attempt}/{3})"
    return ""


def _default_on_after_llm_response(response: str, stage: str, context: dict = None,
                                   min_response_length: int = 50) -> dict:
    """응답 수신 후 검증 — 가드레일.

    Returns:
        dict with keys: valid (bool), cleaned_response (str), issues (list)
    """
    issues = []
    valid = True

    # 1) 응답 길이 검증 (< min_response_length → reject)
    if len(response.strip()) < min_response_length:
        issues.append(f"Response too short: {len(response)} chars")
        valid = False

    # 2) 빈 응답
    if not response.strip():
        issues.append("Empty response")
        valid = False

    # 3) 메타 텍스트 패턴 감지 (script generation)
    if stage == "generate_script":
        meta_patterns = [
            r"^Each\s", r"^All\s", r"^The\s", r"^This\s", r"^Every\s",
            r"^Line\s*\d", r"^Draft", r"^Revised",
        ]
        lines = [l.strip() for l in response.split("\n") if l.strip()]
        meta_lines = sum(1 for l in lines if any(re.match(p, l) for p in meta_patterns))
        # Word-count traces: "Task(1) managers2 with3 simple4" — 3+ embedded digits
        digit_trace = sum(1 for l in lines if len(re.findall(r'\d', l)) >= 3)
        if lines and (meta_lines > len(lines) * 0.5 or digit_trace > len(lines) * 0.5):
            issues.append(f"Meta text detected: {max(meta_lines, digit_trace)}/{len(lines)} lines are meta patterns")
            valid = False

    return {
        "valid": valid,
        "cleaned_response": response,
        "issues": issues,
    }


def _default_on_error(error: Exception, stage: str, context: dict = None) -> str:
    """에러 시 복구 지시."""
    error_type = type(error).__name__
    msg = str(error)[:200]
    return f"[HARNESS ERROR] Stage: {stage}, Error: {error_type}: {msg}"


# 기본 hook登録
hooks.register("episode_start", _default_on_episode_start)
hooks.register("before_llm_call", _default_on_before_llm_call)
hooks.register("after_llm_response", _default_on_after_llm_response)
hooks.register("on_error", _default_on_error)


# ── Failure Packet Collector ─────────────────────────────────────────────────
def collect_failure(
    task: str,
    stage: str,
    attempt: int,
    prompt_snippet: str,
    raw_response: str,
    parsed_output: Any,
    error_type: str,
    error_message: str,
    expected: str = "",
    actual_pattern: str = "",
) -> FailurePacket:
    """실패 패킷 생성 + JSON файл 저장 + console 출력.

    추후 9B 모델이 이 파일을 읽고 패치 코드를 생성하는 데 사용.
    """
    packet = FailurePacket(
        task=task,
        stage=stage,
        attempt=attempt,
        prompt_snippet=prompt_snippet[:500],
        raw_response=raw_response[:2000],
        parsed_output=parsed_output,
        error_type=error_type,
        error_message=error_message,
        expected=expected,
        actual_pattern=actual_pattern[:100],
    )
    packet.to_json_file()

    # console에 요약 출력
    print(f"  [FAILURE PACKET] {stage}/attempt{attempt}: {error_type} — {error_message[:80]}")
    return packet


# ── Wrapper: llm_chat_with_hooks ─────────────────────────────────────────────
def llm_chat_with_hooks(
    prompt: str,
    max_tokens: int = 300,
    temperature: float = 0.8,
    stage: str = "unknown",
    max_retries: int = 3,
    min_response_length: int = 50,
    model_backend: str = "local",  # "local" | "h3"
):
    """LLM 호출을 4 가지 hook으로 감싸는 래퍼.

    Args:
        prompt: LLM 프롬프트
        max_tokens: 최대 토큰 수
        temperature: temperatura
        stage: 스테이지 이름 (hook 컨텍스트用)
        max_retries: 최대 재시도 횟수 (엄격 제한)
        min_response_length:最小响应长度 (< this value → reject)
        model_backend: "local" (llama.cpp) or "h3" (MiniMax API)

    Returns:
        유효한 LLM 응답 문자열 (valid한 것만 반환)

    Raises:
        RuntimeError: Backend-specific errors (missing key, connection, etc.)
    """
    import sys

    if "openai" not in sys.modules:
        from openai import OpenAI
    else:
        from openai import OpenAI

    # Choose backend
    if model_backend == "h3":
        if not _H3_TEXT_API_KEY:
            raise RuntimeError(
                "MiniMax API key not found. "
                "Set MINIMAX_API_KEY environment variable."
            )
        base_url = "https://api-minimax.ai/v1"
        api_key = _H3_TEXT_API_KEY
        model = _H3_TEXT_MODEL
    elif model_backend == "cpu":
        # CPU evaluator — 35B-A3B MoE Q4 on 3990X (localhost:8085)
        base_url = CPU_EVALUATOR_URL
        api_key = "sk-xxx"
        model = _query_models(CPU_EVALUATOR_URL) or "35b-cpu"
    else:
        # local backend — 3090 VRAM author server (auto-detected port, 27B)
        base_url = _detect_author_base()
        api_key = "sk-xxx"
        model = _detect_local_model()

    client = OpenAI(base_url=base_url, api_key=api_key)

    for attempt in range(1, max_retries + 1):
        # Hook 1: 에피소드 시작점
        hooks.fire("episode_start", stage=stage)

        # Hook 2: LLM 호출 직전
        hooks.fire("before_llm_call", prompt=prompt, stage=stage, attempt=attempt)

        # LLM 호출
        try:
            # local llama.cpp + reasoning 모델(qwen38 등): thinking을 끄지 않으면
            # 토큠 예산이 전부 reasoning_content에 소진되고 content가 비어
            # validation이 항상 실패한다. enable_thinking=false로 직접 응답하게 한다.
            extra_body = {}
            if model_backend != "h3":
                extra_body["chat_template_kwargs"] = {"enable_thinking": False}
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
                **({"extra_body": extra_body} if extra_body else {}),
            )
            text = r.choices[0].message.content or ""
            if not text and hasattr(r.choices[0].message, "reasoning_content"):
                text = r.choices[0].message.reasoning_content or ""
        except Exception as e:
            hooks.fire("on_error", error=e, stage=stage)
            raise

        # Hook 3: 응답受信后验证
        validation = _default_on_after_llm_response(text, stage, min_response_length=min_response_length)

        if not validation["valid"]:
            # Hook 4: 에러 시 복구
            hooks.fire("on_error", error=RuntimeError(validation["issues"][0]), stage=stage)

            # 실패 패cket收集
            collect_failure(
                task=stage,
                stage=stage,
                attempt=attempt,
                prompt_snippet=prompt[:500],
                raw_response=text,
                parsed_output=[],
                error_type="llm_validation_fail",
                error_message=" | ".join(validation["issues"]),
                expected=f"response >= {min_response_length} chars, no meta text",
                actual_pattern=" | ".join(validation["issues"]),
            )

            # 마지막 시도면 빈 문자열 반환 (caller가 처리)
            if attempt == max_retries:
                print(f"  [HARNESS] All {max_retries} retries failed for '{stage}'. Returning empty.")
                return ""
            continue

        # 검증 통과 — 응답返回
        return text

    # reaching here should not happen
    return ""


# ── H3 T2V: Text-to-Video API ───────────────────────────────────────────────
def generate_video_with_h3(
    prompt: str,
    duration: int = 30,
    api_key: str = None,
    model: str = "video-v2",
) -> str:
    """Generate video via MiniMax H3 T2V API.

    Args:
        prompt: Text prompt for the video.
        duration: Video duration in seconds (max 30).
        api_key: MiniMax API key. Falls back to MINIMAX_H3_API_KEY env.
        model: Model ID (default: video-v2).

    Returns:
        Path to downloaded video file.

    Raises:
        RuntimeError: If API key is missing or generation fails.
    """
    import sys
    if "openai" not in sys.modules:
        from openai import OpenAI
    else:
        from openai import OpenAI

    if not api_key:
        api_key = os.getenv("MINIMAX_H3_API_KEY", os.getenv("MINIMAX_API_KEY", ""))
    if not api_key:
        raise RuntimeError(
            "MiniMax T2V API key not found. "
            "Set MINIMAX_H3_API_KEY environment variable."
        )

    duration = min(duration, 30)
    client = OpenAI(base_url="https://api-minimax.ai/v1", api_key=api_key)

    response = client.videos.create(
        model=model,
        input=prompt,
        duration=duration,
    )

    video_id = response.data.id
    status_url = f"https://api-minimax.ai/v1/videos/{video_id}"

    # Poll for completion
    import time
    print(f"  [H3 T2V] Video ID: {video_id} — waiting for generation...")

    for _ in range(120):  # Max 10 minutes polling
        try:
            status = client.videos.get(id=video_id)
            status = status.data
            if status.status == "completed":
                download_url = status.downloads.url
                break
            elif status.status == "failed":
                raise RuntimeError(f"Video generation failed: {status.message}")
        except Exception as e:
            print(f"  [H3 T2V] Poll error: {e}")

        time.sleep(5)
        print(f"  [H3 T2V] Polling... status: {status.status if 'status' in dir(status) else 'unknown'}")
    else:
        raise RuntimeError("Video generation timed out after 10 minutes.")

    # Download video
    import io
    import requests
    print(f"  [H3 T2V] Downloading video from API...")
    r = requests.get(download_url)
    r.raise_for_status()

    import tempfile
    import os
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(r.content)
    tmp.close()
    return tmp.name


# ── Exports ────────────────────────────────────────────────────────────────
__all__ = [
    "llm_chat_with_hooks",
    "collect_failure",
    "hooks",
    "HookRegistry",
    "FailurePacket",
    "generate_video_with_h3",
]
