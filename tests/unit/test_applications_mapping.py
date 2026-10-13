"""Field classification and auto-fill rules (pure logic, no browser)."""

from __future__ import annotations

import pytest

from job_agent.applications.documents import DocumentRef
from job_agent.applications.field_mapping import (
    FieldCategory as C,
    MappingInputs,
    SuggestedAnswer,
    apply_drafts,
    classify_field,
    map_field,
    map_fields,
)
from job_agent.browser.forms import FieldOption, FormField
from test_applications_support import opt, update_profile


def _doc(id_: int, fmt: str, doc_type: str = "resume") -> DocumentRef:
    return DocumentRef(id=id_, job_id=1, doc_type=doc_type, version=1, format=fmt, file_name=f"resume.{fmt}",
                       relative_path=f"x/resume_{id_}.{fmt}", sha256="0" * 64, status="approved")


def _inputs(profile, **kw) -> MappingInputs:
    return MappingInputs(profile=profile, resume_variants=kw.pop("resume", [_doc(1, "pdf"), _doc(2, "docx")]),
                         cover_letter_variants=kw.pop("cover", []), **kw)


@pytest.mark.parametrize("label,key,ftype,expected", [
    ("First name", "first_name", "text", C.FIRST_NAME),
    ("Last Name *", "job_application[last_name]", "text", C.LAST_NAME),
    ("Full name", "name", "text", C.FULL_NAME),
    ("Email", "email", "email", C.EMAIL),
    ("Phone", "phone", "tel", C.PHONE),
    ("Location (City)", "job_application[location]", "text", C.LOCATION),
    ("City", "city", "text", C.CITY),
    ("LinkedIn Profile", "q0", "text", C.LINKEDIN),
    ("URL", "urls[GitHub]", "text", C.GITHUB),
    ("Portfolio / website", "website", "text", C.WEBSITE),
    ("Resume/CV", "resume", "file", C.RESUME),
    ("Cover Letter", "cover", "file", C.COVER_LETTER_FILE),
    ("Other attachment", "attach", "file", C.OTHER_UPLOAD),
    ("School", "school", "text", C.SCHOOL),
    ("Degree", "degree", "text", C.DEGREE),
    ("Discipline", "d", "text", C.DISCIPLINE),
    ("Expected graduation date", "graduation", "text", C.GRADUATION),
    ("GPA", "gpa", "text", C.GPA),
    ("Are you legally authorized to work in the United States?", "q", "radio", C.LEGAL),
    ("Will you now or in the future require sponsorship for employment visa status?", "q", "select", C.LEGAL),
    ("Country of citizenship", "c", "select", C.LEGAL),
    ("Do you have an active security clearance?", "c", "radio", C.LEGAL),
    ("Are you a U.S. person under export control regulations (ITAR)?", "c", "radio", C.LEGAL),
    ("Have you ever been convicted of a felony?", "c", "radio", C.LEGAL),
    ("Do you consent to a background check?", "c", "radio", C.LEGAL),
    ("Gender", "job_application[gender]", "select", C.EEO),
    ("Race / Ethnicity", "race", "select", C.EEO),
    ("Veteran status", "veteran", "radio", C.EEO),
    ("Disability Status", "disability", "select", C.EEO),
    ("Pronouns", "eeo[pronouns]", "text", C.EEO),
    ("I certify that the information provided is true.", "certify", "checkbox", C.AGREEMENT),
    ("I agree to the privacy policy", "consent", "checkbox", C.AGREEMENT),
    ("I acknowledge the terms of use", "ack", "checkbox", C.AGREEMENT),
    ("Salary expectations", "salary", "text", C.LOGISTICS),
    ("When can you start?", "start", "text", C.LOGISTICS),
    ("Are you willing to relocate?", "reloc", "radio", C.LOGISTICS),
    ("What is your notice period?", "notice", "text", C.LOGISTICS),
    ("How did you hear about us?", "source", "select", C.REFERRAL_SOURCE),
    ("Why do you want to work here?", "why", "textarea", C.OPEN_ENDED),
    ("Additional information", "comments", "textarea", C.OPEN_ENDED),
    ("Current company", "org", "text", C.OTHER),
    ("Emergency contact name", "x", "text", C.OTHER),
    ("Phone type", "phone_type", "select", C.OTHER),
])
def test_classify_field(label, key, ftype, expected):
    assert classify_field(label, key, ftype) is expected


