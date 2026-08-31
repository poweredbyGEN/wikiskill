# Agent instructions

- Keep generated traces, wikis, skills, credentials, deployment identities, and organization data outside this public repository.
- Keep forge hosts, group names, model IDs, and Graphify endpoints configurable; examples use reserved domains and neutral names only.
- Preserve the actor/wiki boundary and strict validation gate from Tang et al., [“WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution”](https://arxiv.org/abs/2608.27454), arXiv:2608.27454 (2026).
- Repositories in a configured group share knowledge; nested folders and worktrees inherit their registered repository.

## Progress

- The built-in proof validator grades candidate skills against deterministic checks in isolated, history-free fixtures.

## Next steps

- Define representative held-out fixtures and immutable refs for each adopting group before enabling skill promotion.
