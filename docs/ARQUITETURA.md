# Arquitetura

Documento de desenho interno do CTF Orchestrator: como os subsistemas se encaixam, quais são os
contratos entre eles e quais decisões foram tomadas (e por quê).

Leitura recomendada antes de mexer no código: `app/main.py`, `app/models.py`,
`app/agents/orchestrator.py`, `app/tooling/runner.py`.

---

## 1. Princípios

1. **Monolito em um processo.** Um único FastAPI serve UI, REST, runtime de agentes, sandbox e
   knowledge. Nada de filas, workers ou banco externo.
2. **Uma única camada de serviços.** UI e REST chamam os mesmos serviços
   (`app/services/tracking.py`); nenhuma regra de negócio duplicada na rota.
3. **PE (Persistent/Process Memory) é o tracking.** Agentes são stateless: cada invocação monta o
   contexto do zero a partir do banco. Não há memória de conversa entre papéis.
4. **O LLM decide tática, o framework decide execução.** O modelo escolhe ferramenta e argumentos
   semânticos; o framework valida, monta o `argv` e executa sem shell.
5. **Checkpoint humano por ciclo.** Nenhum passo de ataque roda sem aprovação explícita do
   operador.
6. **Determinismo onde dá.** Recon, validação de flag e priorização são código, não LLM.

---

## 2. Camadas

```
Rotas HTML (app/web)         Rotas REST (app/api)
        │                            │
        └──────────┬─────────────────┘
                   ▼
        Serviços (app/services)
        ├── tracking.py  → regras de Event/Challenge/Attempt/Note
        └── storage.py   → arquivos em storage/
                   │
   ┌───────────────┼────────────────────┬─────────────────────┐
   ▼               ▼                    ▼                     ▼
SQLite         ToolRunner           Knowledge            Agent Runtime
(ctf.db)       (subprocess)         (FTS5 + markdown)    (pydantic-ai)
```

Dependências permitidas: `web/api → services → (tooling | knowledge | agents) → models`.
`agents` pode usar `tooling`, `knowledge` e `services`, nunca o contrário.

---

## 3. Modelo de dados

SQLModel sobre SQLite (`ctf.db`). Tabelas e campos em `app/models.py`.

| Tabela | Campos principais | Notas |
|---|---|---|
| `Event` | `id`, `name`, `url`, `flag_format`, `starts_at`, `ends_at`, `status`, `created_at` | `flag_format` é o regex usado na validação de flag |
| `Challenge` | `id`, `event_id`, `title`, `category`, `points`, `difficulty`, `status`, `flag`, `flag_format`, `description`, `target`, `tags`, `created_at`, `first_interaction`, `solved_at` | `target` (`host:porta`) é o **único** alvo permitido pelo sandbox |
| `Attempt` | `id`, `challenge_id`, `timestamp`, `type`, `description`, `tool_command`, `result`, `learning` | uma linha por tentativa; `learning` alimenta o contexto do júri |
| `Note` | `id`, `challenge_id`, `timestamp`, `content` | append-only; "nota atual" = a mais recente |
| `Attachment` | `id`, `challenge_id`, `name`, `path`, `mime`, `size`, `type`, `created_at` | `path` é relativo a `storage/` |

Enums (`app/models.py`):

- `EventStatus`: `upcoming`, `active`, `finished`
- `ChallengeStatus`: `open`, `solved`, `gave_up`
- `Difficulty`: `easy`, `medium`, `hard`, `insane`
- `AttemptType`: `manual`, `tool`, `agent`, `submission`
- `AttemptResult`: `failed`, `partial`, `breakthrough`, `solved`
- `AttachmentType`: `screenshot`, `download`, `output`

**Derivados, nunca armazenados**: tempo ativo e contagem de tentativas. Calculados em
`tracking.challenge_summary()` a partir de `first_interaction`, `solved_at` e `Attempt`.

**Migração leve**: `database.ensure_schema()` compara os modelos com as colunas existentes e faz
`ALTER TABLE ... ADD COLUMN` para o que faltar. Cobre adição de coluna; mudanças destrutivas são
manuais.