def test_personal_fields_from_profile(profile):
    inp = _inputs(profile)
    first = map_field(FormField("fn", "First name", "text", required=True), inp)
    assert (first.answer, first.source, first.confidence, first.requires_review) == (
        "Test", "profile:personal.full_name", "verified", False)
    assert map_field(FormField("ln", "Last name", "text"), inp).answer == "Candidate"
    assert map_field(FormField("e", "Email", "email"), inp).fill_value == "test.candidate@example.com"
    assert map_field(FormField("p", "Phone", "tel"), inp).fill_value == "+1 315 555 0100"
    assert map_field(FormField("c", "City", "text"), inp).fill_value == "Springfield"
    assert map_field(FormField("l", "Location", "text"), inp).fill_value == "Springfield, NY"
    linkedin = map_field(FormField("li", "LinkedIn", "text"), inp)  # null in profile, optional
    assert linkedin.fill_value is None and not linkedin.requires_review
    required_gh = map_field(FormField("gh", "GitHub", "text", required=True), inp)
    assert required_gh.fill_value is None and required_gh.requires_review


def test_placeholder_profile_values_are_never_filled(ctx):
    update_profile(ctx, lambda d: d["personal"].update(phone="<your phone>", linkedin_url="<your linkedin>"))
    inp = _inputs(ctx.profile)
    phone = map_field(FormField("p", "Phone", "tel", required=True), inp)
    assert phone.fill_value is None and "placeholder" in phone.review_reason
    assert map_field(FormField("li", "LinkedIn", "text"), inp).fill_value is None


def test_name_with_three_parts_is_only_a_suggestion(ctx):
    update_profile(ctx, lambda d: d["personal"].update(full_name="Test Middle Candidate"))
    m = map_field(FormField("fn", "First name", "text"), _inputs(ctx.profile))
    assert m.answer == "Test" and m.confidence == "uncertain" and m.fill_value is None and m.requires_review


def test_legal_never_derived_from_work_authorization_booleans(ctx):
    def mutate(d):
        d["work_authorization"].update(authorized_to_work_in_us_now=True, requires_sponsorship_now=False,
                                       requires_sponsorship_future=False, us_citizen=False)
    update_profile(ctx, mutate)
    inp = _inputs(ctx.profile)
    for fld in (FormField("wa", "Are you legally authorized to work in the United States?", "radio",
                          options=opt("Yes", "No")),
                FormField("sp", "Will you require visa sponsorship?", "select", options=opt("Yes", "No")),
                FormField("cz", "Are you a U.S. citizen?", "text")):
        m = map_field(fld, inp)
        assert m.is_legal and m.requires_review and m.answer is None and m.fill_value is None, fld.key


def test_legal_filled_only_from_matching_verified_answer_with_exact_option(ctx):
    update_profile(ctx, lambda d: d["verified_answers"].append(
        {"id": "sponsor", "question_patterns": ["require visa sponsorship"], "answer": "No",
         "is_legal_declaration": True}))
    inp = _inputs(ctx.profile)
    ok = map_field(FormField("sp", "Will you now or in the future require visa sponsorship?", "select",
                             options=opt("Yes", "No")), inp)
    assert (ok.fill_value, ok.source, ok.is_legal, ok.requires_review) == ("No", "verified_answer:sponsor", True, True)
    not_an_option = map_field(FormField("sp2", "Do you require visa sponsorship?", "select",
                                        options=opt("Yes, now", "Yes, in the future", "Never")), inp)
    assert not_an_option.fill_value is None and not_an_option.is_legal and not_an_option.requires_review
    free_text = map_field(FormField("sp3", "Will you require visa sponsorship? Explain.", "text"), inp)
    assert free_text.fill_value == "No" and free_text.is_legal
    checkbox = map_field(FormField("sp4", "I will not require visa sponsorship", "checkbox"), inp)
    assert checkbox.fill_value is None and checkbox.is_legal


def test_eeo_left_blank_even_with_matching_verified_answer(ctx):
    update_profile(ctx, lambda d: d["verified_answers"].append(
        {"id": "gender", "question_patterns": ["gender"], "answer": "Decline to self-identify"}))
    m = map_field(FormField("g", "Gender", "select", options=opt("Female", "Male", "Decline to self-identify")),
                  _inputs(ctx.profile))
    assert m.category is C.EEO and m.fill_value is None and m.requires_review


def test_agreements_never_auto_checked(ctx):
    update_profile(ctx, lambda d: d["verified_answers"].append(
        {"id": "certify", "question_patterns": ["i certify"], "answer": "yes"}))
    m = map_field(FormField("c", "I certify that everything is true", "checkbox", required=True), _inputs(ctx.profile))
    assert m.category is C.AGREEMENT and m.fill_value is None and m.requires_review


