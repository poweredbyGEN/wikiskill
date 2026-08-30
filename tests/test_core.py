import json
import pathlib
import subprocess
import sys
import threading

import pytest

from wikiskill.cli import main
from wikiskill.core import (
    Project,
    WikiSkillError,
    _graphify_args,
    _group_lock,
    _group_score,
    _pending_sessions,
    _record_sessions,
    _score,
    apply_maintenance,
    build_candidate,
    call_codex,
    canonical_root,
    config_home,
    configure_group,
    context,
    data_home,
    evolve,
    gate,
    identity,
    ingest,
    initialize,
    load_registry,
    recent_sessions,
    register,
    run,
    stable_root,
    state_dir,
)
from wikiskill.text import _apply_edits, _read_wiki, _trace_text, redact


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))


def git(path: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, text=True, check=True, stdout=subprocess.PIPE
    ).stdout.strip()


def repo(path: pathlib.Path) -> pathlib.Path:
    git(path, "init", "-b", "main")
    git(path, "remote", "add", "origin", "https://code.example/acme/widget.git")
    (path / "nested").mkdir()
    return path


def test_subfolders_map_to_one_repository(tmp_path):
    root = repo(tmp_path)
    assert canonical_root(root / "nested") == root
    assert identity(root) == "code.example/acme/widget"
    assert state_dir("backend") != state_dir("frontend")


def test_identity_includes_the_forge_host(tmp_path):
    root = repo(tmp_path)
    git(root, "remote", "set-url", "origin", "https://other.example/acme/widget.git")
    assert identity(root) == "other.example/acme/widget"


