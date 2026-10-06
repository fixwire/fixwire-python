"""Client-side redaction: a port of the Fixwire server's scrubber
(pkg/redact in fixwire/fixwire) with identical output, proven by the shared corpus
pkg/redact/testdata/vectors.json.

Detectors run in a fixed order; a cheap prefilter skips each regular
expression on text that cannot match, and validators (Luhn, mod-97,
checksums) reject look-alikes so trace ids, hashes and timestamps survive.
Patterns are ASCII-only, like Go's RE2: ``\\b`` and ``\\d`` never match other
scripts, and whitespace classes are spelled out (RE2's ``\\s`` has no ``\\v``).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

FILTERED = "[Filtered]"

_WS = r"[\t\n\f\r ]"

# Case-insensitive "s" and "k" as the server folds them (the long s and the
# Kelvin sign), and the letters its case-insensitive classes add to [A-Za-z].
_S = "[s\u017f]"
_K = "[k\u212a]"
_FOLDED = "\u017f\u212a"


def _lower(s: str) -> str:
    """Lower case one code point at a time, like the server: only U+0130
    lowers to two code points in Python, and the server makes it "i"."""
    return (s.replace("\u0130", "i") if "\u0130" in s else s).lower()


DEFAULT_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "apikey",
    "accesskey",
    "token",
    "credential",
    "privatekey",
    "authorization",
    "cookie",
    "sessionid",
    "csrf",
    "xsrf",
    "cvv",
    "cvc",
    "ssn",
    "creditcard",
    "cardnumber",
)


@dataclass(frozen=True)
class Finding:
    detector: str
    start: int
    end: int


Span = tuple[int, int]  # (start, end)


@dataclass(frozen=True)
class _Detector:
    name: str
    prefilter: tuple[str, ...] = ()
    case_sensitive: bool = False
    pattern: re.Pattern[str] | None = None
    group: int = 0
    validate: Callable[[str], bool] | None = None
    scan: Callable[[str], list[Span]] | None = None
    may: Callable[[str], bool] | None = None

    def spans(self, s: str) -> list[Span]:
        if self.scan is not None:
            return self.scan(s)
        assert self.pattern is not None
        out: list[Span] = []
        for m in self.pattern.finditer(s):
            if self.group and m.start(self.group) >= 0:
                out.append((m.start(self.group), m.end(self.group)))
            else:
                out.append((m.start(), m.end()))
        return out


def _re(p: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(p, re.ASCII | flags)


_DIGITS = frozenset("0123456789")
_WORD = frozenset("_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _digits(s: str) -> str:
    return "".join(c for c in s if c in _DIGITS)


_CARD_PREFIXES = (
    "4",
    "51",
    "52",
    "53",
    "54",
    "55",
    "2221",
    "2720",
    "34",
    "37",
    "6011",
    "65",
    "35",
    "36",
    "38",
    "300",
    "305",
    "62",
)


def _valid_card(s: str) -> bool:
    d = _digits(s)
    if not 13 <= len(d) <= 19 or not d.startswith(_CARD_PREFIXES):
        return False
    total, double = 0, False
    for c in reversed(d):
        n = ord(c) - 48
        if double:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        double = not double
    return total % 10 == 0


def _valid_iban(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    out: list[str] = []
    for c in s[4:] + s[:4]:
        if c in _DIGITS:
            out.append(c)
        elif c in _UPPER:
            out.append(str(ord(c) - 55))
        else:
            return False
    return int("".join(out)) % 97 == 1


def _valid_ssn(s: str) -> bool:
    area, group, serial = s[0:3], s[4:6], s[7:11]
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def _valid_tckn(s: str) -> bool:
    if len(s) != 11 or s[0] == "0":
        return False
    d = [ord(c) - 48 for c in s]
    odd = d[0] + d[2] + d[4] + d[6] + d[8]
    even = d[1] + d[3] + d[5] + d[7]
    if (odd * 7 - even) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def _valid_phone(s: str) -> bool:
    return 8 <= len(_digits(s)) <= 15


@dataclass
class _Number:
    start: int
    end: int
    digits: int
    sep: str
    groups: list[int]


def _number_spans(s: str) -> list[_Number]:
    """Runs of digits, optionally split by single spaces or dashes, that
    stand alone as words."""
    out: list[_Number] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] not in _DIGITS or (i > 0 and s[i - 1] in _WORD):
            i += 1
            continue
        span = _Number(i, i, 0, "", [])
        group, j = 0, i
        while j < n:
            c = s[j]
            if c in _DIGITS:
                span.digits += 1
                group += 1
                j += 1
                continue
            if c in " -" and j + 1 < n and s[j + 1] in _DIGITS and (span.sep == "" or span.sep == c):
                span.sep = c
                span.groups.append(group)
                group = 0
                j += 1
                continue
            break
        span.groups.append(group)
        span.end = j
        if j == n or s[j] not in _WORD:
            out.append(span)
        i = j + 1
    return out


def _card_spans(s: str) -> list[Span]:
    return [(x.start, x.end) for x in _number_spans(s) if 13 <= x.digits <= 19 and _valid_card(s[x.start : x.end])]


def _ssn_spans(s: str) -> list[Span]:
    return [
        (x.start, x.end)
        for x in _number_spans(s)
        if x.sep == "-" and x.groups == [3, 2, 4] and _valid_ssn(s[x.start : x.end])
    ]


def _tckn_spans(s: str) -> list[Span]:
    return [
        (x.start, x.end) for x in _number_spans(s) if x.sep == "" and x.digits == 11 and _valid_tckn(s[x.start : x.end])
    ]


_LOCAL = _WORD | frozenset(".%+-")
_DOMAIN = (_WORD - {"_"}) | frozenset(".-")
_ALPHA = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _email_spans(s: str) -> list[Span]:
    out: list[Span] = []
    i = s.find("@")
    while i >= 0:
        start, end = i, i + 1
        while start > 0 and s[start - 1] in _LOCAL:
            start -= 1
        while end < len(s) and s[end] in _DOMAIN:
            end += 1
        while end > i + 1 and s[end - 1] in ".-":
            end -= 1
        dom = s[i + 1 : end]
        dot = dom.rfind(".")
        if start < i and dot > 0:
            tld = dom[dot + 1 :]
            ok = 2 <= len(tld) <= 24 and all(c in _ALPHA for c in tld)
            while start < i and s[start] in ".-":
                start += 1
            if ok and start < i:
                out.append((start, end))
        i = s.find("@", i + 1)
    return out


def _may_hold_iban(s: str) -> bool:
    for i in range(len(s) - 3):
        if (
            s[i] in _UPPER
            and s[i + 1] in _UPPER
            and s[i + 2] in _DIGITS
            and s[i + 3] in _DIGITS
            and (i == 0 or s[i - 1] not in _WORD)
        ):
            return True
    return False


def _not_masked(v: str) -> bool:
    return not v.startswith("[REDACTED") and v != FILTERED


def _credential_like(v: str) -> bool:
    """Tells a token from a word after "basic": it has a digit, a base64
    symbol, or capitals past its first letter ("dXNlcjpwYXNz", not
    "Authentication")."""
    if any(c in "0123456789+/=" for c in v):
        return True
    rest = v[1:]
    return any("A" <= c <= "Z" for c in rest) and any("a" <= c <= "z" for c in rest)


# Scanners for four of the server's patterns: the same leftmost matches in
# linear time (as regular expressions here they backtrack quadratically on
# text like "a.a.a….://", many BEGIN lines without an END or "eyJ-eyJ-…",
# and a secret's name backtracks over the spaces after it).

_KEY_LABEL = "PRIVATE KEY-----"


def _key_label_end(s: str, i: int) -> int:
    """The end of ``(?:[A-Z ]+ )?PRIVATE KEY-----`` at i, or -1. "PRIVATE
    KEY" can only end the run of capitals and spaces from i."""
    run = i
    n = len(s)
    while run < n and (s[run] in _UPPER or s[run] == " "):
        run += 1
    label = run - len("PRIVATE KEY")
    if label < i or not s.startswith(_KEY_LABEL, label):
        return -1
    if label > i and (label < i + 2 or s[label - 1] != " "):
        return -1  # the type before the label needs a letter or space, then a space
    return label + len(_KEY_LABEL)