---

## 4. Serviços

### `tracking.py` — regras de negócio

- `create_event`, `list_events`, `set_event_status`
- `create_challenge`, `get_challenge`, `list_challenges(event_id, category, status, tag)` — o
  filtro de tag é feito em Python (tags são string CSV)
- `record_attempt(...)` — carimba `first_interaction` na primeira tentativa e, quando
  `result=solved`, marca o challenge como `solved`, grava `solved_at` e a flag
- `update_challenge(status, flag)`
- `list_attempts`, `challenge_summary`
- `add_note`, `note_history`, `current_note`

### `storage.py` — arquivos

`StorageService.save(challenge_id, filename, data)` grava em
`storage/<challenge_id>/<8hex>_<nome-sanitizado>` e devolve o `Path`. O nome é reduzido a
`Path(...).name`, então `../` no upload não escapa do diretório.

---

## 5. ToolRunner (sandbox de ferramentas)

Arquivos: `app/tooling/config.py` (YAML), `app/tooling/runner.py` (execução),
`app/tooling/doctor.py` (inventário).

### Allowlist

`config/tools.yaml`:

```yaml
defaults: {timeout_s: 300, output_limit_mb: 10}
tools:
  nmap:    {mode: native, guard: strict}
  ncat:    {mode: native, guard: strict}
  curl:    {mode: native, guard: strict}
  strings: {mode: native, guard: strict}
  gdb:     {mode: native, guard: strict}
  objdump: {mode: native, guard: strict}
  sqlmap:  {mode: native, guard: strict}
  python:  {mode: native, guard: none}
```

Ferramenta fora da allowlist → `ToolNotAllowed`. Ferramenta na allowlist mas ausente do `PATH` →
`ToolNotAllowed` com a mensagem de "not found on PATH".

### Guards

`guard: strict` (default) aplica duas checagens em cada argumento:

1. **Metacaracteres de shell**: `; | & $ ` \n \r` → `UnsafeArg`.
2. **Alvo**: todo IPv4 ou domínio encontrado no argumento precisa estar em
   `{localhost, 127.0.0.1, ::1, 0.0.0.0} ∪ {host de challenge.target}` → senão `TargetNotAllowed`.

`guard: none` pula as duas (usado por `python`, que precisa de flags e caminhos arbitrários).
Isso é deliberado e é o ponto de maior confiança do sistema: `python` pode fazer qualquer coisa
que o processo do servidor pode.

### Execução

- `argv = [executável_resolvido, *args]`, executado com `subprocess.run(..., shell=False)`,
  `capture_output=True`, `timeout` e `CREATE_NO_WINDOW` no Windows.
- `cwd` = `storage/sandbox/<event_id>/<challenge_id>/` (criado sob demanda).
- Timeout estourado → `returncode=None`, output parcial preservado.
- Output acima de `output_limit_mb` → trunca `stdout`, descarta `stderr`, marca `truncated=True`.
  Limite `<= 0` vira 1 KB (nunca "ilimitado").

### Persistência

Duas entradas:

- `run_raw(...)`: executa e devolve `ToolRunResult` **sem** gravar nada. Usado pelo recon.
- `run(session, challenge, tool, args, attempt_result=...)`: executa, grava o output completo como
  `Attachment(type=output)` e registra um `Attempt(type=tool)` com `tool_command` = linha de
  comando e `learning` = primeiros 2000 caracteres do stdout.

### Doctor

`inventory(settings)` devolve `[{name, mode, path, present}]` usando `shutil.which`. Ainda não há
rota expondo isso — é a base do futuro `/api/tools/inventory` e da aba de configurações.

---

## 6. Knowledge subsystem

### Vendoring

`knowledge/` contém cópias **content-only** de dois repositórios, fixadas por commit em
`knowledge/knowledge.lock`:

| Repositório | Pasta | Conteúdo usado |
|---|---|---|
| `ljagiello/ctf-skills` | `knowledge/ctf-skills/` | `*/SKILL.md` por categoria |
| `Felipebc2/Dispatch` | `knowledge/dispatch/` | `Arsenal/<CATEGORIA>/**` + manuals |

`scripts/sync_knowledge.py` clona/fetch raso, faz checkout do commit, **remove o `.git`** e grava o
lock. Atualização é sempre manual (nada de auto-update no boot).

### Parser

`app/knowledge/parse.py` percorre `**/*.md` e indexa apenas:

- caminhos que contêm `SKILL.md` → `source="skill"`, `category=<pasta do skill>`;
- caminhos que contêm `Arsenal` → `source="dispatch-arsenal"`, `category=<pasta logo após Arsenal>`.

Frontmatter YAML é separado do corpo; o título vem de `title:` no frontmatter, senão do primeiro
`# H1`, senão do nome do arquivo. O corpo indexado é `str(meta) + "\n" + corpo` (o frontmatter
entra no índice para casar com tags como `tags: [sqli]`).