def test_logistics_need_review_unless_non_legal_verified_answer(ctx):
    inp = _inputs(ctx.profile)
    assert map_field(FormField("s", "Salary expectations", "text"), inp).requires_review
    update_profile(ctx, lambda d: d["verified_answers"].append(
        {"id": "start", "question_patterns": ["when can you start"], "answer": "June 2027"}))
    m = map_field(FormField("st", "When can you start?", "text"), _inputs(ctx.profile))
    assert m.fill_value == "June 2027" and not m.requires_review and not m.is_legal


def test_referral_source_from_verified_answer_exact_option(profile):
    m = map_field(FormField("src", "How did you hear about us?", "select",
                            options=(FieldOption("LinkedIn", "li"), FieldOption("Company Careers Page", "cc"))),
                  _inputs(profile))
    assert m.fill_value == "Company Careers Page" and m.source == "verified_answer:hear_about_us"
    missing = map_field(FormField("src", "How did you hear about us?", "select", options=opt("LinkedIn", "Referral")),
                        _inputs(profile))
    assert missing.fill_value is None and missing.requires_review


def test_open_ended_needs_review_and_draft_is_only_a_suggestion(profile):
    mapped = map_fields([FormField("why", "Why do you want to work here?", "textarea"),
                         FormField("x", "Favourite colour", "text")], _inputs(profile))
    assert all(m.requires_review and m.fill_value is None for m in mapped)
    calls = []

    def drafter(question):
        calls.append(question)
        return SuggestedAnswer("I enjoy analytics work.", ("agency_dashboards",))

    drafted = apply_drafts(mapped, drafter, max_drafts=3)
    assert calls == ["Why do you want to work here?"]  # only open-ended questions
    why = drafted[0]
    assert why.answer == "I enjoy analytics work." and why.confidence == "uncertain"
    assert why.fill_value is None and why.requires_review and why.source == "llm_draft"


def test_unrecognized_fields_need_review(profile):
    m = map_field(FormField("org", "Current company", "text"), _inputs(profile))
    assert m.category is C.OTHER and m.requires_review and m.fill_value is None


def test_education_verbatim_from_profile(profile):
    inp = _inputs(profile)
    assert map_field(FormField("s", "School", "text"), inp).fill_value == "Example State University"
    assert map_field(FormField("d", "Degree", "text"), inp).fill_value == "Master of Science"
    assert map_field(FormField("m", "Major", "text"), inp).fill_value == "Information Systems"
    assert map_field(FormField("g", "Expected graduation date", "text"), inp).fill_value == "May 2027"
    assert map_field(FormField("g2", "Graduation date", "month"), inp).fill_value == "2027-05"
    year = map_field(FormField("g3", "Graduation year", "select", options=opt("2026", "2027", "2028")), inp)
    assert year.fill_value == "2027"
    exact_date = map_field(FormField("g4", "Graduation date", "date"), inp)
    assert exact_date.fill_value is None and exact_date.requires_review  # no day in the profile: never invented
    highest = map_field(FormField("h", "Highest degree obtained", "text"), inp)
    assert highest.fill_value is None and highest.requires_review  # the MS is still expected


def test_resume_pdf_preferred_and_accept_attribute_respected(profile):
    inp = _inputs(profile)
    any_type = map_field(FormField("r", "Resume", "file", is_file=True), inp)
    assert any_type.document.format == "pdf" and any_type.source == "document:1"
    docx_only = map_field(FormField("r", "Resume", "file", is_file=True, accept=".doc,.docx"), inp)
    assert docx_only.document.format == "docx"
    images = map_field(FormField("r", "Resume", "file", is_file=True, accept="image/*", required=True), inp)
    assert images.fill_value is None and images.requires_review


def test_cover_letter_file_and_text(profile):
    cover = [_doc(5, "pdf", "cover_letter")]
    inp = _inputs(profile, cover=cover, cover_letter_text="Dear team", cover_letter_text_doc=_doc(6, "md",
                                                                                                  "cover_letter"))
    assert map_field(FormField("cl", "Cover letter", "file", is_file=True), inp).document.id == 5
    text = map_field(FormField("clt", "Cover letter", "textarea"), inp)
    assert text.fill_value == "Dear team" and text.source == "document:6"
    none = map_field(FormField("cl", "Cover letter", "file", is_file=True), _inputs(profile))
    assert none.fill_value is None and not none.requires_review  # optional and absent: left blank


