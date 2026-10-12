# Gmail setup (read-only)

job-agent can read your recent Gmail messages to spot application
confirmations, recruiter messages, interview and scheduling requests,
assessments, rejections and offers. It uses Google's **official Gmail API**
with **OAuth 2.0** and the **read-only** scope
`https://www.googleapis.com/auth/gmail.readonly`.

What it can and cannot do:

- It **can** list and read messages that match `gmail.query` (default: the last
  14 days, excluding Promotions/Social).
- It **cannot** send, draft, reply to, label, archive, modify or delete mail.
  The code that talks to Gmail (`job_agent/email/gmail_client.py`) only has
  "list message ids" and "get message", and the token only grants read access.
  Config validation rejects any other scope, and a token that carries any other
  scope is refused.
- It **never asks for your Google password.** You sign in on Google's own
  consent page in your browser.

Gmail is optional. Everything else works without it.

---

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com/> and sign in with the Google account
   whose mailbox you want job-agent to read.
2. Top bar → project picker → **New project**. Pick any name (e.g.
   `job-agent-personal`) → **Create**, then select the project.

## 2. Enable the Gmail API

**APIs & Services → Library** → search for **Gmail API** → **Enable**.

## 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen**. In newer consoles this is under
**Google Auth Platform → Branding / Audience / Data access**. The labels differ,
but the settings are the same:

1. **User type / Audience: External**. (Use "Internal" only for a Google
   Workspace account whose admin allows it.)
2. App name (e.g. `job-agent`), your email as the support and developer
   contact. No logo, domain or privacy policy is needed for personal use.
3. **Scopes / Data access:** add `.../auth/gmail.readonly`.
   - Google treats `gmail.readonly` as a **restricted scope**. To *publish* an
     app with this scope, Google requires verification and a security
     assessment. You don't need that for personal use: leave the app in
     **Testing** mode.
4. **Publishing status: Testing.** Under **Test users** add **your own Gmail
   address**. Only test users can authorize an app that is in Testing mode.

> **Important: Testing mode expires your authorization after 7 days.**
> Google invalidates refresh tokens of apps in Testing mode after 7 days (when
> the app requests Gmail scopes). After that, `job-agent gmail sync` (and the
> scheduled `email_sync` task) stops with *"Gmail authorization required … token
> refresh rejected (invalid_grant)"*, `job-agent doctor` shows Gmail as
> **FAIL** and `job-agent gmail status` shows the reason (the dashboard and the
> daily report read the same `gmail_auth_error` state).
> **Re-run `job-agent gmail auth` about once a week.** Until you re-authorize,
> the agent does **not** keep calling Google: it fails immediately without
> any network request, so it doesn't hammer the API.

## 4. Create an OAuth client ID of type "Desktop app"

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   (or **Google Auth Platform → Clients → Create client**).
2. **Application type: Desktop app**. Name it e.g. `job-agent desktop`.
3. **Download JSON**.
4. Save the file as **`data/secrets/gmail_credentials.json`** inside your
   job-agent folder (or set `gmail.credentials_path`):

   ```bash
   mkdir -p data/secrets && chmod 700 data/secrets
   mv ~/Downloads/client_secret_*.json data/secrets/gmail_credentials.json
   chmod 600 data/secrets/gmail_credentials.json
   ```

   `data/` is git-ignored. Never commit this file. The client secret of a
   desktop app isn't truly secret, but treat it as private anyway.

## 5. Enable Gmail in the config

In `config/config.yaml`:

```yaml
gmail:
  enabled: true
  credentials_path: data/secrets/gmail_credentials.json
  token_path: data/secrets/gmail_token.json
  scopes: [https://www.googleapis.com/auth/gmail.readonly]
  query: "newer_than:14d -category:promotions -category:social"
  max_messages_per_sync: 100
  body_char_limit: 4000
  store_snippets: true

scheduler:
  tasks:
    email_sync: {interval_minutes: 30, enabled: true}   # optional: sync automatically
```

## 6. Authorize: `job-agent gmail auth`

```bash
.venv/bin/job-agent gmail auth            # opens your browser
.venv/bin/job-agent gmail auth --no-browser   # prints the URL to open yourself
```

- A temporary server listens on `localhost` on a random free port. Google
  redirects back to it after you approve.
- Google warns *"Google hasn't verified this app"*. That is expected for your
  own app in Testing mode. Click **Continue**, check that the only permission
  requested is **reading** your email, and approve.
- If Google grants anything beyond read-only, job-agent refuses the token and
  stores nothing.

Then:

```bash
.venv/bin/job-agent gmail sync      # first sync
.venv/bin/job-agent gmail status    # CONNECTED only after a successful sync
.venv/bin/job-agent emails          # what was found
```

`gmail status` (and the dashboard) only say **connected** after a sync has
succeeded since the last authorization error. A token file alone doesn't count.

