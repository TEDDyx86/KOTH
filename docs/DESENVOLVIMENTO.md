# Desenvolvimento

Guia para evoluir o framework sem quebrar o que já funciona.

---

## 1. Ambiente de desenvolvimento

```powershell
cd "C:\Users\Administrator\Documents\GitHub\CTF FRAMEWORK"
python -m pip install -e .[dev]
python -m pytest tests -q          # 122 passed
python -m uvicorn app.main:app --port 8777 --reload
```

`--reload` reinicia o processo a cada mudança em `.py` (apaga sessões de agente). Templates
`.html` recarregam sozinhos, sem restart.

---

## 2. Fluxo de trabalho (regra do repo)

1. **Nunca commitar direto na `main`.**
2. Criar branch de feature/fix: `git checkout -b feat/<assunto>` ou `fix/<assunto>`.
3. **TDD**: escrever o teste que falha → implementar → teste passa → refatorar.
4. Rodar a suíte inteira antes de commitar.
5. Merge **fast-forward/direto na `main`** (preferência do operador) e apagar a branch.

```powershell
git checkout -b fix/exemplo
# ... testes + código ...
python -m pytest tests -q
git add app/... tests/...
git commit -m "fix: ..."
git checkout main
git merge --ff-only fix/exemplo
git branch -d fix/exemplo
```

`git add` sempre com **caminhos explícitos** — existem arquivos não rastreados do operador
(`docs/agents-architecture.mmd`, `iniciar_orchestrator.bat`) que não devem entrar por acidente.

Commits seguem Conventional Commits em inglês (`feat:`, `fix:`, `docs:`, `test:`), corpo curto e
explicando o **porquê**.

---

## 3. Testes

```powershell
python -m pytest tests -q            # suíte do projeto
python -m pytest tests -q -k agent   # subconjunto
```

**Sempre passe `tests` explicitamente.** `pytest -q` na raiz tenta coletar
`knowledge/ctf-skills/tests` (repositório vendorizado, com dependências próprias) e quebra a
coleta.

Mapa da suíte:

| Arquivo | Cobre |
|---|---|
| `test_models.py`, `test_database_schema.py` | modelos e migração leve |
| `test_tracking_*.py` | eventos, challenges, tentativas, notas |
| `test_storage.py` | sanitização de nome e gravação |
| `test_tool_config.py`, `test_tool_doctor.py` | YAML de ferramentas e inventário |
| `test_tool_runner_guard.py` | allowlist, metacaracteres, alvo |
| `test_tool_runner_exec.py`, `test_tool_runner_track.py` | execução real (python) e persistência |
| `test_knowledge_parse.py`, `test_knowledge_index.py`, `test_sync_knowledge.py` | parser, FTS5, lock |
| `test_agent_config.py`, `test_agent_models.py` | YAML de agentes e resolução de provider |
| `test_agent_payloads.py`, `test_agent_context.py`, `test_agent_mech.py` | contratos e builders |
| `test_jury_loop.py`, `test_orchestrator.py`, `test_agent_terminate.py` | loop, checkpoint, terminate |
| `test_submission.py` | regex, duplicidade, marcação de solved |
| `test_api_*.py` | rotas REST |
| `test_ui_dashboard.py`, `test_ui_pages.py` | páginas HTML |
| `test_web_agent_panel.py` | painel: parciais, ações, 503/409, e2e scripted |

Padrões usados:

- **LLM fake**: `FakeJury`/`FakeSpecialist` com decisões roteirizadas (sem rede).
- **`FakeOrch`**: injetado em `app.state._orchestrator` para testar rotas sem provider.
- **e2e scripted**: sequência `start → approve → ...` verificando o HTML devolvido.
- Banco em memória/arquivo temporário + `create_app(engine=..., storage_dir=...)`.

---

## 4. Convenções

- Python 3.12+, type hints, dataclasses/Pydantic para contratos, sem framework extra.
- Regra de negócio em `app/services/`; rota só orquestra HTTP.
- Nada de caminho absoluto no código: `app/config.py::BASE_DIR` resolve tudo.
- Docs e comentários de produto em **português**; identificadores e commits em **inglês**.
- CSS: usar tokens de `:root` (`--accent`, `--muted`, `--warning`, `--flag-ok`). Verde
  (`--flag-ok`) é exclusivo de flag confirmada.
- Template Jinja: partials começam com `_` (`_agent_side.html`, `_agent_feed.html`,
  `_flag_result.html`).
- Não introduza dependência de rede em testes (mock de modelo, nunca chamada real).

---

## 5. Como estender

### 5.1 Nova ferramenta no sandbox

1. Adicione em `config/tools.yaml`:
   ```yaml
   ferramenta: {mode: native, guard: strict}
   ```
2. Confirme que está no `PATH` (`python -c "import shutil; print(shutil.which('ferramenta'))"`).
3. Teste em `tests/test_tool_runner_guard.py` se o guard cobre os argumentos típicos (IP/domínio).
4. Atualize a lista de ferramentas na mensagem de `ModelRetry` em
   `app/agents/specialists.py` (o modelo usa essa lista para se corrigir).

Se a ferramenta precisa de pipes/redirect, ela **não** é compatível com `guard: strict` — escreva
um script Python e rode via `python`, ou implemente um wrapper.

### 5.2 Nova persona / papel de agente

1. Crie `agents/personas/<papel>.md` (texto de sistema, terse, escopo explícito).
2. Registre em `config/agents.yaml`:
   ```yaml
   roles:
     meu_papel:
       persona: personas/meu_papel.md
       model: fast        # ou strong
   ```
