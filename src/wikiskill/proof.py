from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import tomllib
from dataclasses import dataclass

from .errors import WikiSkillError
from .process import file_tail, stop_process_group
from .text import redact


@dataclass(frozen=True)
class ProofCase:
    name: str
    prompt: str
    ref: str
    allow: tuple[str, ...]
    verify: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    require_change: bool


@dataclass(frozen=True)
class ProofManifest:
    agent: tuple[str, ...]
    pass_env: tuple[str, ...]
    repetitions: int
    cases: tuple[ProofCase, ...]
    sha256: str


SAFE_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "NO_COLOR",
    "XDG_CONFIG_HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_MANIFEST_BYTES = 1_000_000


def _strings(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise WikiSkillError(f"{label} must be a non-empty string array")
    return tuple(value)


def load_manifest(path: pathlib.Path) -> ProofManifest:
    try:
        content = path.read_bytes()
        if len(content) > MAX_MANIFEST_BYTES:
            raise WikiSkillError("proof manifest exceeds 1 MB")
        value = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise WikiSkillError(f"cannot read proof manifest: {path}") from exc
    if value.get("version") != 1:
        raise WikiSkillError("proof manifest version must be 1")
    raw_agent = value.get("agent")
    if not isinstance(raw_agent, dict):
        raise WikiSkillError("proof manifest needs an agent table")
    agent = _strings(raw_agent.get("command"), "agent.command")
    raw_pass_env = raw_agent.get("pass_env", [])
    if not isinstance(raw_pass_env, list) or not all(
        isinstance(name, str) and ENV_NAME.fullmatch(name) for name in raw_pass_env
    ):
        raise WikiSkillError("agent.pass_env must be an environment-variable array")
    pass_env = tuple(raw_pass_env)
    repetitions = value.get("repetitions", 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise WikiSkillError("repetitions must be an integer between 1 and 10")
    default_ref = value.get("ref", "HEAD")
    default_timeout = value.get("timeout_seconds", 600)
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise WikiSkillError("proof manifest must contain at least one case")

    cases: list[ProofCase] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_cases, 1):
        if not isinstance(raw, dict):
            raise WikiSkillError(f"cases[{index}] must be a table")
        name = raw.get("name")
        prompt = raw.get("prompt")
        ref = raw.get("ref", default_ref)
        timeout = raw.get("timeout_seconds", default_timeout)
        require_change = raw.get("require_change", True)
        if not isinstance(name, str) or not name.strip():
            raise WikiSkillError(f"cases[{index}].name must be a non-empty string")
        if name in names:
            raise WikiSkillError(f"duplicate proof case: {name}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WikiSkillError(f"case {name!r} needs a prompt")
        if not isinstance(ref, str) or not ref:
            raise WikiSkillError(f"case {name!r} needs a Git ref")
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise WikiSkillError(
                f"case {name!r} timeout_seconds must be between 1 and 3600"
            )
        if type(require_change) is not bool:
            raise WikiSkillError(f"case {name!r} require_change must be boolean")
        allow = _strings(raw.get("allow"), f"case {name!r} allow")
        raw_verify = raw.get("verify")
        if not isinstance(raw_verify, list) or not raw_verify:
            raise WikiSkillError(f"case {name!r} needs at least one verifier")
        verify = tuple(
            _strings(command, f"case {name!r} verify[{position}]")
            for position, command in enumerate(raw_verify, 1)
        )
        names.add(name)
        cases.append(
            ProofCase(
                name=name,
                prompt=prompt,
                ref=ref,
                allow=allow,
                verify=verify,
                timeout_seconds=timeout,
                require_change=require_change,
            )
        )
    return ProofManifest(
        agent=agent,
        pass_env=pass_env,
        repetitions=repetitions,
        cases=tuple(cases),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _run(
    command: tuple[str, ...] | list[str],
    cwd: pathlib.Path,
    *,
    timeout: int,
    env: dict[str, str],
    input_text: str | None = None,
) -> tuple[int, str]:
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise WikiSkillError(f"cannot start proof command: {command[0]}") from exc
        try:
            process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _reap_process_group(process)
            return 124, "timed out"
        except BaseException:
            _reap_process_group(process)
            raise
        _reap_process_group(process)
        detail = file_tail(stderr_file, 800) or file_tail(stdout_file, 800)
        return process.returncode, redact(detail.strip())


def _reap_process_group(process: subprocess.Popen) -> None:
    stop_process_group(process)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _git(root: pathlib.Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if completed.returncode:
        detail = redact((completed.stderr or completed.stdout).strip()[-800:])
        raise WikiSkillError(f"git failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def _skills_text(skills: pathlib.Path) -> str:
    parts = [
        f"\n## {path.parent.name}\n{path.read_text(encoding='utf-8').rstrip()}\n"
        for path in sorted(skills.glob("*/SKILL.md"))
    ]
    return "".join(parts) or "(no approved skills)"


def _skills_digest(skills: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(skills.glob("*/SKILL.md")):
        digest.update(str(path.relative_to(skills)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _snapshot(workspace: pathlib.Path) -> dict[str, tuple[object, ...]]:
    files: dict[str, tuple[object, ...]] = {}
    for directory, names, filenames in os.walk(workspace, followlinks=False):
        current = pathlib.Path(directory)
        if current == workspace and ".git" in names:
            names.remove(".git")
        for name in list(names):
            path = current / name
            if path.is_symlink():
                names.remove(name)
                relative = path.relative_to(workspace).as_posix()
                files[relative] = ("link", os.readlink(path))
        for name in filenames:
            path = current / name
            relative = path.relative_to(workspace).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                files[relative] = ("link", mode, os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                files[relative] = ("file", mode, metadata.st_size, digest)
            else:
                files[relative] = ("other", mode, metadata.st_size)
    return files


def _changed_paths(
    baseline: dict[str, tuple[object, ...]],
    current: dict[str, tuple[object, ...]],
) -> list[str]:
    return sorted(
        path
        for path in baseline.keys() | current.keys()
        if baseline.get(path) != current.get(path)
    )


def _fixture(root: pathlib.Path, sha: str, destination: pathlib.Path) -> None:
    archive_path = destination.parent / f"{destination.name}.tar"
    _git(root, "archive", "--format=tar", "-o", str(archive_path), sha)
    destination.mkdir()
    root_path = destination.resolve()
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(root_path) or member.isdev():
                raise WikiSkillError(f"unsafe path in proof fixture: {member.name}")
        archive.extractall(destination)
    archive_path.unlink()

    _git(destination, "init", "--quiet", "-b", "proof")
    entries = sorted(path.name for path in destination.iterdir() if path.name != ".git")
    if entries:
        _git(destination, "add", "-f", "--", *entries)
    _git(
        destination,
        "-c",
        "user.name=WikiSkill",
        "-c",
        "user.email=wikiskill@example.invalid",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        f"proof fixture {sha}",
    )


def _remove_path(path: pathlib.Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_changes(
    source: pathlib.Path,
    destination: pathlib.Path,
    changed: list[str],
    current: dict[str, tuple[object, ...]],
) -> None:
    for relative in changed:
        source_path = source / relative
        destination_path = destination / relative
        _remove_path(destination_path)
        if relative not in current:
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_file() and not source_path.is_symlink():
            shutil.copy2(source_path, destination_path)
        else:
            raise WikiSkillError(f"proof task produced unsupported file: {relative}")


def _case_prompt(case: ProofCase, skills_text: str) -> str:
    allowed = "\n".join(f"- {pattern}" for pattern in case.allow)
    return f"""You are running one isolated skill-validation task.
Complete the task in the current Git checkout. The result is graded by deterministic
commands after you exit. Do not edit tests, evaluators, or files outside the task's
allowed implementation paths. Do not merely describe the solution; make the change.

<allowed_paths>
{allowed}
</allowed_paths>

<approved_skills>
{skills_text}
</approved_skills>

<task>
{case.prompt.strip()}
</task>
"""


def _case_environment(
    manifest: ProofManifest,
    case: ProofCase,
    repetition: int,
    scratch: pathlib.Path,
) -> dict[str, str]:
    missing = [name for name in manifest.pass_env if name not in os.environ]
    if missing:
        raise WikiSkillError(f"proof environment variable is not set: {missing[0]}")
    env = {
        name: os.environ[name]
        for name in (*SAFE_ENV, *manifest.pass_env)
        if name in os.environ
    }
    env["WIKISKILL_PROOF_CASE"] = case.name
    env["WIKISKILL_PROOF_RUN"] = str(repetition)
    for name in ("tmp", "cache", "data"):
        (scratch / name).mkdir(parents=True, mode=0o700)
    env["TMPDIR"] = str(scratch / "tmp")
    env["XDG_CACHE_HOME"] = str(scratch / "cache")
    env["XDG_DATA_HOME"] = str(scratch / "data")
    return env


def _run_case(
    root: pathlib.Path,
    skills: pathlib.Path,
    manifest: ProofManifest,
    case: ProofCase,
    repetition: int,
) -> dict[str, object]:
    sha = _git(
        root, "rev-parse", "--verify", "--end-of-options", f"{case.ref}^{{commit}}"
    )
    with tempfile.TemporaryDirectory(prefix="wikiskill-proof-") as temporary:
        workspace = pathlib.Path(temporary) / "workspace"
        _fixture(root, sha, workspace)
        baseline = _snapshot(workspace)
        env = _case_environment(
            manifest, case, repetition, pathlib.Path(temporary) / "runtime"
        )
        returncode, _detail = _run(
            manifest.agent,
            workspace,
            timeout=case.timeout_seconds,
            env=env,
            input_text=_case_prompt(case, _skills_text(skills)),
        )
        if returncode:
            return {
                "name": case.name,
                "run": repetition,
                "ref": sha,
                "outcome": "agent_failed",
            }

        current = _snapshot(workspace)
        changed = _changed_paths(baseline, current)
        forbidden = [
            path
            for path in changed
            if not any(fnmatch.fnmatchcase(path, pattern) for pattern in case.allow)
        ]
        if forbidden:
            return {
                "name": case.name,
                "run": repetition,
                "ref": sha,
                "outcome": "forbidden_change",
                "changed": changed[:100],
            }
        unsupported = [
            path for path in changed if path in current and current[path][0] != "file"
        ]
        if unsupported:
            return {
                "name": case.name,
                "run": repetition,
                "ref": sha,
                "outcome": "unsupported_change",
                "changed": changed[:100],
            }
        if case.require_change and not changed:
            return {
                "name": case.name,
                "run": repetition,
                "ref": sha,
                "outcome": "no_change",
            }

        verification = pathlib.Path(temporary) / "verification"
        _fixture(root, sha, verification)
        _copy_changes(workspace, verification, changed, current)
        for command in case.verify:
            returncode, _detail = _run(
                command,
                verification,
                timeout=case.timeout_seconds,
                env=env,
            )
            if returncode:
                return {
                    "name": case.name,
                    "run": repetition,
                    "ref": sha,
                    "outcome": "check_failed",
                    "changed": changed[:100],
                }
        return {
            "name": case.name,
            "run": repetition,
            "ref": sha,
            "outcome": "passed",
            "changed": changed[:100],
        }


def proof(
    manifest_path: str | os.PathLike[str],
    skills_path: str | os.PathLike[str],
    project_path: str | os.PathLike[str] = ".",
) -> dict[str, object]:
    manifest = load_manifest(pathlib.Path(manifest_path).expanduser().resolve())
    skills = pathlib.Path(skills_path).expanduser().resolve()
    if not skills.is_dir():
        raise WikiSkillError(f"skills directory does not exist: {skills}")
    root = pathlib.Path(
        _git(
            pathlib.Path(project_path).expanduser().resolve(),
            "rev-parse",
            "--show-toplevel",
        )
    ).resolve()
    results = [
        _run_case(root, skills, manifest, case, repetition)
        for case in manifest.cases
        for repetition in range(1, manifest.repetitions + 1)
    ]
    passed = sum(result["outcome"] == "passed" for result in results)
    return {
        "score": passed / len(results),
        "passed": passed,
        "total": len(results),
        "skills_sha256": _skills_digest(skills),
        "manifest_sha256": manifest.sha256,
        "cases": results,
    }


def write_report(path: str | os.PathLike[str], result: dict[str, object]) -> None:
    destination = pathlib.Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, destination)
