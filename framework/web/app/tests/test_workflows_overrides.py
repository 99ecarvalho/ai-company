"""Smoke tests for the workflows.yaml overrides helpers.

No pytest (not installed in the `web` container). Uses stdlib unittest.

Run inside the web container:
  docker compose exec web python -m unittest app.tests.test_workflows_overrides -v
"""
from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.main import (
    _normalize_step_overrides,
    _normalize_workflow_body,
    _validate_step_overrides,
    _validate_workflow_body,
)


class NormalizeStepOverrides(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(_normalize_step_overrides(None), {})

    def test_non_dict_returns_empty(self):
        self.assertEqual(_normalize_step_overrides("foo"), {})
        self.assertEqual(_normalize_step_overrides(123), {})

    def test_full_nested_preserved(self):
        out = _normalize_step_overrides({
            "model": "claude-opus-4-7",
            "effort": "high",
            "memory": {"enabled": True, "auto_inject_limit": 12},
        })
        self.assertEqual(out, {
            "model": "claude-opus-4-7",
            "effort": "high",
            "memory": {"enabled": True, "auto_inject_limit": 12},
        })

    def test_unknown_keys_dropped(self):
        out = _normalize_step_overrides({
            "model": "opus",
            "bogus_field": "x",
            "memory": {"enabled": False, "phantom": 1},
        })
        self.assertEqual(out, {"model": "opus", "memory": {"enabled": False}})

    def test_empty_strings_omitted(self):
        out = _normalize_step_overrides({"model": "  ", "effort": ""})
        self.assertEqual(out, {})

    def test_strings_stripped(self):
        out = _normalize_step_overrides({"model": "  opus  "})
        self.assertEqual(out, {"model": "opus"})

    def test_memory_subset(self):
        out = _normalize_step_overrides({"memory": {"auto_inject_limit": 0}})
        self.assertEqual(out, {"memory": {"auto_inject_limit": 0}})


class ValidateStepOverrides(unittest.TestCase):
    def _expect_400(self, overrides):
        with self.assertRaises(HTTPException) as ctx:
            _validate_step_overrides("triage", overrides)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_dict_ok(self):
        _validate_step_overrides("triage", {})

    def test_full_valid_passes(self):
        _validate_step_overrides("triage", {
            "model": "opus",
            "effort": "high",
            "memory": {"enabled": True, "auto_inject_limit": 5},
        })

    def test_effort_invalid_400(self):
        self._expect_400({"effort": "bogus"})

    def test_model_empty_400(self):
        self._expect_400({"model": ""})

    def test_memory_enabled_not_bool_400(self):
        self._expect_400({"memory": {"enabled": "yes"}})

    def test_auto_inject_limit_negative_400(self):
        self._expect_400({"memory": {"auto_inject_limit": -1}})

    def test_auto_inject_limit_bool_rejected(self):
        # bool is a subclass of int in python — explicit guard.
        self._expect_400({"memory": {"auto_inject_limit": True}})

    def test_overrides_not_dict_400(self):
        self._expect_400("not-a-dict")


class WorkflowBodyEndToEnd(unittest.TestCase):
    """Ensures normalize+validate run together without breaking."""

    def _wf_body(self, step_overrides=None):
        step = {"agent": "po", "next": ["done"]}
        if step_overrides is not None:
            step["overrides"] = step_overrides
        return {"initial_step": "triage", "steps": {"triage": step}}

    def test_no_overrides_passes(self):
        body = _normalize_workflow_body(self._wf_body())
        _validate_workflow_body("default", body)
        self.assertNotIn("overrides", body["steps"]["triage"])

    def test_valid_overrides_passes(self):
        body = _normalize_workflow_body(self._wf_body({
            "model": "sonnet",
            "effort": "low",
        }))
        _validate_workflow_body("default", body)
        self.assertEqual(
            body["steps"]["triage"]["overrides"],
            {"model": "sonnet", "effort": "low"},
        )

    def test_invalid_effort_400_at_validate(self):
        body = _normalize_workflow_body(self._wf_body({"effort": "ultra"}))
        # normalize accepts (only checks shape); validate rejects.
        with self.assertRaises(HTTPException) as ctx:
            _validate_workflow_body("default", body)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("effort", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