def test_registration_from_worktree_persists_the_main_checkout(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    repo(main)
    git(
        main,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    worktree = tmp_path / "linked"
    git(main, "worktree", "add", "-b", "linked", str(worktree))
    assert stable_root(canonical_root(worktree)) == main.resolve()
    assert pathlib.Path(register(str(worktree)).root) == main.resolve()


def test_empty_or_relative_xdg_paths_stay_outside_the_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    assert config_home() == pathlib.Path.home() / ".config/wikiskill"
    assert data_home() == pathlib.Path.home() / ".local/share/wikiskill"


def test_registration_rejects_data_storage_inside_checkout(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(root / ".data"))

    with pytest.raises(WikiSkillError, match="outside the repository"):
        register(str(root))


def test_patch_requires_exact_target():
    assert (
        _apply_edits(
            "alpha\n", [{"op": "replace", "target": "alpha", "content": "beta"}]
        )
        == "beta\n"
    )
    with pytest.raises(WikiSkillError):
        _apply_edits(
            "alpha\n", [{"op": "replace", "target": "missing", "content": "beta"}]
        )
    with pytest.raises(WikiSkillError):
        _apply_edits(
            "replacement already appears here\n",
            [{"op": "replace", "target": "missing", "content": "replacement"}],
        )
    assert (
        _apply_edits(
            "alpha\nreplacement\nomega\n",
            [{"op": "replace", "target": "missing", "content": "replacement"}],
        )
        == "alpha\nreplacement\nomega\n"
    )
    assert (
        _apply_edits(
            "alpha\n  replacement\nomega\n",
            [{"op": "replace", "target": "missing", "content": "  replacement"}],
        )
        == "alpha\n  replacement\nomega\n"
    )
    for edit in (
        {"op": "replace", "target": "old", "content": "old and new"},
        {"op": "insert_after", "target": "# A", "content": "\nnew"},
    ):
        original = "old\n" if edit["op"] == "replace" else "# A\nrest\n"
        once = _apply_edits(original, [edit])
        assert _apply_edits(once, [edit]) == once
    assert (
        _apply_edits(
            "# first\nold\n# second\nnew\n",
            [{"op": "replace", "target": "old", "content": "new"}],
        )
        == "# first\nnew\n# second\nnew\n"
    )
    assert (
        _apply_edits(
            "# A\nfirst\n# A\nadded\nsecond\n",
            [{"op": "insert_after", "target": "# A", "content": "\nadded"}],
        )
        == "# A\nadded\nfirst\n# A\nadded\nsecond\n"
    )


def test_redacts_common_secret_shapes():
    value = redact(
        "Authorization: Bearer abc123\n"
        "Authorization: Basic dXNlcjpwYXNz\n"
        "Cookie: sessionid=cookie-secret\n"
        "AWS_SECRET_ACCESS_KEY=secret-value\n"
        '{"api_key": "json-secret"}\n'
        'PASSWORD="correct horse battery staple"\n'
        "postgres://user:db-password@example.test/db\n" + "ghp_" + "a" * 26
    )
    assert "abc123" not in value
    assert "dXNlcjpwYXNz" not in value
    assert "cookie-secret" not in value
    assert "secret-value" not in value
    assert "json-secret" not in value
    assert "horse battery staple" not in value
    assert "db-password" not in value
    assert "ghp_" not in value


def test_redacts_modern_github_and_temporary_aws_tokens():
    value = redact("github_pat_" + "A" * 82 + " ASIA" + "B" * 16)

    assert value == "<redacted> <redacted>"


def test_nightly_warms_deja_once_before_project_queries(monkeypatch):
    events = []
    projects = {
        "code.example/acme/one": Project(
            "code.example/acme/one", "backend", "/one", "one"
        ),
        "code.example/acme/two": Project(
            "code.example/acme/two", "backend", "/two", "two"
        ),
    }
    monkeypatch.setattr(
        "wikiskill.cli.run",
        lambda args, timeout: events.append((args, timeout)),
    )
    monkeypatch.setattr("wikiskill.cli.load_registry", lambda: projects)
    monkeypatch.setattr(
        "wikiskill.cli.evolve",
        lambda project, _since, _quiet: events.append(project.project_id) or {},
    )

    assert main(["nightly"]) == 0
    assert events == [
        (["deja", "warmup"], 1800),
        "code.example/acme/one",
        "code.example/acme/two",
    ]


def test_command_errors_redact_credentials():
    with pytest.raises(WikiSkillError) as caught:
        run(["bash", "-lc", "echo 'Authorization: Bearer secret-value' >&2; exit 2"])
    assert "secret-value" not in str(caught.value)


def test_register_writes_state_outside_repo(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root / "nested"), group_id="backend", deja_project="widget")
    assert project.project_id == "code.example/acme/widget"
    assert project.group_id == "backend"
    assert state_dir(project.group_id).is_dir()
    assert not (root / ".wikiskill").exists()
    assert (state_dir(project.group_id).stat().st_mode & 0o777) == 0o700
    assert (
        (state_dir(project.group_id) / "state.json").stat().st_mode & 0o777
    ) == 0o600


def test_repositories_in_one_group_share_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repo(first)
    repo(second)
    git(
        second,
        "remote",
        "set-url",
        "origin",
        "https://code.example/tools/infra.git",
    )
    first_project = register(str(first), deja_project="widget")
    second_project = register(str(second), deja_project="infra")
    assert first_project.group_id == second_project.group_id == "default"
    assert initialize(first_project) == initialize(second_project)


def test_reregistering_one_repo_can_update_group_settings(tmp_path):
    root = repo(tmp_path)
    register(str(root))
    updated = register(str(root), validator_command="validate")
    assert updated.validator_command == "validate"
    assert (
        json.loads((state_dir("default") / "state.json").read_text())["best_score"]
        is None
    )


def test_register_publishes_membership_while_holding_group_lock(tmp_path, monkeypatch):
    root = repo(tmp_path)
    real_save = __import__("wikiskill.core", fromlist=["save_registry"]).save_registry
    checked = []

    def save_while_locked(projects):
        with (
            pytest.raises(WikiSkillError, match="already running"),
            _group_lock(state_dir("default")),
        ):
            pass
        checked.append(True)
        real_save(projects)

    monkeypatch.setattr("wikiskill.core.save_registry", save_while_locked)
    register(str(root))
    assert checked == [True]


def test_configure_group_updates_every_member_and_resets_baseline(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repo(first)
    repo(second)
    git(
        second,
        "remote",
        "set-url",
        "origin",
        "https://code.example/tools/infra.git",
    )
    register(str(first), deja_project="widget")
    register(str(second), deja_project="infra")
    state = state_dir("default") / "state.json"
    payload = json.loads(state.read_text())
    payload["best_score"] = 0.9
    state.write_text(json.dumps(payload))

    updated = configure_group(
        str(first), model="gpt-test", validator_command="validate"
    )

    assert [project.project_id for project in updated] == [
        "code.example/acme/widget",
        "code.example/tools/infra",
    ]
    members = [
        project for project in load_registry().values() if project.group_id == "default"
    ]
    assert {project.model for project in members} == {"gpt-test"}
    assert {project.validator_command for project in members} == {"validate"}
    assert json.loads(state.read_text())["best_score"] is None


def test_configure_group_does_not_publish_when_baseline_reset_fails(
    tmp_path, monkeypatch
):
    root = repo(tmp_path)
    register(str(root))
    registry = __import__("wikiskill.core", fromlist=["registry_path"]).registry_path()
    before = registry.read_text()
    real_write = __import__("wikiskill.core", fromlist=["_atomic_json"])._atomic_json

    def fail_state(path, value):
        if path.name == "state.json":
            raise OSError("state failed")
        real_write(path, value)

    monkeypatch.setattr("wikiskill.core._atomic_json", fail_state)
    with pytest.raises(OSError, match="state failed"):
        configure_group(str(root), model="other")
    assert registry.read_text() == before


def test_invalid_group_is_rejected(tmp_path):
    root = repo(tmp_path)
    with pytest.raises(WikiSkillError, match="invalid group"):
        register(str(root), group_id="bad/group")


def test_register_refuses_a_shared_deja_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repo(first)
    repo(second)
    git(second, "remote", "set-url", "origin", "https://code.example/acme/two.git")
    register(str(first), deja_project="shared")
    with pytest.raises(WikiSkillError, match="already used"):
        register(str(second), deja_project="shared")


def test_evolve_rejects_a_replaced_registered_checkout(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root))
    git(root, "remote", "set-url", "origin", "https://code.example/acme/other.git")
    with pytest.raises(WikiSkillError, match="identity changed"):
        evolve(project)


def test_evolve_reloads_group_settings_under_the_lock(tmp_path, monkeypatch):
    root = repo(tmp_path)
    stale = register(str(root), model="old")
    configure_group(str(root), model="new")
    seen = []
    monkeypatch.setattr(
        "wikiskill.core._evolve_locked",
        lambda project, *_args: seen.append(project.model) or {"outcome": "unchanged"},
    )
    assert evolve(stale) == {"outcome": "unchanged"}
    assert seen == ["new"]


def test_pending_sessions_uses_paper_sample_and_leaves_remainder(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root))
    project_state = initialize(project)
    sessions = [
        {
            "id": f"failure-{index}",
            "gave_up": True,
            "updated": f"2026-08-{index + 1:02d}",
        }
        for index in range(7)
    ]
    sessions.extend(
        [
            {
                "id": f"other-{index}",
                "gave_up": False,
                "updated": f"2026-07-{index + 1:02d}",
            }
            for index in range(5)
        ]
    )
    _record_sessions(project, sessions)
    selected = _pending_sessions(project, project_state, {"failure-6"})
    ids = {session["id"] for session in selected}
    assert len(ids) == 8
    assert (
        len([session_id for session_id in ids if session_id.startswith("failure")]) == 5
    )
    assert (
        len([session_id for session_id in ids if session_id.startswith("other")]) == 3
    )