### Índice e busca

`KnowledgeIndex` cria a tabela virtual FTS5 `knowledge_fts(source, category, title, path, body)` no
próprio `ctf.db`.

- `rebuild(root)`: apaga e reinsere todas as entradas; devolve a contagem.
- `count()`: linhas indexadas.
- `search(q, limit, category)`: `MATCH` + `bm25()` + `snippet(...)`; se a query for sintaxe FTS
  inválida, cai para `LIKE %q%` em `body`. Se o índice estiver vazio, reconstrói na primeira busca.

A busca é **on-demand**: o `specialist_context` consulta o índice a cada dispatch, com
`category=<categoria do especialista>` e fallback para busca sem filtro.

### Exposição

- REST: `POST /api/knowledge/rebuild`, `GET /api/knowledge/search`, `GET /api/knowledge/skills`.
- CLI: `python scripts/knowledge-search.py "<query>"`.
- Runtime: snippets entram no prompt do especialista como "Arsenal references (use these
  techniques)".

---

## 7. Agent Runtime

Arquivos: `app/agents/` — `orchestrator.py` (loop), `jury.py`, `specialists.py`, `recon.py`,
`submission.py`, `context.py` (builders), `payloads.py` (contratos), `config.py` (YAML),
`models.py` (providers), `planner.py` (priorização, ainda não ligada).

### Papéis

| Papel | Persona | Modelo | Responsabilidade |
|---|---|---|---|
| OP | `op.md` | `strong` | coordena e consolida (o `Orchestrator`) |
| PL | `planner.md` | `strong` | prioriza por pontos/dificuldade, define parada |
| RC | `recon.md` | `fast` | recon determinístico de fato (`recon_bundle`) |
| JU | `jury.md` | `strong` | analisa achados, decide ação, despacha **um** especialista por ciclo |
| S1–S6 | `specialist_*.md` | `fast` | executam a tarefa com ferramentas e devolvem relatório estruturado |
| SU | `submission.md` | `fast` | valida regex/duplicidade da flag (implementado em código) |
| PE | — | — | o tracking (`app/services/tracking.py`) |
| SB | — | — | o ToolRunner |

Os arquivos `specialist_*.md` **não existem** hoje; `app/agents/api.py::_persona_text` cai num
texto default ("Você é o agente '<nome>' deste framework...") quando o arquivo falta.

### Estado da sessão

`AgentSession` (dataclass em memória): `id`, `challenge_id`, `state`, `pending_decision`,
`findings`, `reports`, `created_at`.

```
            start()
   ┌──────────────────────────► running
   │                               │  recon + júri
   │                               ▼
   │                       awaiting_approval ◄──── reject(feedback) ──┐
   │                          │    │    │                             │
   │              approve()   │    │    │   terminate()               │
   │        ┌─────────────────┘    │    └──────────────► rejected    │
   │        ▼                      │                                   │
   │   dispatch/decide ────────────┘ (novo ciclo, volta ao checkpoint)│
   │        │                                                          │
   │        ├── GIVE_UP ──► gave_up                                    │
   │        └── SUBMIT aceito ──► solved                               │
   └───────────────────────────────────────────────────────────────────┘
```

