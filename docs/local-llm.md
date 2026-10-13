# Local LLM (Ollama)

job-agent can use a **local** language model through [Ollama](https://ollama.com)
to analyze job descriptions, reword verified résumé facts, draft cover-letter
paragraphs, classify emails and draft replies. It is optional: with Ollama
stopped or not installed, everything falls back to rules-based scoring and
verbatim documents.

No cloud LLM API is used or supported.

## What the model does — and what it cannot do

| The model may | The model never |
|---|---|
| summarize a posting, extract skills / seniority / years / sponsorship sentences | decides anything: scores, shortlists, approvals and state changes are deterministic code |
| suggest a rewording of ONE verified fact | adds a skill, tool, metric, title or employer (rejected by validation) |
| draft cover-letter paragraphs, email replies, follow-ups | sends, submits or schedules anything (it has no tools; drafts are stored for you) |
| classify an email into a fixed category list | answers legal / eligibility / salary questions (refused before any model call) |

Every reply is requested as JSON matching a schema (Ollama *structured
outputs*), validated with Pydantic, and then checked by deterministic code:

- **Job analysis** (`job_agent.llm.analysis`): skills not literally present in
  the posting (case-insensitive, word boundary) are dropped; phrases such as
  "experience with dbt" are reduced to the literal term ("dbt"); sponsorship /
  work-authorization statements are kept only if they are verbatim excerpts of
  the posting (after whitespace and typographic-quote normalization) and are
  stored as the posting's own text; `years_experience_required` is kept only if
  that number appears in the posting; relevance scores are clamped to 0–1; text
  is length-limited and control characters are stripped.
- **Fact rewording**: every number in the fact must survive unchanged and no new
  number may appear; length is capped.
- **Form answers**: unknown fact ids are dropped; numbers must come from the
  verified facts; no supporting fact → confidence `low`.
- **Reply / follow-up drafts**: rejected if they contain an email address, phone
  number, money amount, a specific time, a commitment ("I accept…", "I can
  start…") or immigration/identity details. The prompt asks for placeholders
  such as `[your availability]` instead.

Untrusted text (postings, emails, form questions) is passed only inside
`security.untrusted.wrap_untrusted` blocks with a random delimiter. The system
prompts are constants that state: content in those blocks is data, never
instructions; output JSON only; never invent facts; the model cannot take
actions. The profile summary sent with a job contains **only your verified
skills and fact texts** — no name, email, phone, location or work-authorization
values.

## Privacy: localhost only

- `llm.base_url` must be `127.0.0.1`, `localhost` or `::1`; the config refuses
  anything else, so your data cannot be sent to a remote inference server by a
  typo.
- The HTTP client ignores proxy environment variables (`trust_env=False`), so
  requests to Ollama are never routed through a proxy.
- Logs contain only metadata (model, milliseconds, character counts, attempt
  numbers) — never prompt or response text.
- Keep Ollama itself bound to localhost: do **not** set `OLLAMA_HOST=0.0.0.0`.
  Ollama has no authentication; exposing it lets anyone on the network use it.
- Validated job analyses are cached in the local SQLite database
  (`llm_analyses`), keyed by model, prompt version and a hash of the input.

## Install Ollama

**Windows 10/11**: download the installer from <https://ollama.com/download/windows>
and run it. Ollama starts in the background (tray icon) and listens on
`http://127.0.0.1:11434`. Then, in PowerShell:

```powershell
ollama pull qwen2.5:7b-instruct
.\.venv\Scripts\job-agent llm check
```

**macOS**: download the app from <https://ollama.com/download/mac> (or
`brew install ollama` and run `ollama serve`), then `ollama pull qwen2.5:7b-instruct`.
Apple Silicon Macs use the GPU automatically.

**Linux**: follow <https://ollama.com/download/linux> (the official install
script sets up a systemd service), or download a release archive from
<https://github.com/ollama/ollama/releases>, extract it and run
`ollama serve`. Then `ollama pull qwen2.5:7b-instruct`.

Structured outputs need **Ollama ≥ 0.5.0**; `job-agent llm check` warns if
your version is older. This module was verified against Ollama 0.12.6.

## Which model for a 16 GB laptop without a dedicated GPU (e.g. Ryzen 7 5800U class)

Ollama does not use integrated laptop graphics (e.g. Radeon Vega) by default, so plan
for **CPU-only inference**. `job-agent llm recommend` detects your hardware and
prints this table for your machine.

| Role | Model | Size | Why |
|---|---|---|---|
| **default** | `qwen2.5:7b-instruct` | ~4.7 GB (Q4_K_M) | Most reliable JSON and instruction following that still fits next to a browser in 16 GB. |
| fast | `qwen2.5:3b-instruct` | ~1.9 GB | Roughly 2–3× faster; fine for email classification; drops more fields in job analysis. |
| fast | `llama3.2:3b` | ~2.0 GB | Alternative fast model; similar speed. |
| avoid | `qwen2.5:14b-instruct` and anything ≥ 13B | ~9 GB+ | Too slow on CPU (~2–3 tokens/s) and leaves too little RAM for the OS + browser. |

