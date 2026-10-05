"""The shared corpus from pkg/redact in fixwire/fixwire: the server's scrubber and this
port must agree on every case."""

import json
import pathlib

import pytest

from fixwire._core.redact import DEFAULT_DETECTORS, DEFAULT_SENSITIVE_KEYS, Redactor


def _corpus() -> pathlib.Path:
    """The corpus, in the repository root's pkg/ above this SDK (wherever it sits)."""
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "pkg" / "redact" / "testdata" / "vectors.json"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("pkg/redact/testdata/vectors.json not found above " + __file__)


CORPUS = _corpus()
VECTORS = json.loads(CORPUS.read_text())


def expand(s: str) -> str:
    for name, parts in VECTORS["fixtures"].items():
        s = s.replace("{{%s}}" % name, "".join(parts))
    return s


def expand_value(v):
    if isinstance(v, str):
        return expand(v)
    if isinstance(v, list):
        return [expand_value(x) for x in v]
    if isinstance(v, dict):
        return {k: expand_value(x) for k, x in v.items()}
    return v


def test_same_detectors_and_keys():
    assert list(DEFAULT_DETECTORS) == VECTORS["detectors"]
    assert list(DEFAULT_SENSITIVE_KEYS) == VECTORS["sensitive_keys"]


@pytest.mark.parametrize("case", VECTORS["strings"], ids=lambda c: c["name"])
def test_strings(case):
    masked, findings = Redactor().mask(expand(case["input"]))
    assert masked == case["masked"]
    assert [f.detector for f in findings] == case["findings"]


@pytest.mark.parametrize("case", VECTORS["documents"], ids=lambda c: c["name"])
def test_documents(case):
    doc = expand_value(case["input"])
    out, count = Redactor().walk(doc)
    assert out == case["masked"]
    assert count == case["count"]