def _private_key_spans(s: str) -> list[Span]:
    """The server's ``-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\\s\\S]*?-----END
    (?:[A-Z ]+ )?PRIVATE KEY-----``: each BEGIN line with the first END line
    after it."""
    out: list[Span] = []
    start = 0
    while True:
        begin = s.find("-----BEGIN ", start)
        if begin < 0:
            return out
        head = _key_label_end(s, begin + 11)
        if head < 0:
            start = begin + 1
            continue
        end = -1
        line = s.find("-----END ", head)
        while line >= 0:
            end = _key_label_end(s, line + 9)
            if end >= 0:
                break
            line = s.find("-----END ", line + 1)
        if end < 0:
            return out  # a later BEGIN line finds no END line either
        out.append((begin, end))
        start = end


_SCHEME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ENDS_PASSWORD = frozenset("\t\n\f\r /?#@")


def _url_credential_spans(s: str) -> list[Span]:
    """The password of the server's ``\\b[A-Za-z][A-Za-z0-9+.\\-]*://[^\\s/?#@:]*:
    ([^\\s/?#@]+)@``. Every start in the scheme before one "://" shares the
    rest of the match, so only the first is tried."""
    out: list[Span] = []
    n = len(s)
    start = 0
    sep = s.find("://")
    while sep >= 0:
        scheme = sep
        while scheme > start and s[scheme - 1] in _SCHEME:
            scheme -= 1
        # The first letter at a word boundary starts the scheme.
        while scheme < sep and not (s[scheme] in _LETTERS and (scheme == 0 or s[scheme - 1] not in _WORD)):
            scheme += 1
        if scheme < sep:
            user = sep + 3
            while user < n and s[user] != ":" and s[user] not in _ENDS_PASSWORD:
                user += 1
            if user < n and s[user] == ":":
                end = user + 1
                while end < n and s[end] not in _ENDS_PASSWORD:
                    end += 1
                if end > user + 1 and end < n and s[end] == "@":
                    out.append((user + 1, end))
                    start = end + 1
                    sep = s.find("://", start)
                    continue
        sep = s.find("://", sep + 1)
    return out


