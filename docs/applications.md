# Applications: preparation, approval, submission, tracking

job-agent can open an employer's official application form in a local
Chromium browser (Playwright), fill in the answers it can *verify*, take a
screenshot, and ask you to approve a precise summary. **It never clicks
"Submit" while preparing.** By default, you submit approved applications
yourself; browser submission is an explicit opt-in.

## The workflow

```
job-agent prepare <job-uid>          # résumé / cover letter (resume module)  -> documents_ready
job-agent apply <job-uid> [--headed] # open form, fill verified answers, screenshot -> awaiting_approval
job-agent approvals                  # list pending approvals
job-agent approvals show <AP-...>    # everything you would approve
job-agent approvals answer <A-...> <field-key|#n> <value>   # resolve review items ("" = leave blank)
job-agent approvals approve <AP-...> [--note ...]           # shows the full view, then asks to confirm
job-agent approvals reject <AP-...> --note "..."            # back to documents_ready
# then EITHER (default)
#   submit it yourself in your browser, then:
job-agent applications mark-submitted <A-...> [--note ...]
# OR (only with applications.browser_submission_enabled: true)
job-agent applications submit <AP-...>                      # asks to confirm, clicks submit once
job-agent applications [list|show <A-...>|withdraw <A-...>|note <A-...> "text"]
```

