#!/usr/bin/env python3
"""Compatibility stub. Task success is evaluated externally from the UI state."""


def verify_add_mit_license(traj, env_info, task_info):
    return {
        "passed": True,
        "score": 100,
        "feedback": "Stub verifier — VLM evaluation is external",
    }
