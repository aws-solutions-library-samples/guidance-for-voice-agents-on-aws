"""Parsing the navigator's output, with a gate on pressing keys.

Nova Sonic would press a key even when no menu option served the goal, which on a real
call means a misrouted provider. Telling it not to, in the prompt, did not work.

So the prompt now makes it name the option first, and this module enforces that:

  <match>2 - providers and insurance</match><dtmf>2</dtmf>   accepted
  <dtmf>1</dtmf>                                             rejected, nothing named
  <match>2 - prior auth</match> when no such option was said  rejected, invented

A rejected digit is reported as a `wait`, which is what the model should have said.
Tags are matched loosely because Nova Sonic emits "<dtmf> 2 < dtmf>" rather than clean
XML; see the README.
"""

import re
from dataclasses import dataclass, field

# Closing tags vary by model: Nova 2 Sonic writes "< dtmf>", Nova 2.5 Sonic just ">".
TAG = r"<\s*{0}\s*>\s*(.*?)\s*(?:<\s*/?\s*{0}\s*>|>)"
DTMF_RE = re.compile(TAG.format("dtmf"), re.IGNORECASE | re.DOTALL)
IVR_RE = re.compile(TAG.format("ivr"), re.IGNORECASE | re.DOTALL)
MATCH_RE = re.compile(TAG.format("match"), re.IGNORECASE | re.DOTALL)
DIGIT_RE = re.compile(r"[0-9*#]")
WORD_RE = re.compile(r"[a-z]+")

# Words carried by almost every menu, so overlapping on these proves nothing.
FILLER = {
    "press",
    "for",
    "the",
    "to",
    "and",
    "or",
    "a",
    "an",
    "of",
    "please",
    "your",
    "our",
    "if",
    "you",
    "are",
    "is",
    "this",
    "that",
    "all",
    "other",
    "option",
    "options",
    "menu",
    "call",
    "calling",
    "number",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "zero",
    "star",
    "pound",
}


def _content_words(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if w not in FILLER and len(w) > 2}


@dataclass
class Decision:
    """What the model's output amounts to, after the gate."""

    digits: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # human-readable reasons

    @property
    def waiting(self) -> bool:
        return not self.digits and ("wait" in self.statuses or bool(self.rejected))


def parse(text: str, heard: str | None = None) -> Decision:
    """Read digits, statuses and matches out of `text`, dropping ungated digits.

    `heard` is the menu text the model was listening to, when the caller knows it. Given
    it, a <match> quoting words that were never said is treated as invented.
    """
    d = Decision()
    d.statuses = [s.lower() for s in IVR_RE.findall(text)]
    d.matches = [m.strip() for m in MATCH_RE.findall(text)]

    said = _content_words(heard) if heard else None
    # Walk the matches and digits in the order they appear, so each digit is judged
    # against the match that precedes it.
    events = sorted(
        [(m.start(), "match", m.group(1).strip()) for m in MATCH_RE.finditer(text)]
        + [(m.start(), "dtmf", m.group(1).strip()) for m in DTMF_RE.finditer(text)]
    )
    pending: str | None = None
    for _, kind, value in events:
        if kind == "match":
            pending = value
            continue
        digit = DIGIT_RE.search(value)
        if not digit:
            d.rejected.append(f"unreadable digit {value!r}")
            continue
        digit = digit.group(0)
        if pending is None:
            d.rejected.append(f"digit {digit} with no option named")
            continue
        if said is not None:
            quoted = _content_words(pending)
            # A match with no content words of its own cannot be checked, so let it
            # through; the model still had to commit to naming something.
            if quoted and not (quoted & said):
                d.rejected.append(f"digit {digit} for an option never offered: {pending!r}")
                pending = None
                continue
        d.digits.append(digit)
        pending = None

    if d.rejected and "wait" not in d.statuses:
        d.statuses.append("wait")
    return d


def gate_keypress(option: str, heard: str | None) -> tuple[bool, str]:
    """Whether a `press_key(digit, option)` tool call should go through.

    Same content-word overlap check `parse` applies to a `<match>` tag, lifted out so the
    LiveKit worker's `press_key` tool can gate a real tool call against the far end's
    transcript instead of a text tag. Accepted when `heard` is `None` (nothing to check
    against yet) or `option`'s content words overlap words actually heard; also accepted
    when `option` itself has no content words, since there is nothing to check. Returns
    `(True, "")` on acceptance, or `(False, reason)` naming why the press was refused.
    """
    if heard is None:
        return True, ""
    quoted = _content_words(option)
    if not quoted:
        return True, ""
    said = _content_words(heard)
    if not (quoted & said):
        return False, f"option never offered: {option!r}"
    return True, ""


# What a menu says when it hands the call to a person, and what the person says first.
# Not a bare "this is": menus open with it ("This is a demonstration phone system").
HANDOFF_RE = re.compile(
    r"\b(transfer\w*|hold|connect(ing)? you|put(ting)? you through|one moment|"
    r"next available|hello|hi|speaking|how (can|may) i help|"
    r"good (morning|afternoon|evening))\b",
    re.IGNORECASE,
)


def gate_completed(heard: str | None) -> tuple[bool, str]:
    """Whether `menu_status("completed")` should end the menu phase.

    Nova Sonic would report "completed" on a submenu's opening line ("Authorizations and
    referrals.") as if it had arrived, and then sat silent while the menu timed out.
    Completed only counts once the far end has said transfer or hold language, or a
    person has greeted. `heard` is what was heard since the last key press, so an earlier
    menu line can't pass the check. Returns `(True, "")` or `(False, reason)`.
    """
    if heard and HANDOFF_RE.search(heard):
        return True, ""
    return (
        False,
        "nothing heard since the last key press says the call is being transferred or that a person answered",
    )
