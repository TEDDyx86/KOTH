# Agent Panel UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (execução inline escolhida pelo usuário). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Painel do Agente em `/challenges/{id}/agent` — feed HTMX com polling, checkpoint humano (Aprovar/Ajustar/Rejeitar) e submissão manual de flag.

**Architecture:** Página Jinja com grid de 2 colunas; polling HTMX (1.5s) em dois parciais (`_agent_feed.html`, `_agent_side.html`) servidos por novas rotas web que delegam ao `Orchestrator` existente (em memória, `app.state._orchestrator`). Lookup de sessão por challenge; POSTs retornam o parcial lateral. `Orchestrator.terminate()` é o único método novo do runtime.

**Tech Stack:** FastAPI + Jinja2, HTMX 2.0.4, pytest/TestClient, sqlmodel.

**Spec:** `docs/specs/2026-11-17-agent-panel-design.md`

## Global Constraints

- Branch de feature: `feat/agent-panel-ui` (nunca implementar direto na `main`)
- TDD: teste falha → implementação → teste passa → commit, por task
- UI copy em português; tema: preto degradê, `--accent: #e01b24`, verde `#2ea043` só para flag confirmada
- `git add` sempre com caminhos explícitos — NUNCA `git add -A` (existem arquivos untracked do usuário: `docs/agents-architecture.mmd`, `iniciar_orchestrator.bat`)
- Rodar testes de `C:\Users\Administrator\ctf-orchestrator` com `python -m pytest <arquivo> -v`
- Nenhuma conexão com plataformas de CTF; flag é 100% manual
- Suite atual: 106 passed — deve permanecer verde ao final de cada task

### Ajustes da spec vs. realidade do código (aprovados)

- Spec §6 fala em "Output ≤2000 chars": `SpecialistReport` não carrega output bruto — o feed renderiza `findings`/`next_step` (já são resumos do LLM, limitados). Nenhum truncamento necessário.
- "Ajustar" reusa `orch.reject(feedback)` (nota `[agent rejected]`), "Rejeitar (encerrar)" usa o novo `orch.terminate()`.

---

### Task 0: Branch + commit pendente do `app/main.py`

**Files:**
- Modify: `app/main.py` (mudança legítima pendente: registra `agent_api.router`)

- [ ] **Step 1: Criar branch e commitar o pending**

```powershell
git checkout -b feat/agent-panel-ui
git add app/main.py
git commit -m "feat: register agent REST API router in create_app"
```

- [ ] **Step 2: Verificar suite verde**

Run: `python -m pytest -q`
Expected: `106 passed`

---

### Task 1: `Orchestrator.terminate()`

**Files:**
- Modify: `app/agents/orchestrator.py` (após `reject`, ~linha 117)
- Test: `tests/test_agent_terminate.py` (novo)

**Interfaces:**
- Consumes: `AgentSession`, `tracking.add_note`
- Produces: `Orchestrator.terminate(session: Session, session_id: str, reason: str = "") -> AgentSession` — seta `state="rejected"`, `pending_decision=None`, acumula finding e registra nota `[agent terminated] {reason}`. Task 5 e Task 6 (rota `/terminate`) dependem desta assinatura.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_terminate.py
import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.agents.orchestrator import AgentSession, Orchestrator
from app.models import Challenge, Event, Note


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="ctf")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="c")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch


def test_terminate_discards_decision_and_notes(env):
    session, ch = env
    orch = Orchestrator(runner=None, jury=None, specialists={})
    s = AgentSession(id="abc123", challenge_id=ch.id, state="awaiting_approval")
    s.pending_decision = None
    orch.sessions["abc123"] = s

    out = orch.terminate(session, "abc123", "operator rejected the plan")

    assert out.state == "rejected"
    assert out.pending_decision is None
    assert any("terminated by operator" in f for f in out.findings)
    notes = session.query(Note).filter(Note.challenge_id == ch.id).all()
    assert any("[agent terminated]" in n.content for n in notes)


