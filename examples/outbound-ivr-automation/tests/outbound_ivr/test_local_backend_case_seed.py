"""On AWS the local backend seeds its in-memory store from the cases table, so a call
uses the case the dashboard started; without a table it keeps the demo case."""

from __future__ import annotations

from outbound_ivr import prompts
from outbound_ivr.tools.store import InMemoryCaseStore, default_case, default_office
from outbound_ivr.voice import backend


def _remote_with(case_id: str) -> InMemoryCaseStore:
    store = InMemoryCaseStore()
    store.put_office(default_office("office-1", name="Example Clinic"))
    store.put_case(default_case(case_id, patient_name="Morgan Reyes", office_id="office-1"))
    return store


def test_seeds_from_the_cases_table_when_configured(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    monkeypatch.setattr(backend, "case_store_from_env", lambda: _remote_with("case-1"))
    store = backend._seed_case_store("case-1")
    assert store.get_case("case-1")["patient_name"] == "Morgan Reyes"
    assert store.get_office("office-1")["name"] == "Example Clinic"


def test_falls_back_to_the_demo_case_when_the_case_is_missing(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "cases")
    monkeypatch.setattr(backend, "case_store_from_env", lambda: _remote_with("other"))
    store = backend._seed_case_store("case-1")
    assert store.get_case("case-1")["patient_name"] == prompts.CASE["patient_name"]


def test_uses_the_demo_case_without_a_cases_table(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_CASES_TABLE", raising=False)
    store = backend._seed_case_store("case-1")
    assert store.get_case("case-1")["patient_name"] == prompts.CASE["patient_name"]
