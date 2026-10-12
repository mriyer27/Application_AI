# Scoring

`job-agent score` gives every active job an **explainable 0–100 score**, a
**match category**, a **shortlist decision**, and separate **eligibility flags**
quoted word for word from the listing. Scoring is deterministic code
(`src/job_agent/scoring/`). The local LLM can add information, but it never
decides whether you have a qualification.

```
job-agent score [--rescore] [--limit N] [--job J-...] [--no-llm]
job-agent jobs  [--top N] [--category strong_match|possible_match|weak_match|needs_review]
                [--include-filtered] [--include-closed]
job-agent job show <job-uid>          # factors, matching/missing, flags + excerpts, evidence, reasons, URLs
job-agent job mark <job-uid> relevant|irrelevant|clear
job-agent job note <job-uid> "text" [--append]
```

## How a score is computed

Seven factors are each scored from 0 to 1, and each comes with a plain-language
explanation (shown by `job show`). The overall score is

    overall = round( Σ weight_f × score_f , 1 )        weights normalized to sum to 100

| Factor | Default weight | What it measures |
|---|---|---|
| `career_interest` | 25 | Compares the title with `search.target_titles` and your profile's `preferences.target_roles`. An exact match scores 1.0 and a title that contains a target scores 0.85; both ignore seniority words such as "Associate", "Senior" or "II". Partial word overlap scores 0.2 + 0.5 × share, and a `title_keywords` match scores at least 0.5 (capped at 0.7); a title unrelated to your targets scores 0.1. **Penalties:** −0.40 for a software-engineering title; −0.15 when the description has ≥3 software-engineering phrases (production code, microservices, CI/CD, …); −0.10 for each avoid-focus topic, up to −0.30 (data structures & algorithms, LeetCode, HackerRank, competitive programming, coding challenges, plus your `preferences.avoid_focus`); −0.10 when the local AI rates coding intensity high. The floor is 0.05. Ordinary mentions of Python or SQL are never penalized. |
| `skills` | 25 | Skills come from `required_qualifications`, or from the description when the listing has no requirements list, plus `preferred_qualifications`. A skill counts as matching **only** when the verified profile supports it: it is listed in `profile.skills` (directly or through a strict "is a kind of" link such as MySQL → SQL or SAS Enterprise Guide → SAS) and/or a fact mentions it in `skills`, `keywords` or its text. The score is the weighted share matched: required ×1.0, preferred ×0.4, soft skills (stakeholder management) at half weight. No recognizable skills → **0.5 "insufficient information"**. |
| `experience_level` | 15 | You are treated as early-career. Explicit years: 0 → 1.0, 1 → 0.95, 2 → 0.8, 3 → 0.55, 4 → 0.3, 5+ → 0.1 (you have internships plus well under two years of professional experience). In one sentence that offers alternatives ("4 years, or 2 with a Master's") the lower number counts; across sentences the highest counts. Title level: senior / staff / principal / director → 0.15, lead → 0.25, people manager → 0.2, III/IV → 0.3, II → 0.6, entry / new grad / intern / graduate → 1.0, associate / junior → 0.9. Explicit years take precedence over an entry-level title, but a senior title can still lower the score. A description that talks about managing people caps it at 0.3. A required degree you do not hold caps it at 0.2 for a PhD (0.3 otherwise). A degree you are still completing is listed as missing but not penalized. Nothing found → **0.6, with that stated**. |
| `domain_relevance` | 10 | AI, business, analytics and product signals. A category named in the title scores 1.0; otherwise 0.3 per distinct description term, up to 0.9. The factor is the mean of the two strongest categories. |
| `location` | 10 | Uses `search.locations`, `preferences.preferred_locations` and `search.remote_preference`, with **word-boundary** matching ("US" does not match "Austin"). With `any`: matched → 1.0, unmatched → 0.2, remote → 1.0, remote in an unlisted region ("Remote (Canada)") → 0.5. Nothing listed → **0.5**. `remote_only` and `remote_or_hybrid` use stricter tables (see `engine._location`). |
| `compensation` | 10 | Compared with `scoring.salary_target_base`. Hourly pay is annualized ×2080, weekly ×52 and monthly ×12, and the explanation **says this is an estimate**. Bottom of range ≥ target → 1.0; top of range ≥ target → 0.85; otherwise 0.8 × (top/target − 0.5)/0.5. Total compensation such as OTE (`salary_is_base = 0`) scores ×0.7 with the note "total compensation, not base". If the listing does not say base or total, the pay is treated as base and the explanation says so. A different currency or an unclear pay period → 0.5. **Not listed → 0.5 "salary not listed"**. A salary is never estimated or invented. |
| `employer_growth` | 5 | Starts at 0.5. +0.3 for a `search.target_employers` match, and +0.15 for each signal category: mentorship, training / learning budget, early-career / rotational program, career growth. Capped at 1.0. |

