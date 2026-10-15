#!/usr/bin/env python3
"""evaluate.py — Script quality evaluation using evaluator profile."""
import json
import re

def evaluate_script(script: str, topic: str) -> dict:
    """Evaluate script quality."""
    # Call via curl to the evaluator profile's LLM
    # --- MAIN MODEL DISABLED (100% CPU offload) ---
    # import subprocess
    # cmd = [
    #     'curl', '-s', '-X', 'POST',
    #     'http://localhost:8080/v1/chat/completions',
    #     '-H', 'Content-Type: application/json',
    #     '-d', json.dumps({
    #         'model': 'qwen36',
    #         'messages': [{'role': 'user', 'content': f"""
    # Evaluate this YouTube Shorts script about: {topic}
    #
    # SCRIPT:
    # {script}
    #
    # Score each on 1-5 scale (5 is best):
    # - Entertainment: Is it funny/interesting/will people watch till end?
    # - Educational value: Does it teach something useful or surprising?
    # - Retention: Would viewers stay engaged throughout?
    #
    # Output ONLY this JSON format:
    # {{"entertainment": 4, "education": 3, "retention": 4, "total": 11}}
    # """}})
    # ]
    # result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    # try:
    #     data = json.loads(result.stdout)
    #     choice = data.get('choices', [{}])[0].get('message', {}).get('content', '{}')
    #     scores = json.loads(choice)
    #     total = scores.get('entertainment', 0) + scores.get('education', 0) + scores.get('retention', 0)
    #     scores['total'] = total
    #     scores['pass'] = total >= 11
    #     return scores
    # except:
    #     return {"entertainment": 3, "education": 3, "retention": 3, "total": 9, "pass": False}
    return {"entertainment": 3, "education": 3, "retention": 3, "total": 9, "pass": False}

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        topic = sys.argv[1]
    else:
        topic = "쌍둥이와 연인의 애정 서열 연구"
    script = """태어나자마자 동일한 유전자를 공유하는 일란성 쌍둥이는 로맨틱한 연인 관계보다 더 깊은 생물학적 유대감을 영원히 유지한다.
"""
    scores = evaluate_script(script, topic)
    print(json.dumps(scores, indent=2, ensure_ascii=False))
