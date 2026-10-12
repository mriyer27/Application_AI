from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from job_agent.errors import ProfileError
from job_agent.profile import CandidateProfile, load_profile

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "profile_test.yaml"


def _data() -> dict:
    return yaml.safe_load(FIXTURE.read_text())


def test_fixture_profile_loads():
    p = load_profile(FIXTURE)
    assert p.education[0].end == "May 2027"
    assert p.fact_by_id("agency_regression") is not None
    assert p.has_skill("python") and not p.has_skill("Java")
    assert p.placeholders() == []


def test_example_profile_is_all_placeholders():
    p = load_profile(REPO / "profiles" / "candidate_profile.example.yaml")
    assert "personal.full_name" in p.placeholders()


def test_work_authorization_defaults_to_unknown():
    p = load_profile(FIXTURE)
    unknown = p.unknown_work_authorization_fields()
    assert "requires_sponsorship_now" in unknown
    assert "authorized_for_full_time_after_graduation" in unknown
    assert p.work_authorization.requires_sponsorship_future is None


def test_metric_must_appear_in_fact_text():
    data = _data()
    data["experience"][0]["facts"][0]["metrics"] = ["99%"]
    with pytest.raises(Exception, match="99%"):
        CandidateProfile.model_validate(data)


def test_duplicate_fact_ids_rejected():
    data = _data()
    data["experience"][0]["facts"][1]["id"] = data["experience"][0]["facts"][0]["id"]
    with pytest.raises(Exception, match="duplicate"):
        CandidateProfile.model_validate(data)


def test_unknown_fields_rejected():
    data = _data()
    data["personal"]["favorite_color"] = "blue"
    with pytest.raises(Exception):
        CandidateProfile.model_validate(data)


def test_missing_and_invalid_files(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("education: []\n")
    with pytest.raises(ProfileError, match="invalid candidate profile"):
        load_profile(bad)


def test_profile_hash_changes_with_content():
    a = load_profile(FIXTURE)
    data = _data()
    data["skills"].append("Statistics")
    b = CandidateProfile.model_validate(data)
    assert a.profile_hash() != b.profile_hash()