## 7. Where the token is stored

`data/secrets/gmail_token.json` (or `gmail.token_path`, which must be inside the
project folder):

- written with permissions **0600**; `data/secrets/` is made 0700
- git-ignored (`data/`, `token*.json`, `*_token.json`)
- contains the OAuth access + refresh token and the granted scopes; never
  logged, audited or put in reports (logs and audit records are redacted)

## 8. Revoke or replace access

- **Delete the local token:** `job-agent gmail revoke`.
- **Revoke the grant at Google:** <https://myaccount.google.com/permissions> →
  select your app (e.g. `job-agent`) → **Remove access**. Do this if the laptop
  is lost or you stop using job-agent.
- **Replace the token** (expired, revoked, or after changing scopes): run
  `job-agent gmail auth` again. The new token overwrites the old one.
- **Replace the OAuth client:** Google Cloud Console → Credentials → delete the
  old client, create a new *Desktop app* client, save it to
  `data/secrets/gmail_credentials.json`, run `job-agent gmail auth`.

## 9. What data is stored

For each message that matches the query, the database (`email_messages`)
stores **metadata only**:

| Stored | Not stored |
|---|---|
| Gmail message id + thread id, received time (UTC) | the message **body** |
| sender address and domain, subject | attachments (never downloaded) |
| category, confidence, classifier (`rules` / `llm:<model>`) | your own sent messages' content |
| optional snippet (≤200 chars) and short summary (≤300 chars), if `gmail.store_snippets: true` | |
| "needs attention" flag, prompt-injection flag | |
| a suggested reply draft (template; never sent) | |
| links to jobs/applications (`email_associations`) with confidence | |

Bodies are decoded **in memory only** (truncated to `gmail.body_char_limit`)
for classification and matching, then discarded. With
`gmail.store_snippets: false`, no snippet is stored and the summary is built
only from category, sender and subject.

Messages labelled SENT or DRAFT, and messages from your own profile address,
are recorded (so they are not downloaded again) but never classified or acted
on. The query also adds `-in:sent -in:drafts -in:chats`.

## 10. What email can change (and what needs you)

Email content is untrusted. It can never send anything, accept an interview,
negotiate, reply or approve. The **only** effect it can have is a
deterministic, evidence-based status update, and only when **all** of these hold:

