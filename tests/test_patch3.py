"""Tests for patch batch 3 (putra/vm-0-86): header= multi-header, llm cache, llm budget.
No network required: uses monkeypatched _llm_chat + offline header resolution."""
import os, sys, json, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agsh
from agsh import Runtime, parse_stage, AGSHError

class TestHeaderParser(unittest.TestCase):
    def test_single_header(self):
        verb, subj, kw = parse_stage(["fetch", "https://api.example.com/x", "header=X-API-Key:abc123"])
        self.assertEqual(verb, "fetch")
        self.assertEqual(kw.get("header"), ["X-API-Key:abc123"])

    def test_multi_header_accumulates(self):
        verb, subj, kw = parse_stage(["fetch", "https://api.example.com/x",
                                      "header=Accept:application/xml", "header=X-Version:2"])
        self.assertEqual(kw.get("header"), ["Accept:application/xml", "X-Version:2"])

    def test_other_kwargs_keep_last_wins(self):
        verb, subj, kw = parse_stage(["fetch", "https://api.example.com/x", "retries=1", "retries=2"])
        self.assertEqual(kw.get("retries"), "2")

class TestHeaderResolver(unittest.TestCase):
    def test_literal_header(self):
        rt = Runtime()
        h = rt._resolve_extra_headers("fetch", {"header": ["X-API-Key:literal-val"]})
        self.assertEqual(h, {"X-API-Key": "literal-val"})

    def test_env_ref_resolved_and_redacted_in_audit(self):
        os.environ["AGSH_T_KEY"] = "supersecret"
        logdir = tempfile.mkdtemp()
        ap = os.path.join(logdir, "audit.jsonl")
        rt = Runtime(audit_path=ap)
        h = rt._resolve_extra_headers("fetch", {"header": ["X-API-Key:$AGSH_T_KEY"]})
        self.assertEqual(h, {"X-API-Key": "supersecret"})
        raw = open(ap).read()
        self.assertNotIn("supersecret", raw)
        self.assertIn("REDACTED", raw)

    def test_missing_env_raises_without_value(self):
        rt = Runtime()
        os.environ.pop("AGSH_T_MISSING", None)
        with self.assertRaises(AGSHError) as cm:
            rt._resolve_extra_headers("fetch", {"header": ["X-API-Key:$AGSH_T_MISSING"]})
        self.assertNotIn("supersecret", str(cm.exception))

    def test_header_name_injection_blocked(self):
        rt = Runtime()
        with self.assertRaises(AGSHError):
            rt._resolve_extra_headers("fetch", {"header": ["X-Bad\r\nInject:yes"]})
        with self.assertRaises(AGSHError):
            rt._resolve_extra_headers("fetch", {"header": ["Bad Name:yes"]})

    def test_colonless_header_rejected(self):
        rt = Runtime()
        with self.assertRaises(AGSHError):
            rt._resolve_extra_headers("fetch", {"header": ["justname"]})

class TestLLMCacheBudget(unittest.TestCase):
    def _rt(self, **kw):
        return Runtime(allow_llm=True, llm_base="http://127.0.0.1:1/v1", **kw)

    def test_budget_hard_fail(self):
        rt = self._rt(llm_budget=1)
        # first call: budget check passes (1 <= 1), network refused -> URLError wrapped as AGSHError
        try:
            rt._llm_chat(None, "one", {"model": "m", "max-tokens": 5, "temp": 0}, "llm")
        except AGSHError as e:
            if "budget" in str(e): self.fail("budget consumed at call 1 with budget=1")
        with self.assertRaises(AGSHError) as cm:
            rt._llm_chat(None, "two", {"model": "m", "max-tokens": 5, "temp": 0}, "llm")
        self.assertIn("budget", str(cm.exception).lower())

    def test_env_budget_override(self):
        os.environ["AGSH_LLM_BUDGET"] = "0"
        try:
            rt = self._rt()
            with self.assertRaises(AGSHError) as cm:
                rt._llm_chat(None, "x", {"model": "m", "max-tokens": 5, "temp": 0}, "llm")
            self.assertIn("budget", str(cm.exception).lower())
        finally:
            os.environ.pop("AGSH_LLM_BUDGET", None)

    def test_cache_roundtrip_and_hit(self):
        cdir = tempfile.mkdtemp()
        cpath = os.path.join(cdir, "cache.sqlite")
        rt = self._rt(llm_cache=cpath)
        payload_calls = {"n": 0}
        real_urlopen = agsh.urllib.request.urlopen
        def fake_urlopen(req, timeout=None):
            payload_calls["n"] += 1
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self):
                    return json.dumps({"choices": [{"message": {"content": "CACHED-ANSWER"},
                                                    "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            return R()
        agsh.urllib.request.urlopen = fake_urlopen
        try:
            kw = {"model": "m", "max-tokens": 10, "temp": 0}
            a1 = rt._llm_chat(None, "same input", kw, "llm")
            self.assertEqual(a1, "CACHED-ANSWER")
            self.assertEqual(payload_calls["n"], 1)
            rt2 = self._rt(llm_cache=cpath)
            a2 = rt2._llm_chat(None, "same input", kw, "llm")
            self.assertEqual(a2, "CACHED-ANSWER")
            self.assertEqual(payload_calls["n"], 1, "second run must be served from cache (0 network)")
            self.assertEqual(rt2.stats.get("llm_cache_hits"), 1)
        finally:
            agsh.urllib.request.urlopen = real_urlopen

if __name__ == "__main__":
    unittest.main(verbosity=1)