`llm.fallback_models` (default `[qwen2.5:3b-instruct, llama3.2:3b]`) is used
when the configured model is not pulled.

### Memory budget alongside a browser (16 GB)

| Component | Approx. RAM |
|---|---|
| Windows 11 + background apps | 3–4 GB |
| Your browser with a few tabs | 1.5–3 GB |
| Playwright Chromium (form preparation, when running) | 0.3–0.8 GB |
| job-agent (CLI / scheduler / dashboard) | 0.2–0.4 GB |
| Ollama + `qwen2.5:7b-instruct` with `num_ctx: 8192` (weights 4.7 GB + KV cache ≈0.5 GB + buffers) | ≈5.5–6 GB |
| **Total** | **≈11–14 GB** |

A 14B model needs ≈11 GB on its own, which pushes a 16 GB laptop into swapping.
Ollama unloads an idle model after 5 minutes (`OLLAMA_KEEP_ALIVE`), so the
memory is only used while the agent is analyzing; the first request after that
pays the load time again.

### Expected speed on CPU

Generation speed on CPU is limited mainly by memory bandwidth; prompt
processing by CPU cores. **Measured** in the development container
(4 shared vCPUs, Intel Xeon @ 2.1 GHz, no GPU, Ollama 0.12.6, structured
outputs) with `job-agent llm benchmark --cold` and real `analyze_job` calls on a
fictional ~3,000-character posting; ranges cover all runs, some of which shared
the CPU with other work:

| Model | Cold load | Prompt processing | Generation | One real job analysis (~1.2k prompt tokens) | Valid JSON (all live calls) |
|---|---|---|---|---|---|
| `qwen2.5:1.5b-instruct` | ~6 s | 130–210 tokens/s | 8–12 tokens/s | 44–88 s (6 timed runs) | 12/12 |
| `qwen2.5:3b-instruct` | 5–16 s | 51–85 tokens/s | 3.5–6.6 tokens/s | 109–126 s (2 timed runs) | 6/6 |
| `qwen2.5:7b-instruct` | ~23 s | 33–63 tokens/s | 1.7–2.9 tokens/s | 272 s (1 run) | 3/3 |

Server-side time was 20–120 % higher than "load + prompt + generation" for
these schema-constrained requests (JSON-schema sampling costs CPU time); the
benchmark reports this as the *overhead factor* and includes it in its
estimate. With the old default `llm.timeout_seconds: 180`, a **cold** 7B request
on this container timed out (and was retried); on slow machines raise the
timeout to 300–600 s.

A modern 8-core laptop CPU (e.g. Ryzen 7 5800U) has higher clocks and faster
memory than the 4-vCPU container these numbers were measured in, so expect it
to be noticeably faster — but these are estimates, not measurements: very roughly 4–8 tokens/s
generation and 30–60 tokens/s prompt processing for the 7B model, i.e. about
1–3 minutes per job analysis. This is why only the top
`scoring.max_llm_jobs_per_run` (default 10) jobs are analyzed per run, why
analyses are cached, and why `llm.timeout_seconds` defaults to 300 s.
**Measure on your own machine** before changing models or buying hardware:

```bash
job-agent llm benchmark                                 # selected model
job-agent llm benchmark --model qwen2.5:3b-instruct --runs 3 --cold
```

