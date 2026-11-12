#!/usr/bin/env python3
"""AGSH — Agent Shell prototype interpreter.

Pipe-style DSL: verb subject key=value ... | verb ...
Verbs: fetch, post, read, write, append, filter, pick, map, search, run,
       ask, checkpoint, guard, undo, assert
All data flows as JSON. Mutations are logged for `undo`. Runtime returns
delta-feedback (written counts, diffs) instead of silent success.
"""
import json, os, sys, re, copy, time, socket, shlex, fcntl, datetime
import urllib.request, urllib.error
from contextlib import contextmanager

class AGSHError(Exception):
    def __init__(self, step, msg, hint=None, got=None):
        self.step, self.msg, self.hint, self.got = step, msg, hint, got
        super().__init__(f"[{step}] {msg}" + (f" | hint: {hint}" if hint else "") + (f" | got: {got}" if got is not None else ""))

def tokenize(line):
    """Split a pipeline line into stage TOKEN LISTS, honoring quotes:
    quotes group content (including spaces) into a single token, and quote
    characters are stripped."""
    stages, cur, buf, in_q, qch = [], [], "", False, None
    for c in line:
        if in_q:
            if c == qch:
                in_q = False
            else:
                buf += c
        elif c in "\"'":
            in_q, qch = True, c
        elif c == "|" or c == ";":
            # '|' and ';' both terminate a stage (';' per AGENT-GUIDE §13 review)
            if buf:
                cur.append(buf)
                buf = ""
            stages.append(cur)
            cur = []
        elif c in " \t":
            if buf:
                cur.append(buf)
                buf = ""
        else:
            buf += c
    if buf:
        cur.append(buf)
    if cur:
        stages.append(cur)
    # drop empty stages (e.g. leading "|" continuation lines)
    return [t for t in stages if any(tok != "|" for tok in t)]

def parse_stage(toks):
    verb, subject, kwargs = None, None, {}
    toks = [t for t in toks if t != "|"]  # continuation-pipe marker
    for i, t in enumerate(toks):
        if "==" in t and i > 1:
            pass  # equality op inside subject — never kwargs (PR-2: shadowing reorder)
        elif "=" in t and i > 0 and not re.search(r"(>=|<=|==|>|<)", t):
            k, v = t.split("=", 1)
            if k == "header":
                # header=NAME:VALUE may repeat (multi-header support, backlog #1);
                # only 'header' accumulates — all other keys keep last-wins
                prev = kwargs.get(k)
                kwargs[k] = prev + [v] if isinstance(prev, list) else [v]
            else:
                kwargs[k] = v
        elif i == 0:
            verb = t
        else:
            subject = (subject + " " + t) if subject else t
    return verb, subject, kwargs