def test_user_answers_preserved_and_legal_stays_flagged(profile):
    inp = _inputs(profile, user_answers={
        "wa": {"answer": "Yes", "answer_source": "user", "is_legal_declaration": 1},
        "salary": {"answer": "", "answer_source": "user", "is_legal_declaration": 0},
    })
    wa = map_field(FormField("wa", "Are you legally authorized to work in the US?", "radio", options=opt("Yes", "No")),
                   inp)
    assert wa.fill_value == "Yes" and wa.source == "user" and wa.is_legal and wa.requires_review
    sal = map_field(FormField("salary", "Salary expectations", "text"), inp)
    assert sal.answer == "" and sal.fill_value is None and not sal.requires_review


def test_hidden_custom_widget_is_not_filled(profile):
    m = map_field(FormField("e", "Email", "email", required=True, visible=False), _inputs(profile))
    assert m.fill_value is None and m.requires_review and "hidden" in m.review_reason


# --------------------------------------------------------------------------- #
# Legal-question detection and verified-answer matching (review findings C4)
# --------------------------------------------------------------------------- #
REVIEW_LEGAL_LABELS = [
    "Will you now or in the future require us to support an H-1B?",
    "Are you legally able to work in the U.S.?",
    "Do you need a visa?",
    "Are you a U.S. person under ITAR?",
    "Are you able to legally work in the United States?",
    "Are you legally allowed to work in the United States?",
    "Are you lawfully permitted to work in the US?",
    "Can you work in the United States without restrictions?",
    "Do you currently have unrestricted work rights in the U.S.?",
    "Will you now or in the future need an employer to file an employment-based petition on your behalf?",
    "Are you currently on OPT?",
    "Are you an F-1 student?",
    "Do you hold an EAD card?",
    "Do you hold an active Secret or TS/SCI?",
    "Are you a U.S. national?",
    "Can you provide proof of identity and employment eligibility (Form I-9)?",
    "Are you a protected individual as defined in 8 U.S.C. 1324b(a)(3)?",
    "Will you require a change of status or transfer to begin employment?",
]


def _with_answers(profile, *answers):
    from job_agent.profile import VerifiedAnswer

    extra = [VerifiedAnswer(**a) for a in answers]
    return profile.model_copy(update={"verified_answers": [*profile.verified_answers, *extra]})


@pytest.mark.parametrize("label", REVIEW_LEGAL_LABELS)
@pytest.mark.parametrize("ftype", ["radio", "text", "textarea"])
def test_every_legal_wording_is_flagged_and_never_filled(profile, label, ftype):
    fld = FormField("q", label, ftype, required=True, options=opt("Yes", "No") if ftype == "radio" else ())
    m = map_field(fld, _inputs(profile))
    assert m.category is C.LEGAL and m.is_legal and m.requires_review and m.fill_value is None
    assert classify_field(label, "q", ftype) is C.LEGAL


def test_non_legal_verified_answer_never_fills_combined_legal_question(profile):
    prof = _with_answers(profile, {"id": "relocate", "question_patterns": ["willing to relocate"], "answer": "Yes"})
    fld = FormField("q", "Are you willing to relocate, and will you require visa sponsorship now or in the future?",
                    "radio", required=True, options=opt("Yes", "No"))
    m = map_field(fld, _inputs(prof))
    assert m.category is C.LEGAL and m.is_legal and m.requires_review and m.fill_value is None
    assert "relocate" in m.review_reason and "not marked is_legal_declaration" in m.review_reason
    # the same answer still fills the plain relocation question
    plain = map_field(FormField("r", "Are you willing to relocate?", "radio", options=opt("Yes", "No")),
                      _inputs(prof))
    assert plain.fill_value == "Yes" and not plain.is_legal


def test_non_legal_verified_answer_never_fills_legal_work_question(profile):
    prof = _with_answers(profile, {"id": "us_based", "question_patterns": ["work in the united states"],
                                   "answer": "Yes"})
    fld = FormField("q", "Are you able to legally work in the United States?", "radio", required=True,
                    options=opt("Yes", "No"))
    m = map_field(fld, _inputs(prof))
    assert m.is_legal and m.requires_review and m.fill_value is None and m.source is None


