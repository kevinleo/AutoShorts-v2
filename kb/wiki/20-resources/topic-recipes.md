# Topic Recipes — What Worked
<!-- kind: topic -->
<!-- source: pipeline history -->
<!-- tags: topic, recipe, scores -->

Topics that passed the gate (ent>=3 AND edu>=3), with their scores and why they
worked. `daily_generate.sh` reads the top entries as in-context examples for the
next topic suggestion (few-shot from our OWN successes).

Format per entry:
- **topic:** <one-liner>
  - scores: ent=X edu=Y ret=Z total=N | video: <url or archive path>
  - why it worked: <hook / format / payoff notes>

---

- **topic:** why your phone battery dies by noon even when you barely use it
  - scores: ent=4 edu=3 ret=3 total=10 (2026-09-17, LTX B-roll E2E)
  - why it worked: concrete object (phone battery), relatable annoyance,
    "wait, really?" hook, fixable payoff (background app kill list)

- **topic:** why your ice cube tray always cracks from the bottom
  - scores: ent=4 edu=4 ret=4 total=12/15 (2026-09-18)
  - why it worked: concrete mechanism (water expands 9% -> stress points) + doable fix (silicone tray / boiled water / 2min wait)

- **topic:** why the flu hits way harder than a common cold
  - scores: ent=4 edu=4 ret=4 total=12/15 (2026-09-19)
  - why it worked: seasonal hook (flu outbreak on town elevator board) + 48h antiviral window as doable payoff

- **topic:** your AI agent coding while your GPU melts down
  - scores: ent=4 edu=4 ret=4 total=12/15 (2026-09-19)
  - why it worked: concrete tech scenario + expectation-vs-reality hook

- **topic:** OpusClip the AI tool that finds the viral moment in your long videos
  - scores: ent=4 edu=3 ret=4 total=11/15 (2026-09-20)
  - why it worked: pending — note the hook/format that worked

- **topic:** why your AI agent writes poetry instead of code when using free APIs
  - scores: ent=3 edu=4 ret=4 total=11/15 (2026-09-20)
  - why it worked: pending — note the hook/format that worked

- **topic:** Why your GPU idles at 0% while coding in Cursor
  - scores: ent=3 edu=4 ret=4 total=11/15 (2026-09-21)
  - why it worked: pending — note the hook/format that worked
