"""Shared prompt lines for the Amazon Connect mock-provider-office flow.

These are the lines `connect/make_flow.py` reuses as-is (the Connect office overrides
main-menu/providers-menu/auth-menu with its own wording and adds eligibility-closed;
see `CONNECT_PROMPTS` in `make_flow.py`). Prompt text is plain strings, one line each.
"""

from __future__ import annotations

PROMPTS: dict[str, str] = {
    "welcome": "Thank you for calling AnyCompany Medical Group. This is a demonstration phone system, not a real medical office. Please listen carefully, as our menu options have changed.",
    "invalid": "Sorry, that is not a valid option.",
    "no-input": "We did not receive a selection.",
    "transfer": "Thank you. Please hold while I transfer you to our prior authorization desk.",
    "patients-closed": "Our patient scheduling line is closed for this demonstration. Goodbye.",
    "billing-closed": "Our billing line is closed for this demonstration. Goodbye.",
    "claims-closed": "Our claim status line is closed for this demonstration. Goodbye.",
    "referrals-closed": "Our referral line is closed for this demonstration. Goodbye.",
    "pharmacy-closed": "Our pharmacy authorization line is closed for this demonstration. Goodbye.",
    "frontdesk-busy": "Sorry, all of our staff are helping other callers. Please call again later. Goodbye.",
    "goodbye": "Goodbye.",
}
