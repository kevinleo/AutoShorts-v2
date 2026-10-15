# Workflow Insights
<!-- kind: insight -->
<!-- source: research loop -->
<!-- tags: workflow, insight, improvement -->

Research-loop findings that improve the pipeline itself. One entry per finding,
newest first. Format:

## <date> — <title>
- **Finding:** what was learned (source: <url>)
- **Applied:** <which part of the workflow changed, or "pending">
- **Impact:** <gate/script/topic/production>

---

## 2026-09-17 — Topic prompt: concrete+fun+useful beats abstract
- **Finding:** topics phrased as "felt but never named" drift into self-help
  quote tone. Concrete object/behavior + "wait, really?" hook + do-it-tomorrow
  utility scores higher on entertainment+education.
- **Applied:** `daily_generate.sh` topic prompt rewritten with GOOD/BAD few-shot;
  `topics.txt` fallback list replaced.
- **Impact:** topic selection (gate unchanged, per standing rule)

## 2026-09-17 — Concrete Mechanism Hook Structure
- **Finding:** The research brief's hook successfully combines a specific, relatable pain point ('cracks in the exact same spot') with a precise, counter-intuitive scientific mechanism ('water expands by 9%... thin dividers are engineered stress points'). This structure likely drives high 'education' scores by providing immediate, tangible value rather than vague curiosity.
- **Apply to:** script-writing
- **Concrete change:** Update the script-writing prompt to mandate that the first line (hook) must follow the format: [Specific Relatable Failure] + [Precise Quantitative/Physical Cause], avoiding generic questions or broad statements.
- **Status:** pending-review

## 2026-09-19 — Lab vs. Production Gap as a Hook
- **Finding:** The research highlights a significant discrepancy between controlled test results (5.94x speedup) and real-world deployments (1.87x speedup) for the MARS scheduler, illustrating that 'lab performance' often fails to predict 'production reality' in AI systems.
- **Apply to:** topic-selection
- **Concrete change:** Add a 'Reality Check' filter to the topic selection step: prioritize topics that expose the gap between marketing/lab benchmarks and real-world user experience, as this specific angle (expectation vs. reality) is a high-engagement hook for technical audiences.
- **Status:** pending-review

## 2026-09-19 — Actionable Payoff Structure
- **Finding:** The research brief explicitly defines a 'payoff' as a specific, time-sensitive action (see a doctor within 48 hours) rather than just a general fact, which directly supports the 'practical' requirement of the topic selection phase and ensures the script has a clear utility hook.
- **Apply to:** topic-selection
- **Concrete change:** Update the topic selection criteria to require that every chosen topic must have a verifiable, time-sensitive, or immediate actionable 'payoff' (e.g., 'do X within Y hours') to ensure the content meets the 'practical' definition and improves retention.
- **Status:** pending-review