def test_legal_verified_answer_does_not_answer_neighbouring_questions(profile):
    prof = _with_answers(
        profile,
        {"id": "sponsor", "question_patterns": ["require visa sponsorship"], "answer": "No",
         "is_legal_declaration": True},
        {"id": "auth", "question_patterns": ["authorized to work"], "answer": "Yes", "is_legal_declaration": True},
    )
    inp = _inputs(prof)
    for label in (
        "Are you willing to relocate, and will you require visa sponsorship now or in the future?",
        "Did you ever require visa sponsorship at a previous employer?",
        "If you require visa sponsorship, which visa type?",
        "Will you not require visa sponsorship?",
        "Are you authorized to work in Canada?",
        "Are you authorized to work in the country where this job is located?",
        "Are you authorized to work in the US and will you require sponsorship?",
        "Are you not authorized to work in the United States?",
    ):
        m = map_field(FormField("q", label, "radio", required=True, options=opt("Yes", "No")), inp)
        assert m.is_legal and m.requires_review and m.fill_value is None, label
    # the questions the answers were saved for still work (and stay flagged for your approval)
    for label, expected, source in (
        ("Will you now or in the future require visa sponsorship?", "No", "verified_answer:sponsor"),
        ("Will you now or in the future require visa sponsorship for employment visa status (e.g. H-1B visa "
         "status)?", "No", "verified_answer:sponsor"),
        ("Are you legally authorized to work in the United States?", "Yes", "verified_answer:auth"),
    ):
        m = map_field(FormField("q", label, "radio", required=True, options=opt("Yes", "No")), inp)
        assert (m.fill_value, m.source, m.is_legal, m.requires_review) == (expected, source, True, True), label


def test_legal_wording_in_name_placeholder_or_options_makes_the_field_legal(profile):
    prof = _with_answers(profile, {"id": "anything", "question_patterns": ["please answer"], "answer": "Yes"})
    inp = _inputs(prof)
    by_key = map_field(FormField("visa_sponsorship", "Please answer", "text"), inp)
    by_placeholder = map_field(FormField("q1", "Please answer", "text", placeholder="Do you require sponsorship?"),
                               inp)
    by_options = map_field(FormField("q2", "Please answer", "radio",
                                     options=opt("I am a U.S. citizen", "I will require sponsorship")), inp)
    for m in (by_key, by_placeholder, by_options):
        assert m.category is C.LEGAL and m.is_legal and m.requires_review and m.fill_value is None


def test_llm_drafter_never_called_for_legal_questions(profile):
    fields = [FormField("w", "Can you work in the United States without restrictions?", "textarea", required=True),
              FormField("s", "Describe your eligibility to work here", "textarea"),
              FormField("why", "Why do you want to work here?", "textarea")]
    mapped = map_fields(fields, _inputs(profile))
    # even an (inconsistent) open-ended row with a legal label is never drafted
    from dataclasses import replace
    mapped.append(replace(map_field(FormField("x", "Tell us about yourself", "textarea"), _inputs(profile)),
                          field=FormField("x", "Do you hold a green card? Tell us more.", "textarea")))
    calls = []

    def drafter(question):
        calls.append(question)
        return SuggestedAnswer("Yes, I can work without restrictions.", ("agency_regression",))

    drafted = apply_drafts(mapped, drafter, max_drafts=5)
    assert calls == ["Why do you want to work here?"]
    assert all(m.answer is None for m in drafted[:2]) and drafted[3].answer is None


def test_non_legal_matching_uses_whole_words_and_respects_negation(ctx):
    update_profile(ctx, lambda d: d["verified_answers"].extend([
        {"id": "start", "question_patterns": ["when can you start"], "answer": "June 2027"},
        {"id": "startup", "question_patterns": ["start"], "answer": "Yes"},
    ]))
    inp = _inputs(ctx.profile)
    assert map_field(FormField("su", "Do you have startup experience?", "text"), inp).fill_value is None
    negated = map_field(FormField("n", "If you cannot start in June, when can you start?", "text"), inp)
    assert negated.fill_value is None and negated.requires_review
    hired = map_field(FormField("h", "If hired, when can you start?", "text"), inp)
    assert hired.requires_review  # "start" and "when can you start" both match -> you choose
    update_profile(ctx, lambda d: d["verified_answers"].pop())
    hired = map_field(FormField("h", "If hired, when can you start?", "text"), _inputs(ctx.profile))
    assert hired.fill_value == "June 2027" and hired.source == "verified_answer:start"


# --------------------------------------------------------------------------- #
# Documents must be approved before they are used as answers (review finding SEC)
# --------------------------------------------------------------------------- #
def _unapproved(id_: int, fmt: str, doc_type: str = "resume", status: str = "needs_review") -> DocumentRef:
    return DocumentRef(id=id_, job_id=1, doc_type=doc_type, version=1, format=fmt, file_name=f"{doc_type}.{fmt}",
                       relative_path=f"x/{doc_type}_{id_}.{fmt}", sha256="0" * 64, status=status)


