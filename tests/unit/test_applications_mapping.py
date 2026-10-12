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