def test_approve_after_terminate_raises(env):
    session, ch = env
    orch = Orchestrator(runner=None, jury=None, specialists={})
    s = AgentSession(id="abc123", challenge_id=ch.id, state="awaiting_approval")
    orch.sessions["abc123"] = s
    orch.terminate(session, "abc123")
    with pytest.raises(ValueError):
        orch.approve(session, "abc123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_terminate.py -v`
Expected: FAIL — `AttributeError: 'Orchestrator' object has no attribute 'terminate'`

- [ ] **Step 3: Write minimal implementation**

Adicionar em `app/agents/orchestrator.py`, logo após `reject`:

```python
    def terminate(self, session: Session, session_id: str,
                  reason: str = "") -> AgentSession:
        """Operator rejection: stop the session for good (no new decision)."""
        agent_session = self.sessions[session_id]
        agent_session.state = "rejected"
        agent_session.pending_decision = None
        self._accumulate(agent_session, [f"terminated by operator: {reason}"])
        tracking.add_note(session, agent_session.challenge_id,
                          f"[agent terminated] {reason}")
        return agent_session
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_terminate.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```powershell
git add app/agents/orchestrator.py tests/test_agent_terminate.py
git commit -m "feat: orchestrator.terminate (operator reject stops session)"
```

---

### Task 2: Extrair `get_orchestrator` compartilhado

**Files:**
- Modify: `app/agents/api.py:46-51` (renomear `_orchestrator` → `get_orchestrator` e atualizar os 4 usos internos: linhas 65, 75, 85, 97)

**Interfaces:**
- Produces: `app.agents.api.get_orchestrator(request: Request) -> Orchestrator` — cache em `request.app.state._orchestrator`. A Task 4 importa isto em `app/web/agent_routes.py`.

- [ ] **Step 1: Renomear**

Em `app/agents/api.py`, trocar a definição:

```python
def get_orchestrator(request: Request) -> Orchestrator:
    """Shared accessor — the web panel routes reuse the same instance."""
    orch = getattr(request.app.state, "_orchestrator", None)
    if orch is None:
        orch = _make_orchestrator(request.app.state.storage)
        request.app.state._orchestrator = orch
    return orch
```

e todos os pontos de chamada `_orchestrator(request)` → `get_orchestrator(request)` (4 ocorrências: `start_session`, `get_session_state`, `approve`, `reject`).

- [ ] **Step 2: Run existing agent tests to verify no regression**

Run: `python -m pytest tests/test_api_agent.py tests/test_agent_models.py -v`
Expected: todos PASS (nenhum teste referencia `_orchestrator`; os testes mockam `_make_orchestrator`, que continua intacto)

- [ ] **Step 3: Commit**

```powershell
git add app/agents/api.py
git commit -m "refactor: shared get_orchestrator accessor for web panel"
```

---

### Task 3: Filtro Jinja `badge_for`

**Files:**
- Modify: `app/templates.py` (substituir conteúdo)

**Interfaces:**
- Produces: filtro Jinja `{{ state|badge_for }}` → classe CSS do badge. Templates das Tasks 4/5/6 usam; CSS `.badge.warn` definido na Task 7.

- [ ] **Step 1: Escrever o filtro (não há teste de unidade dedicado — os testes de UI da Task 4 exercitam indiretamente)**

Substituir todo o conteúdo de `app/templates.py` por:

```python
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "web" / "templates"))

_BADGE_FOR = {
    "solved": "flag-ok",        # verde — única cor de sucesso
    "awaiting_approval": "warn",
    "running": "warn",
}


def badge_for(state: str) -> str:
    """Badge CSS class for an agent session state."""
    return _BADGE_FOR.get(state, "solved")  # gave_up/rejected → muted