def test_unapproved_cover_letter_text_is_not_used_as_a_verified_answer(profile):
    inp = _inputs(profile, cover=[_unapproved(5, "pdf", "cover_letter")], cover_letter_text="Dear team, I led ...",
                  cover_letter_text_doc=_unapproved(6, "md", "cover_letter"))
    text = map_field(FormField("clt", "Cover letter", "textarea"), inp)
    assert text.fill_value is None and text.requires_review and text.source is None
    assert "approve the cover letter first (job-agent documents approve 6)" in text.review_reason
    no_doc = map_field(FormField("clt", "Cover letter", "textarea"),
                       _inputs(profile, cover_letter_text="Dear team"))
    assert no_doc.fill_value is None and no_doc.requires_review


def test_unapproved_cover_letter_file_is_not_attached(profile):
    inp = _inputs(profile, cover=[_unapproved(5, "pdf", "cover_letter", status="draft")])
    m = map_field(FormField("cl", "Cover letter", "file", is_file=True), inp)
    assert m.fill_value is None and m.document is None and m.requires_review
    assert "approve the cover letter first (job-agent documents approve 5)" in m.review_reason


def test_unapproved_resume_is_attached_but_needs_review(profile):
    inp = _inputs(profile, resume=[_unapproved(1, "pdf"), _unapproved(2, "docx")])
    m = map_field(FormField("r", "Resume", "file", is_file=True, required=True), inp)
    assert m.fill_value == "resume.pdf" and m.source == "document:1" and m.requires_review
    assert "job-agent documents approve 1" in m.review_reason
    approved = map_field(FormField("r", "Resume", "file", is_file=True), _inputs(profile))
    assert approved.fill_value and not approved.requires_review


# --------------------------------------------------------------------------- #
# Through prepare_application with the in-memory fake browser
# --------------------------------------------------------------------------- #
def _prepare(ctx, make_job, fields, *, mutate_docs=None):
    from job_agent.applications import service
    from job_agent.database import repo
    from test_applications_support import fake_for, setup_application

    info = setup_application(ctx, make_job)
    if mutate_docs:
        ctx.db.execute(mutate_docs)
    fake = fake_for(fields=fields)
    prep = service.prepare_application(ctx, info["job_uid"], browser_factory=fake)
    return fake, repo.approval_payload(repo.get_approval(ctx.db, prep.approval_uid))


def test_prepare_never_answers_legal_questions_from_non_legal_answers(ctx, make_job):
    from job_agent.applications.payload import submission_blockers

    update_profile(ctx, lambda d: d["verified_answers"].extend([
        {"id": "relocate", "question_patterns": ["willing to relocate"], "answer": "Yes"},
        {"id": "us_work", "question_patterns": ["work in the united states"], "answer": "Yes"},
    ]))
    fields = [
        FormField("first_name", "First name", "text", required=True, locator="#first_name"),
        FormField("resume", "Resume", "file", required=True, is_file=True, accept=".pdf", locator="#resume"),
        FormField("relo_spons", "Are you willing to relocate, and will you require visa sponsorship now or in the "
                                "future?", "radio", required=True, options=opt("Yes", "No"), locator="#a"),
        FormField("legal_work", "Are you able to legally work in the United States?", "radio", required=True,
                  options=opt("Yes", "No"), locator="#b"),
    ]
    fake, payload = _prepare(ctx, make_job, fields)
    assert "relo_spons" not in fake.filled and "legal_work" not in fake.filled
    legal = {d["field_key"]: d for d in payload["legal_declarations"]}
    assert set(legal) == {"relo_spons", "legal_work"}
    assert all(d["answer"] is None and not d["verified"] for d in legal.values())
    blockers = submission_blockers(payload)
    assert any("relo_spons" in b for b in blockers) and any("legal_work" in b for b in blockers)


