"""Shared helpers for tests that inspect source text.

WHY THIS MODULE EXISTS
----------------------
Searching source for a construct and finding it in a COMMENT has produced a wrong answer in
this project at least five separate times, three of them in a single evening:

  * tools/fleet-drift-check.sh flagged a commit as security-relevant because a comment
    contained the word "authority"
  * tests/test-artifact-identity.py reported the version-string gate as "still live" by
    matching the comment that explains its removal
  * tests/test-mac-bundle.py reported over-broad entitlements by matching the comment that
    says "deliberately NOT <entitlement>"

The pattern is always the same, and it is worse than a plain false positive: the comment exists
precisely BECAUSE the thing was fixed, so the better the explanation, the louder the false
alarm. A checker that cries wolf gets ignored -- the same failure as a permanently-red CI.

Related: a substring that is split across two source lines reads as ABSENT to a naive grep,
which is the mirror-image error and has happened five times. Where behaviour can be executed,
execute it; where only text is available, at least strip the prose first.
"""
import re


def code_only(text, comment="#"):
    """Drop whole-line comments so a search sees code, not prose.

    Deliberately conservative: only lines whose first non-space character starts a comment are
    removed. Trailing comments are left alone, because stripping them correctly requires
    knowing about string literals, and a half-correct stripper is worse than none.
    """
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith(comment):
            continue
        out.append(line)
    return "\n".join(out)


def code_only_sh(text):
    """Shell/YAML flavour."""
    return code_only(text, "#")


def has_code(text, pattern, comment="#", flags=0):
    """True when `pattern` appears in the code, ignoring whole-line comments."""
    return re.search(pattern, code_only(text, comment), flags) is not None