Estados possíveis: `running`, `awaiting_approval`, `solved`, `gave_up`, `rejected`.

### API pública do orquestrador

| Método | Efeito |
|---|---|
| `start(session, challenge_id)` | roda `recon_bundle`, cria a sessão, chama o júri e **para** em `awaiting_approval` |
| `approve(session, session_id)` | executa **uma** decisão pendente: `GIVE_UP` encerra; `DISPATCH` roda o especialista e pede nova decisão; `SUBMIT` valida a flag (aceita → `solved`; recusada → nova decisão); `RECHECK`/`WAIT_USER` apenas pedem nova decisão |
| `reject(session, session_id, feedback)` | grava nota `[agent rejected] ...` e pede nova decisão **com** o feedback do operador (é o "Ajustar") |
| `terminate(session, session_id, reason)` | encerra de vez: `state=rejected`, limpa `pending_decision`, grava nota `[agent terminated] ...` |

Regras: `approve` em sessão que não está em `awaiting_approval`/`rejected` levanta `ValueError`
(HTTP 409 na API). Falha de especialista (exceção) é convertida em `SpecialistReport(status=
"FAILED", findings=["specialist crashed: ..."])` — o loop nunca morre por erro de modelo/ferramenta.

### Recon determinístico

`recon_bundle(runner, session, challenge)`:

1. para cada `Attachment(type=download)`: `strings <arquivo>` (timeout 30 s, output 1 MB), guarda
   até 40 linhas e resume as 8 primeiras;
2. se `challenge.target`: `nmap --top-ports 100 <host>` (timeout 120 s), filtra linhas `/tcp`;
3. registra `Attempt(type=agent, tool_command="recon_bundle")` com o resumo como `learning`;
4. devolve `ReconDigest(target, files, summary)`.

Falha de ferramenta ausente/guard é capturada e vira linha "skipped (motivo)" — recon nunca
derruba o `start()`.

> ⚠️ **Bug latente**: o bloco final faz `challenge.status = ChallengeStatus.in_progress`, valor que
> não existe no enum (só `open|solved|gave_up`). Ele só é alcançado quando
> `challenge.first_interaction is None`. Em um challenge **nunca tocado**, `start()` levanta
> `AttributeError` → HTTP 500. Ver `docs/DESENVOLVIMENTO.md#dívida-técnica-conhecida`.

### Júri

`JuryAgent.decide(session, challenge, reports, extra_feedback)` monta o contexto (challenge card,
artefatos, tentativas recentes, relatórios anteriores, feedback opcional) e chama o modelo com
`output_type=JuryDecision`.

```python
class JuryDecision(BaseModel):
    action: Literal["DISPATCH", "RECHECK", "SUBMIT", "GIVE_UP", "WAIT_USER"]
    order: TaskOrder | None            # obrigatório quando action == DISPATCH
    flag_candidate: str | None
    reason: str
    findings: list[str]

class TaskOrder(BaseModel):
    category: str                      # web | pwn | crypto | reversing | forensics | osint
    task: str
    hints: list[str] = []
    max_tool_runs: int = 5

class SpecialistReport(BaseModel):
    status: Literal["SOLVED", "NEED_HINT", "FAILED"]
    flag_candidate: str | None
    findings: list[str]
    next_step: str | None
    tools_used: list[str]
```

Um validador Pydantic rejeita `DISPATCH` sem `order`.

### Especialistas

`SpecialistRunner` cria um `Agent` pydantic-ai com uma única tool:

```python
@agent.tool(retries=4)
def run_tool(ctx, tool: str, args: list[str]) -> str:
    ...
```

A tool chama `ToolRunner.run(...)` (portanto **persiste** Attachment + Attempt) e devolve
`rc`, `truncated`, `stdout` (até 4000 chars) e `stderr` (até 1000). Violação de allowlist/guard
levanta `ModelRetry` com a lista de ferramentas permitidas, para o modelo corrigir a chamada em vez
de falhar.

