"""Contract between the Native Host and the server validators.

The two programs are deployed separately (a Mac and a Linux server) and cannot
share code, so each carries its own copy of the Cookie and JSON checks.  The
host is the first gate and is deliberately a little stricter than the server
(smaller size limit, no empty ``;`` segments).  What must never happen is the
opposite: the host forwarding something the server then rejects.  This test
pins that invariant on a corpus plus a deterministic fuzz run, so the two
copies cannot drift apart unnoticed.
"""

import io
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native-host"))
sys.path.insert(0, str(ROOT / "server"))

import native_host  # noqa: E402
import rsshub_cookie_sync as server  # noqa: E402

CORPUS = [
    "a=b",
    "a=b; c=d",
    "a=",
    "a=b=c",
    "a=b;",  # trailing separator: the host rejects it, the server tolerates it
    "a=b;; c=d",
    ";a=b",
    "",
    " ",
    "a",
    "=b",
    "a b=c",
    "a=b\n",
    "a=b\r",
    "a=b\x00",
    "a=b\x7f",
    "a=é",
    "a=\ud800",
    "d_c0=\"x|y\"; z_c0=2|1:0|10:1|4:abc",
    "bad name=1",
    "x=" + "A" * 1000,
    "auth_token=abc_DEF-123",
]
NAME_ALPHABET = "abAZ09_-.!#$%&'*+^`|~ \t=;\x00\n\r\x7fé\"(),/:<>?@[\\]{}"


def host_accepts(header):
    try:
        native_host._validate_cookie_header(header)
    except native_host.ProtocolError:
        return False
    return True


def server_accepts(header):
    try:
        server.validate_cookie_header(header)
    except server.InvalidInput:
        return False
    return True


class ProtocolContractTests(unittest.TestCase):
    def assert_host_subset_of_server(self, header):
        if host_accepts(header):
            self.assertTrue(server_accepts(header), f"host accepts but server rejects {header!r}")

    def test_corpus(self):
        for header in CORPUS:
            with self.subTest(header=header):
                self.assert_host_subset_of_server(header)

    def test_non_string_values_are_rejected_by_both(self):
        for value in (None, 1, 1.5, True, [], {}, b"a=b"):
            with self.subTest(value=value):
                self.assertFalse(host_accepts(value))
                self.assertFalse(server_accepts(value))

    def test_fuzzed_headers(self):
        rng = random.Random(20261016)
        for _ in range(20000):
            header = "".join(rng.choice(NAME_ALPHABET) for _ in range(rng.randint(0, 24)))
            self.assert_host_subset_of_server(header)

    def test_host_limit_is_not_larger_than_server_limit(self):
        self.assertLessEqual(native_host.MAX_COOKIE_BYTES, server.MAX_COOKIE_BYTES)
        header = "a=" + "x" * (native_host.MAX_COOKIE_BYTES - 2)
        self.assertTrue(host_accepts(header))
        self.assertTrue(server_accepts(header))

    def test_twitter_token_rule_is_identical(self):
        samples = ["auth_token=abc", "auth_token=", "auth_token=a b", "auth_token=a;b=c", "auth_token=" + "a" * 4085,
                   "auth_token=" + "a" * 4086, "x=y", "auth_token=ab-_Z9"]
        for header in samples:
            with self.subTest(header=header[:30]):
                request = ('{"version":1,"providers":{"twitter":{"cookieHeader":"%s"}}}' % header).encode()
                try:
                    native_host.validate_request(request)
                    host_ok = True
                except native_host.ProtocolError:
                    host_ok = False
                try:
                    server.twitter_token(header, header=True)
                    server_ok = True
                except server.InvalidInput:
                    server_ok = False
                self.assertEqual(host_ok, server_ok)

    def test_json_strictness_matches(self):
        hostile = [
            b'{"version":1,"version":1,"providers":{}}',  # duplicate key
            b'{"version":NaN,"providers":{}}',
            b'{"version":Infinity,"providers":{}}',
            b'\xff\xfe',
            b'[]',
        ]
        for raw in hostile:
            with self.subTest(raw=raw):
                with self.assertRaises(native_host.ProtocolError):
                    native_host.validate_request(raw)
                with self.assertRaises(server.InvalidInput):
                    server.strict_json_from_stdin(io.BytesIO(raw))


if __name__ == "__main__":
    unittest.main()