**Worked example** (tested in `tests/unit/test_scoring_engine.py`): a *Business
Analyst* role in New York at $95k–$120k base, asking for SQL, Python, Tableau,
Java and "0-2 years", with a mentorship mention:
25×1 + 25×0.75 + 15×1 + 10×1 + 10×1 + 10×1 + 5×0.65 = **92.0**.

### Categories and shortlist

* `needs_review` applies if **any eligibility flag has severity `review` or
  `blocker`**, or if the description is shorter than 300 characters (too little
  information).
* Otherwise the category is `strong_match` when overall ≥ `scoring.strong_threshold`
  (80), `possible_match` when ≥ `possible_threshold` (60), and `weak_match` below that.
* **Shortlisted** means overall ≥ `shortlist_threshold` (70) **and no `blocker`
  flag**. A `needs_review` job can still be shortlisted when its flags are only
  `review`; the recommended action then says to review the statement first.
* The recommended next action is one of: *Review eligibility statement before
  investing time*, *Skip - requires N+ years*, *Skip - senior-level role*, *Open
  the listing and read the full description…*, *Prepare tailored documents*,
  *Read the full listing and decide…*, *Low priority…*.

## Eligibility flags — not legal advice

Flags come from verbatim sentences in `sponsorship_info`,
`work_authorization_requirements`, `description_text`, and the LLM's verbatim
`sponsorship_statements`, which go through the same rules. Every flag stores
the sentence as `excerpt`, and its `message` begins with "Review: the listing
states…". Messages **never** say that you are or are not eligible. The flags
only point at text a human must read. Your work-authorization profile fields
are **not** used here. Legal questions on application forms are always routed
to you (CLAUDE.md rule 2).

| Code | Severity | Triggered by (examples) |
|---|---|---|
| `no_sponsorship` | review | "will not sponsor", "unable to sponsor", "no visa sponsorship", "without sponsorship", "must be authorized to work … without", "not able to provide sponsorship" |
| `citizenship_required` | blocker | "U.S. citizen", "US citizenship required", "must be a citizen" (EEO "regardless of citizenship" text and "not required" are ignored) |
| `permanent_residency_required` | blocker | "green card", "permanent resident" (but not "green card sponsorship") |
| `clearance_required` | blocker | "security clearance", "TS/SCI", "secret clearance", "clearance required" (but not "no clearance required") |
| `export_control` | blocker | "ITAR", "EAR" (uppercase), "export control", "U.S. person" |
| `sponsorship_available` | info | "visa sponsorship available", "we sponsor", "will sponsor" (skipped when the sentence contains a negation) |
| `graduation_timing` | review | A graduation or start-date deadline earlier than the **expected graduation in your profile** (`education[].expected: true`, `end`): "December 2026 graduates", "graduated by August 2026", "start date in 2026", "must have completed a Master's degree" (when yours is still expected). "2027 or later", "since 2015" and similar lower bounds are ignored. Start dates are ignored for internships. |

The patterns are heuristics. A flag can be wrong, and a statement phrased in an
unusual way can be missed. **Always read the listing yourself.**

## Local AI (Ollama), and what it cannot do

Without an analyzer, scoring is rules-only (`rules-1.0`). When
`scoring.use_llm` and `llm.enabled` are true and `job_agent.llm.analysis.make_analyzer`
returns an analyzer, a run works like this:

1. Every stale job is scored and saved with the rules alone first, which is fast and durable.
2. Only the top `scoring.max_llm_jobs_per_run` of those jobs, ranked by rules
   score, are sent to the analyzer. The resulting validated `JobAnalysis` is then blended in:
   * `required_skills` / `preferred_skills` (the llm module has already checked
     they appear in the listing) are **added** as requirements. Whether you have
     them is still decided **only against your verified profile**;
   * `coding_intensity: high` → career_interest −0.10;
   * the four relevance floats → domain_relevance = 0.5 × rules + 0.5 × mean of the
     two highest values;
   * `seniority` / `years_experience_required` are used **only if the rules found
     nothing**;
   * `sponsorship_statements` are passed through the same eligibility rules;
   * `fit_explanation` is appended to the domain_relevance explanation as
     "Local AI analysis: …".
