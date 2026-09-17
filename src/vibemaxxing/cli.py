"""Argument parsing and the human renderer. Every command funnels its output
through redact.out / redact.err, so no path prints an unscrubbed byte."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final

from vibemaxxing import __version__, credentials, envelope, history, oauth, store, tui, web
from vibemaxxing.credentials import Credential
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import NeedsLoginError, NetworkError, UsageError, VibeError
from vibemaxxing.httpclient import HttpClient, UrllibClient
from vibemaxxing.keychain import KeychainPort, default_port
from vibemaxxing.models import AccountState
from vibemaxxing.pool import dry_in
from vibemaxxing.redact import err, install_excepthook, out
from vibemaxxing.web import LOOPBACK

DEFAULT_PORT: Final = 8787


@dataclass
class Context:
    root: Path
    client: HttpClient
    port: KeychainPort
    now_s: float
    # A long-lived surface needs a clock, not a snapshot: the dashboard reads this
    # every cycle. A one-shot command reads now_s and never calls it.
    clock: Callable[[], float] = time.time
    prompt: Callable[[str], str] = input
    browser: Callable[[str], bool] = webbrowser.open
    argv0: str = field(default="vibe")


def default_context() -> Context:
    return Context(
        root=store.store_root(),
        client=UrllibClient(),
        port=default_port(),
        now_s=time.time(),
    )


# --- rendering ---------------------------------------------------------------


def _render_accounts(views: Sequence[AccountView], *, now_s: float, dry: float | None) -> str:
    if not views:
        return "no accounts yet — run: vibe add\n"
    lines: list[str] = []
    for view in views:
        marker = "*" if view.active else " "
        who = view.identity.email or view.identity.display_name or "—"
        plan = view.plan or "—"
        lines.append(f"{marker} {view.alias}  {who}  {plan}")
        if view.message:
            lines.append(f"    {view.message}")
        summary = view.summary
        if summary is not None and not summary.rows:
            lines.append("    no limits reported")
        for row in summary.rows if summary else ():
            percent = "—" if row.percent is None else f"{row.percent}%"
            flag = "" if row.severity in (None, "normal") else f"  ({row.severity})"
            lines.append(f"    {row.label:<24}{percent:>5}{flag}")
        lines.append("")
    plural = "" if len(views) == 1 else "s"
    tail = f"pool  {envelope.pool_weeks(views)} account-weeks across {len(views)} account{plural}"
    if dry is not None:
        tail += f"  ·  dry in {dry / 3600:.1f}h"
    lines.append(tail)
    return "\n".join(lines) + "\n"


def _emit(payload: dict[str, object], *, as_json: bool, human: str) -> None:
    out(envelope.dumps(payload) + "\n" if as_json else human)


def _action(action: str, **fields: object) -> dict[str, object]:
    return {"schema": envelope.ENVELOPE_SCHEMA, "ok": True, "action": action, **fields}


# --- commands ----------------------------------------------------------------


def _slug(text: str) -> str:
    # Lower case and strip the leading punctuation store.valid_alias rejects, so
    # an email local part like "_Jan.S" cannot make a bare `vibe add` abort.
    kept = [c if (c.isalnum() or c in "._-") else "-" for c in text.lower()]
    slug = "".join(kept).strip("-._")
    return slug[:64] or "account"


def _free_alias(root: Path, wanted: str) -> str:
    taken = set(store.list_aliases(root))
    if wanted not in taken:
        return wanted
    for n in range(2, 100):
        candidate = f"{wanted}-{n}"
        if candidate not in taken:
            return candidate
    raise UsageError(f'too many accounts named like "{wanted}"', "vibe list")


def _already_stored(root: Path, credential: Credential) -> str | None:
    wanted = credential.refresh_token
    for alias in store.list_aliases(root):
        try:
            if store.read_account(root, alias).credential.refresh_token == wanted:
                return alias
        except VibeError:
            continue  # a broken account file must not block an adopt
    return None


def _adopt(ctx: Context, as_json: bool) -> int:
    blob = ctx.port.read()
    if blob is None:
        raise NeedsLoginError(
            "Claude Code has no login on this machine to adopt",
            "claude /login",
        )
    credential = credentials.parse_blob(blob)
    existing = _already_stored(ctx.root, credential)
    if existing is not None:
        # Adopting twice would put one refresh token in two account files, and
        # the first rotation under either alias silently kills the other.
        _emit(
            _action("add", alias=existing, adopted=True),
            as_json=as_json,
            human=f'that login is already stored as "{existing}"\n',
        )
        return 0
    identity = credentials.read_claude_identity(Path.home() / ".claude.json")
    base = identity.email or identity.display_name or "account"
    alias = _free_alias(ctx.root, _slug(base.split("@", 1)[0]))
    store.write_account(
        ctx.root,
        store.Account(
            alias=alias,
            credential=credential,
            identity=identity,
            state=AccountState.OK,
            message=None,
            added_at=ctx.now_s,
        ),
    )
    store.write_active(ctx.root, alias)
    _emit(
        _action("add", alias=alias, adopted=True),
        as_json=as_json,
        human=f'adopted the login already in Claude Code as "{alias}"\n',
    )
    return 0


def _login(ctx: Context, alias: str, as_json: bool) -> int:
    if not store.valid_alias(alias):
        raise UsageError(
            f'"{alias}" is not a usable alias: letters, digits, dot, dash, underscore',
            "vibe add work",
        )
    verifier, state = oauth.new_verifier(), oauth.new_state()
    url = oauth.build_authorize_url(verifier, state)
    # URL first, then the browser. On Linux, CPython registers text-mode console
    # browsers whenever TERM is set and GenericBrowser.open waits for the child,
    # so a headless login hands the terminal to lynx and blocks there -- with the
    # URL never printed, because printing came after the call.
    err(f"open this to log in:\n\n{url}\n\n")
    if sys.platform == "darwin" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        ctx.browser(url)
    # The prompt goes to stderr: with --json, stdout must be the envelope alone.
    err("paste the code shown in the browser: ")
    paste = ctx.prompt("").strip()
    code, _ = oauth.parse_pasted_code(paste, state)
    credential, identity = oauth.exchange_code(
        ctx.client, code=code, verifier=verifier, state=state
    )
    # A stash from the previous lineage would be consumed as a successor by the
    # next refresh, destroying the credential this login just issued.
    store.delete_stash(ctx.root, alias)
    store.write_account(
        ctx.root,
        store.Account(
            alias=alias,
            credential=credential,
            identity=identity,
            state=AccountState.OK,
            message=None,
            added_at=ctx.now_s,
        ),
    )
    _emit(
        _action("add", alias=alias, adopted=False),
        as_json=as_json,
        human=f'added "{alias}" — run: vibe switch {alias}\n',
    )
    return 0


def cmd_add(args: argparse.Namespace, ctx: Context) -> int:
    if args.alias is None:
        return _adopt(ctx, args.json)
    return _login(ctx, args.alias, args.json)


def _collect_and_record(ctx: Context) -> tuple[list[AccountView], float | None]:
    views = envelope.collect(ctx.root, client=ctx.client, port=ctx.port, now_s=ctx.now_s)
    with closing(history.connect(store.history_path(ctx.root))) as conn:
        history.record(conn, at_s=ctx.now_s, pool=envelope.pool_weeks(views))
        window = history.samples(conn, since_s=ctx.now_s - history.RETENTION_DAYS * 86_400.0)
    forecast = dry_in(window)
    return views, None if forecast is None else forecast.total_seconds()


def cmd_list(args: argparse.Namespace, ctx: Context) -> int:
    views, dry = _collect_and_record(ctx)
    _emit(
        envelope.build(
            views,
            now_s=ctx.now_s,
            dry_in=None if dry is None else timedelta(seconds=dry),
        ),
        as_json=args.json,
        human=_render_accounts(views, now_s=ctx.now_s, dry=dry),
    )
    return 0


def check_loopback(host: str) -> None:
    if host != LOOPBACK:
        raise UsageError(
            f"--host {host} is refused: the dashboard binds loopback ({LOOPBACK}) only, "
            "because it has no authentication and no TLS",
            f"vibe usage web --host {LOOPBACK}",
        )


def cmd_dashboard(args: argparse.Namespace, ctx: Context) -> int:
    if not args.json and not sys.stdin.isatty():
        # Textual would take the alt screen and wait forever for input that is
        # never coming: ssh without -t, cron, a pipeline, a CI step.
        raise UsageError(
            "the dashboard needs a terminal, and stdin is not one",
            "vibe list",
        )
    # Bare `vibe` opens the dashboard; `vibe --json` stays machine-readable.
    return cmd_list(args, ctx) if args.json else tui.run(ctx)


def cmd_usage(args: argparse.Namespace, ctx: Context) -> int:
    if args.mode == "web":
        if args.once:
            raise UsageError(
                "vibe usage web serves continuously; --once fetches and exits",
                "vibe usage --once",
            )
        check_loopback(args.host)
        return web.serve(ctx, port=args.port)
    return cmd_list(args, ctx)


def cmd_switch(args: argparse.Namespace, ctx: Context) -> int:
    store.switch(ctx.root, args.alias, ctx.port)
    _emit(
        _action("switch", alias=args.alias),
        as_json=args.json,
        human=f'Claude Code now uses "{args.alias}"\n',
    )
    return 0


def cmd_remove(args: argparse.Namespace, ctx: Context) -> int:
    store.delete_account(ctx.root, args.alias)
    _emit(
        _action("remove", alias=args.alias),
        as_json=args.json,
        human=f'removed "{args.alias}"\n',
    )
    return 0


def cmd_alias(args: argparse.Namespace, ctx: Context) -> int:
    store.rename_account(ctx.root, args.old, args.new)
    _emit(
        _action("alias", old=args.old, new=args.new),
        as_json=args.json,
        human=f'"{args.old}" is now "{args.new}"\n',
    )
    return 0


def _fresh_token(ctx: Context, alias: str) -> Credential:
    account = store.read_account(ctx.root, alias)
    now_ms = int(ctx.now_s * 1000)
    if store.read_active(ctx.root) == alias:
        # Claude Code refreshes this lineage; refreshing it here too logs it out.
        # With the refresh buffer: the child cannot refresh, so a token with
        # seconds left would fail inside it.
        token, problem = envelope.active_token(
            ctx.root, account, ctx.port, now_ms=now_ms, buffer_ms=credentials.EXPIRY_BUFFER_MS
        )
        if token is None:
            raise VibeError(problem, "claude")
        return token
    if credentials.login_lapsed(account.credential, now_ms=now_ms):
        raise NeedsLoginError(f'the login for "{alias}" has lapsed', f"vibe add {alias}")
    if not credentials.is_expired(account.credential, now_ms=now_ms):
        return account.credential
    outcome = oauth.refresh(ctx.root, alias, account.credential, ctx.client, now_ms=now_ms)
    if outcome.credential is not None:
        return outcome.credential
    if outcome.error == "active":
        raise UsageError(f'"{alias}" became the active account', f"vibe run {alias} -- ...")
    if outcome.error in ("invalid_grant", "no_refresh_token"):
        raise NeedsLoginError(f'the refresh token for "{alias}" is dead', f"vibe add {alias}")
    # transient, busy, invalid_client: the login is fine, the attempt was not, so
    # sending the user to a browser login here would be wrong advice.
    raise NetworkError(
        f'could not refresh "{alias}" right now ({outcome.error})',
        f"vibe run {alias} -- ...",
    )


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    # Only the leading separator is ours; a `--` the child itself needs must
    # survive into its argv.
    command = args.command[1:] if args.command[:1] == ["--"] else list(args.command)
    if not command:
        raise UsageError("vibe run needs a command after --", "vibe run work -- claude")
    credential = _fresh_token(ctx, args.alias)
    # The fifth and last permitted reveal() site: the child's environment. The
    # global Keychain credential is untouched, so other shells keep their account.
    child_env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": credential.access_token.reveal()}
    try:
        completed = subprocess.run(command, env=child_env, check=False)
    except KeyboardInterrupt:
        # The child got the same SIGINT and is already shutting down. Report
        # the conventional 130 rather than a traceback from the wrapper.
        return 130
    if args.json:
        out(envelope.dumps(_action("run", alias=args.alias, exit_code=completed.returncode)) + "\n")
    return completed.returncode


# --- parser ------------------------------------------------------------------

HELP: Final = """vibe — several Claude Code accounts, one pooled view of the plan limits.

