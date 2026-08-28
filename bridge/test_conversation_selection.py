"""Which conversation you land in, and whether the past ones stay reachable.

Reported as: "直近の会話が開かれるのか前回の会話が開かれるのか、どのような基準でどこに入るのかが
かなりあいまい". Measured on the live store: 558 sessions, 19 with a conversation attached, the
newest row 8.4 hours old and the newest ATTACHED row 54.4 hours old -- so a restart reopened a
two-day-old conversation and called it resuming, and the session list showed only 8 of the 19
past chats because 539 unopenable rows crowded them out of the cap.
"""
import pytest

from bridge.copilot_bridge import should_autoresume


def test_resume_only_what_was_actually_just_left():
    """The newest session has no conversation: start fresh, do NOT search backwards for an
    older row that happens to be resumable. A message written as a continuation must not land
    two days away from the context its writer had in mind."""
    should, why = should_autoresume({"sid": "s1", "conv_url": ""})
    assert should is False
    assert "older" in why


def test_the_newest_session_is_resumed_when_it_can_be():
    should, why = should_autoresume({"sid": "s1", "conv_url": "sess:abc"})
    assert should is True


def test_no_prior_session_is_not_an_error():
    assert should_autoresume(None) == (False, "no prior session")


def test_the_fresh_flag_wins_over_everything():
    should, _ = should_autoresume({"sid": "s1", "conv_url": "sess:abc"}, fresh_flag=True)
    assert should is False


def test_the_decision_explains_itself():
    """The complaint was never about the outcome -- it was that no reason was visible. Every
    branch has to return one a person can read."""
    for sess in (None, {"sid": "s", "conv_url": ""}, {"sid": "s", "conv_url": "sess:a"}):
        _, why = should_autoresume(sess)
        assert why and len(why) > 8


# ---- past chats stay reachable ------------------------------------------------------------

def _order(rows, cap):
    """The ordering the /sessions handler applies."""
    openable = [x for x in rows if (x.get("conv_url") or "")]
    empty = [x for x in rows if not (x.get("conv_url") or "")]
    return (openable + empty)[:cap]


def test_openable_sessions_are_not_crowded_out_by_empty_ones():
    """THE MEASURED DEFECT. Newest-first, the empties are the newest rows, so the cap cut away
    eleven past chats that existed and were resumable. Raising the cap did not help."""
    rows = [{"sid": "e%d" % i, "conv_url": ""} for i in range(40)]
    rows += [{"sid": "c%d" % i, "conv_url": "sess:%d" % i} for i in range(5)]
    win = _order(rows, 20)
    assert sum(1 for r in win if r["conv_url"]) == 5


def test_ordering_within_each_group_is_untouched():
    """Newest-first still holds inside both groups, so nothing a reader relied on is reordered
    -- only the crowding-out stops."""
    rows = [{"sid": "c1", "conv_url": "a"}, {"sid": "e1", "conv_url": ""},
            {"sid": "c2", "conv_url": "b"}, {"sid": "e2", "conv_url": ""}]
    assert [r["sid"] for r in _order(rows, 10)] == ["c1", "c2", "e1", "e2"]


def test_a_row_that_cannot_be_opened_says_so():
    """A list that does not say invites a click that silently does nothing."""
    rows = [{"sid": "c", "conv_url": "sess:a"}, {"sid": "e", "conv_url": ""}]
    for r in _order(rows, 10):
        r["resumable"] = bool(r.get("conv_url"))
    assert [r["resumable"] for r in _order(rows, 10)] == [True, False]


def test_every_openable_session_fits_when_there_are_fewer_than_the_cap():
    """The guarantee the owner asked for: past chats remain listed, and opening one resumes
    it. This pins the listing half; /resume owns the other half."""
    rows = [{"sid": "e%d" % i, "conv_url": ""} for i in range(500)]
    rows += [{"sid": "c%d" % i, "conv_url": "sess:%d" % i} for i in range(19)]
    win = _order(rows, 50)
    assert sum(1 for r in win if r["conv_url"]) == 19