templates.env.filters["badge_for"] = badge_for
```

- [ ] **Step 2: Smoke — suite continua verde**

Run: `python -m pytest tests/test_ui_pages.py -q`
Expected: `5 passed`

- [ ] **Step 3: Commit**

```powershell
git add app/templates.py
git commit -m "feat: badge_for Jinja filter for agent session states"
```

---

### Task 4: Página do painel + feed + lateral (GET) e registro no app

**Files:**
- Create: `app/web/agent_routes.py`
- Create: `app/web/templates/agent_panel.html`
- Create: `app/web/templates/_agent_feed.html`
- Create: `app/web/templates/_agent_side.html`
- Modify: `app/main.py` (registrar router)
- Test: `tests/test_web_agent_panel.py` (novo)

**Interfaces:**
- Consumes: `app.agents.api.get_orchestrator` (Task 2), filtro `badge_for` (Task 3)
- Produces: rotas `GET /challenges/{id}/agent`, `GET .../agent/feed`, `GET .../agent/side`; helpers `latest_session(orch, challenge_id) -> AgentSession | None`, `_challenge_or_404(session, challenge_id)`, `_side(...)`, `_feed(...)`. As Tasks 5/6 estendem este módulo.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_web_agent_panel.py
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.agents.orchestrator import AgentSession
from app.agents.payloads import JuryDecision, TaskOrder
from app.main import create_app
from app.models import Challenge, Event


class FakeOrch:
    """Same shape as Orchestrator for the web-layer tests."""

    def __init__(self):
        self.sessions: dict[str, AgentSession] = {}
        self.calls: list[tuple] = []

    def start(self, session, challenge_id):
        self.calls.append(("start", challenge_id))
        s = AgentSession(id="s1", challenge_id=challenge_id,
                         state="awaiting_approval")
        s.pending_decision = JuryDecision(
            action="DISPATCH", reason="start with web recon",
            order=TaskOrder(category="web", task="dump the source"))
        self.sessions[s.id] = s
        return s

    def approve(self, session, session_id):
        self.calls.append(("approve", session_id))
        s = self.sessions[session_id]
        s.pending_decision = JuryDecision(action="RECHECK", reason="verify",
                                          findings=[])
        return s

    def terminate(self, session, session_id, reason=""):
        self.calls.append(("terminate", session_id, reason))
        s = self.sessions[session_id]
        s.state = "rejected"
        s.pending_decision = None
        return s

    def reject(self, session, session_id, feedback):
        self.calls.append(("adjust", session_id, feedback))
        s = self.sessions[session_id]
        s.pending_decision = JuryDecision(
            action="DISPATCH", reason=f"adjusted: {feedback}",
            order=TaskOrder(category="web", task="try harder"))
        return s


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    fake = FakeOrch()
    monkeypatch.setattr("app.web.agent_routes.get_orchestrator",
                        lambda request: fake)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        with Session(engine) as session:
            event = Event(name="ctf")
            session.add(event)
            session.commit()
            session.refresh(event)
            ch = Challenge(event_id=event.id, title="c")
            session.add(ch)
            session.commit()
            session.refresh(ch)
        yield c, ch.id, fake


def test_panel_page_renders(client):
    c, ch_id, _ = client
    r = c.get(f"/challenges/{ch_id}/agent")
    assert r.status_code == 200
    assert "Painel do Agente" in r.text
    assert "Registrar flag" in r.text


def test_feed_empty_then_session(client):
    c, ch_id, fake = client
    r = c.get(f"/challenges/{ch_id}/agent/feed")
    assert r.status_code == 200
    assert "Nenhuma sessão" in r.text

    fake.sessions["s1"] = AgentSession(id="s1", challenge_id=ch_id,
                                       state="awaiting_approval")
    r = c.get(f"/challenges/{ch_id}/agent/feed")
    assert r.status_code == 200
    assert "s1" in r.text


def test_side_start_button_then_checkpoint(client):
    c, ch_id, fake = client
    r = c.get(f"/challenges/{ch_id}/agent/side")
    assert "Iniciar agente" in r.text

    s = AgentSession(id="s1", challenge_id=ch_id, state="awaiting_approval")
    s.pending_decision = JuryDecision(action="DISPATCH", reason="go",
                                      order=TaskOrder(category="web", task="t"))
    fake.sessions["s1"] = s
    r = c.get(f"/challenges/{ch_id}/agent/side")
    assert "Aprovar" in r.text
    assert "Ajustar" in r.text
    assert "Rejeitar" in r.text
    assert "web: t" in r.text


def test_latest_session_wins(client):
    c, ch_id, fake = client
    old = AgentSession(id="old", challenge_id=ch_id, state="rejected")
    old.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    fake.sessions["old"] = old
    fake.sessions["new"] = AgentSession(id="new", challenge_id=ch_id,
                                        state="running")
    r = c.get(f"/challenges/{ch_id}/agent/feed")
    assert "new" in r.text
    assert "old" not in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web_agent_panel.py -v`
Expected: FAIL — `ModuleNotFoundError`/404 (`app.web.agent_routes` não existe)

- [ ] **Step 3: Implementar `app/web/agent_routes.py`**

