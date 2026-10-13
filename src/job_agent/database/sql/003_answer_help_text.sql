-- 003_answer_help_text: keep the help text the employer's page shows next to a
-- question, so the approval you review includes the full wording you are answering.
ALTER TABLE application_answers ADD COLUMN field_help_text TEXT;
