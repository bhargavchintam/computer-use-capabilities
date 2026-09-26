"""Every `uv run cua ...` command in the README must parse against the real CLI (docs stay runnable).

Commands are parsed down to the leaf command, which validates option names, required arguments and
types, without invoking anything.
"""

from __future__ import annotations

import shlex
from typing import Any

import pytest
import typer

from cua.cli import app
from tests.conftest import ROOT


def readme_commands() -> list[str]:
    lines = (ROOT / "README.md").read_text().splitlines()
    cmds, buf = [], ""
    for raw in lines:
        line = raw.split("  #")[0].rstrip()  # drop trailing comments
        if buf:
            buf += " " + line.rstrip("\\").strip()
        elif line.lstrip().startswith("uv run cua "):
            buf = line.strip().rstrip("\\").strip()
        else:
            continue
        if not line.endswith("\\"):
            cmds.append(buf)
            buf = ""
    return cmds


def parse(argv: list[str]) -> Any:
    """Walk group -> subcommand like the CLI would, validating each level; never invokes a command.
    (Typer vendors its own click, so groups are recognised by behaviour, not by class.)"""
    cmd: Any = typer.main.get_command(app)
    ctx = cmd.make_context("cua", list(argv))
    while hasattr(cmd, "resolve_command"):
        pending = [*getattr(ctx, "_protected_args", getattr(ctx, "protected_args", [])), *ctx.args]
        name, sub, rest = cmd.resolve_command(ctx, pending)
        assert sub is not None and name is not None, f"no subcommand in {argv}"
        cmd, ctx = sub, sub.make_context(name, rest, parent=ctx)
    return ctx


COMMANDS = readme_commands()


def test_the_readme_has_a_command_for_every_part_of_the_demo_path() -> None:
    joined = "\n".join(COMMANDS)
    for sub in (
        "cua discover",
        "cua replay",
        "cua capabilities approve",
        "cua catalog ask",
        "cua mock fault",
    ):
        assert sub in joined, sub


@pytest.mark.parametrize(
    "argv",
    [
        ["catalog", "export", "pinecrest"],
        ["replay", "x", "--tenant", "pinecrest", "--bogus"],
        ["catalog", "ask", "q"],
    ],
)
def test_the_parser_rejects_broken_commands(argv: list[str]) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any usage error from the vendored click
        parse(argv)


@pytest.mark.parametrize("command", COMMANDS)
def test_readme_command_parses(command: str) -> None:
    argv = shlex.split(command)[3:]  # drop "uv run cua"
    parse(argv)
