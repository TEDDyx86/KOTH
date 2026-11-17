# Agent Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the orchestrated flow from the user's draw.io diagram: OP → PL → RC →
Júri → specialists (sequential) → SU, with PE as the sole memory point, per-role context
builders with budgets, structured payloads, human checkpoint per cycle, and per-role
model matrix (`models.yaml` + `agents.yaml`).

**Architecture:** `app/agents/` package. Judgment roles are LLM agents (pydantic-ai):
Júri, specialists, OP (summary). Mechanical roles are deterministic code (testable,
cheap): PL prioritization, RC recon bundle, SU flag validation. PE = existing tracking
services. ToolRunner = existing `app/tooling/`. Model connections via pydantic-ai
providers (`google:` tested; `nvidia`/`9router` OpenAI-compatible declared).

**Decisions locked (2026-11-16):** native sandbox now; human approval per cycle with
findings presented; sequential specialists; manual flag submission; providers from what
exists on the machine; strong models for JU/PL/OP + cheap for specialists.

**Tech Stack:** pydantic-ai-slim[google] 2.68 (installed; `google:` prefix, model
`gemini-3-flash-preview`), PyYAML, existing stack. Fake models for tests:
`pydantic_ai.models.test.TestModel` / `FunctionModel`.

## Global Constraints

- Agents are stateless: every invocation builds context from PE only. No cross-message
  memory inside an agent.
- Tool output never enters a context directly — goes to Attachment; contexts carry
  digests (summary + path).
- Context budget per role, truncate from the oldest end.
- Specialist dispatch is sequential — one at a time, structured payload back.
- No attack step runs without checkpoint approval (approve/reject/adjust).
- Subagent payloads are Pydantic models — the LLM must fill a typed schema
  (`output_type`), never free text.
- Tests use fake models only (no network); real model only in the final smoke script.
- Tests run with `python -m pytest tests/ -v`.

---

### Task 1: models.yaml + agents.yaml loaders

**Files:**
- Create: `app/agents/__init__.py`, `app/agents/config.py`, `config/models.yaml`, `config/agents.yaml`
- Test: `tests/test_agent_config.py`

**Interfaces:**
- `ModelRef(provider: str, name: str)`; `ProviderCfg(name, base_url|None, env_var, key_env)`
- `load_models(path=None) -> ModelsConfig` with `.providers: dict[str, ProviderCfg]`,
  `.roles: dict[str, ModelRef]` (`strong`, `fast`), `.fallbacks: dict[str, list[ModelRef]]`
- `load_agents(path=None) -> AgentsConfig` with `.roles: dict[str, AgentCfg]`,
  `AgentCfg(persona: str, model: str, tools: list[str])` — `model` = `strong`|`fast`
- Seed `config/models.yaml`: providers google (env `GEMINI_API_KEY`), nvidia
  (`NVIDIA_API_KEY`, base_url `https://integrate.api.nvidia.com/v1`), nine_router
  (`NINE_ROUTER_API_KEY`), anthropic (`ANTHROPIC_API_KEY`), openai (`OPENAI_API_KEY`);
  roles strong/fast → google `gemini-3-flash-preview`; fallbacks strong → fast.
- Seed `config/agents.yaml`: roles `op`, `planner`, `recon`, `jury`, `submission`,
  `specialist_web|pwn|crypto|reversing|forensics|osint` → persona path + model.

- [ ] Step 1: failing test (load defaults; load custom path with modified yaml).
- [ ] Step 2: implement loaders. Commit: `feat: agent/model config loaders`.

---

### Task 2: provider/model resolution

**Files:**
- Create: `app/agents/models.py`
- Test: `tests/test_agent_models.py`

**Interfaces:**
- `resolve_model(ref: ModelRef, cfg: ModelsConfig) -> pydantic_ai.models.Model`
  - `google` → `GoogleProvider(api_key=env)` and model `f"google:{ref.name}"`
  - provider with `base_url` → OpenAI-compatible: `OpenAIProvider(base_url=..., api_key=env)`
    and `f"openai:{ref.name}"`
  - missing env key → raise `ProviderUnavailable` (name included)
- `resolve_for_role(role: str, agents_cfg, models_cfg) -> Model` (agent role → model ref;
  fallback list tried in order, first available wins).

- [ ] Step 1: failing test (google resolves with env set; missing env → `ProviderUnavailable`;
      fallback picked when primary unavailable — patch env via monkeypatch).
- [ ] Step 2: implement. Commit: `feat: provider/model resolution with fallback`.

---

### Task 3: structured payloads

**Files:**
- Create: `app/agents/payloads.py`
- Test: `tests/test_agent_payloads.py`

