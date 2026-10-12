"""A small built-in lexicon of tools, technologies and skill concepts.

Used in two directions:

* keyword extraction: which tools/skills a job listing asks for, and
* validation: a generated sentence may not name a lexicon tool/skill that is not
  in the candidate's verified source fact (or profile skills, where allowed).

Matching is deterministic, word-boundary based, and case-insensitive unless an
entry is marked ``case_sensitive`` (for names that are also English words,
e.g. "Excel", "Spark", "React", or the single letter "R"). Validation passes
``ignore_case=True`` so that a generated "snowflake", "oracle" or "pyspark" is
still recognized as a tool claim (a false match there only rejects a rewording).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class LexEntry:
    name: str
    aliases: tuple[str, ...] = ()
    case_sensitive: bool = False
    category: str = "tool"  # tool | language | platform | concept


def _e(name: str, *aliases: str, cs: bool = False, cat: str = "tool") -> LexEntry:
    return LexEntry(name, (name, *aliases), cs, cat)


LEXICON: tuple[LexEntry, ...] = (
    # Languages
    _e("Python", cat="language"),
    _e("SQL", cat="language"),
    _e("R", cs=True, cat="language"),
    _e("Java", cs=True, cat="language"),
    _e("JavaScript", "Javascript", cat="language"),
    _e("TypeScript", cat="language"),
    _e("C++", cat="language"),
    _e("C#", cat="language"),
    _e("Golang", cat="language"),
    _e("Scala", cs=True, cat="language"),
    _e("Rust", cs=True, cat="language"),
    _e("Ruby", cs=True, cat="language"),
    _e("Swift", cs=True, cat="language"),
    _e("Kotlin", cat="language"),
    _e("PHP", cat="language"),
    _e("MATLAB", "Matlab", cat="language"),
    _e("VBA", cat="language"),
    _e("SAS", cs=True, cat="language"),
    _e("SPSS"),
    _e("Stata", cs=True),
    _e("HTML", cat="language"),
    _e("CSS", cat="language"),
    _e("Bash", "shell scripting", cat="language"),
    # Data / BI tools
    _e("Excel", "Microsoft Excel", "MS Excel", cs=True),
    _e("Power BI", "PowerBI", "Microsoft Power BI"),
    _e("Tableau"),
    _e("Looker"),
    _e("Qlik", "QlikView", "Qlik Sense"),
    _e("Alteryx"),
    _e("SAS Enterprise Guide"),
    _e("IBM Cognos", "Cognos"),
    _e("Google Analytics"),
    _e("Power Query"),
    _e("Power Automate"),
    _e("Microsoft Access", "MS Access"),
    _e("SSIS"),
    _e("SSRS"),
    _e("dbt", cs=True),
    _e("Jupyter", "Jupyter Notebook"),
    _e("pandas", cs=True),
    _e("NumPy", "numpy"),
    _e("scikit-learn", "sklearn", "scikit learn"),
    _e("TensorFlow"),
    _e("PyTorch"),
    _e("Keras"),
    _e("Hugging Face", "HuggingFace"),
    _e("LangChain"),
    _e("OpenAI API", "OpenAI"),
    _e("spaCy", "spacy"),
    _e("Matplotlib"),
    _e("Seaborn"),
    _e("Plotly"),
    _e("Streamlit"),
    # Databases / data platforms
    _e("MySQL"),
    _e("PostgreSQL", "Postgres"),
    _e("SQL Server", "MS SQL Server", "Microsoft SQL Server"),
    _e("Oracle", cs=True),
    _e("DB2", "IBM DB2"),
    _e("SQLite"),
    _e("MongoDB", "Mongo DB"),
    _e("Cassandra"),
    _e("Redis"),
    _e("Elasticsearch"),
    _e("Snowflake", cs=True),
    _e("BigQuery", "Big Query"),
    _e("Redshift", "Amazon Redshift"),
    _e("Databricks"),
    _e("Hadoop"),
    _e("Hive", "Apache Hive", cs=True),
    _e("Spark", "Apache Spark", "PySpark", cs=True),
    _e("Kafka", "Apache Kafka"),
    _e("Airflow", "Apache Airflow"),
    _e("Informatica"),
    _e("Fivetran"),
    # Cloud / devops
    _e("AWS", "Amazon Web Services", cat="platform"),
    _e("Azure", "Microsoft Azure", cat="platform"),
    _e("GCP", "Google Cloud", "Google Cloud Platform", cat="platform"),
    _e("Kubernetes", "k8s", cat="platform"),
    _e("Docker", cat="platform"),
    _e("Terraform", cat="platform"),
    _e("Jenkins"),
    _e("Git", cs=True),
    _e("GitHub", "Github"),
    _e("GitLab", "Gitlab"),
    _e("Linux", cat="platform"),
    _e("CI/CD", "CI / CD", cat="concept"),
    # Web frameworks
    _e("Flask"),
    _e("Django"),
    _e("FastAPI"),
    _e("React", "React.js", "ReactJS", cs=True),
    _e("Angular", cs=True),
    _e("Vue.js", "Vue", cs=True),
    _e("Node.js", "NodeJS", "Node js"),
    _e(".NET", "dotnet"),
    _e("Spring Boot"),
    _e("REST API", "REST APIs", "RESTful", cat="concept"),
    _e("GraphQL"),
    # Business / product tools
    _e("Jira", "JIRA"),
    _e("Confluence"),
    _e("Salesforce"),
    _e("SAP", cs=True, cat="platform"),
    _e("Workday", cs=True, cat="platform"),
    _e("ServiceNow"),
    _e("HubSpot"),
    _e("Figma"),
    _e("Visio", "Microsoft Visio"),
    _e("Asana", cs=True),
    _e("Trello"),
    _e("Smartsheet"),
    _e("Microsoft Project", "MS Project"),
    _e("SharePoint"),
    _e("Lucidchart"),
    _e("Miro", cs=True),
    _e("Amplitude", cs=True),
    _e("Mixpanel"),
    # Skill concepts
    _e("machine learning", cat="concept"),
    _e("deep learning", cat="concept"),
    _e("natural language processing", "NLP", cat="concept"),
    _e("computer vision", cat="concept"),
    _e("large language models", "large language model", "LLM", "LLMs", cat="concept"),
    _e("generative AI", "GenAI", "gen AI", cat="concept"),
    _e("prompt engineering", cat="concept"),
    _e("A/B testing", "AB testing", "A/B tests", cat="concept"),
    _e("ETL", "ELT", cat="concept"),
    _e("data modeling", "data modelling", cat="concept"),
    _e("data warehousing", "data warehouse", cat="concept"),
    _e("statistical modeling", "statistical modelling", cat="concept"),
    _e("predictive modeling", "predictive modelling", cat="concept"),
    _e("time series", "time-series", cat="concept"),
    _e("forecasting", cat="concept"),
    _e("regression", cat="concept"),
    _e("data visualization", "data visualisation", cat="concept"),
    _e("Agile", "agile methodology", cs=True, cat="concept"),
    _e("Scrum", cat="concept"),
    _e("Kanban", cat="concept"),
    _e("Six Sigma", "Lean Six Sigma", cat="concept"),
    _e("microservices", cat="concept"),
    _e("process mapping", cat="concept"),
    _e("requirements gathering", cat="concept"),
    _e("user stories", cat="concept"),
    _e("UML", cat="concept"),
    _e("BPMN", cat="concept"),
)


def _alias_pattern(alias: str) -> str:
    parts = [re.escape(p) for p in alias.split()]
    body = r"\s+".join(parts)
    return rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_+#&])"


@lru_cache(maxsize=2)
def _compiled(ignore_case: bool = False) -> tuple[tuple[LexEntry, re.Pattern[str]], ...]:
    out = []
    for entry in LEXICON:
        pattern = "|".join(_alias_pattern(a) for a in sorted(entry.aliases, key=len, reverse=True))
        flags = 0 if entry.case_sensitive and not ignore_case else re.IGNORECASE
        out.append((entry, re.compile(pattern, flags)))
    return tuple(out)


def find_skills(text: str, *, ignore_case: bool = False) -> list[str]:
    """Canonical names of lexicon entries mentioned in ``text`` (lexicon order).

    ``ignore_case=True`` also matches case-sensitive entries in any case (used by
    the validators, where over-matching is the safe direction).
    """
    if not text:
        return []
    return [entry.name for entry, rx in _compiled(ignore_case) if rx.search(text)]


def find_skill_spans(text: str, *, ignore_case: bool = False) -> list[tuple[str, str]]:
    """``(canonical name, matched text)`` for every lexicon entry mentioned in ``text``."""
    if not text:
        return []
    out = []
    for entry, rx in _compiled(ignore_case):
        m = rx.search(text)
        if m:
            out.append((entry.name, m.group(0)))
    return out


def lexicon_names() -> list[str]:
    return [e.name for e in LEXICON]
