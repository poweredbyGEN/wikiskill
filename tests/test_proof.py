import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys

import pytest

from wikiskill.cli import main
from wikiskill.core import WikiSkillError
from wikiskill.proof import _run, load_manifest, proof


def git(path: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def fixture(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    verifier = root / "verify.py"
    verifier.write_text(
        "import pathlib,sys\n"
        "sys.exit(0 if pathlib.Path('answer.txt').read_text() == 'proved\\n' else 1)\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text("ignored-proof.py\n", encoding="utf-8")
    git(root, "add", "verify.py", ".gitignore")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "fixture",
    )

    agent = tmp_path / "agent.py"
    agent.write_text(
        "import pathlib,sys\n"
        "prompt=sys.stdin.read()\n"
        "pathlib.Path('answer.txt').write_text('proved\\n' if 'WRITE_PROOF' in prompt else 'guess\\n')\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "proof.toml"
    manifest.write_text(
        f'''version = 1
repetitions = 1

[agent]
command = ["{sys.executable}", "{agent}"]

[[cases]]
name = "artifact"
prompt = "Create answer.txt with the required result."
allow = ["answer.txt"]
verify = [["{sys.executable}", "verify.py"]]
''',
        encoding="utf-8",
    )
    return root, agent, manifest


def test_candidate_skill_must_produce_verifiable_artifact(tmp_path):
    root, _agent, manifest = fixture(tmp_path)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    (candidate / "proof").mkdir(parents=True)
    (candidate / "proof/SKILL.md").write_text("WRITE_PROOF\n", encoding="utf-8")

    assert proof(manifest, baseline, root)["score"] == 0
    result = proof(manifest, candidate, root)

    assert result["score"] == 1
    assert result["passed"] == result["total"] == 1
    assert len(result["skills_sha256"]) == 64
    assert (
        result["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert result["cases"][0]["outcome"] == "passed"
    assert result["cases"][0]["changed"] == ["answer.txt"]
    assert len(result["cases"][0]["ref"]) == 40
    assert not (root / "answer.txt").exists()


def test_agent_cannot_change_the_verifier_to_fake_proof(tmp_path):
    root, agent, manifest = fixture(tmp_path)
    agent.write_text(
        "import pathlib,subprocess,sys\n"
        "sys.stdin.read()\n"
        "pathlib.Path('verify.py').write_text('raise SystemExit(0)\\n')\n"
        "pathlib.Path('ignored-proof.py').write_text('hidden')\n"
        "pathlib.Path('answer.txt').write_text('proved\\n')\n"
        "subprocess.run(['git','update-index','--assume-unchanged','verify.py'],check=True)\n",
        encoding="utf-8",
    )
    skills = tmp_path / "skills"
    skills.mkdir()

    result = proof(manifest, skills, root)

    assert result["score"] == 0
    assert result["cases"][0]["outcome"] == "forbidden_change"
    assert result["cases"][0]["changed"] == [
        "answer.txt",
        "ignored-proof.py",
        "verify.py",
    ]


def test_successful_agent_cannot_leave_background_process(tmp_path):
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import pathlib,subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n",
        encoding="utf-8",
    )

    returncode, _detail = _run(
        (sys.executable, str(agent)),
        tmp_path,
        timeout=5,
        env=os.environ.copy(),
    )
    child = int((tmp_path / "child.pid").read_text())

    assert returncode == 0
    try:
        os.kill(child, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(child, signal.SIGTERM)
        pytest.fail("background child survived the proof command")


def test_detached_child_cannot_mutate_the_verification_workspace(tmp_path):
    root, agent, manifest = fixture(tmp_path)
    (root / "verify.py").write_text(
        "import pathlib,time\n"
        "time.sleep(0.4)\n"
        "raise SystemExit(0 if pathlib.Path('late.txt').exists() else 1)\n",
        encoding="utf-8",
    )
    git(root, "add", "verify.py")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "delayed verifier",
    )
    agent.write_text(
        "import pathlib,subprocess,sys\n"
        "sys.stdin.read()\n"
        "subprocess.Popen([sys.executable,'-c',"
        "\"import pathlib,time;time.sleep(.1);pathlib.Path('late.txt').write_text('fake')\"],"
        "start_new_session=True)\n"
        "pathlib.Path('answer.txt').write_text('allowed')\n",
        encoding="utf-8",
    )
    skills = tmp_path / "skills"
    skills.mkdir()

    result = proof(manifest, skills, root)

    assert result["score"] == 0
    assert result["cases"][0]["outcome"] == "check_failed"


def test_case_cannot_reuse_shared_temporary_state(tmp_path, monkeypatch):
    root, agent, manifest = fixture(tmp_path)
    shared_tmp = tmp_path / "shared-tmp"
    shared_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(shared_tmp))
    agent.write_text(
        "import os,pathlib,sys\n"
        "sys.stdin.read()\n"
        "marker=pathlib.Path(os.environ['TMPDIR'])/'previous-run'\n"
        "pathlib.Path('answer.txt').write_text('proved\\n' if marker.exists() else 'guess\\n')\n"
        "marker.write_text('seen')\n",
        encoding="utf-8",
    )
    skills = tmp_path / "skills"
    skills.mkdir()

    assert proof(manifest, skills, root)["score"] == 0
    assert proof(manifest, skills, root)["score"] == 0
    assert not any(shared_tmp.iterdir())


def test_case_cannot_read_newer_source_history(tmp_path):
    root, agent, manifest = fixture(tmp_path)
    (root / "solution.txt").write_text("bug\n", encoding="utf-8")
    git(root, "add", "solution.txt")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "bug fixture",
    )
    bug_sha = git(root, "rev-parse", "HEAD")
    (root / "solution.txt").write_text("proved\n", encoding="utf-8")
    git(root, "add", "solution.txt")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "future solution",
    )
    manifest.write_text(
        manifest.read_text().replace(
            'name = "artifact"', f'name = "artifact"\nref = "{bug_sha}"'
        )
    )
    agent.write_text(
        "import pathlib,subprocess,sys\n"
        "sys.stdin.read()\n"
        "result=subprocess.run(['git','show','origin/main:solution.txt'],capture_output=True,text=True)\n"
        "pathlib.Path('answer.txt').write_text(result.stdout if result.returncode == 0 else 'guess\\n')\n",
        encoding="utf-8",
    )
    skills = tmp_path / "skills"
    skills.mkdir()

    result = proof(manifest, skills, root)

    assert result["score"] == 0
    assert result["cases"][0]["outcome"] == "check_failed"


def test_manifest_requires_deterministic_verifier(tmp_path):
    manifest = tmp_path / "proof.toml"
    manifest.write_text(
        'version=1\n[agent]\ncommand=["agent"]\n'
        '[[cases]]\nname="missing"\nprompt="work"\nallow=["src/**"]\n',
        encoding="utf-8",
    )

    with pytest.raises(WikiSkillError, match="at least one verifier"):
        load_manifest(manifest)


def test_manifest_size_is_bounded(tmp_path):
    manifest = tmp_path / "proof.toml"
    manifest.write_bytes(b"#" * 1_000_001)

    with pytest.raises(WikiSkillError, match="exceeds 1 MB"):
        load_manifest(manifest)


def test_agent_does_not_inherit_unlisted_secrets(tmp_path, monkeypatch):
    root, agent, manifest = fixture(tmp_path)
    agent.write_text(
        "import os,pathlib,sys\n"
        "prompt=sys.stdin.read()\n"
        "pathlib.Path('answer.txt').write_text('proved\\n' if 'WRITE_PROOF' in prompt else 'guess\\n')\n"
        "pathlib.Path('secret-seen').write_text('yes') if os.getenv('PROOF_SECRET') else None\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    (candidate / "proof").mkdir(parents=True)
    (candidate / "proof/SKILL.md").write_text("WRITE_PROOF\n", encoding="utf-8")
    monkeypatch.setenv("PROOF_SECRET", "must-not-reach-agent")

    result = proof(manifest, candidate, root)

    assert result["score"] == 1
    assert result["cases"][0]["changed"] == ["answer.txt"]


def test_cli_prints_score_as_final_json_line(tmp_path, monkeypatch, capsys):
    root, _agent, manifest = fixture(tmp_path)
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("WIKISKILL_SKILLS_DIR", str(skills))
    report = tmp_path / "reports/proof.json"
    monkeypatch.setenv("WIKISKILL_VALIDATION_REPORT", str(report))

    assert main(["proof", str(manifest)]) == 0

    output = capsys.readouterr()
    assert "proof artifact#1: check_failed" in output.err
    assert json.loads(output.out)["score"] == 0
    assert json.loads(report.read_text())["cases"][0]["outcome"] == "check_failed"
    assert report.stat().st_mode & 0o777 == 0o600