**Interfaces (all Pydantic BaseModel):**
- `TaskOrder(category, task, hints=[], max_tool_runs=5)` — Júri → specialist
- `SpecialistReport(status: Literal["SOLVED","NEED_HINT","FAILED"], flag_candidate: str|None,
   findings: list[str], next_step: str|None, tools_used: list[str])` — specialist → Júri
- `JuryDecision(action: Literal["DISPATCH","RECHECK","SUBMIT","GIVE_UP","WAIT_USER"],
   order: TaskOrder|None, reason: str, findings: list[str])` — Júri decision each cycle
- `FinalReport(challenge_id, status, attempts_summary, flag_candidate|None)` — OP → user

- [ ] Step 1: failing test (instantiate/validate each; invalid status rejected).
- [ ] Step 2: implement. Commit: `feat: structured agent payloads`.

---

### Task 4: context builders (PE → role context, with budgets)

**Files:**
- Create: `app/agents/context.py`
- Test: `tests/test_agent_context.py`

**Interfaces:**
- `ContextBudget(max_chars: int)`; default budgets in module: OP=4000, JURY=12000, SPECIALIST=8000, SUBMISSION=4000
- `challenge_card(session, challenge) -> str` — id/event/title/category/points/difficulty/target/description(+flag format)
- `jury_context(session, challenge, budget=JURY) -> str` — card + recon digest + last 10 attempts (`{type}|{result}|{description}|{learning[:200]}`) + artifact list (name/size/type only)
- `specialist_context(session, challenge, order: TaskOrder, budget=SPECIALIST) -> str` — card + order + knowledge snippets (KnowledgeIndex top-k for order.task/category, 3 hits max) + artifact refs
- `submission_context(session, challenge, flag_candidate) -> str` — event flag_format + candidate + submitted flags history
- All builders truncate to budget **from the top** (keep newest attempts; drop oldest).
- Tests: seed PE via tracking services; assert sections present; assert oversized attempts list is truncated and newest retained; assert knowledge snippets appear (uses tmp knowledge root via monkeypatch of DEFAULT_KNOWLEDGE_ROOT imported at call time).

- [ ] Step 1: failing test. [ ] Step 2: implement. Commit: `feat: role context builders with budgets`.

---

### Task 5: mechanical roles — planner + recon

**Files:**
- Create: `app/agents/planner.py`, `app/agents/recon.py`
- Test: `tests/test_agent_mech.py`

**Interfaces:**
- `planner_rank(session, event) -> list[tuple[Challenge, float]]` — score = points weight +
  difficulty weight + open-status bonus (deterministic, documented formula).
- `stop_criteria(cfg) -> StopCriteria(max_attempts: int, max_minutes: int)` from
  `config/agents.yaml` section `limits` (defaults 12 / 60).
- `recon_bundle(runner: ToolRunner, session, challenge) -> ReconDigest`:
  - attachment `strings` on each attachment file (first 2000 chars digest)
  - `nmap` top-100 ports when `challenge.target` set (guard strict already enforced)
  - writes outputs as Attachments (ToolRunner already persists) and returns
    `ReconDigest(target, files=[{name, digest}], summary)`; records Attempt type=tool.
- Tests: rank ordering with fake data; recon with real python tool writing a temp file
  attachment; targetless challenge skips nmap.

- [ ] Step 1: failing test. [ ] Step 2: implement. Commit: `feat: planner ranking and recon bundle`.

---

### Task 6: jury + specialist agents (fake-model tested)

**Files:**
- Create: `app/agents/jury.py`, `app/agents/specialists.py`
- Test: `tests/test_jury_loop.py`

**Interfaces:**
- `make_specialist(role: str, agents_cfg, models_cfg) -> pydantic_ai.Agent` with
  `output_type=SpecialistReport`, system prompt = persona file + specialist context.
- `SpecialistRun(runner, agents_cfg, models_cfg)`:
  `run(session, challenge, order: TaskOrder) -> tuple[SpecialistReport, list[int]]`
  — builds specialist context, agent.run_sync, and executes up to `order.max_tool_runs`
  tool calls the agent requests via function tools (`run_tool(tool, args)`) against the
  ToolRunner (guards enforced there). Returns report + attempt ids.
- `JuryAgent(agents_cfg, models_cfg)` with `output_type=JuryDecision`;
  `decide(session, challenge, reports_so_far: list[SpecialistReport]) -> JuryDecision`.
- Tests use `TestModel` (auto output) — verify output types parsed, tool call path
  works with a stub agent, sequential dispatch order, and that report list is fed
  into next decision context. NO network.

- [ ] Step 1: failing test. [ ] Step 2: implement. Commit: `feat: jury and specialist agents with structured outputs`.

---

### Task 7: submission (deterministic)

**Files:**
- Create: `app/agents/submission.py`
- Test: `tests/test_submission.py`

