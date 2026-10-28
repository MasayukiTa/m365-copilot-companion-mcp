"""docs/research holds implementation plans kept in their intended order, so the order of
work is not forgotten once a chat scrolls away. The request that created this folder said,
in plain terms: "this order; keep it under docs/research so it is not forgotten."

This test fails if that saved plan is deleted or renamed out from under the folder's reason
to exist. It does NOT judge whether the plan is still correct or still being followed -- only
that the files the folder is supposed to carry are actually present and non-empty, which is
the one guarantee "keep it here so it is not lost" actually makes.
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_DIR = os.path.join(REPO, "docs", "research")

#: The files docs/research must carry. README explains the folder; the dated plan is the
#: ordered plan the request asked to keep.
REQUIRED = (
    "README.md",
    "2026-09-08-implementation-order.md",
)


def test_research_dir_exists():
    assert os.path.isdir(RESEARCH_DIR), (
        "docs/research is missing; the saved implementation plan has no home")


def test_required_plan_files_exist_and_are_non_empty():
    for name in REQUIRED:
        path = os.path.join(RESEARCH_DIR, name)
        assert os.path.isfile(path), "docs/research/%s is missing" % name
        assert os.path.getsize(path) > 0, "docs/research/%s is empty" % name