```python
"""Painel do Agente — rotas HTMX (feed, checkpoint, flag manual)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlmodel import Session

from ..agents.api import get_orchestrator
from ..agents.orchestrator import AgentSession
from ..agents.submission import validate_flag
from ..database import get_session
from ..models import ChallengeStatus
from ..services import tracking
from ..templates import templates

router = APIRouter()


def latest_session(orch, challenge_id: int) -> AgentSession | None:
    matches = [s for s in orch.sessions.values() if s.challenge_id == challenge_id]
    return max(matches, key=lambda s: s.created_at) if matches else None


def _challenge_or_404(session: Session, challenge_id: int):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    return challenge


def _session_or_404(orch, session_id: str) -> AgentSession:
    sess = orch.sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return sess


def _side(request: Request, challenge_id: int, sess: AgentSession | None):
    return templates.TemplateResponse(request, "_agent_side.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
    })


@router.get("/challenges/{challenge_id}/agent")
def agent_panel(request: Request, challenge_id: int,
                session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    return templates.TemplateResponse(request, "agent_panel.html",
                                      {"challenge": challenge})


@router.get("/challenges/{challenge_id}/agent/feed")
def agent_feed(request: Request, challenge_id: int,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    sess = latest_session(get_orchestrator(request), challenge_id)
    return templates.TemplateResponse(request, "_agent_feed.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
    })


@router.get("/challenges/{challenge_id}/agent/side")
def agent_side(request: Request, challenge_id: int,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    sess = latest_session(get_orchestrator(request), challenge_id)
    return _side(request, challenge_id, sess)
```

- [ ] **Step 4: Criar os três templates**

`app/web/templates/agent_panel.html`:

```jinja
{% extends "base.html" %}
{% block title %}Agente — {{ challenge.title }} — CTF Orchestrator{% endblock %}
{% block content %}
<h1>Painel do Agente
  <span class="mono" style="color: var(--muted); font-size: 0.9rem;">{{ challenge.title }}</span>
</h1>
<p><a href="/challenges/{{ challenge.id }}">← voltar ao challenge</a></p>
<div class="agent-grid">
  <section id="agent-feed"
           hx-get="/challenges/{{ challenge.id }}/agent/feed"
           hx-trigger="every 1.5s" hx-swap="innerHTML">
    {% include "_agent_feed.html" %}
  </section>
  <aside class="agent-side">
    <div id="agent-side"
         hx-get="/challenges/{{ challenge.id }}/agent/side"
         hx-trigger="every 1.5s [!document.activeElement || !document.activeElement.closest('#agent-side')]"
         hx-swap="innerHTML">
      {% include "_agent_side.html" %}
    </div>
    <div class="card" id="agent-flag-card">
      <h3>Registrar flag</h3>
      <p style="color: var(--muted); font-size: 0.85rem;">
        submissão é 100% manual — o framework só valida formato e duplicidade
      </p>
      <form hx-post="/challenges/{{ challenge.id }}/flag"
            hx-target="#flag-result" hx-swap="innerHTML">
        <input name="flag" placeholder="CTF{...}" required>
        <button class="btn">Registrar flag</button>
      </form>
      <div id="flag-result"></div>
    </div>
  </aside>
</div>
{% endblock %}
```

`app/web/templates/_agent_feed.html` (recebe `sess` dict|None e `challenge_id`):

```jinja
{% if not sess %}
<div class="card">
  <p style="color: var(--muted);">Nenhuma sessão. Inicie o agente →</p>
</div>
{% else %}
<div class="card">
  <h3>Sessão <span class="mono">{{ sess.id[:8] }}</span>
    <span class="badge {{ sess.state|badge_for }}">{{ sess.state }}</span></h3>
  {% if sess.findings %}
  <div class="findings">
    <h4>Findings acumulados</h4>
    <ul>{% for f in sess.findings %}<li class="mono">{{ f }}</li>{% endfor %}</ul>
  </div>
  {% endif %}
  {% if sess.pending_decision %}
  <p class="mono" style="color: var(--warning);">
    próximo passo (júri): {{ sess.pending_decision.action }} — {{ sess.pending_decision.reason }}
  </p>
  {% endif %}
  {% for rep in sess.reports %}
  <div class="cycle">
    <h4>Ciclo {{ loop.index }} — especialista</h4>
    <p class="mono" style="color: var(--accent);">
      status: {{ rep.status }}{% if rep.tools_used %} · tools: {{ rep.tools_used|join(", ") }}{% endif %}
    </p>
    {% if rep.findings %}
    <ul>{% for f in rep.findings %}<li class="mono">{{ f }}</li>{% endfor %}</ul>
    {% endif %}
    {% if rep.next_step %}<p class="mono">next: {{ rep.next_step }}</p>{% endif %}
  </div>
  {% endfor %}
</div>
{% endif %}
```