1. the **rules** classifier (not the local AI) labelled it with confidence ≥ 0.8.
   An offer must be about employment (a bank's "your offer is ready" is not),
   and "we must rescind your offer" / "unable to extend an offer" count as a
   rejection, never as an offer. Mixed offer + rejection wording is never
   confident enough.
2. **Gmail verified the sender**: the topmost `Authentication-Results` header,
   written by Gmail's own servers (`mx.google.com`), shows `dmarc=pass` or a
   `dkim=pass` for the From domain. SPF alone doesn't count, and headers the
   sender wrote into the message (including `ARC-Authentication-Results`) are
   ignored. A forged `From:` therefore changes nothing.
3. it is **not bulk or promotional mail** (no `List-Unsubscribe` / `List-Id` /
   `Precedence: bulk` header, no "unsubscribe", "manage preferences",
   "pre-approved", "% off"… in the text).
4. no prompt-injection phrases were found.
5. it is linked to one of your applications with confidence ≥ 0.8. Only these
   links reach that level:
   - the **same Gmail thread** as a message that is already linked,
   - the sender's domain is **exactly the domain of the job posting / application
     URL** (`talent@mail.acme.com` for `careers.acme.com/jobs/1`),
   - a sender domain **you linked to this employer before** with
     `job-agent emails associate` (only that exact domain; ATS and free-mail
     domains never become "trusted"),
   - an **ATS / scheduling / assessment platform** (Greenhouse, Lever, Workday,
     Calendly, HackerRank, …) naming the employer's **full name** in the
     subject or sender.

   These never reach it: a domain merely *spelled* like the employer
   (`acme-analytics.xyz`, `acmeanalytics.top`), a prefix or first word
   (`better.com` for Betterment, `capital.com` for Capital One), the employer's
   name typed into a subject by any sender, or the name inside a longer company
   name ("Mercury Insurance" is not "Mercury").
6. the role fits: if the email names a **different job title** ("your Senior
   Data Engineer application" for your Business Analyst application), it is
   not applied. If you have **several applications at the same employer**, the
   email must name this application's role.
7. the state machine allows the move **for an automatic update**:

| Category | Status (automatic) |
|---|---|
| application confirmation | `confirmation_received`, only from `submission_attempted` / `submitted` |
| rejection | `rejected` |
| interview invitation, scheduling request | `interview_requested` |
| assessment | `assessment_received` |
| offer | `offer_received` |

An email **never** changes, by itself:

- an application in `needs_manual_action`: an unknown submission outcome, or an
  application that was never submitted (e.g. stopped at a CAPTCHA). A
  confirmation email for it is flagged for you, because only you know whether
  you finished the application yourself;
- an application with no record of a submission;
- a correction between outcomes (e.g. `rejected` → `interview_requested`); only
  you can make those (`job-agent applications set-status`).

Every automatic change is recorded with `{"type": "email", gmail_message_id,
subject, received_at, category}` as evidence (actor `email`). Everything else
is only flagged **needs attention**, and `job-agent emails show <id>` lists why
it was not applied automatically (e.g. *sender not verified by Gmail
(dmarc=fail)*, *the email names a different role: Senior Data Engineer*,
*email never resolves an unknown submission outcome automatically*). The
message itself is always stored, so it is not downloaded again. You decide:

```bash
job-agent emails --attention
job-agent emails show 12
job-agent emails associate 12 A-1a2b3c4d5e --apply-status   # link it and apply the status it implies
job-agent emails associate 12 A-1a2b3c4d5e                  # just link it (also trusts that sender domain)
job-agent applications set-status A-1a2b3c4d5e rejected     # correct a status yourself
job-agent emails handled 12                                  # e.g. you replied yourself
```

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Gmail integration is disabled` | Set `gmail.enabled: true`. |
| `token does not record which scopes it carries` | The token file has no `scopes` / `granted_scopes`, so it can't be verified as read-only. Run `job-agent gmail revoke`, then `job-agent gmail auth`. |
| A genuine employer email was not applied automatically | See the reasons in `job-agent emails show <id>`. Most common: the job was found on an ATS or job board, so the employer's own mail domain is not known yet. Link one email with `job-agent emails associate <id> <A-...>`; later mail from that exact domain is then applied automatically. |
| `OAuth client file not found` | Save the Desktop-app JSON to `data/secrets/gmail_credentials.json` (step 4). |
| `not a valid Desktop-app client` | You downloaded a "Web application" client; create a **Desktop app** client. |
| `Error 403: access_denied` in the browser | Add your address under **Test users** (step 3). |
| `token refresh rejected (invalid_grant)` | Testing-mode 7-day expiry, password change, or revoked access → `job-agent gmail auth`. |
| `not contacting Google until you re-authorize` | Fail-fast after an auth error. Run `job-agent gmail auth`, then `job-agent gmail sync`. |
| `non-read-only scopes … refusing` | Run `job-agent gmail revoke`, remove the app at <https://myaccount.google.com/permissions>, check `gmail.scopes`, run `job-agent gmail auth`. |
| `HTTP 403: insufficient permission` | Token scope doesn't match `gmail.scopes` (e.g. `gmail.metadata` only); re-run `job-agent gmail auth`. |
| `Gmail libraries are not installed` | `.venv/bin/pip install -e ".[gmail]"` |
| Sync ends "partial" / quota errors | HTTP 429/5xx are retried with backoff. If retries run out, the sync stops early, records the error, and fetches the rest next time. Lower `gmail.max_messages_per_sync` if needed. |
| A message is in the wrong category | Rules are keyword based. Check `job-agent emails show <id>`; link it manually with `emails associate … --apply-status` if it implies a status. |

## Limitations (honest list)

- Classification is pattern-based (English wording, common ATS and assessment
  senders). Unusual wording falls to `other` or low confidence ("needs
  attention"). The optional local LLM is only consulted when the rules are
  unsure, and its label never changes a status on its own.
- Matching an email to a job relies on the sender domain, the employer name in
  subject/sender/body, the Gmail thread and the job title. Recruiters writing
  from personal addresses, or staffing agencies, usually need manual linking.
  The matching is deliberately strict: genuine mail from an employer domain that
  is not the job posting's domain needs one confirmation from you first, and
  job titles are only recognised in common phrasings ("the X position",
  "your X application", "Position: X").
- Sender verification relies on Gmail's `Authentication-Results` header. Mail
  from domains without DKIM/DMARC, or a message that has no such header, is
  never applied automatically. In `gmail.metadata` mode that header is not
  requested, so nothing is applied automatically there.
- An ATS sender is trusted for the employer it names. Someone with their own
  account on the same ATS could send mail naming your employer; such a message
  would still need to pass the role checks, and you can always correct a
  status with `job-agent applications set-status`.
- Interview times are not extracted from emails; no calendar integration.
- Your replies are not detected (sent mail is excluded), so "recruiter reply"
  follow-ups stay open until you mark them done or mark the email handled.
- With the `gmail.metadata` scope (alternative to `gmail.readonly`) Gmail allows
  neither search queries nor bodies: only the newest INBOX messages are listed
  and only headers + snippet are classified.
- This was tested against a fake Gmail service, never a live mailbox, in the
  automated tests. Verify with your own account (`gmail auth`, `gmail sync`,
  `emails`) before you rely on it.
