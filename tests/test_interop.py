"""Cross-language HMAC interop vector. Node and Python must agree."""
import hashlib, hmac, json, os

VECTOR = json.load(open(os.path.join(os.path.dirname(__file__),
                                     "interop_vector.json"),
                        encoding="utf-8"))


def test_python_matches_the_shared_vector():
    """If this fails, the Python side changed and ISKRA will break."""
    secret = VECTOR["secret"].encode("utf-8")
    ts = VECTOR["timestamp"]
    for name, row in VECTOR["cases"].items():
        body = row["body"].encode("utf-8")
        assert len(body) == row["body_bytes"], name
        assert hashlib.sha256(body).hexdigest() == row["sha256_body"], name
        payload = (ts + ".").encode("utf-8") + body
        assert len(payload) == row["payload_bytes"], name
        assert hashlib.sha256(payload).hexdigest() == \
            row["sha256_payload"], name
        got = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        assert got == row["hmac"], f"{name}: {got} != {row['hmac']}"


def test_the_verifier_signs_raw_bytes_not_a_repr():
    """The classic interop bug: f"{t}.{body_bytes}" yields "1789.b\'{}\'"."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "demo_api.py"), encoding="utf-8").read()
    fn = src[src.index("async def _verify("):]
    fn = fn[:fn.index("\ndef _session")]
    assert '(ts + ".").encode() + raw' in fn
    assert 'f"{ts}.{raw}"' not in fn
    assert "json.dumps" not in fn.split("compare_digest")[0]


def test_empty_body_is_not_turned_into_an_object():
    """An empty body and {} are different bytes and must sign differently."""
    v = VECTOR["cases"]
    assert v["empty_body"]["hmac"] != v["empty_object"]["hmac"]
    assert v["empty_body"]["body_bytes"] == 0
    assert v["empty_object"]["body_bytes"] == 2


def test_unicode_signs_identically():
    row = VECTOR["cases"]["unicode_ar_ru"]
    assert "تجربة" in row["body"] and "демо" in row["body"]
    body = row["body"].encode("utf-8")
    assert len(body) == row["body_bytes"] > len(row["body"])
