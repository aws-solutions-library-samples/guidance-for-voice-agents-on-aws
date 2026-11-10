def test_a_record_tracks_the_called_cases_checklist_not_the_demo_case():
    """A call for another case once escalated "incomplete at close" with every item
    collected, because completion still checked the demo case's keys."""
    from outbound_ivr import intake

    record = intake.new_record()
    intake.use_case(
        record,
        {
            "pa_reference": "PA-2026-00001",
            "patient_name": "Test Patient",
            "missing_items": [{"key": "diagnosis"}, {"key": "prior_visits"}],
        },
    )
    assert intake.item_keys(record) == ["diagnosis", "prior_visits"]
    assert record["pa_reference"] == "PA-2026-00001" and record["patient"] == "Test Patient"
    intake.record_item(record, "diagnosis", "x", True)
    intake.record_item(record, "prior_visits", "4", True)
    assert intake.complete(record, "done")["type"] == "complete"
