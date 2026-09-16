# Release checklist

Everything below the line marked **PUBLISH** is reversible. Everything at or after it is
not: a PyPI version number can never be reused, and a public repo's history is public for
good. Nothing in this repository publishes on its own — the workflow in
`.github/workflows/publish.yml` fires only on a GitHub release being *published*.

Status as of 2026-09-16: the history rewrite is pushed and verified clean on GitHub,
`v0.2.0rc1` is published to TestPyPI and was installed and exercised from that index,
`v0.2.0` is tagged and waiting. The name `vibemaxxing` is still unclaimed on PyPI and the
repository is still private. **Two things remain before the repo can go public: the
`pypi` environment, and the decision itself.**

---

## 1. Blockers — it cannot ship without these

- [ ] **Create the `pypi` environment on the repo.** Only `testpypi` exists today, so the
      PyPI job would fail at the environment gate.
      Settings → Environments → New environment → name it exactly `pypi`.
      Add yourself as a required reviewer if you want a manual gate on every upload.
- [ ] **Register the PyPI trusted publisher.** No API token is stored anywhere and none
      should be. On <https://pypi.org/manage/account/publishing/>, add a *pending*
      publisher:
      - PyPI project name: `vibemaxxing`
      - Owner: `jskarbecki`  ·  Repository: `vibemaxxing`
      - Workflow: `publish.yml`  ·  Environment: `pypi`
- [x] **Version cut.** `0.2.0`, tagged `v0.2.0` and pushed. `uv.lock` records the
      project's own version and CI runs `uv sync --locked` first, so the lockfile moves
      with `pyproject.toml` in the same commit; leaving it behind fails the release run
      at its first step.
- [ ] **Re-read `SECURITY.md` and the README's "Known limitations".** Once this is public,
      those two sections are the entire basis on which someone decides whether to trust it
      with their tokens. They are honest right now. Keep them that way.

## 2. Should do before anyone sees it

- [x] **The history was rewritten.** An earlier `docs/RUNBOOK.md` was a transcript of a
      real session and carried a personal gmail address and a real home directory path
      across several commits. `git filter-repo` replaced both throughout the history on
      2026-09-16 and the result was force-pushed. `jan@intra-ai.de` is deliberately kept:
      it is the maintainer contact in `pyproject.toml` and `SECURITY.md`.

- [x] **Screenshot confirmed the demo one.** Shows `you@example.com` / `side@example.com`
      / `team@example.com`, never real accounts. Regenerate with
      `uv run python scripts/demo_dashboard.py`.
- [x] **Topics and homepage set.** `claude`, `claude-code`, `anthropic`, `cli`,
      `developer-tools`, `account-switcher`, `oauth`; homepage points at the PyPI page.
- [ ] **Turn on branch protection for `main`** once other people can open PRs: require the
      CI check, and require a PR rather than a direct push.
- [ ] **Enable private vulnerability reporting.** Settings → Code security. `SECURITY.md`
      links to that form and the link 404s until it is on. The API returns 404 while the
      repository is private, so this one has to wait until after it goes public.
- [ ] **Add a `CODE_OF_CONDUCT.md`** if you want one. GitHub will suggest Contributor
      Covenant and generate it in two clicks. Optional, and a real commitment to enforce.
- [ ] **Pin the GitHub Actions to commit SHAs** rather than tags (`actions/checkout@v4`
      etc.). Standard supply-chain hygiene for a public repo; Dependabot can do it.
- [ ] **Decide on Dependabot.** One runtime dependency, so low value, but it is free:
      `.github/dependabot.yml` for `uv` and `github-actions`.

## 3. Test the whole path first, on TestPyPI — DONE 2026-09-16

- [x] Released `v0.2.0rc1` as a pre-release. Run `35125662123`: **TestPyPI success, PyPI
      skipped**, which is the gate behaving exactly as written.
- [x] Installed it from the TestPyPI index into a clean venv and exercised it:

      | check | result |
      |---|---|
      | `vibe --version` | `0.2.0rc1` |
      | `vibe list`, empty HOME | `no accounts yet — run: vibe add`, exit 0 |
      | `vibe list --json`, empty HOME | valid envelope, `resets: []` |
      | `vibe add`, no Claude Code login | names the problem and `claude /login`, exit 3 |
      | `vibe help` | renders |
      | `vibe usage web` | `GET / -> 200`, page served from package data |
      | against the real accounts | 5 accounts, plans, pooled total and all 5 resets |

      `--extra-index-url https://pypi.org/simple/` is required: TestPyPI has no `textual`.

- [ ] Open <https://test.pypi.org/project/vibemaxxing/> and look at the page. The
      screenshot will be broken there and that is expected — it points at
      `raw.githubusercontent.com` on a repository that is still private. It resolves the
      moment the repo goes public. Check it again after.

---

## PUBLISH — the irreversible part

Do these in this order. Making the repo public *before* the PyPI upload means the raw
screenshot URL resolves by the time anyone reads the package page.

- [ ] **Make the repository public.** Settings → General → Danger Zone → Change visibility.
- [ ] **Publish the GitHub release** for `v0.2.0`, not marked as a prerelease. This is the
      button that ships: it runs TestPyPI *and* PyPI.
- [ ] Watch both jobs. Confirm <https://pypi.org/project/vibemaxxing/> exists and the
      screenshot renders.
- [ ] Install from the real index on a machine that has never seen the source:
      `uv tool install vibemaxxing && vibe help`

## 4. Telling people

Do not post everywhere at once. Pick one, watch what breaks, fix it, then do the next.
The first hour of a launch is when you find out what the README failed to explain.

- [ ] **Anthropic's Discord** and any Claude Code community channel. Highest signal per
      minute: the people there have the exact problem this solves, and they will tell you
      quickly if it does not work on their setup.
- [ ] **r/ClaudeAI**. Lead with the problem, not the tool. A title like "I got tired of
      /logout /login to switch between my two Max accounts, so I built this" does better
      than the project name, which means nothing to anyone yet.
- [ ] **Hacker News**, Show HN. Post it yourself, in the morning US time, and be at the
      keyboard for the next few hours to answer. Expect the top comment to be about
      storing tokens in plaintext — the honest answer is already in the README, so link
      it rather than arguing.
- [ ] **X / Bluesky**, with the screenshot. The dashboard image is the thing people stop
      for; lead with it.
- [ ] **Awesome lists**: `awesome-claude-code` and similar. One PR each, low effort, and
      they keep sending traffic long after a launch post has scrolled away.

Have ready before you post:

- [ ] A one-sentence description you are happy to repeat. Current: *"Run several Claude
      Code accounts from one machine, switch in a second, and see all their plan limits in
      one place."*
- [ ] An answer to "is this against Anthropic's terms?" You should decide what you
      actually believe here before someone asks it in public. The tool automates a login
      flow a user is entitled to perform, against accounts they pay for, and stores the
      result the same way Claude Code does. That is the argument; make sure you are
      comfortable with it.
- [ ] An answer to "why should I trust you with my tokens?" — the README's plaintext-store
      limitation, the three-host allowlist, and the fact that every output is scrubbed.

## 5. The week after

- [ ] Watch the issue tracker for the first real bug on hardware you do not have. Linux
      and the `~/.claude/.credentials.json` path are the least-exercised code.
- [ ] `vibe run` cannot refresh its own token, and someone will hit it in a long session.
      Decide whether that becomes a fix or stays a documented limitation.
- [ ] If it gets traction, the switching-a-running-session gap is the most-requested thing
      you will hear about. It is currently documented as a limitation and not implemented.
