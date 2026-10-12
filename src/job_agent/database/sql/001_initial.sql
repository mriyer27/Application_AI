-- 001_initial: complete initial schema. All timestamps are ISO-8601 UTC text.
-- JSON columns hold JSON text (validated by application code).

CREATE TABLE config_metadata (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- Small key/value store for runtime state (pause flag, last successful run, ...).
CREATE TABLE agent_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- Profile *metadata* only (hash, version). The profile content stays in the YAML file.
CREATE TABLE candidate_profile_meta (
    id              INTEGER PRIMARY KEY,
    profile_hash    TEXT NOT NULL UNIQUE,
    schema_version  INTEGER NOT NULL,
    fact_count      INTEGER NOT NULL,
    skill_count     INTEGER NOT NULL,
    placeholders    INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);

CREATE TABLE job_sources (
    id                    INTEGER PRIMARY KEY,
    source_name           TEXT NOT NULL,          -- adapter, e.g. greenhouse
    identifier            TEXT NOT NULL,          -- board token / company slug
    enabled               INTEGER NOT NULL DEFAULT 1,
    is_mock               INTEGER NOT NULL DEFAULT 0,
    last_run_at           TEXT,
    last_success_at       TEXT,
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_cursor           TEXT,                   -- incremental discovery state
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (source_name, identifier)
);

CREATE TABLE jobs (
    id                          INTEGER PRIMARY KEY,
    job_uid                     TEXT NOT NULL UNIQUE,   -- stable internal id, e.g. J-1a2b3c4d5e
    source_id                   INTEGER REFERENCES job_sources(id) ON DELETE SET NULL,
    source_name                 TEXT NOT NULL,
    source_identifier           TEXT NOT NULL,
    external_id                 TEXT NOT NULL,
    requisition_id              TEXT,
    employer                    TEXT NOT NULL,
    employer_normalized         TEXT NOT NULL,
    title                       TEXT NOT NULL,
    title_normalized            TEXT NOT NULL,
    location                    TEXT,
    location_normalized         TEXT,
    work_arrangement            TEXT NOT NULL DEFAULT 'unknown',
    employment_type             TEXT NOT NULL DEFAULT 'unknown',
    experience_level            TEXT,
    department                  TEXT,
    description_text            TEXT NOT NULL DEFAULT '',
    description_hash            TEXT NOT NULL,
    required_qualifications     TEXT NOT NULL DEFAULT '[]',  -- JSON list
    preferred_qualifications    TEXT NOT NULL DEFAULT '[]',  -- JSON list
    salary_min                  REAL,
    salary_max                  REAL,
    salary_currency             TEXT,
    salary_period               TEXT NOT NULL DEFAULT 'unknown',
    salary_is_base              INTEGER,                     -- NULL = unknown
    salary_text                 TEXT,
    posted_at                   TEXT,
    discovered_at               TEXT NOT NULL,
    closing_at                  TEXT,
    job_url                     TEXT,
    job_url_canonical           TEXT,
    application_url             TEXT,
    application_url_canonical   TEXT,
    sponsorship_info            TEXT,
    work_authorization_requirements TEXT,
    listing_status              TEXT NOT NULL DEFAULT 'active',     -- active|closed|expired
    pipeline_state              TEXT NOT NULL DEFAULT 'discovered', -- see models.JOB_STATES
    filter_reason               TEXT,
    duplicate_of_job_id         INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    user_relevance              TEXT CHECK (user_relevance IN ('relevant', 'irrelevant') OR user_relevance IS NULL),
    user_notes                  TEXT,
    content_hash                TEXT NOT NULL,
    times_seen                  INTEGER NOT NULL DEFAULT 1,
    last_checked_at             TEXT NOT NULL,
    last_changed_at             TEXT NOT NULL,
    raw_metadata                TEXT NOT NULL DEFAULT '{}',
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    UNIQUE (source_name, source_identifier, external_id)
);
CREATE UNIQUE INDEX ux_jobs_job_url_canonical ON jobs(job_url_canonical)
    WHERE job_url_canonical IS NOT NULL AND duplicate_of_job_id IS NULL;
CREATE INDEX ix_jobs_fingerprint ON jobs(employer_normalized, title_normalized, location_normalized);
CREATE INDEX ix_jobs_requisition ON jobs(employer_normalized, requisition_id);
CREATE INDEX ix_jobs_pipeline_state ON jobs(pipeline_state);
CREATE INDEX ix_jobs_discovered_at ON jobs(discovered_at);
CREATE INDEX ix_jobs_listing_status ON jobs(listing_status);

-- Reposts / material changes / closures of a listing.
CREATE TABLE job_changes (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    change_type   TEXT NOT NULL,       -- material_change|repost|closed|reopened|duplicate_detected
    old_hash      TEXT,
    new_hash      TEXT,
    summary       TEXT,
    detected_at   TEXT NOT NULL
);
CREATE INDEX ix_job_changes_job ON job_changes(job_id);

CREATE TABLE job_scores (
    id                        INTEGER PRIMARY KEY,
    job_id                    INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scoring_version           TEXT NOT NULL,
    profile_hash              TEXT NOT NULL,
    config_hash               TEXT NOT NULL,
    content_hash              TEXT NOT NULL,
    overall_score             REAL NOT NULL CHECK (overall_score >= 0 AND overall_score <= 100),
    category                  TEXT NOT NULL,
    shortlisted               INTEGER NOT NULL,
    factors                   TEXT NOT NULL,   -- JSON list[FactorScore]
    matching_qualifications   TEXT NOT NULL DEFAULT '[]',
    missing_qualifications    TEXT NOT NULL DEFAULT '[]',
    potential_disqualifiers   TEXT NOT NULL DEFAULT '[]',
    eligibility_flags         TEXT NOT NULL DEFAULT '[]',
    evidence                  TEXT NOT NULL DEFAULT '[]',
    reasons_to_apply          TEXT NOT NULL DEFAULT '[]',
    reasons_not_to_apply      TEXT NOT NULL DEFAULT '[]',
    recommended_next_action   TEXT NOT NULL DEFAULT '',
    used_llm                  INTEGER NOT NULL DEFAULT 0,
    llm_model                 TEXT,
    is_current                INTEGER NOT NULL DEFAULT 1,
    scored_at                 TEXT NOT NULL,
    UNIQUE (job_id, scoring_version, profile_hash, config_hash, content_hash, used_llm)
);
CREATE INDEX ix_job_scores_current ON job_scores(job_id, is_current);
CREATE INDEX ix_job_scores_score ON job_scores(overall_score);

-- Cached, validated local-LLM analyses (idempotent by input hash).
CREATE TABLE llm_analyses (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    purpose         TEXT NOT NULL,      -- job_analysis|email_classification|resume_rewrite|...
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    output          TEXT NOT NULL,      -- validated JSON
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL,
    UNIQUE (purpose, model, prompt_version, input_hash)
);
CREATE INDEX ix_llm_analyses_job ON llm_analyses(job_id);

-- Résumé and cover-letter versions (doc_type distinguishes them).
CREATE TABLE document_versions (
    id                  INTEGER PRIMARY KEY,
    job_id              INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    doc_type            TEXT NOT NULL CHECK (doc_type IN ('resume', 'cover_letter')),
    version             INTEGER NOT NULL,
    format              TEXT NOT NULL CHECK (format IN ('docx', 'pdf', 'md')),
    file_name           TEXT NOT NULL,
    relative_path       TEXT NOT NULL,      -- relative to documents_dir
    sha256              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'needs_review',
    claims              TEXT NOT NULL DEFAULT '[]',   -- JSON [{text, fact_ids}]
    validation_issues   TEXT NOT NULL DEFAULT '[]',
    generator           TEXT NOT NULL,                -- rules | llm:<model>
    profile_hash        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    reviewed_at         TEXT,
    UNIQUE (job_id, doc_type, version, format),
    UNIQUE (relative_path)
);
CREATE INDEX ix_document_versions_job ON document_versions(job_id, doc_type);

CREATE TABLE applications (
    id                          INTEGER PRIMARY KEY,
    application_uid             TEXT NOT NULL UNIQUE,
    job_id                      INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
    status                      TEXT NOT NULL,
    status_updated_at           TEXT NOT NULL,
    resume_document_id          INTEGER REFERENCES document_versions(id) ON DELETE SET NULL,
    cover_letter_document_id    INTEGER REFERENCES document_versions(id) ON DELETE SET NULL,
    application_url             TEXT,
    prepared_at                 TEXT,
    submitted_at                TEXT,
    confirmation_status         TEXT NOT NULL DEFAULT 'none',  -- none|page_evidence|email_confirmed|user_reported
    confirmation_evidence       TEXT NOT NULL DEFAULT '{}',
    submission_attempts         INTEGER NOT NULL DEFAULT 0,
    last_activity_at            TEXT NOT NULL,
    next_action                 TEXT,
    follow_up_due_at            TEXT,
    notes                       TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);
CREATE INDEX ix_applications_status ON applications(status);

CREATE TABLE application_events (
    id              INTEGER PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,   -- status_change|note|form_prepared|submission_attempt|...
    from_status     TEXT,
    to_status       TEXT,
    actor           TEXT NOT NULL,   -- system|user|browser|email|scheduler
    details         TEXT NOT NULL DEFAULT '{}',
    evidence        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_application_events_app ON application_events(application_id, created_at);

-- Prepared answers to application form questions (shown for approval).
CREATE TABLE application_answers (
    id                    INTEGER PRIMARY KEY,
    application_id        INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    field_key             TEXT NOT NULL,
    field_label           TEXT NOT NULL,
    field_type            TEXT NOT NULL,
    required              INTEGER NOT NULL DEFAULT 0,
    answer                TEXT,
    answer_source         TEXT,       -- profile:<fact/field>|verified_answer:<id>|user|document:<id>
    confidence            TEXT NOT NULL DEFAULT 'unknown',  -- verified|unknown|uncertain
    requires_review       INTEGER NOT NULL DEFAULT 0,
    is_legal_declaration  INTEGER NOT NULL DEFAULT 0,
    review_reason         TEXT,
    updated_at            TEXT NOT NULL,
    UNIQUE (application_id, field_key)
);

CREATE TABLE approval_requests (
    id                INTEGER PRIMARY KEY,
    approval_uid      TEXT NOT NULL UNIQUE,
    request_type      TEXT NOT NULL,
    application_id    INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    followup_id       INTEGER REFERENCES followups(id) ON DELETE CASCADE,
    summary           TEXT NOT NULL,
    payload           TEXT NOT NULL,       -- JSON shown to the human
    payload_hash      TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    requested_at      TEXT NOT NULL,
    expires_at        TEXT,
    decided_at        TEXT,
    decided_by        TEXT,
    decision_note     TEXT,
    consumed_at       TEXT
);
CREATE INDEX ix_approval_requests_status ON approval_requests(status);
-- At most one live (pending/approved) approval per application & type.
CREATE UNIQUE INDEX ux_approval_live_application ON approval_requests(application_id, request_type)
    WHERE status IN ('pending', 'approved') AND application_id IS NOT NULL;
CREATE UNIQUE INDEX ux_approval_live_followup ON approval_requests(followup_id, request_type)
    WHERE status IN ('pending', 'approved') AND followup_id IS NOT NULL;

CREATE TABLE email_messages (
    id                  INTEGER PRIMARY KEY,
    gmail_message_id    TEXT NOT NULL UNIQUE,
    gmail_thread_id     TEXT NOT NULL,
    received_at         TEXT,
    from_address        TEXT,
    from_domain         TEXT,
    subject             TEXT,
    snippet             TEXT,          -- short, optional (gmail.store_snippets)
    category            TEXT NOT NULL DEFAULT 'other',
    category_confidence REAL,
    classifier          TEXT,          -- rules | llm:<model>
    summary             TEXT,
    requires_attention  INTEGER NOT NULL DEFAULT 0,
    suggested_reply     TEXT,          -- draft only; never sent automatically
    injection_suspected INTEGER NOT NULL DEFAULT 0,
    processed_at        TEXT NOT NULL
);
CREATE INDEX ix_email_messages_thread ON email_messages(gmail_thread_id);
CREATE INDEX ix_email_messages_category ON email_messages(category);

CREATE TABLE email_associations (
    id                INTEGER PRIMARY KEY,
    email_id          INTEGER NOT NULL REFERENCES email_messages(id) ON DELETE CASCADE,
    job_id            INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    application_id    INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    method            TEXT NOT NULL,      -- domain|employer_name|thread|user
    confidence        REAL NOT NULL,
    confirmed_by_user INTEGER NOT NULL DEFAULT 0,
    status_applied    TEXT,               -- status change applied because of this email, if any
    created_at        TEXT NOT NULL,
    UNIQUE (email_id, job_id, application_id)
);

CREATE TABLE followups (
    id                  INTEGER PRIMARY KEY,
    application_id      INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    job_id              INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    email_id            INTEGER REFERENCES email_messages(id) ON DELETE SET NULL,
    followup_type       TEXT NOT NULL,
    due_at              TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL UNIQUE,  -- idempotency for repeated reviews
    suggested_action    TEXT NOT NULL,
    draft_text          TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    completed_at        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX ix_followups_status_due ON followups(status, due_at);

CREATE TABLE scheduled_tasks (
    task_name             TEXT PRIMARY KEY,
    schedule              TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    last_run_at           TEXT,
    last_status           TEXT,
    last_success_at       TEXT,
    next_run_at           TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    lock_owner            TEXT,
    lock_acquired_at      TEXT,
    lock_expires_at       TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE agent_runs (
    id              INTEGER PRIMARY KEY,
    run_uid         TEXT NOT NULL UNIQUE,
    task_name       TEXT NOT NULL,
    trigger         TEXT NOT NULL,      -- manual|scheduled|dashboard|retry|startup
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    summary         TEXT NOT NULL DEFAULT '{}',
    error_kind      TEXT,
    error_message   TEXT,
    attempt         INTEGER NOT NULL DEFAULT 1,
    host_pid        INTEGER
);
CREATE INDEX ix_agent_runs_task ON agent_runs(task_name, started_at);
CREATE INDEX ix_agent_runs_status ON agent_runs(status);

CREATE TABLE errors (
    id                     INTEGER PRIMARY KEY,
    occurred_at            TEXT NOT NULL,
    component              TEXT NOT NULL,
    task_name              TEXT,
    run_id                 INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    job_id                 INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    application_id         INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    error_kind             TEXT NOT NULL,
    message                TEXT NOT NULL,     -- redacted
    details                TEXT NOT NULL DEFAULT '{}',
    requires_human_action  INTEGER NOT NULL DEFAULT 0,
    human_action           TEXT,
    retryable              INTEGER NOT NULL DEFAULT 0,
    resolved               INTEGER NOT NULL DEFAULT 0,
    resolved_at            TEXT
);
CREATE INDEX ix_errors_unresolved ON errors(resolved, occurred_at);

CREATE TABLE audit_events (
    id            INTEGER PRIMARY KEY,
    occurred_at   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity_type   TEXT,
    entity_id     TEXT,
    outcome       TEXT NOT NULL,
    details       TEXT NOT NULL DEFAULT '{}',  -- redacted
    run_id        INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL
);
CREATE INDEX ix_audit_events_time ON audit_events(occurred_at);
CREATE INDEX ix_audit_events_entity ON audit_events(entity_type, entity_id);

CREATE TABLE reports (
    id                INTEGER PRIMARY KEY,
    report_date       TEXT NOT NULL UNIQUE,   -- local date YYYY-MM-DD
    xlsx_file         TEXT,
    markdown_file     TEXT,
    stats             TEXT NOT NULL DEFAULT '{}',
    generation_count  INTEGER NOT NULL DEFAULT 1,
    first_generated_at TEXT NOT NULL,
    last_generated_at  TEXT NOT NULL
);