class Runtime:
    def __init__(self, dry=False, allow_run=False, approve_network_write=False, answer=None,
                 allow_private_net=False, audit_path=None, max_fetch=20,
                 allow_llm=False, llm_base=None, llm_key=None, llm_model=None,
                 llm_max_input=8000, run_allowlist_host=None, llm_cache=None, llm_budget=None,
                 lease_wait=0):
        self.log = []          # undo log: [(kind, payload)]
        self.checkpoints = []  # indices into log
        self.allow_run = allow_run
        self.approve_network_write = approve_network_write
        self.answer = answer
        self.allow_private_net = allow_private_net
        self.allow_llm = allow_llm
        self.llm_base = llm_base
        self.llm_key = llm_key
        self.llm_model = llm_model
        self.llm_max_input = llm_max_input
        self.run_allowlist_host = run_allowlist_host  # host-level policy (e.g. --allow-run=echo,wc)
        self.lease_wait = lease_wait
        self._held_leases = []  # (name, fd) — released at process exit
        self._skip = False      # set by a false `when` — remaining stages of the line are skipped
        self._skipped = 0
        self.llm_cache = llm_cache  # sqlite path: deterministic replay of LLM answers (backlog #3)
        self.llm_budget = llm_budget  # max LLM calls per run (env AGSH_LLM_BUDGET overrides hard-fail)
        self.vars = {}         # named state across the pipeline (let/take)
        self.audit_path = audit_path
        self._dns_pins = {}    # host -> frozenset of IPs seen at guard time (DNS-rebinding defense)
        self._llm_calls = 0    # budget counter (llm + summarize-with-llm)
        self.max_fetch = max_fetch
        self.stats = {"round_trips": 1, "lines": 0, "verbs": 0}

    # ---- audit log (append-only JSONL) ----
    def audit(self, verb, subject, outcome, extra=None):
        if not self.audit_path:
            return
        try:
            rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
                   "verb": verb, "risk": RISK.get(verb, "unknown"),
                   "subject": (subject or "")[:200], "outcome": outcome,
                   "cwd": os.getcwd()}
            if extra: rec.update(extra)
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # auditing must never break the pipeline

    # ---- advisory file lock (multi-agent safety) ----
    @contextmanager
    def _file_lock(self, path, verb):
        lock_path = path + ".agshlock"
        try:
            f = open(lock_path, "a+")
        except OSError as e:
            raise AGSHError(verb, f"cannot create lock file: {e}", got=lock_path)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise AGSHError(verb, f"file is locked by another process: {path}",
                            hint="another agent is writing this file — retry later (stale locks are safe to delete: *.agshlock)",
                            got=path)
        try:
            yield
        finally:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

    # ---- undo-able mutations ----
    def _log_write(self, path, existed, prev_content):
        self.log.append(("write", {"path": path, "existed": existed,
                                   "prev": prev_content}))

    def _log_api(self, method, url, body, undo_method=None, undo_url=None):
        self.log.append(("api", {"method": method, "url": url, "body": body,
                                 "undo_method": undo_method, "undo_url": undo_url,
                                 "undone": False}))

    def undo_since(self, checkpoint=None):
        start = self.checkpoints[checkpoint] if isinstance(checkpoint, int) \
                and checkpoint < len(self.checkpoints) else \
                (self.checkpoints[-1] if self.checkpoints else 0)
        undone = []
        while len(self.log) > start:
            kind, payload = self.log.pop()
            if kind == "write":
                p = payload["path"]
                if payload["existed"]:
                    with open(p, "w") as f: f.write(payload["prev"])
                elif os.path.exists(p):
                    os.remove(p)
                undone.append(f"reverted file {p}")
            elif kind == "api" and not payload["undone"]:
                payload["undone"] = True
                um, uu = payload.get("undo_method"), payload.get("undo_url")
                if um and uu:
                    # REAL server-side undo (e.g. DELETE the created resource)
                    # — same SSRF guard as post: undo must never become an
                    # SSRF bypass via a crafted undo_url
                    try:
                        self._pin_host(uu, "undo")
                    except AGSHError as e:
                        undone.append(f"BLOCKED undo for {payload['method']} {payload['url']}: {e}")
                        continue
                    try:
                        req = urllib.request.Request(uu, method=um,
                            headers={"Content-Type": "application/json"})
                        body = json.dumps(payload.get("body")).encode() if um == "POST" else None
                        with urllib.request.urlopen(req, data=body, timeout=15) as r:
                            status = r.status
                        undone.append(f"undone API {payload['method']} {payload['url']} -> {um} {uu} (HTTP {status})")
                    except Exception as e:
                        undone.append(f"FAILED undo for {payload['method']} {payload['url']}: {e}")
                else:
                    undone.append(f"recorded undo intent for {payload['method']} {payload['url']} (no undo_method configured)")
        return undone

    # ---- built-ins ----
    PRIVATE_NET_RE = re.compile(
        r"^(127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0|::1|fe80:|fc00:|fd)"
        r"|^172\.(1[6-9]|2\d|3[01])\.")

    def _url_host(self, url):
        return url.split("/")[2].split("@")[-1].split(":")[0]

    def _resolve_ips(self, host):
        return {i[4][0] for i in socket.getaddrinfo(host, None)}

    def _verify_dns_pin(self, url, verb):
        """DNS-rebinding defense: the IPs seen NOW must equal the IPs seen at
        guard time — otherwise the host may have rebound to a private IP."""
        host = self._url_host(url)
        now = self._resolve_ips(host)
        if now != self._dns_pins.get(host):
            raise AGSHError(verb, f"DNS rebinding suspected: {host} resolved to different IPs between check and request",
                            hint="blocked (TOCTOU defense); if intentional, re-run — but investigate the host", got=sorted(now))

    def _resolve_auth_header(self, verb, kw):
        """auth=$ENVVAR -> Authorization: Bearer *** The secret never
        lands in audit logs, error payloads, or stats - only the variable NAME."""
        var = kw.get("auth")
        if not var:
            return {}
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
            raise AGSHError(verb, "auth must name an environment variable",
                            hint="usage: fetch https://api/... auth=MY_TOKEN_VAR", got=var)
        val = os.environ.get(var)
        if not val:
            raise AGSHError(verb, f"environment variable not set: {var}",
                            hint="export the variable before running agsh (value is never logged)", got=var)
        self.audit(verb, f"auth from ${var}", "ok", {"env_var": var, "secret": "REDACTED"})
        return {"Authorization": f"Bearer {val}"}

    def _resolve_extra_headers(self, verb, kw):
        """header=NAME:VALUE (repeatable, parser accumulates into a list).
        NAME: literal header name, validated (no CR/LF injection).
        VALUE: either a literal, or a $ENVVAR reference — the env value is
        resolved at runtime and never lands in audit/error/stats (same
        contract as auth=). Bare 'header=' is a parse error."""
        raw = kw.get("header")
        if not raw:
            return {}
        items = raw if isinstance(raw, list) else [raw]
        out = {}
        for item in items:
            if ":" not in item:
                raise AGSHError(verb, "header must be NAME:VALUE",
                                hint='usage: header=X-API-Key:$MY_KEY header=Accept:application/xml',
                                got=item)
            name, val = item.split(":", 1)
            name = name.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise AGSHError(verb, "header name may only contain letters, digits, - and _",
                                hint="no spaces, no CR/LF injection", got=name)
            if not val:
                raise AGSHError(verb, "header value is empty",
                                hint='use header=X-API-Key:$MY_KEY with an env var', got=item)
            if val.startswith("$"):
                var = val[1:]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
                    raise AGSHError(verb, "header $ref must name an environment variable",
                                    hint="usage: header=X-API-Key:$MY_KEY", got=val)
                env = os.environ.get(var)
                if not env:
                    raise AGSHError(verb, f"environment variable not set: {var}",
                                    hint="export the variable before running agsh (value is never logged)", got=var)
                self.audit(verb, f"header {name} from ${var}", "ok", {"env_var": var, "secret": "REDACTED"})
                val = env
            out[name] = val
        return out

    def _ssrf_guard(self, url, verb):
        """Resolve host and block private/metadata networks by default
        (prompt-injection defense: an agent must not be able to reach
        internal services or cloud metadata endpoints)."""
        host = url.split("/")[2].split("@")[-1].split(":")[0]
        if os.path.exists(os.path.join("fixtures", url.split("/")[2] + ".json")):
            return "fixture"
        if self.allow_private_net:
            self._dns_pins[host] = frozenset(self._resolve_ips(host))
            return "network"
        try:
            ips = self._resolve_ips(host)
        except Exception as e:
            raise AGSHError(verb, f"cannot resolve host: {host}", got=str(e))
        for ip in ips:
            if self.PRIVATE_NET_RE.match(ip):
                raise AGSHError(verb, f"host {host} resolves to private/metadata IP {ip}",
                                hint="blocked by default (SSRF guard); re-run with --allow-private-net if this is intentional",
                                got=ip)
        self._dns_pins[host] = frozenset(ips)  # pinned for rebinding check
        self.audit(verb, url, "ok", {"source": "network", "host": host})
        return "network"

    def _replay_lookup(self, verb, url, cache):
        """Return cached body for URL or None (cache: JSONL of {url, body})."""
        if not os.path.exists(cache):
            return None
        try:
            with open(cache) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # corrupt line: skip, never crash a replay
                    if rec.get("url") == url:
                        self.audit(verb, url, "ok", {"source": "replay", "cache": cache})
                        return rec.get("body")
        except OSError as e:
            raise AGSHError(verb, f"cannot read replay cache: {cache}", got=str(e))
        return None

    def _record_body(self, verb, url, cache, body):
        """Upsert {url, body} into JSONL cache (rewrite file; caches are small)."""
        if not url or os.path.dirname(cache) and not os.path.isdir(os.path.dirname(cache)):
            raise AGSHError(verb, f"replay cache dir does not exist: {cache}", got=cache)
        recs = []
        if os.path.exists(cache):
            with open(cache) as f:
                recs = [json.loads(l) for l in f if l.strip()]
        recs = [r for r in recs if r.get("url") != url]
        recs.append({"url": url, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "body": body})
        with open(cache, "w") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self.audit(verb, url, "ok", {"recorded": cache})

    def _pin_host(self, url, verb):
        """Resolve + pin the host (must be called before any network write).
        Blocks private/metadata networks by default and pins resolved IPs so
        a later DNS-rebinding check can compare."""
        source = self._ssrf_guard(url, verb)
        if source == "network":
            self._verify_dns_pin(url, verb)
        return source

    def v_fetch(self, data, url, kw):
        if not re.match(r"https?://", url):
            raise AGSHError("fetch", "url must start with http(s)://", got=url)
        # rate limit (backlog #6): bounded fetches per run, host-configurable
        self.stats["fetches"] = self.stats.get("fetches", 0) + 1
        if self.stats["fetches"] > self.max_fetch:
            raise AGSHError("fetch", f"rate limit exceeded: more than {self.max_fetch} fetch stages in one run",
                            hint="raise --max-fetch on the host if this is intentional, or split the task",
                            got=self.stats["fetches"])
        # replay: serve from cache, zero network (bypasses ssrf/dns checks BY DESIGN - nothing leaves the box)
        cache_replay = kw.get("replay")
        if cache_replay:
            cached = self._replay_lookup("fetch", url, cache_replay)
            if cached is not None:
                self.stats.setdefault("fetch_sources", []).append(f"replay:{url.split('/')[2]}")
                self.stats["verbs"] += 1
                return cached
        # fail-fast retries validation BEFORE any DNS/ssrf work
        retries = kw.get("retries")
        attempts = 0
        if retries is not None:
            try:
                attempts = int(retries)
            except (TypeError, ValueError):
                raise AGSHError("fetch", "retries must be an integer", hint="usage: fetch URL retries=2", got=retries)
            if not (0 <= attempts <= 3):
                raise AGSHError("fetch", "retries must be 0..3", hint="hard cap 3 keeps runs bounded", got=retries)
        source = self._ssrf_guard(url, "fetch")
        if source == "network":
            self._verify_dns_pin(url, "fetch")  # rebinding defense: re-resolve must match the pin
        self.stats.setdefault("fetch_sources", []).append(f"{source}:{url.split('/')[2]}")
        # offline demo mode: serve from ./fixtures/<host>.json if present
        fixture = os.path.join("fixtures", url.split("/")[2] + ".json")
        if os.path.exists(fixture):
            with open(fixture) as f:
                body = json.load(f)
            # a fixture is the whole dataset — no pagination
            cache_record = kw.get("record")
            if cache_record:
                self._record_body("fetch", url, cache_record, body)  # record fixtures too (replay parity)
            self.audit("fetch", url, "ok", {"source": "fixture", "host": url.split("/")[2]})  # audit-all-stages: fixture path juga
            self.stats["verbs"] += 1
            return body.get("results", body) if isinstance(body, dict) else body
        hdrs = {"Accept": "application/json",
            "User-Agent": kw.get("ua", "AGSH/0.3 (agent; +https://github.com/Bumbu-indomie13/AGSH-)")}
        hdrs.update(self._resolve_auth_header("fetch", kw))  # auth_env support
        hdrs.update(self._resolve_extra_headers("fetch", kw))  # header=NAME:VALUE (repeatable, backlog #1)
        req = urllib.request.Request(url, headers=hdrs)
        body = None
        last_err = None
        for attempt in range(attempts + 1):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read().decode())
                break
            except AGSHError:
                raise
            except Exception as e:
                last_err = e
                if attempt < attempts:
                    time.sleep(0.2 * (attempt + 1))  # linear backoff
        if body is None:
            raise AGSHError("fetch", f"request failed after {attempts + 1} attempt(s): {last_err}", got=url)
        cache_record = kw.get("record")
        if cache_record:
            self._record_body("fetch", url, cache_record, body)
        page_field = kw.get("paginate")
        out = body
        if page_field and isinstance(body, dict):
            results, page, failed = [], 1, None
            while True:
                chunk = body.get(page_field) if page == 1 else self._fetch_page(url, page_field, page)
                if chunk is None and page > 1:
                    failed = page
                    break
                if not chunk:
                    break
                results += chunk
                page += 1
                if page > 100:
                    # hard cap must be visible, never silent
                    raise AGSHError("fetch", "pagination hit the 100-page cap — results would be truncated",
                                    hint="narrow the query or raise the cap deliberately", got=f"{len(results)} rows")
            if failed:
                # silent truncation violates our own delta-feedback contract
                raise AGSHError("fetch", f"pagination failed at page {failed} — results may be incomplete",
                                hint="fix the API or remove paginate=", got=f"{len(results)} rows collected")
            out = results
        self.stats["verbs"] += 1
        return out

    def _fetch_page(self, url, field, page):
        sep = "&" if "?" in url else "?"
        # page requests bypass nothing: same SSRF/rebinding pin as the parent fetch
        self._pin_host(url, "fetch")
        try:
            req = urllib.request.Request(f"{url}{sep}page={page}", headers={
                "Accept": "application/json",
                "User-Agent": "AGSH/0.3 (agent; +https://github.com/Bumbu-indomie13/AGSH-)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode()).get(field, [])
        except Exception:
            return None  # distinguishable from an empty page []

    def v_filter(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("filter", "input must be a list", got=type(data).__name__)
        if not (subject or "").strip() and not kw:
            raise AGSHError("filter", "no filter expression",
                            hint="use: filter FIELD=VALUE | filter FIELD>NUM | filter FIELD contains TEXT | filter FIELD ends-with TEXT", got=subject)
        # relational form: filter FIELD>NUM (also >= < <=) — numeric compare,
        # non-numeric/missing values simply don't match (consistent with =)
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)", (subject or "").strip())
        if m:
            f, op, num = m.group(1), m.group(2), float(m.group(3))
            ops = {">": lambda v: v > num, "<": lambda v: v < num,
                   ">=": lambda v: v >= num, "<=": lambda v: v <= num}
            def keep(x):
                try: return ops[op](float(x.get(f)))
                except (TypeError, ValueError): return False
            _res = [x for x in data if isinstance(x, dict) and keep(x)]
            return _res
        # substring form: filter FIELD contains TEXT
        m = re.fullmatch(r"([A-Za-z_]\w*)\s+contains\s+(.*)", (subject or "").strip())
        if m:
            f, needle = m.group(1), m.group(2)
            _res = [x for x in data if isinstance(x, dict) and needle in str(x.get(f, ""))]
            return _res
        # suffix form: filter FIELD ends-with TEXT (generic — was hardcoded to email)
        m = re.fullmatch(r"([A-Za-z_]\w*)\s+ends-with\s+(\S+)", (subject or "").strip())
        if m:
            f, suffix = m.group(1), m.group(2)
            _res = [x for x in data if isinstance(x, dict) and str(x.get(f, "")).endswith(suffix)]
            return _res
        if kw:
            k, v = next(iter(kw.items()))
            _res = [x for x in data if isinstance(x, dict) and str(x.get(k)) == v]
            return _res
        # equality form: filter FIELD == VALUE — string equality (PR-2, DeryClaw)
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*==\s*(.+)", (subject or "").strip())
        if m:
            f, val = m.group(1), m.group(2).strip().strip("\"'")
            _res = [x for x in data if isinstance(x, dict) and str(x.get(f)) == val]
            return _res
        if subject.startswith("email ends-with"):
            suffix = subject.split()[-1]
            _res = [x for x in data if str(x.get("email", "")).endswith(suffix)]
            return _res
        raise AGSHError("filter", "unsupported filter form",
                        hint="use: filter FIELD=VALUE | filter FIELD>NUM | filter FIELD contains TEXT | filter FIELD ends-with TEXT", got=subject)

    def v_pick(self, data, subject, kw):
        self.stats["verbs"] += 1
        fields = [f for f in re.split(r"[,\s]+", subject or "") if f]
        if isinstance(data, dict):
            _res = {k: data[k] for k in fields if k in data}
            return _res
        out = []
        for x in data:
            if isinstance(x, dict):
                out.append({k: x[k] for k in fields if k in x})
            else:
                out.append(x)
        _res = out
        return _res

    def _check_path(self, path, verb):
        """Sandbox: paths must stay inside the working directory."""
        real = os.path.realpath(path)
        root = os.path.realpath(os.getcwd())
        if not (real == root or real.startswith(root + os.sep)):
            raise AGSHError(verb, "path escapes working directory",
                            hint="write/read paths must stay inside the working dir", got=path)
        return real

    def v_write(self, data, subject, kw):
        self.stats["verbs"] += 1
        path = self._check_path(subject, "write")
        if data is None:
            raise AGSHError("write", "no input data to write",
                            hint="pipe data into write, e.g. '... | write targets.json'", got=subject)
        existed = os.path.exists(path)
        prev = open(path).read() if existed else None
        with self._file_lock(path, "write"), open(path, "w") as f:
            if isinstance(data, (list, dict)):
                json.dump(data, f, indent=2)
            else:
                f.write(str(data))
        n = len(data) if isinstance(data, list) else 1
        # delta feedback vs previous version
        delta = None
        if existed:
            try:
                old = json.loads(prev)
                if isinstance(old, list): delta = n - len(old)
            except Exception: pass
        self._log_write(path, existed, prev)
        self.audit("write", subject, "ok", {"path": path, "written": n})
        fb = {"written": n, "path": path}
        if delta is not None: fb["diff_vs_previous"] = delta
        return fb

    def v_assert(self, data, subject, kw):
        self.stats["verbs"] += 1
        # type assertion (semantic contract between stages)
        m = re.match(r"type\s+(list|dict|str|num)", subject or "")
        if m:
            want = m.group(1)
            got = type(data).__name__
            ok = {"list": isinstance(data, list), "dict": isinstance(data, dict),
                  "str": isinstance(data, str),
                  "num": isinstance(data, (int, float)) and not isinstance(data, bool)}[want]
            if not ok:
                raise AGSHError("assert", f"expected type {want}, got {got}",
                                hint="check the previous stage's output shape", got=got)
            _res = data
            return _res
        m = re.match(r"count\s*(>|<|>=|<=|==)\s*(\d+)", subject or "")
        if not m:
            raise AGSHError("assert", "unsupported assertion", got=subject)
        op, num = m.group(1), int(m.group(2))
        n = len(data) if isinstance(data, list) else 1
        ok = {">": n > num, "<": n < num, ">=": n >= num,
              "<=": n <= num, "==": n == num}[op]
        if not ok:
            hint = f"filter returned {n} — check field names exist" if n == 0 else None
            raise AGSHError("assert", f"count {n} not {op} {num}", hint=hint, got=n)
        _res = data
        return _res

    def v_when(self, data, subject, kw):
        """when COND — conditional control flow (gap vs orchestration DSLs, backlog #9 eval).
        COND uses the same grammar as assert: `count OP N` | `type T`.
        True  -> data passes through, remaining stages run.
        False -> remaining stages of THIS line are skipped (stats.skipped_stages),
                 data passes through unchanged; the run still succeeds."""
        self.stats["verbs"] += 1
        m = re.match(r"type\s+(list|dict|str|num)", subject or "")
        if m:
            want = m.group(1)
            ok = {"list": isinstance(data, list), "dict": isinstance(data, dict),
                  "str": isinstance(data, str),
                  "num": isinstance(data, (int, float)) and not isinstance(data, bool)}[want]
        else:
            m = re.match(r"count\s*(>|<|>=|<=|==)\s*(\d+)", subject or "")
            if not m:
                raise AGSHError("when", "unsupported condition",
                                hint="conditions: count OP N | type T — same grammar as assert", got=subject)
            op, num = m.group(1), int(m.group(2))
            n = len(data) if isinstance(data, list) else 1
            ok = {">": n > num, "<": n < num, ">=": n >= num,
                  "<=": n <= num, "==": n == num}[op]
        if not ok:
            self._skip = True
        else:
            self._skip = False  # a later `when` re-evaluates and can re-enable skipped stages
        return data  # pass-through either way

    def v_guard(self, data, subject, kw):
        self.stats["verbs"] += 1
        if subject.startswith("file exists"):
            path = subject.split("exists", 1)[1].strip()
            if not os.path.exists(path):
                raise AGSHError("guard", f"file not found: {path}", hint="create it or check the path", got=path)
            _res = data
            return _res
        raise AGSHError("guard", "unsupported guard", got=subject)

    def v_let(self, data, subject, kw):
        """let NAME — bind current pipeline data to a named variable (data flows through)."""
        self.stats["verbs"] += 1
        if not re.fullmatch(r"[A-Za-z_]\w*", subject or ""):
            raise AGSHError("let", "variable name must be [A-Za-z_]word", got=subject)
        self.vars[subject] = data
        self.stats.setdefault("bound", []).append(subject)
        _res = data  # pass-through: a let in the middle of a pipeline must not break the flow
        return _res

    def v_take(self, data, subject, kw):
        """take NAME — replace pipeline data with a previously bound variable."""
        self.stats["verbs"] += 1
        if subject not in self.vars:
            raise AGSHError("take", f"unbound variable: {subject}",
                            hint=f"bound so far: {sorted(self.vars) or 'none'}", got=subject)
        _res = self.vars[subject]
        return _res

    def v_get(self, data, subject, kw):
        """get a.b.c — nested path access: dict keys and list indexes."""
        self.stats["verbs"] += 1
        cur = data
        for part in (subject or "").split("."):
            if part == "": continue
            if isinstance(cur, dict):
                if part not in cur:
                    raise AGSHError("get", f"key not found: {part}",
                                    hint=f"available keys: {sorted(cur)[:10]}", got=subject)
                cur = cur[part]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    raise AGSHError("get", f"bad list index: {part}",
                                    hint=f"list has {len(cur)} items (0..{len(cur)-1})", got=subject)
            else:
                raise AGSHError("get", f"cannot index into {type(cur).__name__} at '{part}'", got=subject)
        _res = cur
        return _res

    def v_checkpoint(self, data, subject, kw):
        save = kw.get("save")
        if save:  # world-state checkpoint: data + snapshot of all tracked files
            path = self._check_path(save, "checkpoint")
            cwd = os.path.realpath(os.getcwd())
            files = {}
            for kind, payload in self.log:
                if kind == "write" and payload["path"] not in files:
                    p = payload["path"]
                    # store RELATIVE to cwd so the world file is portable
                    rel = os.path.relpath(p, cwd)
                    files[rel] = open(p).read() if os.path.exists(p) else None
            json.dump({"data": data, "files": files, "log_len": len(self.log)},
                      open(path, "w"), ensure_ascii=False)
            world = {"world_saved": path, "files": list(files)}
        else:
            world = None
        self.checkpoints.append(len(self.log))
        self.stats["verbs"] += 1
        return {"checkpoint": len(self.checkpoints) - 1, "world": world, "data": data}

    def v_resume(self, data, subject, kw):
        """Restore a world-state saved by `checkpoint save=FILE` (cross-session)."""
        self.stats["verbs"] += 1
        path = self._check_path(subject, "resume")
        if not os.path.exists(path):
            raise AGSHError("resume", f"world file not found: {subject}", got=subject)
        world = json.load(open(path))
        restored = []
        for p, content in world.get("files", {}).items():
            # validate EVERY path — a world file must never be an arbitrary
            # write primitive; paths are relative to cwd (portable)
            p = self._check_path(os.path.join(os.getcwd(), p), "resume")
            existed = os.path.exists(p)
            prev = open(p).read() if existed else None
            with self._file_lock(p, "resume"):
                if content is None:
                    if existed: os.remove(p)
                else:
                    open(p, "w").write(content)
            self._log_write(p, existed, prev)
            restored.append(p)
        self.audit("resume", subject, "ok", {"restored_files": restored})
        return {"resumed": subject, "restored_files": restored, "data": world.get("data")}

    def v_undo(self, data, subject, kw):
        undone = self.undo_since(kw.get("checkpoint", None) if kw.get("checkpoint", "").isdigit() else None)
        self.stats["verbs"] += 1
        self.audit("undo", subject or "last", "ok", {"undone": len(undone)})
        return {"undone": undone, "data": data}

    def v_read(self, data, subject, kw):
        self.stats["verbs"] += 1
        path = self._check_path(subject, "read")
        if not os.path.exists(path):
            raise AGSHError("read", f"file not found: {subject}", got=subject)
        content = open(path).read()
        try: return json.loads(content)
        except Exception: return content

    def v_run(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not self.allow_run:
            raise AGSHError("run", "shell execution is gated",
                            hint="inspect the plan (--plan), then re-run with --allow-run if approved",
                            got=subject)
        allow = kw.get("allow") or (",".join(sorted(self.run_allowlist_host)) if self.run_allowlist_host else None)
        if allow:
            # allowlist mode: no shell metacharacters, argv[0] must be in the
            # list, and the command runs WITHOUT a shell (no injection surface)
            allowed = {x.strip() for x in allow.split(",") if x.strip()}
            if re.search(r"[;&|<>`$(){}\[\]\\\n]", subject):
                raise AGSHError("run", "allowlist mode forbids shell metacharacters",
                                hint="each command must be a plain argv (no pipes, ;, &&, redirects)",
                                got=subject)
            argv0 = shlex.split(subject)[0]
            if argv0 not in allowed:
                raise AGSHError("run", f"command not in allowlist: {argv0}",
                                hint=f"allowed: {sorted(allowed)}; extend with allow=a,b in the script or ask the host for --allow-run approval",
                                got=argv0)
            import subprocess
            env = {"PATH": "/usr/bin:/bin", "HOME": os.getcwd(), "LANG": "C"}
            r = subprocess.run(shlex.split(subject), capture_output=True, text=True,
                               timeout=int(kw.get("timeout", 15)), env=env, cwd=os.getcwd())
        else:
            import subprocess
            # restricted execution: minimal env, working-dir-only cwd, hard timeout
            env = {"PATH": "/usr/bin:/bin", "HOME": os.getcwd(), "LANG": "C"}
            r = subprocess.run(subject, shell=True, capture_output=True, text=True,
                               timeout=int(kw.get("timeout", 15)), env=env, cwd=os.getcwd())
        self.audit("run", subject, "ok", {"exit_code": r.returncode, "allowlisted": bool(allow)})
        return {"exit_code": r.returncode, "stdout": r.stdout.strip()[:2000],
                "stderr": r.stderr.strip()[:2000]}

    def v_search(self, data, subject, kw):
        self.stats["verbs"] += 1
        out = []
        for root, dirs, files in os.walk("."):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    if subject in open(p, errors="ignore").read(): out.append(p)
                except Exception: pass
        _res = out
        return _res

    # ---- LLM verb (bahasa): sub-pertanyaan dijawab DI DALAM pipeline ----
    def _llm_cache_key(self, payload):
        """Content-hash key: model+messages+params. Same input => same answer,
        offline re-run, deterministic benchmarks."""
        import hashlib
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _llm_cache_get(self, key):
        if not self.llm_cache:
            return None
        import sqlite3
        try:
            db = sqlite3.connect(self.llm_cache)
            row = db.execute("SELECT content FROM llm_cache WHERE k=?", (key,)).fetchone()
            db.close()
            return row[0] if row else None
        except sqlite3.Error:
            return None  # cache must never break a run

    def _llm_cache_put(self, key, content):
        if not self.llm_cache:
            return
        import sqlite3
        try:
            db = sqlite3.connect(self.llm_cache)
            db.execute("CREATE TABLE IF NOT EXISTS llm_cache (k TEXT PRIMARY KEY, content TEXT, ts TEXT)")
            db.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,datetime('now'))", (key, content))
            db.commit()
            db.close()
        except sqlite3.Error:
            pass

    def _llm_chat(self, system, user, kw, verb):
        """One OpenAI-compatible chat completion. Endpoint MUST be allowlisted
        by the host (--llm-base); the key comes from the host env, never the script."""
        import urllib.request
        # budget cap (backlog #3): hard-fail when the run exceeds its LLM call allowance
        budget = self.llm_budget
        if budget is None:
            env_b = os.environ.get("AGSH_LLM_BUDGET")
            if env_b:
                try:
                    budget = int(env_b)
                except ValueError:
                    raise AGSHError(verb, "AGSH_LLM_BUDGET must be an integer", got=env_b)
        self._llm_calls += 1
        if budget is not None and self._llm_calls > budget:
            raise AGSHError(verb, f"LLM budget exceeded: more than {budget} llm call(s) in one run",
                            hint="narrow data before llm/summarize, raise --llm-budget, or drop AGSH_LLM_BUDGET",
                            got=self._llm_calls)
        base = (self.llm_base or os.environ.get("AGSH_LLM_BASE") or "").rstrip("/")
        key = self.llm_key or os.environ.get("AGSH_LLM_KEY") or ""
        model = kw.get("model") or self.llm_model or os.environ.get("AGSH_LLM_MODEL") or "default"
        if not base:
            raise AGSHError(verb, "no LLM endpoint configured",
                            hint="host must pass --llm-base http://host/v1 or set AGSH_LLM_BASE",
                            got=None)
        max_in = int(kw.get("max-input", self.llm_max_input))
        body_data = json.dumps(user, ensure_ascii=False) if not isinstance(user, str) else user
        if len(body_data.encode()) > max_in:
            # never send oversized prompts silently — truncate visibly & count
            body_data = body_data[:max_in]
            self.stats["llm_truncated_chars"] = self.stats.get("llm_truncated_chars", 0) + 1
        payload = {
            "model": model,
            "stream": False,  # 9router contract: without this the response is SSE even for non-chunk bodies
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + [{"role": "user", "content": body_data}],
            "max_tokens": int(kw.get("max-tokens", 300)),
            "temperature": float(kw.get("temp", 0)),
        }
        # deterministic cache (backlog #3): serve identical calls from sqlite, zero network
        cache_key = self._llm_cache_key(payload)
        cached = self._llm_cache_get(cache_key)
        if cached is not None:
            self.stats["llm_cache_hits"] = self.stats.get("llm_cache_hits", 0) + 1
            self.audit(verb, f"cache hit {cache_key[:12]}", "ok", {"model": model})
            return cached
        req = urllib.request.Request(base + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=int(kw.get("timeout", 60))) as r:
                resp = json.loads(r.read().decode())
            msg = resp["choices"][0]["message"]
            content = msg.get("content") or ""
            finish = resp["choices"][0].get("finish_reason")
            usage = resp.get("usage", {})
            # reasoning models (deepseek-r1 style) may burn the whole token budget
            # on reasoning_content before emitting any content — retry visibly
            # with a larger budget instead of returning an empty answer
            retries = 0
            while not content.strip() and finish == "length" and retries < 2:
                retries += 1
                payload["max_tokens"] = int(payload["max_tokens"]) * 4
                req = urllib.request.Request(base + "/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=int(kw.get("timeout", 60))) as r:
                    resp = json.loads(r.read().decode())
                msg = resp["choices"][0]["message"]
                content = msg.get("content") or ""
                finish = resp["choices"][0].get("finish_reason")
                usage = resp.get("usage", {})
            self.stats["llm_budget_retries"] = self.stats.get("llm_budget_retries", 0) + retries
        except Exception as e:
            raise AGSHError(verb, f"LLM call failed: {e}",
                            hint="check the endpoint is up and the model name is valid", got=str(e)[:120])
        if not content.strip():
            raise AGSHError(verb, "LLM returned empty content (budget exhausted by reasoning)",
                            hint="raise max-tokens= (e.g. 1000) or use a non-reasoning model via model=",
                            got=f"finish_reason={finish}")
        self._llm_cache_put(cache_key, content)  # deterministic replay next run (backlog #3)
        self.stats["llm_calls"] = self.stats.get("llm_calls", 0) + 1
        self.stats["llm_prompt_tokens"] = self.stats.get("llm_prompt_tokens", 0) + usage.get("prompt_tokens", 0)
        self.stats["llm_completion_tokens"] = self.stats.get("llm_completion_tokens", 0) + usage.get("completion_tokens", 0)
        return content

    def v_llm(self, data, subject, kw):
        """llm "instruction" [model=NAME] [max-tokens=N] [max-input=N] [temp=F]
        Sends the pipeline data (JSON) with an instruction; the model's answer
        REPLACES the pipeline data. Deterministic: false is reported in stats."""
        self.stats["verbs"] += 1
        if not self.allow_llm:
            raise AGSHError("llm", "LLM calls are gated",
                            hint="host must re-run with --allow-llm (and --llm-base) if approved",
                            got=subject)
        if not subject:
            raise AGSHError("llm", "missing instruction",
                            hint='use: llm "kelompokkan error ini" max-tokens=200', got=None)
        if data is None:
            raise AGSHError("llm", "no input data — pipe something in (read/fetch first)",
                            hint="example: read log.jsonl | llm \"ringkas error\"", got=None)
        content = self._llm_chat(None, f"{subject}\n\nDATA:\n{data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)}", kw, "llm")
        self.audit("llm", subject, "ok", {"model": kw.get("model") or self.llm_model or "default"})
        # try to parse the answer as JSON (models often emit fenced blocks)
        txt = content.strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", txt).rstrip("`").strip()
        try:
            return json.loads(txt)
        except Exception:
            return content

    def v_summarize(self, data, subject, kw):
        """summarize [max-chars=N] — DETERMINISTIC compression, no LLM:
        reduces a long string/list into a compact head+tail window + stats,
        so the pipeline (and the chat context) only carries the small version."""
        self.stats["verbs"] += 1
        max_chars = int(kw.get("max-chars", 1200))
        if isinstance(data, list):
            if len(data) <= 20:
                return data
            head, tail = data[:10], data[-5:]
            out = {"summary": True, "total_items": len(data),
                   "head": head, "tail": tail,
                   "note": f"list compressed {len(data)} -> {len(head)+len(tail)} items (deterministic)"}
            self.stats["summarize_savings"] = self.stats.get("summarize_savings", 0) + len(data) - len(head) - len(tail)
            return out
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        if len(text) <= max_chars:
            return data
        cut = max_chars // 2
        out = {"summary": True, "original_chars": len(text),
               "head": text[:cut], "tail": text[-cut:],
               "note": f"text compressed {len(text)} -> {max_chars} chars (deterministic head+tail)"}
        self.stats["summarize_savings"] = self.stats.get("summarize_savings", 0) + len(text) - max_chars
        self.audit("summarize", (subject or "")[:80], "ok", {"original_chars": len(text)})
        return out

    def v_lines(self, data, subject, kw):
        """lines — split a text payload into a list of lines; each line is
        parsed as JSON when possible (JSONL logs become filterable lists).
        Pure/deterministic."""
        self.stats["verbs"] += 1
        if data is None:
            raise AGSHError("lines", "no input — pipe a text payload in (read a file first)", got=None)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        out = []
        for ln in text.splitlines():
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                out.append(ln)
        return out

    def v_map(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("map", "input must be a list", got=type(data).__name__)
        out = []
        # forms: map field=newname=oldname | map upper email | map lower email | map strip email
        toks = (subject or "").split()
        if len(toks) == 2 and toks[0] in ("upper", "lower", "strip"):
            fn = {"upper": str.upper, "lower": str.lower, "strip": str.strip}[toks[0]]
            field = toks[1]
            for x in data:
                if isinstance(x, dict) and field in x:
                    x = dict(x); x[field] = fn(str(x[field]))
                out.append(x)
            return out
        if kw:  # map rename: newname=oldname
            new = next(iter(kw))
            old = kw[new].split("=")[-1]
            for x in data:
                if isinstance(x, dict) and old in x:
                    x = dict(x); x[new] = x.pop(old)
                out.append(x)
            return out
        raise AGSHError("map", "unsupported map form",
                        hint="use: map upper|lower|strip <field> or map newname=oldname", got=subject)

    def v_append(self, data, subject, kw):
        self.stats["verbs"] += 1
        subject = self._check_path(subject, "append")
        existed = os.path.exists(subject)
        prev = open(subject).read() if existed else ""
        with self._file_lock(subject, "append"), open(subject, "a") as f:
            f.write(json.dumps(data) + "\n" if isinstance(data, (dict, list)) else str(data) + "\n")
        self._log_write(subject, existed, prev)
        self.audit("append", subject, "ok", {"path": subject})
        return {"appended": True, "path": subject}

    def v_post(self, data, subject, kw):
        self.stats["verbs"] += 1
        # approval can ONLY come from the host CLI (--approve); a script can
        # never approve itself
        if not self.approve_network_write:
            raise AGSHError("post", "network write requires approval",
                            hint="show the plan (--plan) to the user, then re-run with --approve",
                            got=subject)
        # URL shape check first (cheap, no DNS), then payload validation, then
        # the full SSRF/rebinding pin (DNS) last — a malformed/oversized
        # payload must error BEFORE any DNS lookup or network call.
        if not re.match(r"https?://", subject or ""):
            raise AGSHError("post", "url must start with http(s)://", got=subject)
        undo_method = kw.get("undo_method")   # e.g. DELETE — real server call on undo
        undo_url = kw.get("undo_url")
        # schema validation (backlog #5): required keys + size cap BEFORE any
        # network call — a malformed/oversized payload never leaves the host
        payload = data if data is not None else {}
        if not isinstance(payload, (dict, list)):
            raise AGSHError("post", "payload must be a dict or list (pipe data in)", got=type(data).__name__)
        body = json.dumps(payload)
        max_bytes = int(kw.get("max_bytes", 1_000_000))
        if len(body.encode()) > max_bytes:
            raise AGSHError("post", f"payload too large: {len(body)} bytes (cap {max_bytes})",
                            hint="narrow the data first (pick/filter) or raise max_bytes=", got=len(body))
        required = kw.get("keys")
        if required:
            req_keys = [k.strip() for k in required.split(",") if k.strip()]
            if isinstance(payload, dict):
                items = [payload]
            elif all(isinstance(x, dict) for x in payload):
                items = payload
            else:
                raise AGSHError("post", "keys= requires a dict payload or a list of dicts",
                                got=type(payload).__name__)
            for it in items:
                missing = [k for k in req_keys if k not in it]
                if missing:
                    raise AGSHError("post", f"payload missing required keys: {missing}",
                                    hint="add keys= in the post stage listing all required fields",
                                    got=sorted(it.keys()))
        # SSRF + DNS-rebinding guard — network writes are the most dangerous
        # surface an agent has; a prompt-injected agent must not be able to
        # POST to internal services or cloud metadata endpoints. undo_url is
        # a second network-write surface and gets the same pin.
        self._pin_host(subject, "post")
        if undo_url:
            self._pin_host(undo_url, "post")
        # actually perform the POST
        body = body.encode()
        hdrs = {"Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": kw.get("ua", "AGSH/0.3 (agent; +https://github.com/Bumbu-indomie13/AGSH-)")}
        hdrs.update(self._resolve_auth_header("post", kw))  # auth_env support
        hdrs.update(self._resolve_extra_headers("post", kw))  # header=NAME:VALUE (repeatable, backlog #1)
        req = urllib.request.Request(subject, data=body, method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, resp = r.status, r.read().decode()[:500]
        except Exception as e:
            raise AGSHError("post", f"request failed: {e}", got=subject)
        self._log_api("POST", subject, data, undo_method, undo_url)
        self.audit("post", subject, "ok", {"status": status,
                    "undo_method": undo_method, "payload_keys": list(payload) if isinstance(payload, dict) else None})
        return {"posted": subject, "status": status, "response": resp,
                "payload_keys": list(payload) if isinstance(payload, dict) else None}

    def v_ask(self, data, subject, kw):
        self.stats["verbs"] += 1
        if self.answer is not None:
            return {"question": subject, "answer": self.answer, "data": data}
        # human gate: stop the pipeline with dedicated exit code 2
        print(json.dumps({"needs_human": True, "question": subject,
                          "data": data, "stats": self.stats}, ensure_ascii=False))
        sys.exit(2)

    # ---- enhanced composition verbs (PR2 by @DeryClawbot) ----
    def v_sort(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("sort", "input must be a list", got=type(data).__name__)
        toks = (subject or "").split()
        desc = toks and toks[-1] == "desc"
        field = toks[0] if toks and toks[0] != "desc" else None
        if field:
            keyed = [x for x in data if isinstance(x, dict) and field in x]
            if len(keyed) != len(data):
                raise AGSHError("sort", f"field '{field}' missing in {len(data)-len(keyed)} items",
                                hint="all items must be dicts containing the field", got=field)
            return sorted(keyed, key=lambda x: x[field], reverse=desc)
        try:
            return sorted(data, reverse=desc)
        except TypeError:
            raise AGSHError("sort", "items are not comparable", got=type(data[0]).__name__ if data else None)

    def v_group(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("group", "input must be a list", got=type(data).__name__)
        out = {}
        for x in data:
            k = x.get(subject) if isinstance(x, dict) else None
            out.setdefault(str(k), []).append(x)
        return out

    def v_count(self, data, subject, kw):
        self.stats["verbs"] += 1
        n = len(data) if isinstance(data, (list, dict, str)) else (1 if data is not None else 0)
        return {"count": n}

    def v_limit(self, data, subject, kw):
        """limit N — first N items (alias of head N); documented since the
        composition-verbs commit but only implemented in v0.5."""
        return self.v_head(data, subject, kw)

    def v_schema(self, data, subject, kw):
        """schema SPEC — inter-stage shape contract (backlog #4).
        SPEC grammar: base | list[spec] | dict[base] | dict{k:spec,...}
        base: str | num | bool | list | dict | any
        Errors carry the exact path of the first mismatch."""
        self.stats["verbs"] += 1
        spec = (subject or "").strip()
        if not spec:
            raise AGSHError("schema", "missing schema spec",
                            hint="e.g. schema list[dict[id:num,name:str]]", got=spec)

        def parse(s):  # recursive spec parser -> ("base",name) | ("list",inner) | ("dictk",inner) | ("dictf",{...})
            s = s.strip()
            if s.startswith("list[") and s.endswith("]"):
                return ("list", parse(s[5:-1]))
            if s.startswith("dict[") and s.endswith("]"):
                inner = s[5:-1].strip()
                if re.fullmatch(r"[a-z]+", inner):
                    return ("dictk", parse(inner))
                fields = {}
                depth, cur, parts = 0, "", []
                for c in inner:
                    if c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                    if c == "," and depth == 0:
                        parts.append(cur); cur = ""
                    else:
                        cur += c
                if cur: parts.append(cur)
                for p in parts:
                    k, _, t = p.partition(":")
                    if not k.strip() or not t.strip():
                        raise AGSHError("schema", f"bad field spec: {p}",
                                        hint="fields look like dict[id:num,name:str]", got=inner)
                    fields[k.strip()] = parse(t)
                return ("dictf", fields)
            if not re.fullmatch(r"str|num|bool|list|dict|any", s):
                raise AGSHError("schema", f"unknown type in spec: {s}",
                                hint="types: str num bool list dict any", got=s)
            return ("base", s)

        def check(node, sp, path):
            kind, val = sp
            def typeok(n, t):
                return {"str": isinstance(n, str), "num": isinstance(n, (int, float)) and not isinstance(n, bool),
                        "bool": isinstance(n, bool), "list": isinstance(n, list),
                        "dict": isinstance(n, dict), "any": True}[t]
            if kind == "base":
                if not typeok(node, val):
                    raise AGSHError("schema", f"type mismatch at {path or '<root>'}: expected {val}, got {type(node).__name__}",
                                    hint="fix the producing stage or relax the schema", got=path)
                return
            if kind == "list":
                if not isinstance(node, list):
                    raise AGSHError("schema", f"type mismatch at {path or '<root>'}: expected list, got {type(node).__name__}", got=path)
                for i, item in enumerate(node):
                    check(item, val, f"{path}[{i}]" if path else f"[{i}]")
                return
            if kind == "dictk":
                if not isinstance(node, dict):
                    raise AGSHError("schema", f"type mismatch at {path or '<root>'}: expected dict, got {type(node).__name__}", got=path)
                for k, v in node.items():
                    check(v, val, f"{path}.{k}" if path else k)
                return
            if kind == "dictf":
                if not isinstance(node, dict):
                    raise AGSHError("schema", f"type mismatch at {path or '<root>'}: expected dict, got {type(node).__name__}", got=path)
                for k, tsp in val.items():
                    if k not in node:
                        raise AGSHError("schema", f"missing key at {path or '<root>'}: {k}",
                                        hint=f"required keys: {sorted(val)}", got=sorted(node))
                    check(node[k], tsp, f"{path}.{k}" if path else k)

        check(data, parse(spec), "")
        return data

    # ---- cross-run lease (backlog #2) ----
    def v_lease(self, data, subject, kw):
        """lease NAME — acquire a cross-run advisory lease (flock-backed).
        A second run with the same lease name gets a structured error until the
        holder exits (or is detected dead past ttl=). Host --wait N retries."""
        self.stats["verbs"] += 1
        name = (subject or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise AGSHError("lease", "lease name must be [A-Za-z0-9_-]{1,64}", got=name)
        lock_path = self._check_path(f".{name}.lease", "lease")
        try:
            f = open(lock_path, "a+")
        except OSError as e:
            raise AGSHError("lease", f"cannot create lease file: {e}", got=lock_path)
        deadline = time.time() + float(kw.get("wait", self.lease_wait))
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break  # acquired
            except OSError:
                # crash-safety: steal stale leases whose holder died past ttl
                try:
                    meta = json.load(open(lock_path)) if os.path.getsize(lock_path) else {}
                    holder, ts = meta.get("pid"), float(meta.get("ts", 0))
                    ttl = float(kw.get("ttl", 300))
                    if holder and not os.path.exists(f"/proc/{holder}") and time.time() - ts > ttl:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                except Exception:
                    pass
                if time.time() >= deadline:
                    f.close()
                    raise AGSHError("lease", f"lease held by another run: {name}",
                                    hint="wait for the other run to finish (or host --wait N to retry); stale leases are stolen automatically after ttl=",
                                    got=name)
                time.sleep(0.2)
        try:
            f.seek(0); f.truncate()
            f.write(json.dumps({"pid": os.getpid(), "ts": time.time(), "name": name}))
            f.flush()
        except Exception:
            pass
        self._held_leases.append((name, f))
        self.audit("lease", name, "ok", {"path": lock_path})
        return data  # pass-through

    def release_leases(self):
        for name, f in getattr(self, "_held_leases", []):
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()
        self._held_leases = []

    def v_head(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("head", "input must be a list", got=type(data).__name__)
        try:
            n = int(subject) if subject else 10
        except ValueError:
            raise AGSHError("head", "N must be an integer", hint="usage: head N", got=subject)
        if n <= 0:
            raise AGSHError("head", "N must be >= 1", hint="head -N is ambiguous and rejected; use tail for the end", got=subject)
        return data[:n]

    def v_tail(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("tail", "input must be a list", got=type(data).__name__)
        try:
            n = int(subject) if subject else 10
        except ValueError:
            raise AGSHError("tail", "N must be an integer", hint="usage: tail N", got=subject)
        if n <= 0:
            raise AGSHError("tail", "N must be >= 1", hint="tail -N is ambiguous and rejected", got=subject)
        return data[-n:]

    def v_uniq(self, data, subject, kw):
        self.stats["verbs"] += 1
        if not isinstance(data, list):
            raise AGSHError("uniq", "input must be a list", got=type(data).__name__)
        seen, out = set(), []
        for x in data:
            k = str(x.get(subject)) if isinstance(x, dict) and subject else json.dumps(x, sort_keys=True)
            if k not in seen:
                seen.add(k); out.append(x)
        return out

    def v_regex(self, data, subject, kw):
        self.stats["verbs"] += 1
        toks = (subject or "").split(None, 1)
        if len(toks) != 2:
            raise AGSHError("regex", "usage: regex FIELD PATTERN", got=subject)
        field, pat = toks
        try:
            rx = re.compile(pat)
        except re.error as e:
            raise AGSHError("regex", f"invalid pattern: {e}", hint="escape regex metacharacters or test the pattern separately", got=pat)
        if not isinstance(data, list):
            raise AGSHError("regex", "input must be a list", got=type(data).__name__)
        out = []
        for x in data:
            if isinstance(x, dict):
                x = dict(x)
                m = rx.search(str(x.get(field, "")))
                x[field + "_match"] = m.group(1) if (m and m.groups()) else (m.group(0) if m else None)
            out.append(x)
        return out

    def v_join(self, data, subject, kw):
        """join FILE KEY_A KEY_B - left-merge data list with list from FILE."""
        self.stats["verbs"] += 1
        toks = (subject or "").split()
        if len(toks) != 3:
            raise AGSHError("join", "usage: join FILE KEY_A KEY_B", got=subject)
        path = self._check_path(toks[0], "join")
        if not isinstance(data, list):
            raise AGSHError("join", "input must be a list", got=type(data).__name__)
        if not os.path.exists(path):
            raise AGSHError("join", f"file not found: {toks[0]}", got=toks[0])
        try:
            right = json.load(open(path))
        except json.JSONDecodeError as e:
            raise AGSHError("join", f"joined file is not valid JSON: {e}", got=toks[0])
        if not isinstance(right, list):
            raise AGSHError("join", "joined file must contain a JSON list", got=toks[0])
        ka, kb = toks[1], toks[2]
        index = {}
        for r in right:
            if isinstance(r, dict) and kb in r:
                index.setdefault(str(r[kb]), r)
        out = []
        for x in data:
            if isinstance(x, dict) and ka in x:
                r = index.get(str(x[ka]))
                if r:
                    merged = dict(x); merged.update(r)
                    out.append(merged); continue
            out.append(x)
        return out

    def execute_line(self, line, data=None):
        stages = tokenize(line)
        for toks in stages:
            verb, subject, kw = parse_stage(toks)
            if self._skip and verb != "when":
                self._skipped += 1
                self.stats["skipped_stages"] = self.stats.get("skipped_stages", 0) + 1
                self.audit(verb, subject, "skipped", {"reason": "when"})
                continue
            fn = getattr(self, f"v_{verb}", None)
            if fn is None:
                raise AGSHError(verb, f"unknown verb '{verb}'",
                                hint="available: fetch post read write append filter pick map get let take search run ask checkpoint guard undo resume assert when sort group count head tail uniq regex join llm summarize lines schema lease")
            try:
                data = fn(data, subject, kw)
                if verb not in ("fetch","write","post","run","append","undo","resume"):
                    self.audit(verb, subject, "ok", {"out": len(data) if isinstance(data, list) else None})  # all-stage audit (PR-2, DeryClaw)
            except AGSHError:
                self.audit(verb, subject, "error")
                raise
        return data

def _redact_subject(subject):
    """Plan-time redaction: URLs must not leak embedded credentials
    (https://user:pass@host -> https://user:***@host). Mirrors _url_host's
    acceptance of userinfo; secrets stay out of --plan output."""
    if not subject:
        return subject
    subject = re.sub(r"(https?://[^/@\s:]+:)([^@\s]+)(@)", r"\1***\3", subject)
    subject = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1***", subject, flags=re.I)
    return subject


# risk classes for the informed-permission plan
RISK = {
    "fetch": "network-read", "read": "local-read", "search": "local-read",
    "filter": "pure", "pick": "pure", "map": "pure", "assert": "pure",
    "guard": "pure", "write": "local-write", "append": "local-write",
    "post": "network-write", "run": "exec", "checkpoint": "safe",
    "undo": "mutating", "ask": "human", "resume": "mutating",
    "let": "pure", "take": "pure", "get": "pure",
    # LLM integration (Dery fork): llm = network call to an allowlisted endpoint,
    # summarize = deterministic compression (pure)
    "llm": "network-llm", "summarize": "pure", "lines": "pure",
    # PR2 composition verbs (by @DeryClawbot): pure unless reading a file
    "sort": "pure", "group": "pure", "count": "pure", "head": "pure",
    "tail": "pure", "uniq": "pure", "regex": "pure", "join": "local-read",
    "limit": "pure",  # was documented but never implemented — caught in real use (v0.5)
    "schema": "pure",      # inter-stage shape contract (backlog #4)
    "when": "pure",        # conditional stage-skip (control-flow gap eval, backlog #9)
    "lease": "coordination",  # cross-run advisory lease (backlog #2); host --wait N retries
}

def build_plan(lines):
    stages = []
    for l in lines:
        for toks in tokenize(l):
            verb, subject, kw = parse_stage(toks)
            if not verb: continue
            risk = RISK.get(verb, "unknown")
            # detect sandbox escapes for path-mutating verbs at plan time
            if verb in ("write", "append") and subject:
                try:
                    real = os.path.realpath(subject)
                    root = os.path.realpath(os.getcwd())
                    if not (real == root or real.startswith(root + os.sep)):
                        risk = "sandbox-escape"
                except Exception:
                    pass
            stages.append({"verb": verb, "subject": _redact_subject((subject or ""))[:80],
                           "risk": risk})
    counts = {}
    for s in stages: counts[s["risk"]] = counts.get(s["risk"], 0) + 1
    needs = []
    if counts.get("exec"): needs.append("--allow-run")
    if counts.get("network-write"): needs.append("--approve")
    if counts.get("network-llm"): needs.append("--allow-llm")
    if counts.get("sandbox-escape"):
        verdict = "BLOCKED: escapes sandbox (no flag can approve this)"
        needs = []
    elif needs:
        verdict = "needs approval: " + ", ".join(needs)
    elif counts.get("local-write") or counts.get("mutating"):
        verdict = "runs without flags but MUTATES local state"
    else:
        verdict = "safe to run (all stages read-only/pure)"
    return {"stages": stages, "summary": counts,
            "approval_required": needs, "verdict": verdict}

VERSION = "0.3.1-deryclaw.1"

def main():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(VERSION)
        return
    plan_only = "--plan" in args
    allow_run = "--allow-run" in args or any(a.startswith("--allow-run=") for a in args)
    run_allowlist_host = None
    for a in args:
        if a.startswith("--allow-run="):  # host-level policy: --allow-run=echo,wc
            run_allowlist_host = {x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()}
    approve = "--approve" in args
    allow_private_net = "--allow-private-net" in args
    audit_path = os.environ.get("AGSH_AUDIT")  # env default (review 3 #6); --audit overrides
    if "--audit" in args:
        audit_path = args[args.index("--audit") + 1]
    max_fetch = 20
    if "--max-fetch" in args:
        max_fetch = int(args[args.index("--max-fetch") + 1])
    lease_wait = 0
    if "--wait" in args:
        lease_wait = float(args[args.index("--wait") + 1])  # lease acquisition retry window (backlog #2)
    answer = None
    if "--answer" in args:
        answer = args[args.index("--answer") + 1]
    allow_llm = "--allow-llm" in args
    llm_base = None
    if "--llm-base" in args:
        llm_base = args[args.index("--llm-base") + 1]
    llm_key = None
    if "--llm-key" in args:
        llm_key = args[args.index("--llm-key") + 1]
    llm_model = None
    if "--llm-model" in args:
        llm_model = args[args.index("--llm-model") + 1]
    llm_max_input = 8000
    if "--llm-max-input" in args:
        llm_max_input = int(args[args.index("--llm-max-input") + 1])
    llm_cache = None
    if "--llm-cache" in args:
        llm_cache = args[args.index("--llm-cache") + 1]
    llm_budget = None
    if "--llm-budget" in args:
        llm_budget = int(args[args.index("--llm-budget") + 1])
    rt = Runtime(allow_run=allow_run, approve_network_write=approve, answer=answer,
                 allow_private_net=allow_private_net, audit_path=audit_path,
                 max_fetch=max_fetch, allow_llm=allow_llm, llm_base=llm_base,
                 llm_key=llm_key, llm_model=llm_model, llm_max_input=llm_max_input,
                 run_allowlist_host=run_allowlist_host, llm_cache=llm_cache,
                 llm_budget=llm_budget, lease_wait=lease_wait)
    data = None
    src = sys.stdin.read()
    lines = [l.strip() for l in src.splitlines() if l.strip() and not l.strip().startswith("#")]
    rt.stats["lines"] = len(lines)
    if plan_only:
        print(json.dumps(build_plan(lines), indent=2, ensure_ascii=False))
        return
    for l in lines:
        try:
            data = rt.execute_line(l, data)
        except AGSHError as e:
            rt.release_leases()
            print(json.dumps({"error": {"step": e.step, "message": e.msg,
                                        "hint": e.hint, "got": e.got},
                              "stats": rt.stats}, ensure_ascii=False))
            sys.exit(1)
        except Exception as e:
            rt.release_leases()
            # contract: no tracebacks cross the boundary — even internal bugs
            print(json.dumps({"error": {"step": "?", "message":
                                        f"internal: {e.__class__.__name__}: {e}",
                                        "hint": "report this as a bug — internal error leaked"},
                              "stats": rt.stats}, ensure_ascii=False))
            sys.exit(1)
    rt.release_leases()  # leases are advisory for the run's lifetime; free them on success
    out = {"result": data, "stats": rt.stats}
    print(json.dumps(out, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
