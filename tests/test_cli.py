from argparse import Namespace

from lex.cli import AGENT_NAME_RE, allocate_agent_name, build_session_fingerprint, cmd_agent_identify, main, normalize_agent_role, resolve_install_options
from lex.dashboard import load_dashboard_state
from lex.db import connect, ensure_workspace, initialize_database
from lex.installer import InstallContext, install_scaffold
from lex.merge_workflow import apply_proposal, create_merge_packet, unified_diff


def test_agent_name_pattern_accepts_expected_shape():
    assert AGENT_NAME_RE.match("codex-brisk-otter")
    assert AGENT_NAME_RE.match("claude-steady-ibis")


def test_agent_name_pattern_rejects_other_shapes():
    assert not AGENT_NAME_RE.match("codex")
    assert not AGENT_NAME_RE.match("codex-brisk")


def test_agent_name_pattern_accepts_cursor_shape():
    assert AGENT_NAME_RE.match("cursor-quiet-lynx")


def test_allocate_agent_name_picks_unused_candidate(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    conn.execute("INSERT INTO agents (name, kind, status) VALUES ('codex-brisk-badger', 'codex', 'active')")
    conn.commit()

    assert allocate_agent_name(conn, "codex") == "codex-brisk-falcon"


def test_agent_identify_registers_generated_name(tmp_path, capsys):
    cmd_agent_identify(
        Namespace(
            root=str(tmp_path),
            name=None,
            kind="codex",
            role="dev",
            specialty="frontend",
            json=False,
        )
    )

    out = capsys.readouterr().out
    assert "identified codex-" in out

    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    row = conn.execute("SELECT name, kind, role, specialty FROM agents").fetchone()
    assert row["kind"] == "codex"
    assert row["name"].startswith("codex-")
    assert row["role"] == "dev"
    assert row["specialty"] == "frontend"


def test_agent_role_command_updates_role(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    conn.execute("INSERT INTO agents (name, kind, role, specialty, status) VALUES ('codex-brisk-otter', 'codex', '', '', 'active')")
    conn.commit()

    main(["--root", str(tmp_path), "agent", "role", "codex-brisk-otter", "auditor", "--specialty", "security"])

    row = conn.execute("SELECT role, specialty FROM agents WHERE name = 'codex-brisk-otter'").fetchone()
    assert row["role"] == "auditor"
    assert row["specialty"] == "security"


def test_normalize_agent_role_rejects_legacy_labels():
    try:
        normalize_agent_role("tech_lead")
    except SystemExit as exc:
        assert "invalid agent role" in str(exc)
    else:
        raise AssertionError("expected invalid role to raise SystemExit")


def test_agent_identify_rejects_unknown_specialty_until_added(tmp_path):
    try:
        cmd_agent_identify(
            Namespace(
                root=str(tmp_path),
                name=None,
                kind="codex",
                role="dev",
                specialty="mobile",
                json=False,
            )
        )
    except SystemExit as exc:
        assert "invalid agent specialty" in str(exc)
    else:
        raise AssertionError("expected invalid specialty to raise SystemExit")


def test_custom_specialty_can_be_added_then_used(tmp_path):
    main(["--root", str(tmp_path), "specialty", "add", "mobile"])
    cmd_agent_identify(
        Namespace(
            root=str(tmp_path),
            name=None,
            kind="codex",
            role="dev",
            specialty="mobile",
            json=False,
        )
    )

    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    row = conn.execute("SELECT role, specialty FROM agents").fetchone()
    assert row["role"] == "dev"
    assert row["specialty"] == "mobile"


def test_dx_command_dispatches_to_dx_runner(tmp_path, monkeypatch):
    called = {}

    def fake_run_dx(root):
        called["root"] = root

    monkeypatch.setattr("lex.cli.run_dx", fake_run_dx)

    main(["--root", str(tmp_path), "dx"])

    assert called["root"] == tmp_path.resolve()


def test_build_session_fingerprint_returns_hash_and_label():
    fingerprint, label = build_session_fingerprint(kind="codex", cwd="/tmp/demo")

    assert len(fingerprint) == 16
    assert ":" in label


def test_install_merge_preserves_existing_agent_files(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Existing\n\nKeep this.\n", encoding="utf-8")
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Claude\n\nKeep that too.\n", encoding="utf-8")

    result = install_scaffold(
        tmp_path,
        agent_files="merge",
        ignore_policy="none",
        ignore_target="gitignore",
    )

    agents_text = agents.read_text(encoding="utf-8")
    claude_text = claude.read_text(encoding="utf-8")
    assert "Keep this." in agents_text
    assert "Keep that too." in claude_text
    assert "<!-- lex:begin -->" in agents_text
    assert "<!-- lex:begin -->" in claude_text
    assert "AGENTS.md" in result.updated_files
    assert "CLAUDE.md" in result.updated_files


def test_install_skip_leaves_existing_agent_files_unchanged(tmp_path):
    agents = tmp_path / "AGENTS.md"
    original = "# Existing\n"
    agents.write_text(original, encoding="utf-8")

    result = install_scaffold(
        tmp_path,
        agent_files="preserve",
        ignore_policy="runtime",
        ignore_target="gitignore",
    )

    assert agents.read_text(encoding="utf-8") == original
    assert any("left unchanged" in warning for warning in result.warnings)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".lex/lex.db" in gitignore
    assert ".lex/runtime/" in gitignore


def test_install_overwrite_replaces_existing_agent_files(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Existing\n\nKeep this.\n", encoding="utf-8")

    result = install_scaffold(
        tmp_path,
        agent_files="overwrite",
        ignore_policy="none",
        ignore_target="gitignore",
    )

    agents_text = agents.read_text(encoding="utf-8")
    assert "Keep this." not in agents_text
    assert "Read `.lex/adapters/codex/AGENTS.md` before starting work." in agents_text
    assert any("Overwrote root AGENTS.md and CLAUDE.md" in warning for warning in result.warnings)


def test_install_all_policy_uses_local_exclude_and_ignores_created_bridges(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)

    result = install_scaffold(
        tmp_path,
        agent_files="merge",
        ignore_policy="all",
        ignore_target="local-exclude",
    )

    exclude_text = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".lex/" in exclude_text
    assert "AGENTS.md" in exclude_text
    assert "CLAUDE.md" in exclude_text
    assert "AGENTS.md" in result.created_files
    assert "CLAUDE.md" in result.created_files


def test_resolve_install_options_uses_flags_in_non_interactive_mode(tmp_path):
    args = Namespace(
        non_interactive=True,
        agent_files="preserve",
        ignore_policy="all",
        ignore_target="local-exclude",
        assisted_agent="claude",
    )
    context = InstallContext(
        root=tmp_path,
        has_git_dir=True,
        has_gitignore=True,
        has_agents_file=True,
        has_claude_file=True,
    )

    assert resolve_install_options(args, context) == ("preserve", "all", "local-exclude", "claude")


def test_resolve_install_options_prompts_for_existing_repo(monkeypatch, tmp_path):
    answers = iter(["2", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    args = Namespace(
        non_interactive=False,
        agent_files="merge",
        ignore_policy="runtime",
        ignore_target="gitignore",
        assisted_agent="codex",
    )
    context = InstallContext(
        root=tmp_path,
        has_git_dir=True,
        has_gitignore=True,
        has_agents_file=True,
        has_claude_file=False,
    )

    assert resolve_install_options(args, context) == ("merge", "all", "local-exclude", "codex")


def test_resolve_install_options_prompts_for_assisted_agent(monkeypatch, tmp_path):
    answers = iter(["3", "1", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    args = Namespace(
        non_interactive=False,
        agent_files="merge",
        ignore_policy="runtime",
        ignore_target="gitignore",
        assisted_agent="codex",
    )
    context = InstallContext(
        root=tmp_path,
        has_git_dir=True,
        has_gitignore=False,
        has_agents_file=True,
        has_claude_file=True,
    )

    assert resolve_install_options(args, context) == ("assisted", "runtime", "gitignore", "claude")


def test_merge_packet_and_apply_workflow(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    paths = create_merge_packet(tmp_path, agent_kind="codex")
    proposal = paths.proposal_dir / "AGENTS.md"
    proposal.write_text("# Proposed\n", encoding="utf-8")

    diff_text = unified_diff(tmp_path / "AGENTS.md", proposal, "AGENTS.md")
    assert "Proposed" in diff_text

    applied = apply_proposal(tmp_path)
    assert "AGENTS.md" in applied
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# Proposed\n"


def test_merge_plan_cli_command_creates_packet(tmp_path, capsys):
    from lex.cli import main

    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    main(["--root", str(tmp_path), "merge", "plan", "--agent", "codex"])

    out = capsys.readouterr().out
    assert "created assisted merge packet" in out
    assert (tmp_path / ".lex" / "runtime" / "install-merge-plan.md").exists()
    assert (tmp_path / ".lex" / "runtime" / "install-merge-context" / "AGENTS.original.md").exists()


def test_install_assisted_mode_preserves_files_and_creates_packet(tmp_path):
    from lex.cli import main

    (tmp_path / "AGENTS.md").write_text("# Existing\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    main(["--root", str(tmp_path), "install", "--non-interactive", "--agent-files", "assisted",
          "--ignore-policy", "runtime", "--ignore-target", "gitignore", "--assisted-agent", "claude"])

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# Existing\n"
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "# Claude\n"
    assert (tmp_path / ".lex" / "runtime" / "install-merge-plan.md").exists()
    assert (tmp_path / ".lex" / "runtime" / "install-merge-context" / "AGENTS.original.md").exists()


def test_dashboard_state_loads_empty_workspace(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    state = load_dashboard_state(tmp_path)

    assert state.root == tmp_path
    assert state.summary == {
        "blocked_tasks": 0,
        "dirty_sessions": 0,
        "review_needed_tasks": 0,
        "risky_tasks": 0,
        "sessions_needing_attention": 0,
        "stale_sessions": 0,
    }
    assert state.agents == []
    assert state.sessions == []
    assert state.tasks == []
