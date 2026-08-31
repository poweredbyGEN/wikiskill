# WikiSkill

WikiSkill turns coding-agent experience into a persistent wiki and validation-gated skills.
It is an independent open-source implementation inspired by Tang et al.,
[“WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution”](https://arxiv.org/abs/2608.27454)
(arXiv:2608.27454, 2026).

WikiSkill keeps three layers separate:

- **Evidence:** sanitized snapshots of completed sessions indexed by [Deja](https://github.com/vshulcz/deja-vu).
- **Wiki:** durable patterns, root causes, working procedures, and rejected attempts.
- **Skills:** runtime instructions promoted only after an objective validator reports a strict improvement.

Normal coding sessions receive approved skills only. They never receive the private wiki or raw transcripts.

## Requirements

- Python 3.11+
- `deja` on `PATH`
- `codex` on `PATH` for nightly maintenance and proposals

WikiSkill itself has no third-party Python dependencies.

## Install

```bash
git clone <this-repository-url>
cd wikiskill
python -m pip install .
wikiskill --version
```

## Register repositories

A group is any name you choose. Repositories in the same group share one wiki and accepted skill set.

```bash
wikiskill register /work/api \
  --group backend \
  --deja-project api \
  --model MODEL_ID \
  --validator '/work/api/evaluate-skills --json'

wikiskill register /work/worker \
  --group backend \
  --deja-project worker \
  --model MODEL_ID \
  --validator '/work/worker/evaluate-skills --json'
```

Nested directories and Git worktrees resolve automatically. Repository identity includes the forge host plus `owner/repository`, so different forges cannot silently collide.

The validator receives `WIKISKILL_SKILLS_DIR` and must print a final JSON line:

```json
{"score": 0.82}
```

A candidate must strictly improve the aggregate score across every registered repository in its group. Without a validator, proposals remain drafts and are never injected into coding sessions.

## Built-in proof validator

`wikiskill proof` runs held-out coding tasks in exact, history-free Git fixtures, injects only the
candidate skills, and grades the resulting files with deterministic commands. Every case
has an explicit implementation-path allowlist. Verification runs in a second pristine
fixture containing only those allowlisted changes, so the evaluated agent never controls
the tests or the grading workspace.

Create `.wikiskill-proof.toml`:

```toml
version = 1
ref = "proof-fixtures/parser-bug"
repetitions = 2
timeout_seconds = 600

[agent]
command = ["codex", "exec", "--sandbox", "workspace-write", "-"]
# pass_env = ["EXPLICIT_CREDENTIAL_IF_REQUIRED"]

[[cases]]
name = "preserves quoted separators"
prompt = "Fix the parser so quoted separators do not split a field."
allow = ["src/**"]
verify = [["python3", "-m", "pytest", "tests/test_parser.py", "-q"]]
```

The ref must identify a reproducible task fixture, preferably an immutable commit SHA
containing the bug. Commands are argument arrays, not shell strings. Verifier commands are
not included in the task prompt; keep the manifest outside the fixture when the checks
must be hidden. The score is the fraction of passing cases and repetitions.

The manifest is trusted benchmark configuration. Keep verifier files and their fixtures
outside `allow`. WikiSkill isolates Git history and per-run scratch, but it does not replace
an OS sandbox: `agent.command` must confine writes to the workspace (the Codex example does).

Run it directly:

```bash
wikiskill proof .wikiskill-proof.toml --skills /path/to/skills
```

Or use it as the group validator:

```bash
wikiskill register /work/api \
  --group backend \
  --deja-project api \
  --model MODEL_ID \
  --validator 'wikiskill proof .wikiskill-proof.toml'
```

Use task tests, exact-match checks, schema validation, or artifact comparison. Do not use
an LLM judge as the only verifier. Keep the configured agent model and command fixed so
the current and candidate skill sets are compared under the same harness.
The agent receives a minimal environment by default; `agent.pass_env` is the explicit
escape hatch when a harness cannot use its normal CLI login. Any listed secret is visible
to the evaluated agent, so prefer CLI/OAuth authentication.

When WikiSkill invokes the validator, it also sets `WIKISKILL_VALIDATION_REPORT`.
`wikiskill proof` writes the complete private report there: the exact Git SHA, manifest and
skill-set digests, changed paths, and outcome for every case. The score is the gate; this
report is the evidence behind it.

## Optional Graphify access

Graphify describes current code relationships; WikiSkill compiles reusable procedures. Configure one read-only Graphify MCP per repository in Codex, then bind it to the matching group namespace:

```bash
codex mcp add graphify-backend--api \
  --url https://graphs.example.com/api/mcp \
  --bearer-token-env-var GRAPH_API_KEY

wikiskill register /work/api \
  --group backend \
  --deja-project api \
  --model MODEL_ID \
  --graphify-mcp graphify-backend--api \
  --graphify-host graphs.example.com \
  --validator '/work/api/evaluate-skills --json'
```

Only the selected MCP is exposed to the proposer. The maintainer receives no MCP access.

## Run the nightly compiler

```bash
wikiskill nightly --since 36h --quiet-minutes 30
```

The command:

1. warms the Deja index once;
2. ingests completed, previously unseen sessions per registered repository;
3. updates the group wiki;
4. proposes at most one atomic skill change;
5. runs every group validator;
6. promotes the candidate only on strict aggregate improvement.

Cron example:

```cron
17 3 * * * $HOME/.local/bin/wikiskill nightly >>$HOME/.local/state/wikiskill-nightly.log 2>&1
```

## Load approved skills in coding agents

The fail-open command emits nothing outside a registered repository or when no skill has been accepted:

```bash
wikiskill hook-context "$PWD"
```

Add it to the session-start hook in Claude Code, Codex, or Grok:

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "wikiskill hook-context \"$PWD\"",
        "timeout": 10
      }]
    }]
  }
}
```

OpenCode plugin example:

```js
export const WikiSkill = async ({ $ }) => ({
  "experimental.chat.system.transform": async (_input, output) => {
    try {
      const context = (await $`wikiskill hook-context ${process.cwd()}`.text()).trim()
      if (!context) return
      if (output.system.length) output.system[0] = context + "\n\n" + output.system[0]
      else output.system.push(context)
    } catch {}
  },
})
```

## Inspect and operate

```bash
wikiskill status /work/api
wikiskill ingest /work/api --since 7d
wikiskill evolve /work/api
wikiskill configure-group /work/api --model NEW_MODEL_ID --validator './evaluate-skills --json'
```

## Private state

All generated data stays outside source repositories:

```text
~/.local/share/wikiskill/groups/<group>/
  raw/manifest.json
  raw/sessions/*.json
  wiki/index.md
  wiki/log.md
  wiki/skill-impact.md
  wiki/patterns/*.md
  skills/<name>/{SKILL.md,PURPOSE.md}
  iterations/*
```

Do not commit that directory. WikiSkill redacts common secret formats before storing evidence, but generated state should still be treated as private.

## Method boundary

The task-performing agent does not read the wiki. A separate maintainer updates persistent knowledge, a proposer makes one atomic skill change, and an objective validation gate decides whether that skill becomes active. Rejected skill edits remain in the wiki history so later iterations do not repeat the same failed approach.

This is an early implementation of a new research method. Production use requires representative validators for the work each group performs.