3. Instancie com `resolve_for_role("meu_papel", cfg.roles["meu_papel"].model, models_cfg)` e passe
   `persona_text=_persona_text("meu_papel")`.

Personas existentes: `op`, `planner`, `recon`, `jury`, `submission`. As de especialista
(`specialist_web`, ...) **faltam** — hoje caem no texto default de `_persona_text`.

### 5.3 Novo especialista

1. Adicione a categoria em `SPECIALIST_CATEGORIES` (`app/agents/api.py`).
2. Garanta que o júri possa pedir essa categoria (`TaskOrder.category`, normalizado em lower).
3. Crie a persona correspondente e ajuste o mapeamento em `config/agents.yaml`.
4. O runner é genérico (`SpecialistRunner`) — não precisa de classe nova.

### 5.4 Novo provider / modelo

`config/models.yaml`:

```yaml
providers:
  meu_provider:
    base_url: https://api.exemplo.com/v1
    env_var: MEU_PROVIDER_API_KEY
roles:
  strong: {provider: meu_provider, name: modelo-grande}
  fast:   {provider: meu_provider, name: modelo-rapido}
fallbacks:
  strong:
    - {provider: nvidia, name: nvidia/nemotron-3.5-super-vl-120b-a12b}
```

Qualquer endpoint OpenAI-compatible funciona sem código novo. Providers com SDK próprio (Google)
exigem um branch em `resolve_model`. Fallback só cobre **provider indisponível** (env var ausente),
não erro 503 em runtime — isso está no backlog.

### 5.5 Nova fonte de conhecimento

1. Vendorize em `knowledge/<nome>/` via `scripts/sync_knowledge.py` (adicione a tupla em
   `DEFAULT_REPOS`) ou copie manualmente.
2. Se o layout não for `*/SKILL.md` nem `.../Arsenal/<CAT>/...`, estenda
   `app/knowledge/parse.py::parse_knowledge_dir` com a nova regra de `source`/`category`.
3. Reconstrua: `POST /api/knowledge/rebuild`.

### 5.6 Nova rota

- REST → `app/api/<dominio>.py` com `router = APIRouter()`, incluído em `app/main.py` com prefixo.
- HTML → `app/web/routes.py` (páginas) ou `app/web/agent_routes.py` (painel).
- Regra de negócio nova → `app/services/`, com teste unitário antes.

---

## 6. Checklist antes do merge

- [ ] `python -m pytest tests -q` verde (148+)
- [ ] teste novo cobre o comportamento adicionado (TDD real, não teste retroativo)
- [ ] nenhum caminho absoluto, nenhuma chave/segredo no código
- [ ] `config/*.yaml` válido (`python -c "import yaml;yaml.safe_load(open(...))"`)
- [ ] docs atualizados se mudou rota, config ou comportamento
- [ ] `git add` com caminhos explícitos (sem untracked do operador)
- [ ] branch mergeada na `main` e removida

---

## 7. Dívida técnica conhecida

| # | Problema | Onde | Status |
|---|---|---|---|
| 1 | `ChallengeStatus.in_progress` inexistente no `recon.py` | `app/agents/recon.py` | ✅ corrigido (removido; adicionado `detect_file_type` + staging) |
| 2 | Sessões de agente só em memória | `app/models.py`, `app/agents/orchestrator.py` | ✅ persistido em `AgentSessionRecord` (SQLite) |
| 3 | `start`/`approve` apenas síncronos | `app/agents/orchestrator.py`, `app/web/agent_routes.py`, `app/agents/api.py` | ✅ suporte opcional a `background=true` (thread + polling) |
| 4 | `limits.max_attempts`/`max_minutes` ignorados | `app/agents/planner.py`, `app/agents/orchestrator.py` | ✅ enforçados via `check_stop_criteria` |
| 5 | `planner.py` não ligado | `app/agents/orchestrator.py`, `app/agents/api.py` | ✅ ligado no loop (`reassess_strategy`) + `GET /api/agent/events/{id}/rank` |
| 6 | Sem inventário de ferramentas exposto | `app/agents/api.py` | ✅ exposto em `GET /api/agent/tools/inventory` |
| 7 | Personas de especialista ausentes | `agents/personas/` | ✅ criadas `specialist_web.md` … `specialist_osint.md` |
| 8 | Engine SQLite criada por request | `app/database.py` | ✅ cacheada por `db_path` |
| 9 | `reject` não valida estado | `app/agents/orchestrator.py`, `app/web/agent_routes.py` | ✅ valida `awaiting_approval` e devolve 409 |
| 10 | Rotas do painel não checam `session.challenge_id == challenge_id` | `app/web/agent_routes.py` | ✅ validado em `_session_or_404` (404 se divergir) |
| 11–13 | Enums e import duplicado | `recon.py`, `orchestrator.py`, `tracking.py` | ✅ limpos (`AttemptType`, `AttachmentType`, `select()`) |
| 14 | Fallback de modelo só cobre env var ausente | `app/agents/models.py`, `jury.py`, `specialists.py` | ✅ `run_with_fallback` tenta próximos candidatos em 503/429/timeout |
| 15 | Sem custo/tokens por ciclo | `jury.py`, `specialists.py`, `orchestrator.py` | ✅ captura `result.usage` e grava no `Attempt.learning` |

Pendências restantes:
- Imagem/stack Docker Kali pronta para ativar `mode: docker` nas ferramentas em `config/tools.yaml`.
- UI de configurações (aba Agents/Tools).