def test_pending_sessions_stay_with_their_originating_repo(tmp_path):
    root = repo(tmp_path)
    project = register(str(root), deja_project="widget")
    project_state = initialize(project)
    _record_sessions(project, [{"id": "widget-one", "updated": "2026-08-29"}])
    _record_sessions(
        Project(
            project_id="code.example/tools/infra",
            group_id="default",
            root=str(root),
            deja_project="infra",
        ),
        [{"id": "infra-one", "updated": "2026-08-29"}],
    )
    assert [
        item["id"] for item in _pending_sessions(project, project_state, set())
    ] == ["widget-one"]


def test_raw_snapshots_are_redacted_and_integrity_checked(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root))
    project_state = initialize(project)
    _record_sessions(
        project,
        [
            {
                "id": "one",
                "updated": "2026-08-29",
                "messages": [{"text": "API_KEY=secret"}],
            }
        ],
    )
    manifest = json.loads((project_state / "raw/manifest.json").read_text())
    snapshot = project_state / "raw" / manifest["sessions"]["one"]["snapshot"]
    assert "secret" not in snapshot.read_text()
    snapshot.write_text('{"id":"tampered"}')
    with pytest.raises(WikiSkillError, match="hash mismatch"):
        _pending_sessions(project, project_state, set())


def test_resumed_session_creates_new_pending_evidence(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root), deja_project="widget")
    first = {
        "id": "resumed",
        "project": "widget",
        "updated": "2026-01-01T00:00:00+00:00",
        "messages": [{"text": "first"}],
    }
    _record_sessions(project, [first])
    manifest_path = state_dir("default") / "raw/manifest.json"
    first_digest = json.loads(manifest_path.read_text())["sessions"]["resumed"][
        "sha256"
    ]
    second = {
        **first,
        "updated": "2026-01-02T00:00:00+00:00",
        "messages": [{"text": "first"}, {"text": "resumed"}],
    }

    def deja(args, timeout=180):
        if args[0] == "last":
            return {"sessions": [{"id": "resumed", "updated": second["updated"]}]}
        return {"session": second}

    monkeypatch.setattr("wikiskill.core._deja_json", deja)
    refreshed = recent_sessions(project, "365d", quiet_minutes=0)
    assert refreshed == [second]
    _record_sessions(project, refreshed)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["sessions"]["resumed"]["sha256"] != first_digest
    assert [
        session["messages"][-1]["text"]
        for session in _pending_sessions(project, state_dir("default"), {first_digest})
    ] == ["resumed"]