**Interfaces:**
- `validate_flag(session, challenge, flag_candidate: str) -> SubmissionResult`
  (`accepted: bool, reason: str`) — matches event flag_format regex (default `CTF\{.*\}`)
  AND not already recorded in PE (Attempts of type `submission` with same tool_command);
  on success records Attempt type=submission result=success with tool_command=flag;
  on failure records result=failed + reason. **No platform call — manual submission**;
  the UI shows the flag after `accepted`.
- Tests: good flag accepted + recorded; duplicate rejected; bad regex rejected;
  custom event flag_format honored.

- [ ] Step 1: failing test. [ ] Step 2: implement. Commit: `feat: deterministic flag validation and submission record`.

---

### Task 8: orchestrator session + checkpoint

**Files:**
- Create: `app/agents/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- `AgentSession(id, challenge_id, state: Literal["awaiting_approval","running","solved","gave_up","rejected"], pending_decision: JuryDecision|None, findings: list[str], created_at)`
- `Orchestrator(runner, agents_cfg, models_cfg, storage_dir)`:
  - `start(challenge_id) -> AgentSession` — runs PL rank check, RC bundle, JU first
    decision → state `awaiting_approval` with pending_decision + findings.
  - `approve(session_id, note=None) -> AgentSession` — executes the pending decision
    (DISPATCH → specialist run → JU re-decide; SUBMIT → SU; GIVE_UP → gave_up),
    records everything in PE, then **stops again at awaiting_approval** with the new
    findings/decision (checkpoint por ciclo). If JU returns SUBMIT and SU accepts →
    state `solved`.
  - `reject(session_id, feedback)` — records feedback as Note, JU re-decides with it,
    state `awaiting_approval` again.
  - Sessions in-memory dict keyed by id + Attempt/Note records in PE (resume-safe
    enough for phase 1; true persistence across restarts deferred).
- Tests with `TestModel`: start stops at approval; approve runs one cycle then stops
  again; reject records feedback; solved path when flag candidate passes SU (flag
  injected via stub specialist).

- [ ] Step 1: failing test. [ ] Step 2: implement. Commit: `feat: orchestrator session with per-cycle checkpoint`.

---

### Task 9: REST API for agent sessions

**Files:**
- Create: `app/api/agent.py`; Modify: `app/main.py` (include router `/api/agent`)
- Test: `tests/test_api_agent.py`

**Interfaces:**
- `POST /api/agent/{challenge_id}/start` → session dict (201)
- `GET /api/agent/sessions/{session_id}` → session dict (findings, pending decision, state)
- `POST /api/agent/sessions/{session_id}/approve` (body `note?`) → session dict
- `POST /api/agent/sessions/{session_id}/reject` (body `feedback`) → session dict
- App state carries a shared `Orchestrator` built from `app.state.storage` + default configs.
- Tests: with monkeypatched fake model factory (module-level `_make_agent` overridden)
  — full start/approve/reject flow via TestClient; 404 for unknown session.

- [ ] Step 1: failing test. [ ] Step 2: implement. Commit: `feat: agent session REST API (start, approve, reject)`.

---

### Task 10: real-model smoke + full suite + merge

**Files:**
- Create: `scripts/agent_smoke_test.py`

**Script:** builds Orchestrator with real `resolve_model` (google key from opencode
auth.json, same pattern as `model_smoke_test.py`), seeds a fake challenge in a temp DB,
runs start → approve one cycle, prints findings and decision. Verifies the whole loop
against a real LLM.

- [ ] Step 1: implement script. [ ] Step 2: run it — loop must reach `awaiting_approval`
      with a real JuryDecision from Gemini. If 503/overload: retry once, else report and
      leave Task 2's fallback config as the remedy.
- [ ] Step 3: full suite `python -m pytest tests/ -v` → all PASS.
- [ ] Step 4: Commit: `feat: agent runtime smoke test with real model`. Merge
      `feature/agent-runtime` into `main` (user's standard), delete branch, run suite.

---

## Self-Review

- Diagram coverage: OP (T8/T9 response consolidation), PL (T5 rank + stop criteria),
  RC (T5 recon bundle → PE), JU (T6 decision loop, sequential, Attempt per cycle),
  S1–S6 (T6 specialists with structured payload), SB (ToolRunner native, guards),
  PE (contexts read via tracking services; everything written back), SU (T7 manual),
  checkpoint per cycle (T8 approve/reject with findings), desistência (JU GIVE_UP →
  session gave_up → OP final report), Plataforma CTF = **manual by decision**.
- Context controls: builders with budgets (T4), digest-only tool output (T4/T5),
  structured payloads (T3), stateless agents (all).
- Model matrix: strong/fast roles + fallback (T1/T2), providers limited to what exists.
- Tests never touch network (TestModel); real model isolated in T10 script.
