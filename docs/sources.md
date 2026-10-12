# Job sources

job-agent discovers jobs only from **public, documented job-board APIs** that
employers publish for their own career sites. It never scrapes pages behind a
login, never solves or bypasses CAPTCHAs or anti-bot pages, and never sends
cookies or credentials.

```bash
job-agent sources                 # configured sources, last success, consecutive failures
job-agent sources check           # one lightweight live request per configured board
job-agent discover                # all enabled sources
job-agent discover -s greenhouse:capco -s ashby:ramp --limit 50
```

## Supported sources

| Source | Status | Endpoint used | Notes |
|---|---|---|---|
| `greenhouse` | **real** | `GET https://boards-api.greenhouse.io/v1/boards/{token}` (board name) and `.../{token}/jobs?content=true` | Greenhouse Job Board API. Returns every published job in one response (no pagination). `content` is HTML-escaped HTML. |
| `lever` | **real** | `GET https://api.lever.co/v0/postings/{company}?mode=json&skip=N&limit=100` | Lever Postings API. Paginated with `skip`/`limit`, capped by `sources.max_pages`. |
| `ashby` | **real** | `GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` | Ashby public Posting API. One response per board; unlisted jobs are ignored. |
| `fixture` | **MOCK** | a local JSON file under the project home | For tests and demos only. Never part of the configured sources; must be named explicitly (`-s fixture:data/demo.json`). Its `job_sources` row has `is_mock = 1`. |

These three APIs are read-only endpoints the vendors document specifically so
that job boards and career pages can list an employer's openings. Use them at
human-scale volumes; job-agent's defaults (one request per second per host,
bounded retries) are deliberately conservative.

## Configuring boards

Edit `config/config.yaml`:

```yaml
sources:
  user_agent: "job-agent/0.1 (personal job search assistant)"
  min_request_interval_seconds: 1.0   # per host
  request_timeout_seconds: 20
  max_retries: 3                      # timeouts, connection errors, 5xx, 429 only
  max_pages: 20                       # pagination cap per identifier (Lever)
  greenhouse: {enabled: true, boards: [capco, databricks]}
  lever:      {enabled: true, companies: [palantir]}
  ashby:      {enabled: true, boards: [ramp, notion]}
```

Set `JOB_AGENT_CONTACT_EMAIL` in `.env` to append `(contact: you@example.com)` to
the User-Agent so an API operator can reach you instead of blocking you.

### Finding an employer's board token

Open the employer's careers page and follow a job link:

* `https://job-boards.greenhouse.io/<token>/jobs/123` or `boards.greenhouse.io/<token>` → Greenhouse token `<token>`.
  Career pages on the employer's own domain often carry `?gh_jid=123`; look for a "powered by Greenhouse" link
  or the embed script `boards.greenhouse.io/embed/job_board/js?for=<token>`.
* `https://jobs.lever.co/<company>/<uuid>` → Lever company `<company>`.
* `https://jobs.ashbyhq.com/<board>/<uuid>` → Ashby board `<board>`.