def test_trace_and_wiki_budgets_keep_final_evidence_and_patterns(tmp_path):
    trace = _trace_text(
        {
            "id": "long",
            "messages": [
                {"role": "user", "text": "start " + "x" * 20_000},
                {"role": "assistant", "text": "FINAL VERIFIED"},
            ],
        }
    )
    assert len(trace) == 15_000
    assert "FINAL VERIFIED" in trace

    root = repo(tmp_path)
    project_state = initialize(register(str(root)))
    (project_state / "wiki/index.md").write_text("INDEX\n" + "i" * 30_000)
    (project_state / "wiki/patterns/critical.md").write_text(
        "PATTERN START\n" + "p" * 70_000 + "\nPATTERN END"
    )
    (project_state / "wiki/log.md").write_text("old\n" + "l" * 30_000 + "\nRECENT LOG")
    (project_state / "wiki/skill-impact.md").write_text(
        "old\n" + "s" * 40_000 + "\nRECENT REJECTION"
    )
    wiki = _read_wiki(project_state)
    assert len(wiki) <= 100_000
    assert "PATTERN START" in wiki and "PATTERN END" in wiki
    assert "RECENT LOG" in wiki
    assert "RECENT REJECTION" in wiki


def test_maintenance_failure_keeps_live_wiki_unchanged(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project_state = initialize(register(str(root)))
    before = (project_state / "wiki/index.md").read_text()

    def fail_write(*_args):
        raise OSError("simulated write failure")

    monkeypatch.setattr("wikiskill.core._write_private_text", fail_write)
    with pytest.raises(OSError):
        apply_maintenance(
            project_state,
            {
                "create_patterns": [],
                "update_patterns": [],
                "update_index": "# changed",
                "append_log": "changed",
            },
        )
    assert (project_state / "wiki/index.md").read_text() == before
    assert not list(project_state.glob("wiki.new-*"))


def test_maintenance_replay_is_idempotent(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project_state = initialize(register(str(root)))
    result = {
        "create_patterns": [{"name": "retry.md", "content": "# Retry"}],
        "update_patterns": [],
        "update_index": "# Index",
        "append_log": "maintained session one",
    }
    apply_maintenance(project_state, result)
    apply_maintenance(project_state, result)
    assert (project_state / "wiki/log.md").read_text().count(
        "maintained session one"
    ) == 1


def test_maintenance_rejects_duplicate_pattern_updates(tmp_path):
    root = repo(tmp_path)
    project_state = initialize(register(str(root)))
    (project_state / "wiki/patterns/retry.md").write_text("# Retry\nold\n")
    result = {
        "create_patterns": [],
        "update_patterns": [
            {
                "name": "retry.md",
                "edits": [{"op": "replace", "target": "old", "content": "one"}],
            },
            {
                "name": "retry.md",
                "edits": [{"op": "replace", "target": "old", "content": "two"}],
            },
        ],
        "update_index": "# Index",
        "append_log": "duplicate",
    }
    with pytest.raises(WikiSkillError, match="duplicate pattern update"):
        apply_maintenance(project_state, result)
    assert (project_state / "wiki/patterns/retry.md").read_text() == "# Retry\nold\n"


def test_initialize_recovers_interrupted_directory_swaps(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root))
    project_state = initialize(project)

    (project_state / "wiki/index.md").write_text("# preserved\n")
    (project_state / "wiki.new-abandoned").mkdir()
    (project_state / "wiki").rename(project_state / "wiki.old-interrupted")
    initialize(project)
    assert (project_state / "wiki/index.md").read_text() == "# preserved\n"
    assert not (project_state / "wiki.new-abandoned").exists()

    (project_state / "skills/old").mkdir()
    (project_state / "skills/old/SKILL.md").write_text("old")
    (project_state / "skills").rename(project_state / "skills.old")
    (project_state / "skills/new").mkdir(parents=True)
    (project_state / "skills/new/SKILL.md").write_text("new")
    (project_state / "skills.transaction.json").write_text('{"score": 0.9}')
    initialize(project)
    assert (project_state / "skills/old/SKILL.md").read_text() == "old"
    assert not (project_state / "skills/new").exists()


def test_context_recovers_an_interrupted_skill_swap(tmp_path):
    root = repo(tmp_path)
    project = register(str(root))
    project_state = initialize(project)
    approved = project_state / "skills/approved"
    approved.mkdir()
    (approved / "SKILL.md").write_text("approved")
    (project_state / "skills").rename(project_state / "skills.old")
    assert context(project).endswith("approved\n")
    assert (project_state / "skills/approved/SKILL.md").exists()


def test_group_lock_rejects_overlapping_evolution(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project_state = initialize(register(str(root)))
    with (
        _group_lock(project_state),
        pytest.raises(WikiSkillError, match="already running"),
        _group_lock(project_state),
    ):
        pass


def test_ingest_uses_the_group_lock(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root))
    project_state = initialize(project)
    monkeypatch.setattr("wikiskill.core.recent_sessions", lambda *_args: [])
    with (
        _group_lock(project_state),
        pytest.raises(WikiSkillError, match="already running"),
    ):
        ingest(project)


def test_validator_timeout_is_a_wikiskill_error(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root))

    class TimedOut:
        pid = 123
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls < 3:
                raise subprocess.TimeoutExpired("validator", timeout)
            return "", ""

    killed = []
    captured = {}

    def popen(*_args, **kwargs):
        captured.update(kwargs)
        return TimedOut()

    monkeypatch.setattr("wikiskill.core.subprocess.Popen", popen)
    monkeypatch.setattr(
        "wikiskill.process.os.killpg", lambda pid, sig: killed.append((pid, sig.name))
    )
    with pytest.raises(WikiSkillError, match="timed out"):
        _score("validator", project, tmp_path)
    assert killed == [(123, "SIGTERM"), (123, "SIGKILL")]
    assert captured["stdout"] is not subprocess.PIPE
    assert captured["stderr"] is not subprocess.PIPE


