# Release checklist

Everything below the line marked **PUBLISH** is reversible. Everything at or after it is
not: a PyPI version number can never be reused, and a public repo's history is public for
good. Nothing in this repository publishes on its own — the workflow in
`.github/workflows/publish.yml` fires only on a GitHub release being *published*.

Status as of 2026-09-16: `main` is green, the wheel builds and installs clean, the name
`vibemaxxing` is unclaimed on PyPI, and the repository is still private.

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
- [ ] **Decide the version.** `pyproject.toml` still says `0.1.0`, and the tag `v0.1.0`
      already exists locally with different content. Everything since is unreleased, so
      cut **`0.2.0`**: bump `version` in `pyproject.toml`, move the `[Unreleased]` block
      in `CHANGELOG.md` under `## [0.2.0] - <date>`, commit, tag `v0.2.0`.
- [ ] **Re-read `SECURITY.md` and the README's "Known limitations".** Once this is public,
      those two sections are the entire basis on which someone decides whether to trust it
      with their tokens. They are honest right now. Keep them that way.

## 2. Should do before anyone sees it

- [ ] **Decide what to do about the address in the history.** The working tree is clean:
      `docs/RUNBOOK.md` was a transcript of a real session and carried
      `you@example.com` on eight lines; it now reads `you@example.com`, and local
      paths read `/Users/you`. But the original text is still in commit `e78bf7d`, and
      going public publishes that commit. Three options:

      1. **Accept it.** It is one gmail address you own, in a docs file, not a secret.
         Cheapest, and most projects would not notice.
      2. **Rewrite the history** before going public, while nobody has cloned it:
         `git filter-repo --replace-text` over the two strings, then a force-push. Clean
         result, rewrites every SHA. I have not done this: it needs a force-push, which
         I do not do without you saying so explicitly.
      3. **Squash the pre-release history** into one initial commit. Loses the build
         story, which is genuinely interesting in this repo's case.

      `gitleaks` runs over the whole history in CI and is clean — but it looks for
      secrets, not for your own name. The author email on every commit is already the
      GitHub noreply address.
- [ ] **Confirm the screenshot is the demo one.** `docs/media/dashboard.png` must show
      `you@example.com` / `side@example.com` / `team@example.com`, never your accounts.
      Regenerate any time with `uv run python scripts/demo_dashboard.py`.
- [ ] **Set the repo's topics and homepage** so it is findable:
      ```
      gh repo edit jskarbecki/vibemaxxing \
        --homepage https://pypi.org/project/vibemaxxing/ \
        --add-topic claude --add-topic claude-code --add-topic anthropic \
        --add-topic cli --add-topic developer-tools --add-topic account-switcher
      ```
- [ ] **Turn on branch protection for `main`** once other people can open PRs: require the
      CI check, and require a PR rather than a direct push.
- [ ] **Enable private vulnerability reporting.** Settings → Code security. `SECURITY.md`
      links to that form and the link 404s until it is on.
- [ ] **Add a `CODE_OF_CONDUCT.md`** if you want one. GitHub will suggest Contributor
      Covenant and generate it in two clicks. Optional, and a real commitment to enforce.
- [ ] **Pin the GitHub Actions to commit SHAs** rather than tags (`actions/checkout@v4`
      etc.). Standard supply-chain hygiene for a public repo; Dependabot can do it.
- [ ] **Decide on Dependabot.** One runtime dependency, so low value, but it is free:
      `.github/dependabot.yml` for `uv` and `github-actions`.

## 3. Test the whole path first, on TestPyPI

This is the rehearsal, and it is free. Cut a **prerelease** — the workflow's `pypi` job is
gated on `github.event.release.prerelease == false`, so a prerelease goes to TestPyPI and
stops there.

- [ ] Tag and release `v0.2.0rc1` as a **pre-release** on GitHub.
- [ ] Watch the `TestPyPI` job go green.
- [ ] Install it clean and run it:
      ```
      uv tool install --index-url https://test.pypi.org/simple/ \
        --extra-index-url https://pypi.org/simple/ vibemaxxing==0.2.0rc1
      vibe help && vibe list
      ```
- [ ] Open <https://test.pypi.org/project/vibemaxxing/> and check the README renders and
      the screenshot loads. **The image only appears once the repo is public** — it points
      at `raw.githubusercontent.com`. Expect it broken here, and check it again after.

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
