"""The package works with `garminconnect` absent, as in a default install without the extra `connect`.

Each case runs in a fresh interpreter where `sys.modules["garminconnect"] = None`, which makes
`import garminconnect` raise ImportError. A synthetic export is imported and read back.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from test_garmin_export import _write_export

PRELUDE = """
import sys
sys.modules["garminconnect"] = None
"""


def run_python(code: str, home, *args: str) -> subprocess.CompletedProcess[str]:
    env = {"FARTLEK_HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, "-c", PRELUDE + textwrap.dedent(code), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


@pytest.fixture
def export_zip(tmp_path):
    return _write_export(tmp_path / "export.zip")


def test_the_cli_and_the_server_import_without_garminconnect(tmp_path):
    result = run_python(
        """
        import fartlek.cli as cli
        import fartlek.mcp_server.server as server
        assert cli.Garmin is None
        assert "garminconnect" not in [m for m, v in sys.modules.items() if v is not None]
        print(len(server.mcp._tool_manager.list_tools()))
        """,
        tmp_path / "home",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "16"


def test_import_then_brief_work_without_garminconnect(tmp_path, export_zip):
    result = run_python(
        """
        import asyncio
        from fartlek.cli import main
        from fartlek.mcp_server.context import ToolContext
        from fartlek.mcp_server.tools import brief

        sys.argv = ["fartlek", "import", sys.argv[1], "--since", "2026-01-01"]
        try:
            main()
        except SystemExit as exit_:
            assert exit_.code in (0, None), exit_.code

        async def read():
            ctx = ToolContext()
            await ctx.ensure_ready()
            assert ctx.source_kind == "export"
            print("BRIEF", (await brief.run(ctx))[:60].replace("\\n", " | "))

        asyncio.run(read())
        """,
        tmp_path / "home",
        str(export_zip),
    )
    assert result.returncode == 0, result.stderr
    assert "Done. Data through 2026-03-10" in result.stdout
    assert "BRIEF" in result.stdout


def test_auth_explains_the_missing_extra_instead_of_crashing(tmp_path):
    result = run_python(
        """
        from fartlek.cli import main
        sys.argv = ["fartlek", "auth"]
        main()
        """,
        tmp_path / "home",
    )
    assert result.returncode == 1
    assert "extra `connect`" in result.stderr and "fartlek import" in result.stderr
    assert "Traceback" not in result.stderr
