"""Argument parsing and the human renderer. Every command funnels its output
through redact.out / redact.err, so no path prints an unscrubbed byte."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from vibemaxxing import __version__, credentials, envelope, history, oauth, store
from vibemaxxing.credentials import Credential
from vibemaxxing.envelope import AccountView
from vibemaxxing.errors import NeedsLoginError, UsageError, VibeError
from vibemaxxing.httpclient import HttpClient, UrllibClient
from vibemaxxing.keychain import KeychainPort, default_port
from vibemaxxing.models import AccountState
from vibemaxxing.pool import dry_in
from vibemaxxing.redact import err, install_excepthook, out

LOOPBACK: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8787


@dataclass
class Context:
    root: Path
    client: HttpClient
    port: KeychainPort
    now_s: float
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
    tail = f"pool  {envelope.pool_weeks(views)} account-weeks across {len(views)} accounts"
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
    kept = [c if (c.isalnum() or c in "._-") else "-" for c in text]
    slug = "".join(kept).strip("-.")
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


def _adopt(ctx: Context, as_json: bool) -> int:
    blob = ctx.port.read()
    if blob is None:
        raise NeedsLoginError(
            "Claude Code has no login on this machine to adopt",
            "claude /login",
        )
    credential = credentials.parse_blob(blob)
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
    ctx.browser(url)
    err(f"opening {url}\n\nif the browser did not open, paste that URL yourself.\n")
    paste = ctx.prompt("paste the code shown in the browser: ").strip()
    code, _ = oauth.parse_pasted_code(paste, state)
    credential, identity = oauth.exchange_code(
        ctx.client, code=code, verifier=verifier, state=state
    )
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
    views = envelope.collect(ctx.root, client=ctx.client, now_s=ctx.now_s)
    with closing(history.connect(store.history_path(ctx.root))) as conn:
        history.record(conn, at_s=ctx.now_s, pool=envelope.pool_weeks(views))
        window = history.samples(conn, since_s=ctx.now_s - history.RETENTION_DAYS * 86_400.0)
    forecast = dry_in(window)
    return views, None if forecast is None else forecast.total_seconds()


def cmd_list(args: argparse.Namespace, ctx: Context) -> int:
    views, dry = _collect_and_record(ctx)
    _emit(
        envelope.build(views, now_s=ctx.now_s, dry_in=None),
        as_json=args.json,
        human=_render_accounts(views, now_s=ctx.now_s, dry=dry),
    )
    return 0


def check_loopback(host: str) -> None:
    if host != LOOPBACK:
        raise UsageError(
            f"--host {host} is refused: the dashboard binds loopback ({LOOPBACK}) only, "
            "because it has no authentication and no TLS",
            f"vibe usage --web --host {LOOPBACK}",
        )


def cmd_usage(args: argparse.Namespace, ctx: Context) -> int:
    if args.web:
        check_loopback(args.host)
        raise VibeError("the web dashboard ships in v0.0.5", "vibe usage --once")
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
    if credentials.login_lapsed(account.credential, now_ms=now_ms):
        raise NeedsLoginError(f'the login for "{alias}" has lapsed', f"vibe add {alias}")
    if not credentials.is_expired(account.credential, now_ms=now_ms):
        return account.credential
    outcome = oauth.refresh(ctx.root, alias, account.credential, ctx.client, now_ms=now_ms)
    if outcome.credential is None:
        raise NeedsLoginError(f'could not refresh "{alias}" ({outcome.error})', f"vibe add {alias}")
    return outcome.credential


def cmd_run(args: argparse.Namespace, ctx: Context) -> int:
    command = [part for part in args.command if part != "--"]
    if not command:
        raise UsageError("vibe run needs a command after --", "vibe run work -- claude")
    credential = _fresh_token(ctx, args.alias)
    # The fifth and last permitted reveal() site: the child's environment. The
    # global Keychain credential is untouched, so other shells keep their account.
    child_env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": credential.access_token.reveal()}
    completed = subprocess.run(command, env=child_env, check=False)
    if args.json:
        out(envelope.dumps(_action("run", alias=args.alias, exit_code=completed.returncode)) + "\n")
    return completed.returncode


# --- parser ------------------------------------------------------------------


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit the machine-readable envelope")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe",
        description="Manage several Claude Code accounts and see pooled plan usage.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _add_json(parser)
    parser.set_defaults(handler=cmd_list, alias=None, web=False)
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
    group = use.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="fetch once and print")
    group.add_argument("--web", action="store_true", help="serve the dashboard on loopback")
    use.add_argument("--host", default=LOOPBACK)
    use.add_argument("--port", type=int, default=DEFAULT_PORT)
    _add_json(use)
    use.set_defaults(handler=cmd_usage)

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