_SECRET_NAME = _re(
    "pa"
    + _S
    + _S
    + "word|pa"
    + _S
    + _S
    + "wd|pwd|"
    + _S
    + "ecret(?:[_-]?"
    + _K
    + "ey)?|private[_-]?"
    + _K
    + "ey|to"
    + _K
    + "en|api[_-]?"
    + _K
    + "ey|acce"
    + _S
    + _S
    + "[_-]?"
    + _K
    + "ey|credential"
    + _S
    + "?|"
    + _S
    + "e"
    + _S
    + _S
    + "(?:ion)?[_-]?id|"
    + _S
    + "ig(?:nature)?|[?&#]code",
    re.IGNORECASE,
)
_QUOTES = frozenset("\"'")
_SPACE = frozenset("\t\n\f\r ")
_ENDS_SECRET = _SPACE | frozenset("\"',;&")


def _secret_assignment_spans(s: str) -> list[Span]:
    """The value of the server's ``(?i)(?:password|passwd|pwd|secret(?:[_-]?key)?|
    private[_-]?key|token|api[_-]?key|access[_-]?key|credentials?|sess(?:ion)?[_-]?id|
    sig(?:nature)?|[?&#]code)["']?\\s*[:=]\\s*["']?([^\\s"',;&]{6,})`` in linear
    time. At most one name can be followed by a separator at each start (a
    longer one goes on with a letter), so the rest of the match is decided
    once; the spaces after one name are read from that start only."""
    out: list[Span] = []
    n = len(s)
    m = _SECRET_NAME.search(s)
    while m is not None:
        j = m.end()
        if j < n and s[j] in _QUOTES:
            j += 1
        while j < n and s[j] in _SPACE:
            j += 1
        if j < n and s[j] in ":=":
            j += 1
            while j < n and s[j] in _SPACE:
                j += 1
            if j < n and s[j] in _QUOTES:
                j += 1
            end = j
            while end < n and s[end] not in _ENDS_SECRET:
                end += 1
            if end - j >= 6:
                out.append((j, end))
                m = _SECRET_NAME.search(s, end)
                continue
        m = _SECRET_NAME.search(s, m.start() + 1)
    return out


