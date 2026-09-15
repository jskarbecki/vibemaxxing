# Runbook

Numbered, followable without reading any source. Every command below was executed on the
development machine (macOS 26, Darwin 25.5, Python 3.13.5) and the output under it is the
**real** output, pasted. Where a step needs a second account that does not exist yet, it
says so instead of showing invented output.

---

## 1. Install

```
uv tool install vibemaxxing
```

From a checkout instead:

```
$ cd ~/development/vibemaxxing && uv sync
$ uv run vibe --version
0.1.0
```

Two console scripts, the same program: `vibemaxxing` and `vibe`.

---

## 2. Add the first account

`vibe add` with no alias adopts the login Claude Code already has. It makes **no network
request**, so it works offline.

```
$ vibe add
adopted the login already in Claude Code as "intra.ai.muc2"
```

The alias comes from the email local part. Rename it if you want something shorter:

```
$ vibe alias intra.ai.muc2 work
"intra.ai.muc2" is now "work"
```

Check what landed, and its modes:

```
$ ls -la ~/.vibemaxxing ~/.vibemaxxing/accounts
drwx------@  - jan 15 Sep 13:47 accounts
.rw-------@ 14 jan 15 Sep 13:47 active

/Users/jan/.vibemaxxing/accounts:
.rw-------@ 1.1k jan 15 Sep 13:47 intra.ai.muc2.json
```

Every file `0600`, every directory `0700`. The account file holds your tokens in plaintext
JSON — that is deliberate and it is why the modes matter.

---

## 3. Add the second account

This one opens a browser. There is no localhost callback: the browser lands on a page
showing a string, and you paste it back.

```
vibe add personal
```

It prints the authorize URL first, then opens your browser. Log in as the **second**
account, copy the code the page shows, and paste it at the prompt. The code looks like
`<code>#<state>`; paste the whole thing.

> **Not yet run.** This needs a second Claude subscription account to be logged in, which
> is a human gate. When it runs, `vibe list` below shows two rows instead of one.

---

## 4. See pooled usage

```
$ vibe list
* intra.ai.muc2  intra.ai.muc2@gmail.com  max
    Session                   23%
    Weekly · all models       79%  (warning)
    Weekly · Fable            77%  (warning)

pool  0.21 account-weeks across 1 account
```

`*` marks the account Claude Code is using right now. The pool is the sum of every
account's remaining **weekly** headroom, so `0.21` means about a fifth of one account-week
left in total. With the second account added it would be roughly `1.2`.

Machine-readable, the same document the web dashboard serves:

```
$ vibe list --json
{
  "schema": 1,
  "generated_at": "2026-09-15T11:47:45+00:00",
  "accounts": [
    {
      "alias": "intra.ai.muc2",
      "active": true,
      "state": "ok",
      "message": null,
      "email": "intra.ai.muc2@gmail.com",
      "display_name": "Intra AI",
      "organization": "intra.ai.muc2@gmail.com's Organization",
      "plan": "max",
      "updated_at": "2026-09-15T11:47:45+00:00",
      "rows": [
        {"kind": "session", "label": "Session", "percent": 23,
         "severity": "normal", "resets_at": "2026-09-15T15:40:00.689597+00:00"},
        {"kind": "weekly_all", "label": "Weekly · all models", "percent": 79,
         "severity": "warning", "resets_at": "2026-09-19T13:00:00.689628+00:00"},
        {"kind": "weekly_scoped", "label": "Weekly · Fable", "percent": 77,
         "severity": "warning", "resets_at": "2026-09-19T12:59:59.690035+00:00"}
      ],
      "breakdown": [
        {"label": "Claude Code", "percent": 100},
        {"label": "Chats", "percent": 0},
        {"label": "Cowork", "percent": 0},
        {"label": "Other", "percent": 0}
      ]
    }
  ],
  "pool": {"accounts": 1, "remaining_account_weeks": 0.21, "dry_in_seconds": null}
}
```

`dry_in_seconds` is `null` until there are two samples far enough apart to measure a burn
rate from. It fills in on its own.

---

## 5. Switch, and confirm it took effect

**Back up the live credential first.** This is the one command in this runbook that
overwrites something Claude Code owns.

```
/usr/bin/security find-generic-password -a "$(whoami)" -w -s "Claude Code-credentials" \
  > ~/vibemaxxing-keychain-restore.json
chmod 600 ~/vibemaxxing-keychain-restore.json
```

Confirm you can read it back before going further:

```
$ python3 -c "import json;print(sorted(json.load(open('$HOME/vibemaxxing-keychain-restore.json'))))"
['claudeAiOauth', 'mcpOAuth']
```

Then switch and confirm:

```
vibe switch personal
vibe list          # the * has moved to personal
claude -p "say ok" # Claude Code now runs as the second account
```

To confirm it took effect without spending a request, compare the fingerprint of the
Keychain item against the account file:

```
python3 - <<'EOF'
import json, subprocess, hashlib, getpass, pathlib
live = subprocess.run(["/usr/bin/security","find-generic-password","-a",getpass.getuser(),
                       "-w","-s","Claude Code-credentials"],
                      capture_output=True, text=True, check=True).stdout.rstrip("\n")
tok = json.loads(live)["claudeAiOauth"]["accessToken"]
print("keychain token sha256:", hashlib.sha256(tok.encode()).hexdigest()[:16])
for f in sorted(pathlib.Path.home().joinpath(".vibemaxxing/accounts").glob("*.json")):
    t = json.loads(f.read_text())["credential"]["accessToken"]
    print(f"{f.stem:20} sha256:", hashlib.sha256(t.encode()).hexdigest()[:16])
EOF
```

