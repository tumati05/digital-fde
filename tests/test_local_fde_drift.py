"""Fail if backend/local_fde constants drift from agent/cells.py N1."""
import ast
import re

from agent import cells
from backend import local_fde


def test_local_fde_areas_critical_questions_rate_match_cells():
    src = cells.N1
    areas = ast.literal_eval(re.search(r"AREAS = (\[[^\]]+\])", src).group(1))
    critical = ast.literal_eval(re.search(r"CRITICAL = (\[[^\]]+\])", src).group(1))
    questions = ast.literal_eval(re.search(r"QUESTIONS = (\{.*?\})\nRATE", src, re.S).group(1))
    rate = ast.literal_eval(re.search(r"RATE = (\{[^}]+\})", src).group(1))
    assert list(local_fde.AREAS) == list(areas)
    assert list(local_fde.CRITICAL) == list(critical)
    assert dict(local_fde.QUESTIONS) == dict(questions)
    assert dict(local_fde.RATE) == dict(rate)
