# -*- coding: utf-8 -*-
"""
tools/security_test/ -- deliberate, hand-authored offensive-security probes
against this harness's own guardrails. NOT agent-forged (that is tools/auto/).

Everything under this package exists to answer one question during a
red-team self-assessment: "if a capability like this is reachable from the
tool catalogue, does the agent reach for it when nudged toward misuse?"

This directory is .gitignore'd (see tools/security_test/ in .gitignore) and
must stay that way -- nothing here should ever be committed.
"""
