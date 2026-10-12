-- 002_job_url_indexes: speed up cross-source duplicate detection by application URL.
CREATE INDEX IF NOT EXISTS ix_jobs_application_url_canonical ON jobs(application_url_canonical);