Categorias registradas em `app/agents/api.py`: `web`, `pwn`, `crypto`, `reversing`, `forensics`,
`osint`. `TaskOrder.category` é normalizado com `.lower()`; categoria sem especialista vira
`SpecialistReport(status="FAILED")` com finding explicativo.

### Submissão

`validate_flag(session, challenge, candidate)` é o gate determinístico:

1. `re.fullmatch(event.flag_format or r"CTF\{.*\}", candidate)` → falha grava
   `Attempt(submission, failed)` e devolve `accepted=False`;
2. duplicidade: já existe `Attempt(type=submission, tool_command == candidate)` → idem;
3. aceito → grava `Attempt(submission, solved, flag=candidate)`, o que marca o challenge como
   `solved` (via `tracking.record_attempt`).

Nenhuma chamada de rede. A rota web ainda chama `tracking.update_challenge(status=solved,
flag=...)` para garantir o estado mesmo se a tentativa já existir.

### Contexto por papel

`app/agents/context.py`. Todos os builders são funções puras sobre o banco e truncam pelo **fim**
(descarta o mais antigo), com marcador `…(older context truncated)`.

| Builder | Budget (caracteres) | Conteúdo |
|---|---|---|
| `jury_context` | 48 000 | challenge card + artefatos + últimas tentativas (learning até 200 chars) |
| `specialist_context` | 32 000 | challenge card + tarefa/hints do júri + top-3 do Arsenal + artefatos |
| `submission_context` | 16 000 | challenge card + formato do evento + flag candidata + submissões anteriores |
| `CONTEXT_BUDGETS["op"]` | 16 000 | reservado para o OP (o loop atual não monta prompt de OP) |

Os budgets são **caracteres**, não tokens (a spec original falava em ~8K/12K/4K tokens; a
implementação usa uma aproximação conservadora em chars).

### Modelos e providers

`app/agents/models.py`:

- `resolve_for_role(role, agents_cfg_role, models_cfg)` tenta o papel (`strong`/`fast`) e depois a
  lista de `fallbacks`; o primeiro provider cuja env var existe vence.
- Provider sem chave → `ProviderUnavailable` (vira 503 no painel, não 500).
- Provider `google` tem caminho dedicado (`GoogleModel`); todo o resto usa
  `OpenAIChatModel` + `OpenAIProvider(base_url=...)`, ou seja, qualquer endpoint
  OpenAI-compatible funciona (NVIDIA NIM, 9router, etc.).

`config/agents.yaml` define papel → persona + papel de modelo, além de `limits`
(`max_attempts: 12`, `max_minutes: 60`). **Os limits ainda não são enforçados** — são lidos e
guardados no `Orchestrator.limits`.

---

## 8. UI (Jinja2 + HTMX)

### Páginas

| Rota | Template | Conteúdo |
|---|---|---|
| `/` | `dashboard.html` | eventos com progresso, challenges abertos, feed de tentativas |
| `/events/{id}` | `event.html` | grid de challenges + filtros + formulário de cadastro |
| `/challenges/{id}` | `challenge.html` | descrição, anexos, timeline, notas, link para o painel, registro de tentativa |
| `/challenges/{id}/agent` | `agent_panel.html` | feed + lateral (status/checkpoint/flag) |

### Painel do Agente — desenho

- Grid `7fr / 3fr` (`.agent-grid`), colapsa para uma coluna abaixo de 900 px.
- **Feed** (`#agent-feed`): `hx-get .../agent/feed`, `hx-trigger="every 1.5s"`, swap `innerHTML`.
- **Lateral** (`#agent-side`): polling com guard:
  ```
  every 1.5s [!document.querySelector('#agent-side .checkpoint')
               && (!document.activeElement || !document.activeElement.closest('#agent-side'))]
  ```
  Ou seja: pausa enquanto há checkpoint na tela e enquanto o foco está dentro da lateral — evita
  "comer" cliques e digitação.
- **Sessão exibida**: `latest_session(orch, challenge_id)` = sessão mais recente daquele challenge
  (não há `session_id` na URL).
