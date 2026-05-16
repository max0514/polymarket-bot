"""Tests for probability_estimator.py — response parsing and validation."""

import json

import pytest

from legacy_code.probability_estimator import _parse_response


class TestParseResponse:
    def test_valid_json(self):
        response = json.dumps({
            "estimated_probability": 0.75,
            "confidence": "high",
            "reasoning": "Strong evidence supports this outcome.",
            "key_evidence": ["fact1", "fact2", "fact3"],
            "risks": ["risk1", "risk2"],
            "bayesian_prior": 0.5,
        })
        result = _parse_response(response)
        assert result["estimated_probability"] == 0.75
        assert result["confidence"] == "high"
        assert len(result["key_evidence"]) == 3

    def test_markdown_code_fence(self):
        response = "```json\n" + json.dumps({
            "estimated_probability": 0.6,
            "confidence": "medium",
            "reasoning": "Moderate evidence.",
            "key_evidence": ["a", "b", "c"],
            "risks": ["r1"],
            "bayesian_prior": 0.4,
        }) + "\n```"
        result = _parse_response(response)
        assert result["estimated_probability"] == 0.6

    def test_probability_out_of_range_high(self):
        response = json.dumps({
            "estimated_probability": 1.5,
            "confidence": "high",
            "reasoning": "test",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 0.5,
        })
        with pytest.raises(ValueError, match="not in"):
            _parse_response(response)

    def test_probability_out_of_range_low(self):
        response = json.dumps({
            "estimated_probability": -0.1,
            "confidence": "high",
            "reasoning": "test",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 0.5,
        })
        with pytest.raises(ValueError, match="not in"):
            _parse_response(response)

    def test_invalid_confidence(self):
        response = json.dumps({
            "estimated_probability": 0.5,
            "confidence": "very_high",
            "reasoning": "test",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 0.5,
        })
        with pytest.raises(ValueError, match="Invalid confidence"):
            _parse_response(response)

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_response("not json at all")

    def test_missing_key(self):
        response = json.dumps({
            "estimated_probability": 0.5,
            # missing "confidence"
            "reasoning": "test",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 0.5,
        })
        with pytest.raises(KeyError):
            _parse_response(response)

    def test_boundary_probability_zero(self):
        response = json.dumps({
            "estimated_probability": 0.0,
            "confidence": "low",
            "reasoning": "Extremely unlikely.",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 0.1,
        })
        result = _parse_response(response)
        assert result["estimated_probability"] == 0.0

    def test_boundary_probability_one(self):
        response = json.dumps({
            "estimated_probability": 1.0,
            "confidence": "high",
            "reasoning": "Certain outcome.",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 0.9,
        })
        result = _parse_response(response)
        assert result["estimated_probability"] == 1.0

    def test_bayesian_prior_out_of_range(self):
        response = json.dumps({
            "estimated_probability": 0.5,
            "confidence": "medium",
            "reasoning": "test",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": 1.5,
        })
        with pytest.raises(ValueError, match="not in"):
            _parse_response(response)

    def test_string_probability_coerced(self):
        """Probability as string should be coerced to float."""
        response = json.dumps({
            "estimated_probability": "0.65",
            "confidence": "medium",
            "reasoning": "test",
            "key_evidence": [],
            "risks": [],
            "bayesian_prior": "0.5",
        })
        result = _parse_response(response)
        assert result["estimated_probability"] == 0.65