def test_validator_group_is_stopped_when_parent_is_interrupted(tmp_path, monkeypatch):
    project = register(str(repo(tmp_path)))

    class Interrupted:
        pid = 123
        returncode = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return None, None

    killed = []
    monkeypatch.setattr(
        "wikiskill.core.subprocess.Popen", lambda *_a, **_k: Interrupted()
    )
    monkeypatch.setattr(
        "wikiskill.process.os.killpg", lambda pid, sig: killed.append((pid, sig.name))
    )
    with pytest.raises(KeyboardInterrupt):
        _score("validator", project, tmp_path)
    assert killed == [(123, "SIGTERM")]


def test_validator_rejects_nonnumeric_json_score(tmp_path):
    root = repo(tmp_path)
    validator = tmp_path / "validator.py"
    validator.write_text("print('{\"score\": null}')\n")
    project = register(str(root))
    with pytest.raises(WikiSkillError, match="JSON score"):
        _score(f"{sys.executable} {validator}", project, tmp_path)


@pytest.mark.parametrize("raw_score", ["true", '"0.5"', "NaN"])
def test_validator_rejects_coercible_or_nonfinite_scores(tmp_path, raw_score):
    root = repo(tmp_path)
    validator = tmp_path / "validator.py"
    validator.write_text(f"print('{{\"score\": {raw_score}}}')\n")
    project = register(str(root))
    with pytest.raises(WikiSkillError, match="JSON score"):
        _score(f"{sys.executable} {validator}", project, tmp_path)


def test_graphify_config_exposes_only_the_selected_server(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers."graphify-backend--one"]\n'
        'url = "https://graphs.example.com/one/mcp"\n'
        'bearer_token_env_var = "GRAPH_TOKEN"\n'
        '[mcp_servers."unrelated"]\n'
        'url = "http://unrelated.test/mcp"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    args = _graphify_args("graphify-backend--one", "backend", "graphs.example.com")
    rendered = " ".join(args)
    assert "graphs.example.com" in rendered
    assert "GRAPH_TOKEN" in rendered
    assert "unrelated" not in rendered
    with pytest.raises(WikiSkillError, match="wrong group endpoint"):
        _graphify_args("graphify-backend--one", "backend", "other.example.com")


