"""The shared corpus of the Fixwire server's redaction (a copy of
pkg/redact/testdata/vectors.json in fixwire/fixwire, kept identical): the
server's scrubber and this port must agree on every case."""

import json
import pathlib
import random
import re
import time

import pytest

from fixwire._core.redact import DEFAULT_DETECTORS, DEFAULT_SENSITIVE_KEYS, Redactor

CORPUS = pathlib.Path(__file__).with_name("vectors.json")
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


# Beyond the corpus: what fuzzing against the server's code found.

BEGIN = "-----BEGIN "  # split, so no scanner sees a whole key


@pytest.mark.parametrize(
    "text",
    [
        "a." * 50_000 + "://",
        (BEGIN + "RSA PRIVATE KEY-----\n") * 3_000,
        "x://u:" * 20_000,
        "eyJ-" * 50_000,
        "a@b.co " * 20_000,
        "token" + " " * 100_000,
        "password=" + " " * 100_000 + "x",
        "sessid" * 50_000,
        "?code" * 50_000,
        "secret_key\t" * 30_000,
        "token: abc " * 30_000,
    ],
    # Short names: pytest puts a test's name in the environment, which Windows caps at 32 KB.
    ids=[
        "a URL scheme that never ends",
        "BEGIN lines without an END",
        "credentials that never end",
        "tokens that never end",
        "many findings",
        "a name and spaces",
        "a name, = and spaces",
        "names back to back",
        "codes back to back",
        "names and tabs",
        "short values",
    ],
)
def test_hostile_text_is_masked_in_linear_time(text: str) -> None:
    started = time.perf_counter()
    Redactor().mask(text)
    assert time.perf_counter() - started < 0.5


def test_keys_that_mask_alike_are_numbered_in_linear_time() -> None:
    doc = {"user%d@example.com" % i: i for i in range(5_000)}
    started = time.perf_counter()
    out, count = Redactor().walk(doc)
    assert time.perf_counter() - started < 1.0
    assert count == 5_000 and "[REDACTED:email]" in out and "[REDACTED:email] (5000)" in out


def test_a_string_redaction_fails_on_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    real = Redactor.mask

    def mask(self: Redactor, s: str):
        if "boom" in s:
            raise RuntimeError("redaction failed")
        return real(self, s)

    monkeypatch.setattr(Redactor, "mask", mask)
    out, count = Redactor().walk({"boom key": "kept", "note": "boom ada@example.com", "fine": "ada@example.com"})
    assert out == {"[Filtered]": "kept", "note": "[Filtered]", "fine": "[REDACTED:email]"} and count == 3


def test_tokens_are_found_where_the_servers_expression_finds_them() -> None:
    # The JWT scanner against the server's pattern (same leftmost matches).
    server = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", re.ASCII)
    rng = random.Random(7)
    pieces = ["eyJ", "eyJ", ".eyJ", "abcdefgh", "a", "Z9", "_", "-", ".", " ", "x", "e", "yJ"]
    for _ in range(5_000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 24)))
        assert Redactor(detectors=["jwt"]).mask(text)[0] == server.sub("[REDACTED:jwt]", text), text


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        # The server folds the long s and the Kelvin sign in its case-insensitive detectors
        # (once a plain keyword got the text past their prefilter). Expected values from its code.
        ("to\u212aen=abcdefgh", "to\u212aen=[REDACTED:secret_assignment]"),
        (
            "password: hunter2 pa\u017f\u017fword: hunter2hunter2",
            "password: [REDACTED:secret_assignment] pa\u017f\u017fword: [REDACTED:secret_assignment]",
        ),
        ("basic x ba\u017fic dXNlcjpwYXNzd29yZA==", "basic x ba\u017fic [REDACTED:http_auth]"),
        ("pa\u017f\u017fword: hunter2hunter2", "pa\u017f\u017fword: hunter2hunter2"),
        ("bearer abcdefghij\u212a/x", "bearer [REDACTED:http_auth]"),
        # A key, then the first END line after it; the second BEGIN has none.
        (
            "a "
            + BEGIN
            + "RSA PRIVATE KEY-----\nMII\n-----END RSA PRIVATE KEY----- b "
            + BEGIN
            + "EC PRIVATE KEY-----",
            "a [REDACTED:private_key] b " + BEGIN + "EC PRIVATE KEY-----",
        ),
        (
            "x://u:p:q@h://v:w@z 1a://u:p@h",
            "x://u:[REDACTED:url_credentials]@h://v:[REDACTED:url_credentials]@z 1a://u:p@h",
        ),
    ],
)
def test_matches_the_server_beyond_the_corpus(text: str, masked: str) -> None:
    assert Redactor().mask(text)[0] == masked


def test_keys_lower_case_like_the_server() -> None:
    # Python lowers U+0130 to two code points; the server to "i".
    doc, n = Redactor().walk({"ap\u0130key": "abc", "x": "y"})
    assert doc == {"ap\u0130key": "[Filtered]", "x": "y"} and n == 1