Verify with `job-agent sources check -s greenhouse:<token>` (a 404 means "board
not found"; nothing is stored).

## What happens to each listing

1. **Fetch** politely (see below). A source that fails (timeout, 5xx after
   retries, 401/403, 404, invalid JSON) is recorded in `errors` (component
   `discovery:<source>:<identifier>`) and in `job_sources.last_error` /
   `consecutive_failures`; the other sources still run and the run is *partial*.
2. **Normalize** into `NormalizedJob`. Listings without an id or title are
   skipped and counted as `invalid`.
3. **Title prefilter**: a title is relevant when it contains every word of a
   `search.target_titles` entry (any order) or any `search.title_keywords`
   phrase. Irrelevant listings are counted (`fetched - relevant`) but **not
   stored**.
4. **Filters** (stored as `pipeline_state = filtered_out` with a readable
   `filter_reason` when they fail): `exclude_title_keywords`, `locations`,
   `remote_preference`, `employment_types`, `max_years_experience` (only when
   the listing states years explicitly), `max_posting_age_days`, and a closing
   date in the past. All keyword matching is case-insensitive on word
   boundaries: `"US"` matches "US - New York" but never "Austin"; `"sr"`
   matches "Sr. Analyst" but not "Srinagar"; `"lead"` matches "Team Lead" but
   not "Leadership Development Analyst".
5. **Deduplicate and store** (one transaction per listing).
6. **Close** listings that a *complete* fetch no longer returns.

### Normalization rules (no guessing)

* **Work arrangement** only from explicit signals: the API's workplace field
  (Lever `workplaceType`, Ashby `workplaceType`), then Ashby `isRemote`, then the
  words Remote / Hybrid / On-site in the location. Never from the description.
* **Employment type** from structured fields (Greenhouse metadata named like
  "Employment Type", Lever `commitment`, Ashby `employmentType`); a title with
  "Intern"/"Internship" is always an internship ("Internal Audit" is not).
* **Experience level** from title words (senior/staff/lead…, intern, new grad,
  entry, junior, associate); otherwise from an explicit "N+ years of
  experience" requirement. The smallest stated minimum in the requirements is
  stored as `raw_metadata.years_experience_min` with its verbatim snippet.
* **Salary**: structured fields first (Lever `salaryRange`, Ashby `Salary`
  component — equity/commission/bonus components are ignored); otherwise text
  patterns such as "$90,000 - $120,000", "$45/hour", "€29.000", "from € 3,500",
  only with a currency marker and a pay context, and never near benefit or
  company figures (HRA, 401(k), stipend, revenue, "$200B"…). `salary_text`
  keeps the verbatim snippet. `salary_is_base` is `true` only when the text says
  *base*, `false` only when it says OTE / total compensation / a commission
  split, otherwise unknown. If no period is stated and the amount is ≥ 20,000
  the period is recorded as `year` and `raw_metadata.salary_period_inferred` is
  set. A bare `$` is treated as USD unless a currency code is present.
* **Sponsorship / work authorization**: verbatim sentences mentioning visas,
  sponsorship (with eligibility context, so "executive sponsors" is ignored),
  work authorization, citizenship, permanent residency, U.S. person, security
  clearance, export control or ITAR. Never inferred; the scorer and the
  application module treat them as items for human review.
* **Qualifications**: bullets under headings like Requirements /
  Qualifications / What you'll need / Basic qualifications (required) vs
  Preferred / Nice to have / Bonus points (preferred). No headings → empty.
* **Dates** are stored as ISO-8601 UTC. Greenhouse `first_published` is used as
  the posting date (falling back to `updated_at` when absent), Lever
  `createdAt`, Ashby `publishedAt`; Greenhouse `application_deadline` becomes
  `closing_at`.
* **URLs** are canonicalized for matching: lowercase scheme/host, no fragment,
  no trailing slash, tracking parameters removed (`utm_*`, `gh_src`, `source`,
  `ref`, `lever-source`, `lever-origin`, …). Parameters that identify the job
  (`gh_jid`, `ashby_jid`, `id`, …) are kept.

### Identity, changes and duplicates

* `job_uid` = `J-` + 10 hex characters of SHA-256 over
  `source + identifier + external id` — stable across runs.
* Re-seen, unchanged listings only bump `last_checked_at` / `times_seen`.
* A changed `content_hash` (title, location, description, qualifications,
  salary, closing date, URLs, eligibility text, …) updates the row, sets
  `last_changed_at` and records a `material_change` in `job_changes`. The
  scorer re-scores because the hash changed. If the new content fails the
  filters, the job moves to `filtered_out` (unless an application exists or you
  marked it relevant). A `filtered_out` job that now passes stays
  `filtered_out` (the state machine has no `filtered_out → discovered`
  transition) and the change record says so.
* **Duplicates across sources** (canonical job/application URL match; same
  employer + requisition id with compatible locations; or same
  employer + title + location with ≥ 90% description similarity and no
  conflicting requisition ids) are stored as their own row with
  `duplicate_of_job_id` set, `pipeline_state = filtered_out` and
  `filter_reason = "duplicate of J-…"`; the original gets a `duplicate_detected`
  change. Listings from the *same* board with different ids are always distinct
  (Greenhouse posts one listing per city under one requisition). The same
  listing id appearing twice in one fetch is counted as a duplicate.
* **Reposts**: a new id on the same board with the same title/location/text as
  a closed listing records a `repost` change; a posting date that moves forward
  with unchanged content also records `repost`.
* **Closures**: only after a complete, successful fetch (no error, not cut by
  `max_pages`), active listings of that board that were not returned become
  `listing_status = closed` with a `closed` change. A fetch that returns zero
  listings closes nothing (precaution against an API glitch). A closed listing
  that comes back is set active again with a `reopened` change. Closure does
  not change `pipeline_state`.
* `--limit` / `search.max_jobs_per_run` caps new + updated listings per run.
  Once reached, further new/changed listings are *deferred* (left for the next
  run) and remaining sources are skipped; unchanged bookkeeping and closures
  for the source being processed still happen.

## Politeness

* At most one request per `min_request_interval_seconds` per host.
* Retries only for timeouts, connection errors, HTTP 5xx and 429, with
  exponential backoff (1 s, 2 s, 4 s … capped at 30 s). `Retry-After` is
  honored; if a server asks for more than 60 s, the source stops for this run
  (recoverable error) instead of retrying early.
* HTTP 401/403 → permanent error "access denied - not bypassing". HTTP 404/410
  → "board not found". An HTML page with CAPTCHA / bot-check markers →
  `HumanActionRequired`; job-agent does not try to get around it.
* No cookies are kept or sent, no credentials are used, HTTPS only.

## Not supported (and why)

| Site | Why not |
|---|---|
| LinkedIn | No public job-search API for individuals; listings require a login, and the user agreement prohibits automated access/scraping. |
| Indeed | The public job-search API was retired; the site is protected by anti-bot measures and its terms prohibit scraping. |
| Workday | Each employer's Workday tenant is a JavaScript application with undocumented internal endpoints, often behind bot protection; there is no published job-board API. |
| Handshake | Requires a student/university login; no public API. |
| Glassdoor, ZipRecruiter, Monster, etc. | Partner-only APIs or terms that prohibit automated collection. |

For employers on these platforms, check whether they also publish a
Greenhouse/Lever/Ashby board, or review their listings yourself.

## Limitations

* Only Greenhouse, Lever and Ashby. Employer names for Lever and Ashby are the
  board identifier title-cased (the APIs do not return a company name), e.g.
  `flatironhealth` → "Flatironhealth".
* Filters run when a listing is first stored or materially changes; unchanged
  listings are not re-filtered after you change `search.*` settings, and a
  listing that ages past `max_posting_age_days` keeps its state.
* The `"Remote"` location entry accepts any remote listing, including remote
  roles restricted to another country (e.g. "Remote (Canada)").
* Requisition/fingerprint duplicate matching only considers *active* original
  rows; if an original closes while its duplicate stays open, the duplicate is
  not promoted automatically.
* Years-of-experience extraction uses the smallest explicitly stated minimum in
  the requirements, so "2+ years of SQL; 8+ years overall" counts as 2.
* Salary text patterns cover common US/EU formats; unusual phrasings are left
  empty rather than guessed. Text-derived salaries are not validated against
  the location's currency beyond explicit codes/symbols.
* Greenhouse returns the full board in one response; very large boards
  (thousands of jobs, several MB) are fetched in full every run.
