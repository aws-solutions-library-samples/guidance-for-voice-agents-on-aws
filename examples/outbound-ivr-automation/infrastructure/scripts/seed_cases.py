#!/usr/bin/env python3
"""Create the deployed AWS DynamoDB tables (if missing) and load the demo case and office.

    uv run python scripts/seed_cases.py

Always targets real AWS DynamoDB (the outbound-ivr-cases/outbound-ivr-offices tables the deployed
Runtime and dashboard API read from), never a local endpoint.

The records are fictitious: AnyCompany Medical Group, the demo's own prior-auth case
(`outbound_ivr.prompts.CASE`), and the sample cases below.

Synthetic data only. Adapting this to real patient data makes it PHI, and handling PHI is
the deployer's responsibility under the AWS shared responsibility model: a HIPAA-eligible
service configuration, a Business Associate Agreement with AWS, and the deployer's own
compliance review, none of which this repository provides.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from outbound_ivr import prompts
from outbound_ivr.tools.store import DynamoDBCaseStore, default_case, default_office

CASES_TABLE = os.environ.get("OUTBOUND_IVR_CASES_TABLE", "outbound-ivr-cases")
OFFICES_TABLE = os.environ.get("OUTBOUND_IVR_OFFICES_TABLE", "outbound-ivr-offices")
# Deliberately a separate variable from the ambient AWS_REGION: the deployment target
# region is a deploy-time decision (OUTBOUND_IVR_REGION, same contract as the CDK
# app's constants.py), and a stray AWS_REGION in the environment must not silently
# seed a different region's tables than the ones the agent and Lambdas read from. No
# fallback default: a misconfigured run must fail loudly, not silently seed the wrong
# region's tables.
REGION = os.environ.get("OUTBOUND_IVR_REGION")
if not REGION:
    raise RuntimeError("set OUTBOUND_IVR_REGION to the Region the stacks are deployed to")

DEMO_OFFICE_ID = "anycompany-medical-group"
DEMO_CASE_ID = f"demo-{prompts.CASE['pa_reference'].lower()}"

# Short, synthetic front-desk answers for the demo case's three missing items -- the
# same lines dashboard/src/lib/frontDeskScript.ts has always shown for Sam to read
# aloud, kept in sync here so the dashboard's case-driven Script panel (aws mode) says
# the same thing for this case as the fixed local-mode script. These are never passed
# to Outbound IVR's prompt (see outbound_ivr.tools.business.load_case, which copies only
# key/label/ask) -- they exist for the human playing the front desk to read.
DEMO_ANSWERS = {
    "physical_therapy": (
        "Jordan did eight weeks of physical therapy, twice a week, from June "
        "sixteenth to August eleventh."
    ),
    "progress_note": (
        "The last visit was September twenty-second. Low back pain going down the "
        "left leg, with numbness in the left foot."
    ),
    "symptom_duration": "About four months. It started in early June.",
}


def demo_office() -> dict:
    return default_office(
        DEMO_OFFICE_ID,
        name=prompts.CASE["provider_office"],
        number=os.environ.get("FRONT_DESK_NUMBER", "+15555550100"),
        hours="Mon-Fri 8:00-17:00",
    )


def demo_case() -> dict:
    return default_case(
        DEMO_CASE_ID,
        payer=prompts.CASE["payer"],
        patient_name=prompts.CASE["patient_name"],
        patient_dob=prompts.CASE["patient_dob"],
        member_id=prompts.CASE["member_id"],
        pa_reference=prompts.CASE["pa_reference"],
        procedure=prompts.CASE["procedure"],
        ordering_provider=prompts.CASE["ordering_provider"],
        office_id=DEMO_OFFICE_ID,
        submitted_on=prompts.CASE["submitted_on"],
        return_fax=prompts.CASE["return_fax"],
        portal=prompts.CASE["portal"],
        missing_items=[
            {
                "key": item["key"],
                "label": item["label"],
                "question": item["ask"],
                "answer": DEMO_ANSWERS[item["key"]],
            }
            for item in prompts.CASE["missing_items"]
        ],
        status="pending",
    )


def _item(key: str, label: str, question: str, answer: str) -> dict:
    return {"key": key, "label": label, "question": question, "answer": answer}


# Ten more synthetic cases to try different calls with. Every person, member id,
# reference and answer here is made up; all go to the same demo office. Each item's
# "answer" is a short, plausible front-desk line for whoever is playing the office --
# see dashboard/src/lib/caseScript.ts, which reads it for the aws-mode Script panel.
_EXTRA_CASES = [
    (
        "Morgan Reyes",
        "July 2nd, 1985",
        "LHP-5521-0193",
        "PA-2026-90112",
        "knee MRI without contrast, CPT 73721",
        "Dr. Priya Raman",
        [
            _item(
                "injury_date",
                "Date of injury",
                "when the knee injury happened",
                "Morgan twisted the knee playing soccer on September ninth.",
            ),
            _item(
                "conservative_care",
                "Conservative treatment tried",
                "what treatment was tried before imaging, such as physical therapy or a brace, and for how long",
                "A hinged knee brace and four weeks of physical therapy, with no improvement.",
            ),
            _item(
                "exam_findings",
                "Exam findings",
                "the instability or swelling findings from the last exam",
                "Mild swelling and some instability on the last exam, September twenty-fifth.",
            ),
        ],
    ),
    (
        "Outbound IVR Chen",
        "November 19th, 1972",
        "LHP-3308-7741",
        "PA-2026-90147",
        "CT of the chest with contrast, CPT 71260",
        "Dr. Samuel Okafor",
        [
            _item(
                "symptom_onset",
                "Symptom onset",
                "when the cough and shortness of breath started",
                "The cough and shortness of breath started about six weeks ago.",
            ),
            _item(
                "prior_imaging",
                "Prior chest X-ray",
                "the date and result of the most recent chest X-ray",
                "A chest X-ray on September fifteenth showed a small nodule in the right upper lobe.",
            ),
            _item(
                "smoking_history",
                "Smoking history",
                "the patient's smoking history in pack-years",
                "About twenty pack-years; quit smoking five years ago.",
            ),
        ],
    ),
    (
        "Jamie Patel",
        "April 8th, 1990",
        "LHP-6612-2058",
        "PA-2026-90163",
        "sleep study, CPT 95810",
        "Dr. Elena Vasquez",
        [
            _item(
                "epworth_score",
                "Epworth sleepiness score",
                "the patient's most recent Epworth sleepiness score",
                "The most recent Epworth score was sixteen out of twenty-four.",
            ),
            _item(
                "bmi",
                "Body mass index",
                "the patient's body mass index at the last visit",
                "Body mass index was thirty-four at the last visit.",
            ),
            _item(
                "witnessed_apneas",
                "Witnessed apneas",
                "whether a partner has reported pauses in breathing during sleep",
                "Yes, the patient's partner has reported pauses in breathing several nights a week.",
            ),
        ],
    ),
    (
        "Riley Johnson",
        "February 27th, 1964",
        "LHP-1149-8820",
        "PA-2026-90178",
        "total knee replacement, CPT 27447",
        "Dr. Marcus Bell",
        [
            _item(
                "xray_findings",
                "X-ray findings",
                "the date and findings of the most recent knee X-ray",
                "An X-ray on September fourth showed severe bone-on-bone arthritis in the right knee.",
            ),
            _item(
                "injections",
                "Injections tried",
                "which injections were tried, how many, and when",
                "Two cortisone injections, in June and August, with only brief relief.",
            ),
            _item(
                "functional_limits",
                "Functional limits",
                "how the knee limits walking, stairs, or daily activities",
                "Riley can barely walk a block and can't manage stairs without holding the rail.",
            ),
        ],
    ),
    (
        "Taylor Brooks",
        "September 5th, 1981",
        "LHP-7734-1206",
        "PA-2026-90189",
        "lumbar epidural steroid injection, CPT 62323",
        "Dr. Priya Raman",
        [
            _item(
                "pain_duration",
                "Pain duration",
                "how long the leg pain has lasted",
                "The leg pain has lasted about ten weeks.",
            ),
            _item(
                "mri_date",
                "MRI date and result",
                "the date of the lumbar MRI and what it showed",
                "An MRI on September eighteenth showed a herniated disc at L4-L5.",
            ),
            _item(
                "therapy_weeks",
                "Physical therapy weeks",
                "how many weeks of physical therapy the patient completed",
                "Six weeks of physical therapy with little improvement.",
            ),
        ],
    ),
    (
        "Casey Morgan",
        "December 12th, 1958",
        "LHP-2290-5517",
        "PA-2026-90194",
        "cardiac stress echocardiogram, CPT 93351",
        "Dr. Samuel Okafor",
        [
            _item(
                "chest_pain_pattern",
                "Chest pain pattern",
                "when the chest pain happens and how long it lasts",
                "Chest pain comes on with exertion, lasts a few minutes, and eases with rest.",
            ),
            _item(
                "ecg_result",
                "Resting ECG result",
                "the date and result of the most recent resting ECG",
                "A resting ECG on September twentieth was normal.",
            ),
            _item(
                "risk_factors",
                "Cardiac risk factors",
                "the patient's cardiac risk factors, such as blood pressure, cholesterol, or diabetes",
                "High blood pressure and high cholesterol, both treated with medication.",
            ),
        ],
    ),
    (
        "Drew Kim",
        "June 30th, 1995",
        "LHP-8801-3342",
        "PA-2026-90203",
        "shoulder arthroscopy, CPT 29827",
        "Dr. Marcus Bell",
        [
            _item(
                "injury_mechanism",
                "How the injury happened",
                "how the shoulder was injured",
                "Drew fell on an outstretched arm while mountain biking in August.",
            ),
            _item(
                "mri_findings",
                "MRI findings",
                "what the shoulder MRI showed",
                "The shoulder MRI showed a partial rotator cuff tear.",
            ),
            _item(
                "therapy_outcome",
                "Therapy outcome",
                "how the shoulder responded to physical therapy",
                "Six weeks of physical therapy helped a little but pain is still limiting overhead motion.",
            ),
        ],
    ),
    (
        "Quinn Alvarez",
        "March 3rd, 1977",
        "LHP-4456-9071",
        "PA-2026-90217",
        "brain MRI with and without contrast, CPT 70553",
        "Dr. Elena Vasquez",
        [
            _item(
                "headache_history",
                "Headache history",
                "how long the headaches have been happening and how often",
                "Headaches have been happening for about three months, two or three times a week.",
            ),
            _item(
                "neuro_exam",
                "Neurological exam",
                "the findings of the most recent neurological exam",
                "The most recent neurological exam was normal, no focal deficits.",
            ),
            _item(
                "red_flags",
                "Warning signs",
                "whether there were any warning signs such as vision changes or weakness",
                "No vision changes or weakness, but the headaches wake the patient up at night.",
            ),
        ],
    ),
    (
        "Parker Nguyen",
        "August 21st, 1969",
        "LHP-9032-6648",
        "PA-2026-90226",
        "colonoscopy with biopsy, CPT 45380",
        "Dr. Samuel Okafor",
        [
            _item(
                "symptoms",
                "Symptoms",
                "the symptoms that led to the referral",
                "Intermittent rectal bleeding and a change in bowel habits over the last two months.",
            ),
            _item(
                "last_colonoscopy",
                "Last colonoscopy",
                "the date of the patient's last colonoscopy, if any",
                "The patient has never had a colonoscopy before.",
            ),
            _item(
                "family_history",
                "Family history",
                "any family history of colon cancer",
                "A sibling was diagnosed with colon cancer at age fifty-five.",
            ),
        ],
    ),
    (
        "Sydney Foster",
        "January 16th, 1988",
        "LHP-5567-4410",
        "PA-2026-90231",
        "outpatient physical therapy, 12 visits, CPT 97110",
        "Dr. Priya Raman",
        [
            _item(
                "diagnosis",
                "Diagnosis",
                "the diagnosis the therapy is for",
                "Chronic right shoulder impingement.",
            ),
            _item(
                "functional_goals",
                "Functional goals",
                "the functional goals of the therapy plan",
                "Restore overhead reach and return to lifting at work without pain.",
            ),
            _item(
                "prior_visits",
                "Visits already used",
                "how many therapy visits the patient has already had this year",
                "Four visits so far this year, under a different authorization.",
            ),
        ],
    ),
]


def extra_cases() -> list[dict]:
    return [
        default_case(
            f"case-{pa_reference.lower()}",
            payer=prompts.CASE["payer"],
            patient_name=name,
            patient_dob=dob,
            member_id=member_id,
            pa_reference=pa_reference,
            procedure=procedure,
            ordering_provider=provider,
            office_id=DEMO_OFFICE_ID,
            submitted_on="October 6th",
            return_fax=prompts.CASE["return_fax"],
            portal=prompts.CASE["portal"],
            missing_items=items,
            status="pending",
        )
        for name, dob, member_id, pa_reference, procedure, provider, items in _EXTRA_CASES
    ]


def seed(
    cases_table: str = CASES_TABLE,
    offices_table: str = OFFICES_TABLE,
    extra: bool = False,
) -> DynamoDBCaseStore:
    created = DynamoDBCaseStore.ensure_tables(cases_table, offices_table, region_name=REGION)
    store = DynamoDBCaseStore(
        cases_table=cases_table,
        offices_table=offices_table,
        region_name=REGION,
    )
    store.put_office(demo_office())
    store.put_case(demo_case())
    if extra:
        for case in extra_cases():
            store.put_case(case)
    for name, was_created in created.items():
        print(f"table {name}: {'created' if was_created else 'already existed'}")
    print(f"seeded office {DEMO_OFFICE_ID!r} into {offices_table}")
    print(f"seeded case {DEMO_CASE_ID!r} into {cases_table}")
    return store


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Always targets real AWS DynamoDB (`--table-prefix outbound_ivr`'s
    tables by default). Nothing above `main()` depends on argparse at all, so
    importing this module (as tests/test_seed_cases.py does) never touches it.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table-prefix",
        default=None,
        help=(
            "Derive both table names as <prefix>-cases/<prefix>-offices "
            "(e.g. outbound_ivr for the deployed tables); overrides "
            "--cases-table/--offices-table and OUTBOUND_IVR_CASES_TABLE/"
            "OUTBOUND_IVR_OFFICES_TABLE."
        ),
    )
    parser.add_argument("--cases-table", default=CASES_TABLE)
    parser.add_argument("--offices-table", default=OFFICES_TABLE)
    parser.add_argument(
        "--extra", action="store_true", help="Also load ten more synthetic cases to try."
    )
    args = parser.parse_args(argv)

    cases_table = args.cases_table
    offices_table = args.offices_table
    if args.table_prefix:
        cases_table = f"{args.table_prefix}-cases"
        offices_table = f"{args.table_prefix}-offices"

    seed(cases_table=cases_table, offices_table=offices_table, extra=args.extra)


if __name__ == "__main__":
    main()