def test_recent_sessions_reads_every_unseen_session_in_window(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root), deja_project="widget")
    listed = [
        {"id": f"session-{index}", "updated": "2026-01-01T00:00:00+00:00"}
        for index in range(150)
    ]
    calls = []

    def deja(args, timeout=180):
        calls.append(args)
        if args[0] == "last":
            return {"sessions": listed}
        session_id = args[1]
        return {
            "session": {
                "id": session_id,
                "project": "widget",
                "updated": "2026-01-01T00:00:00+00:00",
            }
        }

    monkeypatch.setattr("wikiskill.core._deja_json", deja)
    sessions = recent_sessions(project, "365d", quiet_minutes=0)
    assert len(sessions) == 150
    assert int(calls[0][1]) > 150


def test_recent_sessions_skips_already_ingested_details(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root), deja_project="widget")
    _record_sessions(
        project,
        [{"id": "known", "updated": "2026-01-01T00:00:00+00:00"}],
    )
    calls = []

    def deja(args, timeout=180):
        calls.append(args)
        return {"sessions": [{"id": "known", "updated": "2026-01-01T00:00:00+00:00"}]}

    monkeypatch.setattr("wikiskill.core._deja_json", deja)
    assert recent_sessions(project, "365d", quiet_minutes=0) == []
    assert len(calls) == 1


def test_codex_runs_without_source_checkout_or_shell_tools(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root), graphify_mcp="")
    iteration = tmp_path / "iteration"
    captured = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        output = pathlib.Path(args[args.index("--output-last-message") + 1])
        output.write_text('{"answer": "ok"}')
        return ""

    monkeypatch.setattr("wikiskill.core.run", fake_run)
    assert call_codex(project, "prompt", {}, iteration) == {"answer": "ok"}
    args = captured["args"]
    assert "--ignore-user-config" in args
    assert "--model" not in args
    assert args[args.index("--cd") + 1] == str(iteration)
    assert project.root not in args
    disabled = [
        args[index + 1] for index, value in enumerate(args) if value == "--disable"
    ]
    assert {
        "browser_use",
        "code_mode_host",
        "computer_use",
        "shell_tool",
        "unified_exec",
        "view_image",
    } <= set(disabled)


def test_graphify_is_exposed_only_to_the_proposer(tmp_path, monkeypatch):
    project = register(str(repo(tmp_path)), graphify_mcp="graphify-default--one")
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        pathlib.Path(args[args.index("--output-last-message") + 1]).write_text("{}")
        return ""

    monkeypatch.setattr("wikiskill.core.run", fake_run)
    monkeypatch.setattr(
        "wikiskill.core._graphify_args",
        lambda _name, _group, _host: ["GRAPHIFY"],
    )
    call_codex(project, "maintain", {}, tmp_path / "maintainer")
    call_codex(project, "propose", {}, tmp_path / "proposer", graphify=True)
    assert "GRAPHIFY" not in calls[0]
    assert "GRAPHIFY" in calls[1]


def test_graphify_mcp_must_match_repository_group(tmp_path, monkeypatch):
    project = register(str(repo(tmp_path)), graphify_mcp="graphify-frontend--one")

    with pytest.raises(WikiSkillError, match="does not belong to group"):
        call_codex(project, "propose", {}, tmp_path / "proposer", graphify=True)


def test_gate_accepts_only_strict_improvement(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    validator = tmp_path / "validator.py"
    validator.write_text(
        "import json,os,pathlib\n"
        "p=pathlib.Path(os.environ['WIKISKILL_SKILLS_DIR'])\n"
        "print(json.dumps({'score': 0.8 if (p/'new-skill'/'SKILL.md').exists() else 0.5}))\n",
        encoding="utf-8",
    )
    project = register(
        str(root),
        validator_command=f"{sys.executable} {validator}",
    )
    project_state = initialize(project)
    iteration = project_state / "iterations/test"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "new-skill",
        "skill_md": "---\nname: new-skill\ndescription: test\n---\nDo the thing.",
        "purpose_md": "# Purpose\nTest.",
        "edits": [],
        "reason": "validated behavior",
    }
    candidate = build_candidate(project_state, proposal, iteration)
    assert gate(project, project_state, proposal, candidate)["outcome"] == "accepted"
    assert (project_state / "skills/new-skill/SKILL.md").exists()
    assert json.loads((project_state / "state.json").read_text())["best_score"] == 0.8


