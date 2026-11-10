"""The keypress gate: a digit only counts if a real menu option was named first."""

from outbound_ivr.navigate import gate_completed, gate_keypress, parse

MENU = (
    "Thank you for calling AnyCompany Medical Group. For patients, press 1. "
    "For providers, insurance companies, and prior authorizations, press 2. "
    "For billing, press 3."
)
NO_MATCH_MENU = (
    "If this is a medical emergency, hang up and dial 911. "
    "For our pharmacy refill line, press 1. For directions to our clinic, press 2."
)


def test_named_option_is_accepted():
    d = parse("<match>2 - providers and insurance</match><dtmf>2</dtmf>", MENU)
    assert d.digits == ["2"] and not d.rejected


def test_sloppy_tags_still_parse():
    # Nova Sonic emits "< dtmf>" instead of "</dtmf>".
    d = parse("<match> 2 - providers and insurance < match><dtmf> 2 < dtmf>", MENU)
    assert d.digits == ["2"]


def test_digit_without_a_named_option_is_rejected():
    d = parse("<dtmf>1</dtmf>", MENU)
    assert d.digits == [] and d.waiting and "no option named" in d.rejected[0]


def test_invented_option_is_rejected():
    d = parse("<match>2 - prior authorization department</match><dtmf>2</dtmf>", NO_MATCH_MENU)
    assert d.digits == [] and d.waiting
    assert "never offered" in d.rejected[0]


def test_explicit_wait_is_passed_through():
    d = parse("<ivr>wait</ivr>", NO_MATCH_MENU)
    assert d.digits == [] and d.statuses == ["wait"] and not d.rejected


def test_repeated_digit_is_kept():
    # Menu paths like "2 then 2" must not collapse to one press.
    text = (
        "<match>2 - providers</match><dtmf>2</dtmf>"
        "<match>2 - prior authorizations</match><dtmf>2</dtmf>"
    )
    assert parse(text, MENU).digits == ["2", "2"]


def test_without_heard_text_only_the_naming_is_required():
    # The live server may not have the menu transcript; naming is still enforced.
    assert parse("<match>2 - anything</match><dtmf>2</dtmf>").digits == ["2"]
    assert parse("<dtmf>2</dtmf>").digits == []


def test_completed_status_survives():
    d = parse("< ivr> completed <  ivr>", MENU)
    assert d.statuses == ["completed"] and not d.digits


def test_gate_keypress_accepts_a_named_option():
    ok, reason = gate_keypress("2 - providers and insurance", MENU)
    assert ok and reason == ""


def test_gate_keypress_rejects_an_invented_option():
    ok, reason = gate_keypress("2 - prior authorization department", NO_MATCH_MENU)
    assert not ok and "never offered" in reason


def test_gate_keypress_accepts_without_a_transcript():
    # No `heard` text to check against yet; naming still counts as the gate.
    ok, reason = gate_keypress("2 - anything", None)
    assert ok and reason == ""


def test_gate_keypress_accepts_an_option_with_no_content_words():
    # Nothing to check an all-filler option against, so let it through.
    ok, reason = gate_keypress("press two", MENU)
    assert ok and reason == ""


def test_nova_2_5_closing_tags():
    # Nova 2.5 Sonic closes tags with a bare ">" instead of "</dtmf>" or "< dtmf>".
    d = parse(
        "<match> for billing, press two> <dtmf> 2>",
        "For appointments, press 1. For billing, press 2.",
    )
    assert d.digits == ["2"] and not d.rejected


def test_nova_2_5_wait_then_press():
    d = parse(
        "< ivr> wait <  ivr><match> for providers, insurance companies, and prior "
        "authorizations, press two> <dtmf> 2>",
        "For providers, insurance companies, and prior authorizations, press 2.",
    )
    assert d.digits == ["2"] and d.statuses == ["wait"]


def test_completed_on_a_submenu_landmark_is_refused():
    # Heard on a real run: Outbound IVR called "completed" on the submenu's opening line.
    ok, reason = gate_completed(
        "for authorizations and referrals press one for claims status authorizations and referrals"
    )
    assert not ok and "transferred" in reason


def test_completed_after_transfer_language_is_accepted():
    assert gate_completed(
        "Thank you. Please hold while I transfer you to our prior authorization desk."
    )[0]


def test_completed_when_a_person_answers_is_accepted():
    assert gate_completed("AnyCompany Medical, this is Sam, how can I help?")[0]


def test_completed_with_nothing_heard_is_refused():
    assert not gate_completed(None)[0]


def test_the_menu_disclaimer_is_not_a_greeting():
    # Seen on a LiveKit test call: "this is" in the opening disclaimer passed the gate.
    assert not gate_completed(
        "thank you for calling any company medical group this is a demonstration phone system"
    )[0]