`app/web/templates/_agent_side.html` (recebe `sess` dict|None e `challenge_id`):

```jinja
{% if not sess %}
<div class="card">
  <h3>Status</h3>
  <p style="color: var(--muted);">idle — nenhuma sessão</p>
  <form hx-post="/challenges/{{ challenge_id }}/agent/start"
        hx-target="#agent-side" hx-swap="innerHTML">
    <button class="btn">Iniciar agente</button>
    <span class="htmx-indicator mono" style="color: var(--warning);">
      trabalhando (recon + júri, pode levar minutos)…
    </span>
  </form>
</div>
{% else %}
<div class="card">
  <h3>Status <span class="badge {{ sess.state|badge_for }}">{{ sess.state }}</span></h3>
  {% if sess.state == "awaiting_approval" and sess.pending_decision %}
  <div class="checkpoint">
    <h4>Checkpoint — aguardando você</h4>
    <p><strong class="mono">{{ sess.pending_decision.action }}</strong> — {{ sess.pending_decision.reason }}</p>
    {% if sess.pending_decision.order %}
    <p class="mono">→ {{ sess.pending_decision.order.category }}: {{ sess.pending_decision.order.task }}</p>
    {% endif %}
    {% if sess.pending_decision.flag_candidate %}
    <p class="mono">flag candidata: {{ sess.pending_decision.flag_candidate }}</p>
    {% endif %}
    <div class="actions">
      <button class="btn"
              hx-post="/challenges/{{ challenge_id }}/agent/sessions/{{ sess.id }}/approve"
              hx-target="#agent-side" hx-swap="innerHTML">Aprovar</button>
      <button class="btn ghost"
              hx-post="/challenges/{{ challenge_id }}/agent/sessions/{{ sess.id }}/terminate"
              hx-target="#agent-side" hx-swap="innerHTML"
              hx-confirm="Encerrar esta sessão?">Rejeitar</button>
    </div>
    <form hx-post="/challenges/{{ challenge_id }}/agent/sessions/{{ sess.id }}/adjust"
          hx-target="#agent-side" hx-swap="innerHTML">
      <input name="instruction" placeholder="instrução para o próximo passo do agente" required>
      <button class="btn ghost">Ajustar com instrução</button>
    </form>
  </div>
  {% elif sess.state == "running" %}
  <p class="mono" style="color: var(--warning);">executando ciclo…</p>
  {% elif sess.state == "solved" %}
  <p class="mono" style="color: var(--flag-ok);">solved — flag validada</p>
  {% else %}
  <p style="color: var(--muted);">sessão encerrada ({{ sess.state }})</p>
  {% endif %}
</div>
{% endif %}
```

Nota: os POSTs das ações (`/start`, `/approve`, `/terminate`, `/adjust`) são criados na Task 5 — os templates já referenciam; os testes de ação viram verdes nela.

- [ ] **Step 5: Registrar router em `app/main.py`**

Logo após o include de `agent_api` (que já existe no arquivo):

```python
    from .web import agent_routes
    app.include_router(agent_routes.router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_web_agent_panel.py -v`
Expected: `4 passed`

- [ ] **Step 7: Suite verde**

Run: `python -m pytest -q`
Expected: `112 passed` (106 base + 2 da Task 1 + 4 desta task)

- [ ] **Step 8: Commit**

```powershell
git add app/web/agent_routes.py app/web/templates/agent_panel.html app/web/templates/_agent_feed.html app/web/templates/_agent_side.html app/main.py tests/test_web_agent_panel.py
git commit -m "feat: agent panel page with HTMX feed and side polling"
```

---

### Task 5: Ações do checkpoint (start / approve / terminate / adjust) + teste ponta-a-ponta

**Files:**
- Modify: `app/web/agent_routes.py` (adicionar POSTs)
- Test: `tests/test_web_agent_panel.py` (adicionar testes)