def test_gate_keeps_equal_score_candidate_as_rejected(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    validator = tmp_path / "validator.py"
    validator.write_text("print('{\"score\": 0.5}')\n", encoding="utf-8")
    project = register(str(root), validator_command=f"{sys.executable} {validator}")
    project_state = initialize(project)
    iteration = project_state / "iterations/test"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "equal-skill",
        "skill_md": "---\nname: equal-skill\ndescription: test\n---\nDo the thing.",
        "purpose_md": "# Purpose\nTest.",
        "edits": [],
        "reason": "no measured gain",
    }
    candidate = build_candidate(project_state, proposal, iteration)
    assert gate(project, project_state, proposal, candidate)["outcome"] == "rejected"
    assert not (project_state / "skills/equal-skill").exists()
    assert json.loads((project_state / "state.json").read_text())["best_score"] == 0.5


def test_group_score_rejects_a_candidate_that_harms_the_group(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repo(first)
    repo(second)
    git(
        second,
        "remote",
        "set-url",
        "origin",
        "https://code.example/tools/infra.git",
    )
    project = register(str(first), deja_project="widget", validator_command="validate")
    register(str(second), deja_project="infra", validator_command="validate")
    project_state = initialize(project)
    iteration = project_state / "iterations/group-score"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "new-skill",
        "skill_md": "new",
        "purpose_md": "purpose",
        "edits": [],
        "reason": "candidate",
    }
    candidate = build_candidate(project_state, proposal, iteration)

    def score(_command, member, skills):
        candidate_score = (skills / "new-skill/SKILL.md").exists()
        values = {
            "code.example/acme/widget": (0.5, 0.8),
            "code.example/tools/infra": (0.9, 0.1),
        }
        return values[member.project_id][candidate_score]

    monkeypatch.setattr("wikiskill.core._score", score)
    assert _group_score(project, project_state / "skills") == pytest.approx(0.7)
    assert gate(project, project_state, proposal, candidate)["outcome"] == "rejected"


def test_group_score_rejects_replaced_member_checkout(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    repo(first)
    repo(second)
    git(
        second,
        "remote",
        "set-url",
        "origin",
        "https://code.example/tools/infra.git",
    )
    project = register(str(first), deja_project="widget", validator_command="validate")
    register(str(second), deja_project="infra", validator_command="validate")
    git(
        second,
        "remote",
        "set-url",
        "origin",
        "https://code.example/tools/replaced.git",
    )
    monkeypatch.setattr("wikiskill.core._score", lambda *_args: 0.5)
    with pytest.raises(WikiSkillError, match="identity changed"):
        _group_score(project, state_dir("default") / "skills")


def test_gate_rescores_current_skills_before_comparing_candidate(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root), validator_command="validate")
    project_state = initialize(project)
    state_path = project_state / "state.json"
    state = json.loads(state_path.read_text())
    state["best_score"] = 0.1
    state_path.write_text(json.dumps(state))
    iteration = project_state / "iterations/rescore"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "candidate",
        "skill_md": "candidate",
        "purpose_md": "purpose",
        "edits": [],
        "reason": "candidate",
    }
    candidate = build_candidate(project_state, proposal, iteration)

    def score(_command, _member, skills):
        return 0.6 if (skills / "candidate/SKILL.md").exists() else 0.7

    monkeypatch.setattr("wikiskill.core._score", score)
    outcome = gate(project, project_state, proposal, candidate)
    assert outcome == {"outcome": "rejected", "score": 0.6, "best_score": 0.7}
    assert not (project_state / "skills/candidate").exists()