Status changes (all via `database.repo.transition_application`, recorded in
the application's timeline and the audit log):

| Step | Status |
|---|---|
| `apply` starts | `documents_ready` → `in_progress` |
| form prepared, approval requested | `in_progress` → `awaiting_approval` |
| CAPTCHA / login wall / off-site redirect / no form / closed listing / browser failure | → `needs_manual_action` (with manual instructions) |
| approval rejected | `awaiting_approval` → `documents_ready` |
| approval approved, browser submission off | stays `awaiting_approval`; next action: *"Approved - submit manually at &lt;url&gt; using the prepared answers, then run job-agent applications mark-submitted &lt;application-uid&gt;"* |
| browser submission: just before the click | → `submission_attempted` (consumes the approval) |
| confirmation page seen | → `submitted` with page evidence |
| form shows validation errors after the click | → `failed` (details + screenshot) |
| timeout / ambiguous result | → `needs_manual_action` + an `unknown_outcome` error |
| you report it | `mark-submitted` → `submitted` (`confirmation_status = user_reported`) |

## What is filled automatically, and what is always left for you

Only answers with confidence **verified** are ever typed into a form:

| Field kind | What happens |
|---|---|
| First / last / full name, email, phone, location / city, LinkedIn, website / portfolio, GitHub | From `profile.personal`, never if the value still contains a `<placeholder>`. First/last name are only split automatically when your full name has exactly two parts (otherwise shown as a suggestion). City = the part of `location` before the first comma. |
| Résumé upload | Your application's résumé document, **PDF preferred**, honouring the input's `accept` attribute. |
| Cover letter upload / text box | Your cover letter document if there is one (text boxes get the text of an `.md`/`.docx` version). |
| School, degree, field of study, graduation date, GPA | Your **first** (most recent) education entry, verbatim (e.g. `May 2027`). A `month` input gets `2027-05`; a select is filled only when an option equals the value or its year. A full `date` input is never filled (no day is in your profile). "Highest degree *obtained*" is never answered with a degree that is still expected. |
| "How did you hear about us?" and other recurring questions | From `profile.verified_answers` whose `question_patterns` match the label (case-insensitive substring). Select / radio answers must **exactly** match an option. Several matching verified answers = review. |
| **Legal / eligibility** (authorized to work, sponsorship now/future, visa, citizenship, clearance, export control / US person, criminal history, background check) | Always flagged `is_legal_declaration` + `requires_review`. Filled **only** from a matching verified answer that exactly matches an option (or a free-text field). Your `work_authorization` booleans are **never** used to answer them — they are shown in the approval view as a read-only reference. |
| EEO / voluntary self-identification (gender, race/ethnicity, veteran, disability, pronouns...) | Left blank; listed for your review, even if a verified answer matches. |
| Agreements / certifications / consents ("I certify", "I agree", "acknowledge", privacy policy, terms) | **Never** checked automatically; you must answer them yourself. |
| Salary, start date / availability, relocation, notice period | Review, unless a non-legal verified answer matches. |
| Open-ended ("Why do you want to work here?") | Review. If the local LLM is enabled, a draft from your verified facts (`llm.generation.draft_answer`) is stored as a **suggestion** (confidence `uncertain`) — never entered. |
| Anything unrecognized, unrecognized uploads, hidden custom widgets | Review. |

If a value cannot be entered or does not read back correctly, the field is
marked for review instead. Answers you give with `approvals answer` are kept
when you re-run `apply`.

## What the approval contains

`job-agent approvals show <AP-...>` (and the dashboard) show, from the stored
payload:

- employer, role, job and application ids, the official application URL;
- résumé and cover letter: file name, SHA-256, document status;
- **answers that will be entered** (field, key, value, source such as
  `profile:personal.email`, `verified_answer:<id>`, `document:<id>`, `user`);
- **legal declarations** with their answers and whether each comes from a
  verified source;
- required fields still unanswered; items needing your review (with any
  suggestion); agreements; fields you deliberately left blank;
- validation errors the form showed after filling, and the screenshot path
  (`data/screenshots/<application-uid>/prepared.png`);
- your work-authorization values as a display-only reference (notes excluded);
- the reasons browser submission would be refused, if any.

The approval is bound to the SHA-256 of that payload. `build_payload` is a pure
function of the database (answers, documents, the prepared-form record), so it
is recomputed at submission time: if anything changed after you approved (an
answer you edited, a regenerated document), the old approval is superseded or
the hash no longer matches, and a new approval is required. Approvals expire
after `applications.approval_expiry_hours` (default 72).

`approvals approve` passes the hash of the view it displayed, so a request
that changed between showing and confirming is refused.

## `applications.browser_submission_enabled` (default `false`)

With the default, the agent never submits anything: approve, submit the form
yourself in your own browser using the prepared answers, then run
`job-agent applications mark-submitted <application-uid>`.

If you set it to `true`, `job-agent applications submit <approval-uid>`:

1. requires `permissions.submit_applications` and an **approved**, unused,
   unexpired approval (pending / rejected / superseded / consumed are refused);
2. refuses if the rebuilt payload's hash differs from the approved one, or if
   it has unanswered required fields, unresolved review items, or a legal
   declaration not answered from a verified source (your verified answers or
   your own `approvals answer`);
3. refuses if any earlier attempt for this application had an unknown outcome;
4. re-opens the official URL, re-checks for security challenges, re-fills
   exactly the approved answers (re-verifying document hashes), reads every
   value back, and stops (nothing sent, approval unused) if the form changed,
   shows errors, has unapproved required fields, or would post off-site;
5. records `submission_attempted` — consuming the approval exactly once —
   **before** clicking; a crash after this point leaves `submission_attempted`,
   which blocks any automatic retry;
6. clicks the submit button **once** and watches the page (never reloads or
   re-clicks) for up to `browser_timeout_seconds`:
   - URL now containing `confirm`/`thank`, or text such as "Thank you for
     applying" / "application has been received/submitted" that was not on the
     form page → `submitted` with evidence
     `{type: "page", url, matched_text, screenshot}`;
   - visible validation errors → `failed`;
   - anything else → unknown outcome.

## Unknown outcomes

A timeout, a security check appearing after the click, an unexpected
navigation or a browser error after clicking means the agent **does not know**
whether the employer received the application. The application moves to
`needs_manual_action`, an `unknown_outcome` error is recorded, and the next
action says: *check your email/portal before doing anything else*. The agent
will never submit that application again on its own (even after a new
approval). If it went through, run `mark-submitted`; otherwise apply manually.

## Manual fallback

Whenever automation stops (CAPTCHA, "verify you are human", Cloudflare,
login wall / password field, account creation, a redirect to another host,
no readable form, closed listing, browser failure), the application moves to
`needs_manual_action` with instructions: the official URL, the résumé file,
`job-agent applications show <application-uid>` for the prepared answers, and
`mark-submitted` afterwards. Challenges are never solved or bypassed.

## Browser safety

- Opens only `http(s)` URLs whose host **exactly equals** the host of the job's
  stored `application_url` / `job_url` (`www.x.com` ≠ `x.com`; no https→http
  downgrade).
- Top-level navigations are checked before the request is sent; redirects
  (which Playwright cannot intercept) are observed and the session stops if
  they leave the allowlist. A redirect to a known ATS host (Greenhouse,
  Lever, Ashby) is accepted only for the job's own source **and** only when
  the URL contains the job's board identifier.
- Form posts are blocked unless armed for the single approved click, so
  preparing cannot submit a form even if the page tries to.
- New windows are closed and stop the session; dialogs are dismissed.
- One browser at a time (`max_concurrent_browsers`), timeouts from config,
  no downloads, service workers blocked.
- Screenshots go to `data/screenshots/<application-uid>/` (path-contained).
  They show your filled-in answers; `data/` is git-ignored.
- Page text is untrusted: it is escaped in the CLI, and form labels sent to
  the local LLM for drafts are wrapped by `wrap_untrusted` inside
  `draft_answer`.

If the Playwright-bundled Chromium revision is missing, the newest Chromium
installed under `PLAYWRIGHT_BROWSERS_PATH` (or the default Playwright cache) is
used; `JOB_AGENT_CHROMIUM_PATH` forces a specific executable. Headed mode
(`--headed`) needs a display.

## Limitations (please read)

- **Field detection is heuristic.** Labels come from `<label for>`, wrapping
  labels, `aria-label(ledby)`, nearby question text, placeholders and names;
  categories come from keyword rules. Unusual wording can be misclassified —
  which is why everything not clearly verified goes to review and you approve
  the exact answers.
- **Tested only against local fixture pages** (`tests/fixtures/forms/`: a
  generic form, Greenhouse-like and Lever-like field names, a reCAPTCHA page, a
  login wall, validation errors, a hanging submit, a thank-you page). Real ATS
  pages vary and change without notice; no real employer site was used.
- Many real ATS forms load invisible reCAPTCHA / Turnstile scripts; such pages
  are routed to manual action by design.
- Forms inside iframes (e.g. Greenhouse embeds on a company site), multi-step
  wizards, typeahead / custom dropdown widgets, repeated education blocks
  (only the first gets your most recent entry) and file inputs that upload via
  JavaScript are not handled reliably. **Ashby and Workday single-page apps
  will usually need manual work** (Workday also requires an account).
- Confirmation detection relies on URL/text patterns; a confirmation that
  shows neither is reported as an unknown outcome (safe, but you must check).
- A server-side redirect to another host has already loaded that page (GET)
  before the session stops; nothing is typed into it. A 307/308 redirect of
  the approved POST itself would be followed by the browser.
- When a form changes between preparation and submission, the agent stops
  before clicking; re-run `apply` and approve again.
