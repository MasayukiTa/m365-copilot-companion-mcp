from bench.review_adjudicate import parse_adjudication
from bench.review_state import FindingState, derive_finding_state


def test_parse_adjudication_three_way():
    assert parse_adjudication(
        "ADJUDICATION_VERDICT: CONFIRM\nADJUDICATION_REASON: code proves it"
    ) == ("CONFIRM", "code proves it")
    assert parse_adjudication("ADJUDICATION_VERDICT: DISPROVE") == ("DISPROVE", "")
    assert parse_adjudication("garbage") == ("INCONCLUSIVE", "")


def test_finding_state_transition_table():
    assert derive_finding_state(True, "UPHELD", None, None) == FindingState.CONFIRMED
    assert derive_finding_state(True, "REFUTED", "CONFIRM", None) == FindingState.CONFIRMED
    assert derive_finding_state(True, "REFUTED", "DISPROVE", None) == FindingState.DISPROVED
    assert derive_finding_state(True, "REFUTED", "INCONCLUSIVE", None) == FindingState.CONTESTED
    assert derive_finding_state(True, "UPHELD", None, "reproduced") == FindingState.REPRODUCED
    assert derive_finding_state(True, "UPHELD", None, "not_reproduced") == FindingState.CONFIRMED
    assert derive_finding_state(True, "UPHELD", None, "inconclusive") == FindingState.CONTESTED