Getting started
  vibe add                    adopt the login Claude Code already has here (no browser)
  vibe add work               log in to another account; opens a browser, you paste a code
  vibe switch work            point Claude Code at that account, then run `claude` normally

Every day
  vibe                        the dashboard, in the terminal
  vibe list                   each account's limits and the pooled headroom
  vibe run work -- claude     one command on one account; the active login is untouched
  vibe usage web              the same dashboard in a browser, http://127.0.0.1:8787

Housekeeping
  vibe alias old new          rename an account
  vibe remove work            forget an account (the login itself is not revoked)
  vibe usage --once           fetch the numbers once and print them
  vibe --version              the installed version

switch or run?
  `switch` is global and lasts: every shell, until you switch again. It swaps the
  credential Claude Code itself reads, so a session started afterwards is on the new
  account -- one already running is not.
  `run` is one command only, on a token handed to that child process alone. Use it to
  borrow headroom from another account without disturbing what you are logged in as.

Good to know
  Limits are per account. `vibe list` shows session, weekly-all-models and weekly-Fable,
  plus how many account-weeks the pool has left and roughly when it runs dry.
  Accounts share one ~/.claude history and one MCP config -- only the credential is
  swapped, exactly as if you had logged out and in by hand.
  `--json` works on every command and emits the envelope documented in docs/CONTRACT.md.
  The web dashboard refetches at most every 3 minutes; `vibe list` always fetches now."""


def _add_json(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    # Only the root parser carries a default. A subparser default would overwrite
    # `vibe --json list` back to False after the root had already set it, so the
    # caller asked for JSON and silently got human text.
    parser.add_argument(
        "--json",
        action="store_true",
        default=False if root else argparse.SUPPRESS,
        help="emit the machine-readable envelope",
    )


def cmd_help(args: argparse.Namespace, ctx: Context) -> int:
    out(HELP + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe",
        description="Manage several Claude Code accounts and see pooled plan usage.",
        epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_json(parser, root=True)
    parser.set_defaults(handler=cmd_dashboard, alias=None, mode=None)
    subs = parser.add_subparsers(dest="command_name")

    add = subs.add_parser("add", help="adopt the current Claude Code login, or log in fresh")
    add.add_argument("alias", nargs="?", help="omit to adopt the login already in Claude Code")
    _add_json(add)
    add.set_defaults(handler=cmd_add)

    listing = subs.add_parser("list", help="show every account and the pooled headroom")
    _add_json(listing)
    listing.set_defaults(handler=cmd_list)

    switch = subs.add_parser("switch", help="make one account the active Claude Code login")
    switch.add_argument("alias")
    _add_json(switch)
    switch.set_defaults(handler=cmd_switch)

    run = subs.add_parser("run", help="run one command pinned to one account")
    run.add_argument("alias")
    run.add_argument("command", nargs=argparse.REMAINDER)
    _add_json(run)
    run.set_defaults(handler=cmd_run)

    remove = subs.add_parser("remove", help="forget an account")
    remove.add_argument("alias")
    _add_json(remove)
    remove.set_defaults(handler=cmd_remove)

    rename = subs.add_parser("alias", help="rename an account")
    rename.add_argument("old")
    rename.add_argument("new")
    _add_json(rename)
    rename.set_defaults(handler=cmd_alias)

    use = subs.add_parser("usage", help="fetch usage once, or serve the web dashboard")
    use.add_argument(
        "mode", nargs="?", choices=["web"], help="omit to fetch once; 'web' serves the dashboard"
    )
    use.add_argument(
        "--once", action="store_true", help="fetch once and print (the default without 'web')"
    )
    use.add_argument("--host", default=LOOPBACK)
    use.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_json(use)
    use.set_defaults(handler=cmd_usage)

    # `vibe help` is what people type; without it argparse answers an unhelpful
    # "invalid choice: 'help'" and lists the commands it just refused to explain.
    helping = subs.add_parser("help", help="what each command is for, and when to use it")
    _add_json(helping)
    helping.set_defaults(handler=cmd_help)

    return parser


def main(argv: Sequence[str] | None = None, *, context: Context | None = None) -> int:
    install_excepthook()
    args = build_parser().parse_args(argv)
    try:
        ctx = context if context is not None else default_context()
        handler: Callable[[argparse.Namespace, Context], int] = args.handler
        return handler(args, ctx)
    except VibeError as exc:
        if args.json:
            out(envelope.dumps(envelope.error_payload(exc)) + "\n")
        else:
            err(exc.render() + "\n")
        return exc.exit_code