3. The score is saved as `rules-1.0+llm` with `used_llm = 1` and `llm_model`.
   If the analyzer raises or returns invalid data, the error is recorded
   (`errors`, component `scoring:llm`) and the rules score stays current.

The LLM never selects actions, never marks a qualification as satisfied, and
never removes or overrides an eligibility flag.

## Persistence, staleness, pipeline state

* `job_scores` keeps every version. The row is unique per (job, scoring version,
  profile hash, config hash, content hash, used_llm), so saving the same score
  again refreshes that row instead of adding a duplicate. Exactly one row per job
  has `is_current = 1`.
* A job is re-scored when its current score is missing or when any of these changed:
  `jobs.content_hash`, `profile.profile_hash()`, the **scoring config hash**
  (the `search` and `scoring` sections, excluding `max_jobs_per_run`,
  `max_posting_age_days`, `use_llm` and `max_llm_jobs_per_run`), or the base
  scoring version. `--rescore` forces it.
* Only `listing_status = 'active'` jobs that are not `filtered_out`, `closed` or
  duplicates are scored automatically. `--job J-…` scores the named jobs
  explicitly, including filtered-out ones, which may then move to
  scored/shortlisted.
* State goes to `shortlisted` or `scored` through `repo.set_job_state`
  (`discovered → scored → shortlisted`). Jobs that already have an application,
  closed jobs, and filtered-out jobs (unless named explicitly) keep their state.
  Only active listings can be shortlisted.
* `job mark … irrelevant` hides a job from `jobs`, together with filtered-out
  jobs. Use `--include-filtered` to show them. It does not change the score.
  Marks and notes are audited, but note text is not copied into the audit log.

## Tuning

Edit `config/config.yaml`. The next `job-agent score` re-scores everything,
because the config hash changes.

```yaml
scoring:
  weights: {career_interest: 25, skills: 25, experience_level: 15, domain_relevance: 10,
            location: 10, compensation: 10, employer_growth: 5}   # any non-negative numbers; normalized to 100
  shortlist_threshold: 70
  strong_threshold: 80
  possible_threshold: 60
  salary_target_base: 90000
  use_llm: true
  max_llm_jobs_per_run: 10
search:
  target_titles: [...]          # career_interest
  title_keywords: [...]         # career_interest fallback
  locations: [...]              # location (word-boundary)
  remote_preference: any        # location
  target_employers: [...]       # employer_growth bonus
```

The profile also matters: `skills`, fact `skills` / `keywords` (evidence),
`preferences.target_roles`, `preferences.avoid_focus`, `preferences.preferred_locations`,
`preferences.salary_currency`, and the expected `education` end date (graduation timing).
A profile skill that is not in the built-in taxonomy (`scoring/skills.py`) is still
matched literally, with word boundaries.

## Limitations (honest)

* Everything here is keyword and regex heuristics over English text. Unusual
  wording can be missed or misread: a requirement written only in prose, years
  stated as "a few years", or degree alternatives spread over several sentences.
* When there is no separate requirements list, every skill mentioned anywhere in the
  description counts as *required*. The `skills` factor is therefore an estimate.
* The taxonomy is finite (82 built-in skills). An unknown requirement is invisible
  to the rules unless the LLM names it. Soft skills other than stakeholder
  management are ignored on purpose.
* Fact `keywords` count as evidence (e.g. a fact tagged `statistics` supports a
  "statistics" requirement), so a too-generous tag in your profile overstates a match.
* Domain relevance counts distinct terms. A long description that mentions
  "data" or "business" casually scores higher than its real focus deserves.
* Salary parsing belongs to discovery. A wrong `salary_period` or
  `salary_is_base` there will mislead the compensation factor. Unknown periods
  ≥ 20,000 are assumed annual, and the explanation says so.
* Eligibility flags are not legal advice and are not exhaustive.
* Jobs that were scored rules-only and have not changed are not re-analyzed when
  Ollama becomes available later. Run `job-agent score --rescore` to send the
  current top N through the analyzer.