_JWT_RUN = _re(r"[A-Za-z0-9_-]*")


def _jwt_run_end(s: str, i: int) -> int:
    m = _JWT_RUN.match(s, i)
    return m.end() if m else i


def _jwt_spans(s: str) -> list[Span]:
    """The server's ``\\beyJ[A-Za-z0-9_-]{8,}\\.eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}``
    in linear time. Each part runs to the end of its run of base64url
    characters, so every start in one run shares the rest of the match:
    when one fails, the run's later starts fail too and are skipped."""
    out: list[Span] = []
    n = len(s)
    i = s.find("eyJ")
    while i >= 0:
        if i > 0 and s[i - 1] in _WORD:
            i = s.find("eyJ", i + 1)
            continue
        head = _jwt_run_end(s, i + 3)
        end = -1
        if head - i >= 11 and head < n and s[head] == "." and s.startswith("eyJ", head + 1):
            payload = _jwt_run_end(s, head + 4)
            if payload - head >= 12 and payload < n and s[payload] == ".":
                signature = _jwt_run_end(s, payload + 1)
                if signature - payload >= 9:
                    end = signature
        if end >= 0:
            out.append((i, end))
            i = s.find("eyJ", end)
        else:
            i = s.find("eyJ", head)
    return out


_REGISTRY = (
    _Detector(
        "private_key",
        ("PRIVATE KEY-----",),
        True,
        scan=_private_key_spans,
    ),
    _Detector(
        "aws_access_key", ("AKIA", "ASIA", "ABIA", "ACCA"), True, _re(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")
    ),
    _Detector("gcp_api_key", ("AIza",), True, _re(r"\bAIza[0-9A-Za-z_\-]{35}")),
    _Detector(
        "azure_storage_key",
        ("accountkey=",),
        False,
        _re("Account" + _K + "ey=([A-Za-z0-9+/" + _FOLDED + "]{86}==)", re.IGNORECASE),
        group=1,
    ),
    _Detector(
        "github_token",
        ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
        True,
        _re(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{60,255})\b"),
    ),
    _Detector(
        "stripe_key",
        ("sk_live_", "sk_test_", "rk_live_", "rk_test_", "whsec_"),
        True,
        _re(r"\b(?:(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,247}|whsec_[A-Za-z0-9+/=]{24,})"),
    ),
    _Detector("slack_token", ("xox",), True, _re(r"\bxox[abposr]-[0-9A-Za-z-]{10,250}\b")),
    _Detector(
        "slack_webhook",
        ("hooks.slack.com/services/",),
        True,
        _re(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    ),
    _Detector("anthropic_key", ("sk-ant-",), True, _re(r"\bsk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{80,}")),
    _Detector(
        "openai_key",
        ("sk-",),
        True,
        _re(r"\bsk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,}|[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20})"),
    ),
    _Detector("jwt", ("eyJ",), True, scan=_jwt_spans),
    _Detector(
        "fixwire_secret_key", ("_sk_live_", "_sk_test_"), True, _re(r"\b[a-z]{2,4}_sk_(?:live|test)_[0-9A-Za-z]{38}\b")
    ),
    # The password in scheme://user:password@host (the user stays).
    _Detector(
        "url_credentials",
        ("://",),
        True,
        scan=_url_credential_spans,
        validate=_not_masked,
    ),
    # Bearer and Basic credentials outside a header (messages, breadcrumbs).
    _Detector(
        "http_auth",
        ("bearer", "basic"),
        False,
        _re(r"\b(?:bearer|ba" + _S + "ic)" + _WS + r"+([A-Za-z0-9._~+/\-" + _FOLDED + "]{12,}=*)", re.IGNORECASE),
        group=1,
        validate=_credential_like,
    ),
    # A value given to a secret's name, in text, config and URLs. The name may
    # end a longer one (access_token, client_secret, csrfToken, PHPSESSID,
    # X-Amz-Signature); an OAuth code counts in a query or fragment only.
    _Detector(
        "secret_assignment",
        ("pass", "pwd", "secret", "key", "token", "credential", "sess", "sig", "code"),
        False,
        scan=_secret_assignment_spans,
        validate=_not_masked,
    ),
    _Detector("email", ("@",), True, scan=_email_spans),
    _Detector("credit_card", scan=_card_spans),
    _Detector(
        "iban",
        may=_may_hold_iban,
        pattern=_re(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
        validate=_valid_iban,
    ),
    _Detector("us_ssn", ("-",), True, scan=_ssn_spans),
    _Detector("tr_tckn", scan=_tckn_spans),
    _Detector("phone", ("+",), True, _re(r"\+\d(?:[ .\-()]?\d){7,14}\b"), validate=_valid_phone),
    _Detector(
        "ipv4",
        (".",),
        True,
        _re(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
    ),
)

DEFAULT_DETECTORS = tuple(d.name for d in _REGISTRY if d.name != "ipv4")


def _overlaps(spans: list[Span], start: int, end: int) -> bool:
    """Whether [start, end) overlaps one of spans (sorted and apart): only
    the last one starting before end can."""
    i = bisect.bisect_left(spans, (end, -1))
    return i > 0 and spans[i - 1][1] > start


def _normalize_key(k: str) -> str:
    return _lower(k).replace("-", "").replace("_", "").replace(" ", "")


def _token_count(k: str) -> bool:
    return k.endswith("tokens") or "tokencount" in k or "usage" in k


def _empty(v: Any) -> bool:
    return v is None or v == ""


class Redactor:
    """Masks secrets and personal data. Safe for concurrent use."""

    def __init__(
        self,
        detectors: Iterable[str] | None = None,
        patterns: dict[str, str] | None = None,
        sensitive_keys: Iterable[str] | None = None,
    ) -> None:
        names = list(DEFAULT_DETECTORS if detectors is None else detectors)
        by_name = {d.name: d for d in _REGISTRY}
        self._detectors: list[_Detector] = []
        for name in names:
            if name not in by_name:
                raise ValueError("redact: unknown detector %r" % name)
            self._detectors.append(by_name[name])
        for name in sorted(patterns or {}):
            self._detectors.append(_Detector(name, pattern=re.compile((patterns or {})[name])))
        self._keys = (
            DEFAULT_SENSITIVE_KEYS if sensitive_keys is None else tuple(_normalize_key(k) for k in sensitive_keys)
        )

    def find(self, s: str) -> list[Finding]:
        """Non-overlapping findings, leftmost first; when two overlap, the
        earlier detector wins."""
        out: list[Finding] = []
        # The findings' (start, end) so far, sorted, and the detector's own
        # (leftmost first): neither overlaps itself, so ends are sorted too.
        taken: list[Span] = []
        lower: str | None = None
        for d in self._detectors:
            if d.prefilter:
                hay = s
                if not d.case_sensitive:
                    if lower is None:
                        lower = _lower(s)
                    hay = lower
                if not any(p in hay for p in d.prefilter):
                    continue
            if d.may is not None and not d.may(s):
                continue
            found: list[Span] = []
            for start, end in d.spans(s):
                if d.validate is not None and not d.validate(s[start:end]):
                    continue
                if _overlaps(taken, start, end) or _overlaps(found, start, end):
                    continue
                found.append((start, end))
                out.append(Finding(d.name, start, end))
            if found:
                taken += found
                taken.sort()  # two sorted runs: merged in linear time
        out.sort(key=lambda f: f.start)
        return out

    def mask(self, s: str) -> tuple[str, list[Finding]]:
        """Replaces each finding with [REDACTED:<detector>]."""
        fs = self.find(s)
        if not fs:
            return s, fs
        parts: list[str] = []
        last = 0
        for f in fs:
            parts.append(s[last : f.start])
            parts.append("[REDACTED:%s]" % f.detector)
            last = f.end
        parts.append(s[last:])
        return "".join(parts), fs

    def mask_or_filter(self, s: str) -> tuple[str, int]:
        """mask(), and the number of findings; a string it fails on is sent
        as [Filtered], never unmasked."""
        try:
            masked, fs = self.mask(s)
        except Exception:
            return FILTERED, 1
        return masked, len(fs)

    def sensitive(self, key: str) -> bool:
        k = _normalize_key(key)
        if k == "auth":
            return True
        return any(frag in k and (frag != "token" or not _token_count(k)) for frag in self._keys)

    def walk(self, v: Any) -> tuple[Any, int]:
        """Masks every string in a JSON-like value in place (lists and dicts
        are modified) and filters the values of sensitive keys. Returns the
        new value and the number of values masked."""
        counter = [0]
        return self._walk(v, counter), counter[0]

    def _walk(self, v: Any, n: list[int]) -> Any:
        if isinstance(v, dict):
            obj = cast("dict[object, Any]", v)
            renamed: dict[str, tuple[str, int]] = {}
            for k in list(obj):
                if isinstance(k, str):
                    masked, found = self.mask_or_filter(k)
                    if masked != k:
                        renamed[k] = masked, found
                val: Any = obj[k]
                if isinstance(k, str) and self.sensitive(k) and not _empty(val):
                    # A typed attribute ({"type": …, "value": …}) keeps its shape.
                    if isinstance(val, dict) and cast("dict[str, Any]", val).get("value") is not None:
                        typed = cast("dict[str, Any]", val)
                        if typed["value"] != FILTERED:
                            typed["value"], typed["type"] = FILTERED, "string"
                            n[0] += 1
                        continue
                    if val != FILTERED:
                        obj[k] = FILTERED
                        n[0] += 1
                    continue
                obj[k] = self._walk(val, n)
            # Keys hold data too ({"ada@example.com": 3}). Keys that mask
            # alike are numbered in key order: "[REDACTED:email] (2)", the
            # count going on from the last number given.
            following: dict[str, int] = {}
            for k in sorted(renamed):
                masked, found = renamed[k]
                key, i = masked, max(following.get(masked, 2), 2)
                while key in obj:
                    key, i = f"{masked} ({i})", i + 1
                following[masked] = i
                obj[key] = obj.pop(k)
                n[0] += found
            return obj
        if isinstance(v, list):
            items = cast("list[object]", v)
            # Some maps are sent as [key, value] pairs (headers, tags).
            if len(items) == 2 and isinstance(items[0], str) and self.sensitive(items[0]) and not _empty(items[1]):
                items[1] = FILTERED
                n[0] += 1
                return items
            for i, item in enumerate(items):
                items[i] = self._walk(item, n)
            return items
        if isinstance(v, str):
            masked, found = self.mask_or_filter(v)
            n[0] += found
            return masked
        return v


_default: Redactor | None = None


def default() -> Redactor:
    global _default
    if _default is None:
        _default = Redactor()
    return _default