**Interfaces:**
- Consumes: `orch.start/approve/terminate/reject` (Task 1 e existentes); `_scripted` de `tests/test_api_agent.py` para o e2e
- Produces: `POST /challenges/{id}/agent/start`; `POST .../agent/sessions/{sid}/approve|terminate|adjust` — todos retornam o parcial `_agent_side.html`

- [ ] **Step 1: Write the failing tests** (anexar em `tests/test_web_agent_panel.py`)

```python
def test_start_posts_and_renders_side(client):
    c, ch_id, fake = client
    r = c.post(f"/challenges/{ch_id}/agent/start")
    assert r.status_code == 200
    assert fake.calls[0] == ("start", ch_id)
    assert "awaiting_approval" in r.text


def test_start_409_when_session_active(client):
    c, ch_id, fake = client
    fake.sessions["s1"] = AgentSession(id="s1", challenge_id=ch_id,
                                       state="running")
    r = c.post(f"/challenges/{ch_id}/agent/start")
    assert r.status_code == 409
    assert fake.calls == []


def test_approve_terminate_adjust(client):
    c, ch_id, fake = client
    s = AgentSession(id="s1", challenge_id=ch_id, state="awaiting_approval")
    fake.sessions["s1"] = s

    r = c.post(f"/challenges/{ch_id}/agent/sessions/s1/approve")
    assert r.status_code == 200
    assert ("approve", "s1") in fake.calls

    r = c.post(f"/challenges/{ch_id}/agent/sessions/s1/adjust",
               data={"instruction": "focus on the csv file"})
    assert r.status_code == 200
    assert ("adjust", "s1", "focus on the csv file") in fake.calls

    r = c.post(f"/challenges/{ch_id}/agent/sessions/s1/terminate")
    assert r.status_code == 200
    assert "rejected" in r.text
    assert ("terminate", "s1", "rejected via panel") in fake.calls


def test_unknown_session_404(client):
    c, ch_id, _ = client
    r = c.post(f"/challenges/{ch_id}/agent/sessions/nope/approve")
    assert r.status_code == 404
    r = c.post(f"/challenges/{ch_id}/agent/sessions/nope/terminate")
    assert r.status_code == 404
    r = c.post(f"/challenges/{ch_id}/agent/sessions/nope/adjust",
               data={"instruction": "x"})
    assert r.status_code == 404


def test_panel_end_to_end(tmp_path, monkeypatch):
    """Wiring real: painel + orchestrator com modelos scriptados."""
    from app.agents.jury import JuryAgent
    from app.agents.orchestrator import Orchestrator
    from app.agents.specialists import SpecialistRunner
    from app.services.storage import StorageService
    from app.tooling.config import load_tools_config
    from app.tooling.runner import ToolRunner
    from tests.test_api_agent import _scripted
    import app.agents.api as agent_api

    engine = create_engine(f"sqlite:///{tmp_path / 'e.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    def _fake_orchestrator(storage_dir):
        runner = ToolRunner(load_tools_config(), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "recon first",
             "order": {"category": "misc", "task": "look"}, "findings": []},
            {"action": "RECHECK", "reason": "nothing yet", "findings": []},
        ]), persona_text="jury test")
        specialist = SpecialistRunner(_scripted([
            {"status": "NEED_HINT", "findings": ["nothing found"]},
        ]), persona_text="spec test")
        return Orchestrator(runner=runner, jury=jury,
                            specialists={"misc": specialist})

    monkeypatch.setattr(agent_api, "_make_orchestrator", _fake_orchestrator)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        with Session(engine) as session:
            event = Event(name="ctf")
            session.add(event)
            session.commit()
            session.refresh(event)
            ch = Challenge(event_id=event.id, title="c")
            session.add(ch)
            session.commit()
            session.refresh(ch)

        r = c.post(f"/challenges/{ch.id}/agent/start")
        assert r.status_code == 200
        assert "awaiting_approval" in r.text

        r = c.get(f"/challenges/{ch.id}/agent/feed")
        assert "recon first" in r.text

        m = re.search(r"sessions/([0-9a-f]+)/approve", r.text)
        sid = m.group(1)

        r = c.post(f"/challenges/{ch.id}/agent/sessions/{sid}/approve")
        assert r.status_code == 200
        assert "RECHECK" in r.text

        r = c.post(f"/challenges/{ch.id}/agent/sessions/{sid}/terminate")
        assert r.status_code == 200
        assert "rejected" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web_agent_panel.py -v`
