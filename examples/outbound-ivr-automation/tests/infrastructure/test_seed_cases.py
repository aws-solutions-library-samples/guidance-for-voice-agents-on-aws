"""scripts/seed_cases.py against moto's mocked DynamoDB."""

import importlib.util
import sys
from pathlib import Path

import pytest
from moto import mock_aws

SCRIPTS = Path(__file__).resolve().parents[2] / "infrastructure" / "scripts"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "outbound_ivr_seed_cases", SCRIPTS / "seed_cases.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_cases(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
    monkeypatch.setenv("OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices")
    monkeypatch.delenv("FRONT_DESK_NUMBER", raising=False)
    with mock_aws():
        module = _load_seed_module()
        yield module


def test_seed_creates_tables_and_loads_demo_data(seed_cases):
    store = seed_cases.seed()

    office = store.get_office(seed_cases.DEMO_OFFICE_ID)
    assert office["name"] == "AnyCompany Medical Group"
    assert office["number"] == "+15555550100"

    case = store.get_case(seed_cases.DEMO_CASE_ID)
    assert case["payer"] == "Example Health Plan"
    assert case["status"] == "pending"
    assert {i["key"] for i in case["missing_items"]} == {
        "physical_therapy",
        "progress_note",
        "symptom_duration",
    }


def test_demo_case_items_carry_the_frontdesk_script_answers(seed_cases):
    """The demo case's answers must match dashboard/src/lib/frontDeskScript.ts word for
    word, so the local demo's fixed script and the aws-mode case-driven script agree."""
    case = seed_cases.demo_case()
    answers = {i["key"]: i["answer"] for i in case["missing_items"]}
    assert answers == {
        "physical_therapy": (
            "Jordan did eight weeks of physical therapy, twice a week, from June "
            "sixteenth to August eleventh."
        ),
        "progress_note": (
            "The last visit was September twenty-second. Low back pain going down "
            "the left leg, with numbness in the left foot."
        ),
        "symptom_duration": "About four months. It started in early June.",
    }


def test_seed_uses_front_desk_number_env(monkeypatch):
    monkeypatch.setenv("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
    monkeypatch.setenv("OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices")
    monkeypatch.setenv("FRONT_DESK_NUMBER", "+15550123456")
    with mock_aws():
        module = _load_seed_module()
        store = module.seed()
        office = store.get_office(module.DEMO_OFFICE_ID)
        assert office["number"] == "+15550123456"


def test_seed_is_safe_to_run_twice(seed_cases):
    seed_cases.seed()
    store = seed_cases.seed()
    case = store.get_case(seed_cases.DEMO_CASE_ID)
    assert case["status"] == "pending"


def test_seed_accepts_explicit_table_names(monkeypatch):
    """seed() can target different table names than the module-level
    defaults, without needing the OUTBOUND_IVR_CASES_TABLE/OUTBOUND_IVR_OFFICES_TABLE env
    vars set to those names -- what main()'s --table-prefix relies on."""
    monkeypatch.delenv("OUTBOUND_IVR_CASES_TABLE", raising=False)
    monkeypatch.delenv("OUTBOUND_IVR_OFFICES_TABLE", raising=False)
    with mock_aws():
        module = _load_seed_module()
        store = module.seed(cases_table="outbound-ivr-cases", offices_table="outbound-ivr-offices")
        office = store.get_office(module.DEMO_OFFICE_ID)
        assert office["name"] == "AnyCompany Medical Group"
        case = store.get_case(module.DEMO_CASE_ID)
        assert case["status"] == "pending"


def test_main_table_prefix_overrides_cases_and_offices_table_names(monkeypatch):
    monkeypatch.delenv("OUTBOUND_IVR_CASES_TABLE", raising=False)
    monkeypatch.delenv("OUTBOUND_IVR_OFFICES_TABLE", raising=False)
    with mock_aws():
        module = _load_seed_module()
        module.main(["--table-prefix", "outbound-ivr"])

        store = module.DynamoDBCaseStore(
            cases_table="outbound-ivr-cases",
            offices_table="outbound-ivr-offices",
            region_name=module.REGION,
        )
        assert store.get_office(module.DEMO_OFFICE_ID)["name"] == "AnyCompany Medical Group"
        assert store.get_case(module.DEMO_CASE_ID)["status"] == "pending"


def test_extra_cases_are_ten_distinct_synthetic_cases_with_checklists(seed_cases):
    cases = seed_cases.extra_cases()
    assert len(cases) == 10
    assert len({c["case_id"] for c in cases}) == 10
    for case in cases:
        assert case["office_id"] == seed_cases.DEMO_OFFICE_ID
        assert case["status"] == "pending"
        assert len(case["missing_items"]) == 3
        assert all({"key", "label", "question", "answer"} <= set(i) for i in case["missing_items"])
        assert all(i["answer"].strip() for i in case["missing_items"])
        assert case["return_fax"] and case["portal"] and case["submitted_on"]