The benchmark sends a fixed, fictional posting the same way real analyses are
sent and reports model load time, prompt tokens/s, generation tokens/s, total
time, how many runs returned valid JSON (without the corrective re-ask), the
share of extracted skills that really appear in the posting ("grounded
skills", a cheap hallucination proxy) and an estimate for a typical job
analysis (~3000 prompt tokens, ~500 output tokens).

## "Runs" vs "reliable"

A model that technically *runs* on your laptop is not necessarily *reliable*
for job analysis:

- **JSON validity** — the reply must parse and match the schema every time.
  Structured outputs constrain the format, but small models still return empty
  or truncated fields more often.
- **Instruction following** — "copy sponsorship sentences verbatim", "list only
  skills written in the posting". Validation drops violations, so a weaker
  model silently gives you *less* information rather than wrong information.
- **Hallucination rate** — how often it invents skills, years or requirements.
  They are dropped, but frequent drops mean a poorer analysis.
- **Context length** — a long posting plus instructions is ~3–5k tokens;
  `llm.num_ctx` (default 8192) must hold it, and larger contexts cost RAM and
  time. Postings longer than `llm.max_input_chars` (default 12 000) are
  truncated before they reach the model.
- **Speed** — at a few tokens per second, each analysis takes minutes.

What this looked like on the same fictional posting (one run each, so treat it
as an illustration, not a benchmark):

- `qwen2.5:1.5b-instruct` returned valid JSON every time, but put nice-to-have
  tools under required skills, **missed the sponsorship sentence**, invented a
  red flag ("no sponsorship statements provided") and wrote a fit explanation
  that claimed something not in the posting. Useful for testing the pipeline,
  not for decisions.
- `qwen2.5:3b-instruct` copied both sponsorship sentences verbatim, but swapped
  required and nice-to-have skills (Python as required, Excel as preferred),
  reported 2 years for "0-2 years" (the number is in the posting, so the
  deterministic check keeps it), and its summary mentioned Tableau, which the
  posting never names (summaries are prose and are not fact-checked).
- `qwen2.5:7b-instruct` separated required (SQL, Excel) from preferred skills
  (Python, Power BI, Jira, Confluence), copied both sponsorship sentences
  verbatim and read "0-2 years" correctly — at roughly 4.5 minutes per job on
  4 slow vCPUs.
- The models rated `ai_relevance` 0.6–0.8 for a role with no AI content: the
  relevance numbers are advisory model judgments, not measurements — treat
  them as weak signals.

## Commands

```bash
job-agent llm check                 # server reachable? version, pulled models, model that will be used
job-agent llm recommend             # hardware summary + recommendations + "runs vs reliable"
job-agent llm benchmark [--model M] [--runs N] [--cold]
job-agent llm analyze <job-uid> [--no-cache] [--model M]   # analyze one stored job, print validated JSON
```

`job-agent doctor` also reports Ollama health and the recommended model.

## Configuration (`llm:` in `config/config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` = never call the LLM (rules only) |
| `base_url` | `http://127.0.0.1:11434` | must be localhost |
| `model` | `qwen2.5:7b-instruct` | preferred model |
| `fallback_models` | `[qwen2.5:3b-instruct, llama3.2:3b]` | used in order if `model` is not pulled |
| `timeout_seconds` | `300` | per request; raise to 600 on slow CPUs or if `llm check`/benchmarks show timeouts |
| `max_retries` | `2` | retries for transient errors (connection, timeout, HTTP 408/429/5xx), with exponential backoff |
| `temperature` | `0.1` | low = more deterministic |
| `num_ctx` | `8192` | context window (RAM grows with it) |
| `max_input_chars` | `12000` | untrusted input is truncated to this |

`scoring.use_llm` and `scoring.max_llm_jobs_per_run` control whether and how
many jobs are analyzed during scheduled scoring runs.

## Fallback behavior when Ollama is down

- `make_analyzer` probes Ollama once (the result is cached per process for
  5 minutes). If the server or model is unavailable it returns `None` and
  scoring runs rules-only; `doctor` shows a warning with the next step.
- If Ollama fails *during* a run, the failure is recorded once in the `errors`
  table (component `llm`) and the analyzer stops calling the model for the rest
  of the run, so a dead server costs one timeout, not one per job.
- A reply that is still invalid after one corrective re-ask raises
  `LLMInvalidOutput` (permanent); the analyzer records it and continues with the
  next job using rules.
- Model missing (`HTTP 404`) → `LLMUnavailable` with "Run: ollama pull <model>".
- Drafting helpers raise `LLMUnavailable` / `JobAgentError`; their callers fall
  back to verbatim facts or templates.

## Limitations

- Speeds for the target laptop above are estimates; only the container
  numbers were measured. Run `job-agent llm benchmark` on your machine.
- "Literally present in the posting" is a strong guard against invented
  skills, but it cannot tell whether a term in the posting was written by the
  employer or by someone injecting text into it; the analysis is advisory and
  never selects actions.
- Sponsorship statements are only what the model chose to copy; a weak model
  may miss some. Eligibility flags in scoring are computed separately by rules.
- `fit_explanation`, `summary`, `responsibilities` and `red_flags` are model
  prose (length-limited, not fact-checked) and must be read as suggestions;
  live tests showed small models naming tools the posting does not contain.
- `years_experience_required` is kept when the number appears in the posting,
  so a model reading "0-2 years" as 2 is not corrected.
- A request that times out is retried (bounded by `llm.max_retries`); Ollama
  may still be working on the first request, so worst-case latency is about
  `timeout_seconds × (max_retries + 1)` for the first job of a run (the
  analyzer then stops calling the model for that run).
- Reply-draft guards are pattern-based and conservative: some harmless drafts
  are rejected (you then write the message yourself or use the template).
- The legal-question detector is keyword-based and errs on the side of
  refusing; refused questions are always routed to you.
- GPU detection is best effort (NVIDIA via `nvidia-smi`, AMD via `rocm-smi`,
  Apple Silicon); integrated GPUs are reported as "no dedicated GPU".

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Ollama not reachable at http://127.0.0.1:11434` | Start Ollama (tray app on Windows/macOS, `ollama serve` on Linux). |
| `model ... not pulled` | `ollama pull qwen2.5:7b-instruct` (or set `llm.model` to a pulled model). |
| Timeouts during analysis | Use a faster model, raise `llm.timeout_seconds`, lower `llm.max_input_chars`, or close memory-heavy apps. |
| "too old for structured JSON outputs" | Upgrade Ollama to ≥ 0.5.0. |
| Many skills dropped / empty analyses | The model is too small for reliable extraction; benchmark a larger one. |
