#!/usr/bin/env python3
"""Stub verifier for triage_windows_build_failure.

Actual verification is performed externally by VLM evaluators.
"""


def verify_triage_windows_build_failure(traj, env_info, task_info):
    """Return the compatibility result used by externally evaluated tasks."""
    return {
        "passed": True,
        "score": 100,
        "feedback": "Stub verifier -- VLM evaluation is external",
    }