def test_context_cannot_roll_back_an_active_skill_swap(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root), validator_command="validate")
    project_state = initialize(project)
    (project_state / "skills/old").mkdir()
    (project_state / "skills/old/SKILL.md").write_text("old")
    iteration = project_state / "iterations/context-race"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "new",
        "skill_md": "new",
        "purpose_md": "purpose",
        "edits": [],
        "reason": "candidate",
    }
    candidate = build_candidate(project_state, proposal, iteration)
    scores = iter((0.5, 0.8))
    monkeypatch.setattr("wikiskill.core._score", lambda *_args: next(scores))
    real_replace = __import__("wikiskill.core", fromlist=["os"]).os.replace
    paused = threading.Event()
    resume = threading.Event()

    def pause_skill_swap(source, destination):
        result = real_replace(source, destination)
        if (
            source == project_state / "skills"
            and destination == project_state / "skills.old"
        ):
            paused.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr("wikiskill.core.os.replace", pause_skill_swap)

    def swap_skills():
        with _group_lock(project_state):
            gate(project, project_state, proposal, candidate)

    worker = threading.Thread(target=swap_skills)
    worker.start()
    assert paused.wait(5)
    assert context(project) == ""
    assert not (project_state / "skills").exists()
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    assert (project_state / "skills/new/SKILL.md").exists()
    assert not (project_state / "skills.old").exists()


def test_context_serves_committed_skills_while_evolution_is_running(tmp_path):
    project = register(str(repo(tmp_path)))
    project_state = initialize(project)
    approved = project_state / "skills/approved"
    approved.mkdir()
    (approved / "SKILL.md").write_text("approved")
    with _group_lock(project_state):
        assert context(project).endswith("approved\n")


def test_context_cannot_interrupt_a_wiki_swap(tmp_path, monkeypatch):
    root = repo(tmp_path)
    project = register(str(root))
    project_state = initialize(project)
    real_replace = __import__("wikiskill.core", fromlist=["os"]).os.replace
    paused = threading.Event()
    resume = threading.Event()
    errors = []

    def pause_wiki_swap(source, destination):
        result = real_replace(source, destination)
        if source == project_state / "wiki" and destination.name.startswith(
            "wiki.old-"
        ):
            paused.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr("wikiskill.core.os.replace", pause_wiki_swap)
    result = {
        "create_patterns": [],
        "update_patterns": [],
        "update_index": "# changed",
        "append_log": "changed",
    }

    def maintain():
        try:
            with _group_lock(project_state):
                apply_maintenance(project_state, result)
        except OSError as exc:
            errors.append(exc)

    worker = threading.Thread(target=maintain)
    worker.start()
    assert paused.wait(5)
    context(project)
    assert not (project_state / "wiki").exists()
    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert (project_state / "wiki/index.md").read_text() == "# changed\n"


def test_gate_restores_skills_when_score_state_write_fails(tmp_path, monkeypatch):
    root = repo(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path.parent / f"{tmp_path.name}-data"))
    project = register(str(root), validator_command="validator")
    project_state = initialize(project)
    iteration = project_state / "iterations/test"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "new-skill",
        "skill_md": "---\nname: new-skill\ndescription: test\n---\nDo the thing.",
        "purpose_md": "# Purpose\nTest.",
        "edits": [],
        "reason": "validated behavior",
    }
    candidate = build_candidate(project_state, proposal, iteration)
    scores = iter((0.5, 0.8))
    monkeypatch.setattr("wikiskill.core._score", lambda *_args: next(scores))
    monkeypatch.setattr(
        "wikiskill.core._atomic_json",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated state failure")),
    )
    with pytest.raises(OSError):
        gate(project, project_state, proposal, candidate)
    assert not (project_state / "skills/new-skill").exists()


def test_gate_restores_skills_when_impact_write_fails(tmp_path, monkeypatch):
    project = register(str(repo(tmp_path)), validator_command="validator")
    project_state = initialize(project)
    iteration = project_state / "iterations/impact-failure"
    iteration.mkdir()
    proposal = {
        "action": "create",
        "name": "new-skill",
        "skill_md": "new",
        "purpose_md": "purpose",
        "edits": [],
        "reason": "candidate",
    }
    candidate = build_candidate(project_state, proposal, iteration)
    scores = iter((0.5, 0.8))
    monkeypatch.setattr("wikiskill.core._score", lambda *_args: next(scores))
    real_write = __import__(
        "wikiskill.core", fromlist=["_write_private_text"]
    )._write_private_text

    def fail_impact(path, content):
        if path.name == "skill-impact.md" and "accepted" in content:
            raise OSError("impact failed")
        real_write(path, content)

    monkeypatch.setattr("wikiskill.core._write_private_text", fail_impact)
    with pytest.raises(OSError, match="impact failed"):
        gate(project, project_state, proposal, candidate)
    assert not (project_state / "skills/new-skill").exists()
    assert json.loads((project_state / "state.json").read_text())["best_score"] == 0.5
    assert (project_state / "wiki/skill-impact.md").read_text() == "# Skill impact\n"
