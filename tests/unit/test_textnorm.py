from __future__ import annotations

import pytest

from job_agent.security.textnorm import has_non_ascii_numerals, normalize_text, suspicious_characters


@pytest.mark.parametrize("raw, normalized", [
    ("Ｌｅｄ IBM Cognos dashboards", "Led IBM Cognos dashboards"),
    ("Lеd the team", "Led the team"),  # Cyrillic е
    ("Analyzed ⑤⓪⓪ results", "Analyzed 500 results"),
    ("(Ⅻ programs)", "(XII programs)"),
    ("Ｋｕｂｅｒｎｅｔｅｓ", "Kubernetes"),
    ("spon​sorship", "sponsorship"),
    ("José Núñez", "José Núñez"),
])
def test_normalize_text(raw, normalized):
    assert normalize_text(raw) == normalized


@pytest.mark.parametrize("text, suspicious", [
    ("Supported IBM Cognos dashboards — reporting “workflows”.", False),
    ("Café analytics with José", False),
    ("Ｌｅｄ dashboards", True),
    ("Lеd dashboards", True),
    ("Analyzed ⑤ results", True),
    ("x​y", True),
    ("Analyzed данные", True),
])
def test_suspicious_characters(text, suspicious):
    assert bool(suspicious_characters(text)) is suspicious


def test_non_ascii_numerals():
    assert has_non_ascii_numerals("×⑩") and has_non_ascii_numerals("Ⅻ") and not has_non_ascii_numerals("10x")