def test_prepare_with_unapproved_documents(ctx, make_job):
    fields = [
        FormField("first_name", "First name", "text", required=True, locator="#first_name"),
        FormField("resume", "Resume", "file", required=True, is_file=True, accept=".pdf,.docx", locator="#resume"),
        FormField("cl", "Cover letter", "textarea", locator="#cl"),
        FormField("clf", "Cover letter", "file", is_file=True, accept=".md,.pdf", locator="#clf"),
    ]
    fake, payload = _prepare(ctx, make_job, fields, mutate_docs="UPDATE document_versions SET status = 'needs_review'")
    assert "resume" in fake.uploaded  # preparation is still possible ...
    assert "cl" not in fake.filled and "clf" not in fake.uploaded  # ... but an unapproved cover letter is never used
    review = {d["field_key"]: d["reason"] for d in payload["requires_review"]}
    assert "documents approve" in review["resume"]
    assert review["cl"].startswith("approve the cover letter first (job-agent documents approve ")
    assert review["clf"].startswith("approve the cover letter first (job-agent documents approve ")


# --------------------------------------------------------------------------- #
# Round 2: a non-legal verified answer covers only labels it fully explains
# (structural guard that holds even when legal detection misses a clause)
# --------------------------------------------------------------------------- #
from job_agent.applications.field_mapping import may_draft  # noqa: E402

RELOCATE = {"id": "relocate", "question_patterns": ["willing to relocate"], "answer": "Yes"}

COMBINED_LABELS = [
    "Are you willing to relocate, and will you require sponsership now or in the future?",
    "Are you willing to relocate? / ¿Tiene permiso para trabajar en Estados Unidos?",
    "Are you willing to relocate (Benötigen Sie ein Visum?)",
    "Are you willing to relocate, and are you a US citzen?",
    "Are you willing to relocate? Avez-vous le droit de travailler au Canada?",
    "Are you willing to relocate, and have you ever committed a crime?",
]
#: Clauses no detector recognizes (Swahili, Finnish, made-up paraphrases): only the structural rule catches them.
UNDETECTED_CLAUSE_LABELS = [
    "Are you willing to relocate? / Kuna haja ya kudhaminiwa na mwajiri?",
    "Are you willing to relocate ja tarvitsetko meiltä apua oleskeluun?",
    "Are you willing to relocate and will you require the company to file papers for you?",
    "Are you willing to relocate (e.g. would you need employer help to stay in the country)?",
    "Are you willing to relocate to Austin, and when?",
]


@pytest.mark.parametrize("label", COMBINED_LABELS)
def test_non_legal_answer_never_fills_typo_or_foreign_legal_clause(profile, label):
    m = map_field(FormField("q", label, "radio", required=True, options=opt("Yes", "No")),
                  _inputs(_with_answers(profile, RELOCATE)))
    assert m.category is C.LEGAL and m.is_legal and m.requires_review and m.fill_value is None, label


@pytest.mark.parametrize("label", UNDETECTED_CLAUSE_LABELS)
def test_unrecognized_extra_clause_is_never_filled_even_if_not_detected_as_legal(profile, label):
    m = map_field(FormField("q", label, "radio", required=True, options=opt("Yes", "No")),
                  _inputs(_with_answers(profile, RELOCATE)))
    assert m.fill_value is None and m.requires_review and m.source is None, label
    assert m.is_legal or m.unrecognized_clause, label


@pytest.mark.parametrize("label,ftype,expected", [
    ("Are you willing to relocate?", "radio", "Yes"),
    ("Are you willing to relocate for this role? *", "radio", "Yes"),
    ("Are you willing to relocate (required)", "radio", "Yes"),
    ("Are you willing to relocate (Yes/No)", "radio", "Yes"),
    ("Willing to relocate", "radio", "Yes"),
    ("How did you hear about this job?", "text", "Company careers page"),
    ("How did you hear about us?", "text", "Company careers page"),
    ("If hired, when can you start?", "text", "June 2027"),
    ("When can you start?", "text", "June 2027"),
])
def test_fully_covered_labels_are_still_filled(profile, label, ftype, expected):
    prof = _with_answers(profile, RELOCATE, {"id": "start", "question_patterns": ["when can you start"],
                                             "answer": "June 2027"})
    fld = FormField("q", label, ftype, required=True, options=opt("Yes", "No") if ftype == "radio" else ())
    m = map_field(fld, _inputs(prof))
    assert (m.fill_value, m.requires_review, m.is_legal, m.unrecognized_clause) == (expected, False, False, False), label


def test_placeholder_must_be_covered_too(profile):
    inp = _inputs(_with_answers(profile, RELOCATE))
    neutral = map_field(FormField("q", "Are you willing to relocate?", "radio", options=opt("Yes", "No"),
                                  placeholder="Select one"), inp)
    assert neutral.fill_value == "Yes"
    unknown = map_field(FormField("q", "Are you willing to relocate?", "radio", options=opt("Yes", "No"),
                                  placeholder="Kuna haja ya kudhaminiwa"), inp)
    assert unknown.fill_value is None and unknown.requires_review and unknown.unrecognized_clause
    legal = map_field(FormField("q", "Are you willing to relocate?", "radio", options=opt("Yes", "No"),
                                placeholder="Benötigen Sie ein Visum?"), inp)
    assert legal.fill_value is None and legal.is_legal


