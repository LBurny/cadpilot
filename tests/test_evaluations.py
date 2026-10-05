"""The mcp-builder Phase 4 evaluations must not rot.

Count-based answers are the ones that rot silently: adding one operation to the
cad() enum or one topic to CAD_OP_DOCS invalidates an answer that no code
touches, and the evaluation quietly starts failing every client that trusts it
(exactly what happened when the `color` op landed: "22" and "31" were both
stale, the topic count having drifted a release earlier). This test derives
those two answers from the live server instead of trusting the file.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from cadpilot.operations import core
from cadpilot.tool_docs import CAD_OP_DOCS

_EVAL_DIR = Path(__file__).resolve().parents[1] / "evaluations"
_XML = _EVAL_DIR / "cadpilot_eval.xml"


def _pairs() -> list[tuple[str, str]]:
    root = ET.parse(_XML).getroot()
    return [
        (pair.findtext("question", ""), pair.findtext("answer", ""))
        for pair in root.findall("qa_pair")
    ]


def test_eval_file_is_well_formed():
    pairs = _pairs()
    assert len(pairs) >= 10, "the Phase 4 process asks for 10 questions"
    for question, answer in pairs:
        assert question.strip(), "every qa_pair needs a question"
        # "Verifiable by string comparison" needs ONE unambiguous answer.
        assert answer.strip() and "\n" not in answer.strip(), (question, answer)


def test_eval_count_answers_match_the_server():
    pairs = _pairs()
    expected = {
        "operation enum": str(len(core.CAD_OPERATIONS)),
        "help topics": str(len(CAD_OP_DOCS)),
    }
    for needle, count in expected.items():
        answers = [a.strip() for q, a in pairs if needle in q]
        assert answers, f"no evaluation question about the {needle!r}"
        assert answers[0] == count, (
            f"the {needle} answer is {answers[0]} but the server now reports {count} — "
            "update evaluations/cadpilot_eval.xml AND evaluations/README.md"
        )


def test_eval_readme_counts_match_the_file():
    """The README states how many pairs there are, and that number rots too."""
    text = (_EVAL_DIR / "README.md").read_text(encoding="utf-8")
    stated = re.search(r"(\d+)\s*`<qa_pair>`\s*entries", text)
    assert stated, "README.md must state the qa_pair count"
    assert int(stated.group(1)) == len(_pairs())

    # Every answer appears in the README's verification table.
    table = {row.strip() for row in text.splitlines() if row.startswith("|")}
    for _, answer in _pairs():
        assert any(f"`{answer}`" in row or f"| {answer} |" in row for row in table), (
            f"answer {answer!r} is missing from the README's answer table"
        )