- **Ações** (todas devolvem o parcial `_agent_side.html`):
  `start`, `sessions/{id}/approve`, `sessions/{id}/terminate`, `sessions/{id}/adjust`.
- **Indicadores**: `hx-disabled-elt="this"` desabilita o botão durante a requisição;
  `#start-spinner` e `#cycle-spinner` mostram "pode levar minutos"; `hx-confirm` no Rejeitar.
- **Erros**: listener global `htmx:responseError` mostra `status + corpo` em `#agent-error-card`.
  Isso cobre 409 (sessão já ativa) e 503 (provider sem chave).
- **Flag**: form `hx-post /challenges/{id}/flag` → parcial `_flag_result.html`; em sucesso o
  challenge é marcado `solved` e o badge fica verde.

### Semântica visual

Tokens em `app/static/theme.css`: fundo `#0a0a0a → #1a0d0d`, accent `#e01b24`, texto `#e8e3e1`,
muted `#8f8886`, warning `#d29922`, **verde `#2ea043` só para flag confirmada**.

`badge_for(state)` (filtro Jinja em `app/templates.py`):

| Estado | Classe | Cor |
|---|---|---|
| `solved` | `flag-ok` | verde |
| `awaiting_approval`, `running` | `warn` | amarelo |
| `gave_up`, `rejected`, desconhecido | `solved` (fallback) | cinza/muted |

O nome do fallback (`solved`) é histórico: a classe CSS `.badge.solved` é a cinza.

---

## 9. Concorrência e ciclo de vida

- **Um processo, sem threads de trabalho.** `approve`/`start` rodam **sincronamente** dentro da
  requisição HTTP: o navegador fica esperando o ciclo inteiro (especialista + júri), o que pode
  levar minutos. É aceito no MVP; execução em background está no backlog.
- **Sessões em memória** (`Orchestrator.sessions`, guardado em `app.state._orchestrator`). Reiniciar
  o servidor apaga as sessões; o tracking (banco) permanece.
- `get_orchestrator(request)` cria o runtime sob demanda e cacheia em `app.state` — a UI e a REST
  compartilham a mesma instância (mesmas sessões).
- **Engine por requisição**: `get_session()` chama `default_engine()` a cada request (cria engine,
  roda `create_all` e `ensure_schema`). Funciona, mas é desperdício — ver dívida técnica.
- SQLite com `check_same_thread=False`; escrita concorrente real não é um cenário do MVP (um
  operador, uma sessão ativa por challenge).

---

## 10. Decisões arquiteturais (resumo)

| # | Decisão | Motivo |
|---|---|---|
| 1 | FastAPI + Jinja2 + HTMX, sem SPA e sem build step | um operador, deploy local, zero toolchain JS |
| 2 | Polling HTMX 1.5 s em vez de SSE/WebSocket | o ciclo é lento (minutos); polling é trivial e resiliente a restart |
| 3 | Submissão 100% manual | decisão de produto: o framework não se conecta a plataformas |
| 4 | Tracking como única memória (agentes stateless) | contexto controlado por papel, sem inflar histórico de conversa |
| 5 | Checkpoint humano por ciclo | o modelo propõe, o operador autoriza; nenhum ataque sem aprovação |
| 6 | Recon e validação de flag em código, não em LLM | determinismo e reprodutibilidade |
| 7 | ToolRunner com allowlist + guard de alvo + `shell=False` | conter o raio de ação do modelo |
| 8 | Só NVIDIA nesta fase (strong/fast + fallback) | chave disponível e OpenAI-compatible |
| 9 | Sessões em memória no MVP | simplicidade; persistência é o próximo item do backlog |
| 10 | Sandbox nativo Windows (Docker/Kali adiado) | ferramentas já instaladas no host; menos fricção no evento |
| 11 | Knowledge vendorizado e fixado por commit | reprodutibilidade durante a competição, sem depender de rede |
| 12 | `python` com `guard: none` | scripts e one-liners exigem argumentos livres; risco aceito e documentado |