@pytest.mark.parametrize("label", [
    "Will you now or in the future require sponsership? Please explain.",
    "¿Tiene permiso para trabajar en Estados Unidos? Explique.",
    "Have you ever committed a crime? Please explain.",
    "Describe your current status in the U.S.",
    "Please describe your age and background.",          # sensitive (identity)
    "Describe your religious background.",               # sensitive (identity)
    "Tell us about a project / Kuna haja ya kudhaminiwa na mwajiri?",  # partial verified-answer match
    "Описание вашего опыта",                               # unreadable script
])
def test_llm_drafter_never_called_for_legal_sensitive_or_unrecognized_questions(profile, label):
    prof = _with_answers(profile, RELOCATE, {"id": "project", "question_patterns": ["tell us about a project"],
                                             "answer": "See my portfolio."})
    m = map_field(FormField("q", label, "textarea", required=True), _inputs(prof))
    assert m.category in {C.OPEN_ENDED, C.LEGAL}, label
    calls = []

    def drafter(question):
        calls.append(question)
        return SuggestedAnswer("No, not now or in the future.", ())

    out = apply_drafts([m], drafter)[0]
    assert calls == [] and not may_draft(m), label
    assert out.answer is None and out.source is None and out.fill_value is None and out.requires_review


def test_yes_no_or_legal_drafts_are_dropped_and_good_drafts_kept(profile):
    mapped = map_fields([FormField("a", "Why do you want to work here?", "textarea"),
                         FormField("b", "What interests you about this role?", "textarea"),
                         FormField("c", "Tell us about a project you are proud of.", "textarea")], _inputs(profile))
    answers = iter(["No, not now or in the future.", "I am a US citzen and need no sponser.",
                    "I enjoy building dashboards for public agencies."])
    out = apply_drafts(mapped, lambda q: SuggestedAnswer(next(answers), ("agency_dashboards",)), max_drafts=3)
    assert out[0].answer is None and out[1].answer is None
    assert out[2].answer == "I enjoy building dashboards for public agencies." and out[2].source == "llm_draft"
    assert all(m.fill_value is None and m.requires_review for m in out)


def test_prepare_never_fills_typo_or_foreign_legal_clause_from_non_legal_answer(ctx, make_job):
    """Reviewer's test_legal_typo_e2e.py, inverted: nothing filled, every field flagged legal and blocking."""
    from job_agent.applications.payload import submission_blockers

    update_profile(ctx, lambda d: d["verified_answers"].append({**RELOCATE, "is_legal_declaration": False}))
    labels = {
        "relo_spons": "Are you willing to relocate, and will you require sponsership now or in the future?",
        "relo_es": "Are you willing to relocate? / ¿Tiene permiso para trabajar en Estados Unidos?",
        "relo_de": "Are you willing to relocate (Benötigen Sie ein Visum?)",
        "relo_sw": "Are you willing to relocate? / Kuna haja ya kudhaminiwa na mwajiri?",
        "relo_ok": "Are you willing to relocate?",
    }
    fields = [FormField("first_name", "First name", "text", required=True, locator="#first_name"),
              FormField("resume", "Resume", "file", required=True, is_file=True, accept=".pdf", locator="#resume")]
    fields += [FormField(k, v, "radio", required=True, options=opt("Yes", "No"), locator=f"input[name={k}]")
               for k, v in labels.items()]
    fake, payload = _prepare(ctx, make_job, fields)
    assert fake.filled.get("relo_ok") == "Yes"  # the plain relocation question is still answered
    assert not {"relo_spons", "relo_es", "relo_de", "relo_sw"} & set(fake.filled)
    legal = {d["field_key"] for d in payload["legal_declarations"]}
    assert {"relo_spons", "relo_es", "relo_de"} <= legal
    blockers = " | ".join(submission_blockers(payload))
    for key in ("relo_spons", "relo_es", "relo_de", "relo_sw"):
        assert key in blockers, key
    rows = {r["field_key"]: dict(r) for r in ctx.db.query_all("SELECT * FROM application_answers")}
    assert all(rows[k]["requires_review"] == 1 and rows[k]["answer"] is None
               for k in ("relo_spons", "relo_es", "relo_de", "relo_sw"))