The Keychain's fingerprint matches exactly one account file: the one you switched to.

The scripted round trip — switch away, verify, switch back, verify, and restore from the
backup on any failure:

```
uv run python scripts/verify_switch_live.py \
  --restore ~/vibemaxxing-keychain-restore.json --a work --b personal
```

> **Not yet run.** Needs the second account from step 3.

To pin one shell instead of switching globally:

```
vibe run personal -- claude
```

The global credential is untouched, so another terminal keeps using `work`.

---

## 6. Open both dashboards

Terminal dashboard — this is also what bare `vibe` does:

```
vibe
```

`q` quits, `r` refreshes now. It repaints every 180 s.

Web dashboard:

```
$ vibe usage web
dashboard on http://127.0.0.1:8787 — ctrl-c to stop
```

Open http://127.0.0.1:8787. It binds loopback and refuses anything else:

```
$ vibe usage web --host 0.0.0.0
--host 0.0.0.0 is refused: the dashboard binds loopback (127.0.0.1) only, because it has no authentication and no TLS
  run: vibe usage web --host 127.0.0.1
$ echo $?
2
```

The three routes, checked live:

```
$ curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8788/
200 text/html; charset=utf-8
$ curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8788/api/usage
200 application/json
$ curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8788/nope
404 text/plain; charset=utf-8

$ curl -s -D- -o /dev/null http://127.0.0.1:8788/api/usage
HTTP/1.0 200 OK
Content-Type: application/json
Content-Length: 1479
Cache-Control: no-store
```

---

## 7. Run the test suite

```
$ uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
All checks passed!
39 files already formatted
Success: no issues found in 19 source files
93 passed in 14.48s
```

Every test runs offline. A test that reaches for the real `~/.claude`, the real
`~/.vibemaxxing`, or the real Keychain is failed by name by an autouse guard.

---

## 8. Check the dashboard's memory after an hour

Find the python process, not the `uv run` wrapper:

```
$ pgrep -f "vibemaxxing/.venv/bin/python3.*vibe"
6685
$ ps -o rss= -p 6685
 31984
```

**What number to expect: about 31 MB, and flat.** Measured on this machine, 200 requests
apart:

```
RSS before soak: 31.2 MB
200 requests against /api/usage and / ...
RSS after 200 requests: 31.2 MB
upstream requests actually made (cache TTL 180s): 1
```

Flat to the kilobyte, and the 100 `/api/usage` hits collapsed to **one** upstream request.
The terminal dashboard is held to the same standard by a test: 200 refresh cycles grow the
traced heap by under 1 MB and leave the live widget count unchanged.

If you come back after an hour and RSS has climbed past ~40 MB, that is a bug worth
reporting — nothing in either dashboard is supposed to accumulate.

---

## 9. Confirm no credential leaks

The tokens live in exactly one place on disk. Everything else must be clean.

```
$ TOK=$(python3 -c "import json,glob;print(json.load(open(glob.glob('$HOME/.vibemaxxing/accounts/*.json')[0]))['credential']['accessToken'])")
$ vibe list --json > /tmp/j.txt; vibe list > /tmp/h.txt
$ python3 - "$TOK" <<'EOF'
import sys, base64, json, pathlib
tok = sys.argv[1]
forms = [tok, base64.b64encode(tok.encode()).decode(), json.dumps(tok)[1:-1]]
for name in ("/tmp/j.txt", "/tmp/h.txt"):
    raw = pathlib.Path(name).read_text()
    print(f"{name}: token present in any form = {any(f in raw for f in forms)}")
print("access token length:", len(tok), "(never printed)")
EOF
/tmp/j.txt: token present in any form = False
/tmp/h.txt: token present in any form = False
access token length: 108 (never printed)
```

Sweep the whole store, exempting the account files that are supposed to hold it:

```
python3 - <<'EOF'
import base64, glob, json, pathlib
root = pathlib.Path.home() / ".vibemaxxing"
toks = []
for f in glob.glob(str(root / "accounts" / "*.json")):
    c = json.load(open(f))["credential"]
    toks += [c["accessToken"], c["refreshToken"]]
forms = [f for t in toks for f in (t, base64.b64encode(t.encode()).decode())]
bad = []
for p in root.rglob("*"):
    if p.is_file() and p.parent.name != "accounts":
        raw = p.read_bytes()
        if any(f.encode() in raw for f in forms):
            bad.append(str(p))
print("files outside accounts/ holding a token:", bad or "none")
EOF
```

`none` is the expected answer, including for `history.db`, the stash and the claim files.

One leak is real and documented rather than fixed: `vibe switch` passes the credential to
`/usr/bin/security` in `argv`, and macOS shows any local user a process's argv through
`ps`. See **Known limitations** in the README for why every alternative was worse.

---

## 10. Uninstall, and delete everything it created

```
uv tool uninstall vibemaxxing
rm -rf ~/.vibemaxxing
rm -f ~/vibemaxxing-keychain-restore.json
```

Confirm nothing is left:

```
$ ls -la ~/.vibemaxxing 2>&1
ls: /Users/jan/.vibemaxxing: No such file or directory
$ command -v vibe vibemaxxing || echo "both scripts gone"
```

`~/.vibemaxxing` is the only directory it ever creates. Your Claude Code login is
untouched and whichever account was active stays active — if you want a specific one,
`vibe switch` to it **before** uninstalling.