Expected: os 5 novos FAIL (404/405 — rotas POST não existem)

- [ ] **Step 3: Implementar os POSTs** (anexar em `app/web/agent_routes.py`)

```python
@router.post("/challenges/{challenge_id}/agent/start")
def agent_start(request: Request, challenge_id: int,
                session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = get_orchestrator(request)
    existing = latest_session(orch, challenge_id)
    if existing and existing.state in ("running", "awaiting_approval"):
        raise HTTPException(status_code=409, detail="session already active")
    try:
        sess = orch.start(session, challenge_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/approve")
def agent_approve(request: Request, challenge_id: int, session_id: str,
                  session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id)
    try:
        sess = orch.approve(session, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/terminate")
def agent_terminate(request: Request, challenge_id: int, session_id: str,
                    session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id)
    sess = orch.terminate(session, session_id, "rejected via panel")
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/adjust")
def agent_adjust(request: Request, challenge_id: int, session_id: str,
                 instruction: str = Form(...),
                 session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id)
    sess = orch.reject(session, session_id, instruction)
    return _side(request, challenge_id, sess)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_web_agent_panel.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```powershell
git add app/web/agent_routes.py tests/test_web_agent_panel.py
git commit -m "feat: panel checkpoint actions (start/approve/terminate/adjust)"
```

---

### Task 6: Submissão manual de flag

**Files:**
- Modify: `app/web/agent_routes.py` (adicionar `POST /challenges/{id}/flag`)
- Create: `app/web/templates/_flag_result.html`
- Test: `tests/test_web_agent_panel.py` (adicionar testes)

**Interfaces:**
- Consumes: `validate_flag(session, challenge, candidate) -> SubmissionResult(accepted, reason)`; `tracking.update_challenge(session, challenge_id, status=..., flag=...)`
- Produces: `POST /challenges/{id}/flag` → parcial `_flag_result.html`

- [ ] **Step 1: Write the failing tests** (anexar em `tests/test_web_agent_panel.py`)

```python
def test_flag_manual_flow(client):
    c, ch_id, _ = client
    r = c.post(f"/challenges/{ch_id}/flag", data={"flag": "CTF{abc}"})
    assert r.status_code == 200
    assert "validada" in r.text

    page = c.get(f"/challenges/{ch_id}").text
    assert "CTF{abc}" in page          # badge na página do challenge
    assert "solved" in page

    r = c.post(f"/challenges/{ch_id}/flag", data={"flag": "CTF{abc}"})
    assert "duplicate submission" in r.text

    r = c.post(f"/challenges/{ch_id}/flag", data={"flag": "nope"})
    assert "does not match format" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_agent_panel.py::test_flag_manual_flow -v`
Expected: FAIL — 405 (rota não existe)

- [ ] **Step 3: Implementar** (anexar em `app/web/agent_routes.py`)

```python
@router.post("/challenges/{challenge_id}/flag")
def submit_flag(request: Request, challenge_id: int, flag: str = Form(...),
                session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    candidate = flag.strip()
    result = validate_flag(session, challenge, candidate)
    if result.accepted:
        tracking.update_challenge(session, challenge_id,
                                  status=ChallengeStatus.solved, flag=candidate)
    return templates.TemplateResponse(request, "_flag_result.html",
                                      {"result": result, "flag": candidate})
```

- [ ] **Step 4: Criar `app/web/templates/_flag_result.html`**

```jinja
{% if result.accepted %}
<p class="mono" style="color: var(--flag-ok);">
  ✔ {{ flag }} — validada; registre manualmente na plataforma
</p>
{% else %}
<p class="mono" style="color: var(--accent);">✖ {{ result.reason }}</p>
{% endif %}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_web_agent_panel.py -v`
Expected: `10 passed`

- [ ] **Step 6: Commit**

```powershell
git add app/web/agent_routes.py app/web/templates/_flag_result.html tests/test_web_agent_panel.py
git commit -m "feat: manual flag submission from agent panel"
```

---

### Task 7: Estilo (CSS) + link na página do challenge

**Files:**
- Modify: `app/static/theme.css` (token `--warning` em `:root` + bloco de estilos do painel)
- Modify: `app/web/templates/challenge.html` (link "painel do agente")
- Test: `tests/test_web_agent_panel.py` (1 teste simples do link)

**Interfaces:**
- Consumes: nada
- Produces: classes `.agent-grid`, `.checkpoint`, `.cycle`, `.findings`, `.badge.warn`, `.htmx-indicator` usadas pelos templates das Tasks 4-6

- [ ] **Step 1: Write the failing test** (anexar em `tests/test_web_agent_panel.py`)

```python
def test_challenge_page_links_panel(client):
    c, ch_id, _ = client
    r = c.get(f"/challenges/{ch_id}")
    assert f'href="/challenges/{ch_id}/agent"' in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web_agent_panel.py::test_challenge_page_links_panel -v`
Expected: FAIL (link ainda não existe)

- [ ] **Step 3: Adicionar link em `app/web/templates/challenge.html`**

Na linha 8-13 (parágrafo mono com categoria/pts), inserir antes do `</p>`:

```jinja
  · <a href="/challenges/{{ challenge.id }}/agent">painel do agente</a>
```

Ficará:

```jinja
<p class="mono" style="color: var(--muted);">
  {{ challenge.category }} · {{ challenge.points }} pts ·
  {% if challenge.target %}target: {{ challenge.target }} · {% endif %}
  {{ summary.attempt_count }} tentativas
  {% if summary.active_seconds is not none %} · {{ (summary.active_seconds / 60) | round(1) }} min ativos{% endif %}
  · <a href="/challenges/{{ challenge.id }}/agent">painel do agente</a>
</p>
```

- [ ] **Step 4: Adicionar estilos em `app/static/theme.css`**

Em `:root` (após `--flag-ok`):

```css
  --warning: #d29922;
```

No fim do arquivo:

```css
/* --- Painel do Agente --- */
.badge.warn { border-color: var(--warning); color: var(--warning); }

.agent-grid {
  display: grid;
  grid-template-columns: 7fr 3fr;
  gap: 1rem;
  align-items: start;
}
@media (max-width: 900px) {
  .agent-grid { grid-template-columns: 1fr; }
}

#agent-side .card { position: sticky; top: 1rem; }

.checkpoint {
  border: 1px solid var(--warning);
  border-radius: 8px;
  padding: 0.75rem;
  margin: 0.5rem 0;
}
.checkpoint .actions { display: flex; gap: 0.5rem; margin: 0.5rem 0; }
.checkpoint form { margin-top: 0.5rem; }

.cycle {
  border-left: 2px solid var(--panel-edge);
  padding-left: 0.75rem;
  margin: 0.75rem 0;
}
.cycle h4, .findings h4 {
  margin: 0.25rem 0;
  color: var(--muted);
  font-size: 0.8rem;
  text-transform: uppercase;
}
.findings ul, .cycle ul { margin: 0.25rem 0; padding-left: 1.25rem; }

.htmx-indicator { display: none; }
.htmx-request .htmx-indicator { display: inline; }
```

- [ ] **Step 5: Run test to verify it passes + suite inteira**

Run: `python -m pytest tests/test_web_agent_panel.py tests/test_ui_pages.py -v`
Expected: `12 passed` no total dos dois arquivos
Run: `python -m pytest -q`
Expected: todos PASS (106 + novos; sem regressão)

- [ ] **Step 6: Commit**

```powershell
git add app/static/theme.css app/web/templates/challenge.html tests/test_web_agent_panel.py
git commit -m "feat: agent panel styling and challenge page link"
```

---

### Task 8: Verificação final manual

**Files:** nenhum

- [ ] **Step 1: Suite completa**

Run: `python -m pytest -q`
Expected: todos PASS

- [ ] **Step 2: Smoke manual (servidor local)**

```powershell
python -m uvicorn app.main:app --port 8000
```

Abrir `http://127.0.0.1:8000/challenges/1/agent`, conferir: tema degradê/vermelho, "Iniciar agente" com spinner, feed polando a cada 1.5s, checkpoint com os 3 botões quando o júri parar, card de flag com validação verde/erro. (Requer o evento/challenge 1 e `agents/config` com chave NVIDIA; se não houver, apenas visual idle é suficiente para o smoke de layout.)

- [ ] **Step 3: git status limpo**

Run: `git status --short`
Expected: apenas os untracked do usuário (`docs/agents-architecture.mmd`, `iniciar_orchestrator.bat`) — nada de trabalho desta task pendente.
