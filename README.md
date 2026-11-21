# CTF Orchestrator

Framework orquestrador especialista em CTF, para um único operador, rodando como app web local
(**FastAPI + HTMX + SQLite**). Ele cobre o ciclo de vida de um desafio do início ao fim:

- **tracking** de eventos, challenges, tentativas, notas e artefatos;
- **execução de ferramentas** em sandbox com allowlist, guard de alvo e timeout;
- **conhecimento embutido** (skills e Arsenal de CTF indexados em FTS5);
- **agentes de IA** (orquestrador, planejador, recon, júri e especialistas por categoria)
  com **checkpoint humano obrigatório** antes de qualquer ataque.

Decisão de produto central: **o framework é o host dos agentes**. Ele monta o prompt, injeta o
conhecimento, aplica o checkpoint, chama o LLM direto e grava tudo no tracking. Não existe
dependência de MCP, de plataforma de CTF ou de orquestrador externo.

> **Submissão de flag é 100% manual.** O framework valida formato e duplicidade e mostra a flag
> para você colar na plataforma. Ele nunca se conecta a CTFd/HTB/etc.

---

## Status atual

| Subsistema | Estado | Onde |
|---|---|---|
| 1. Núcleo de tracking | ✅ implementado | `app/models.py`, `app/services/tracking.py`, UI em `app/web/` |
| 2. Integração com plataforma CTF | ⛔ cancelado por decisão de produto | — |
| 3. Executor de ferramentas (ToolRunner) | ✅ implementado (sandbox nativo Windows) | `app/tooling/` |
| 4. Knowledge subsystem | ✅ implementado (vendoring + FTS5) | `app/knowledge/`, `knowledge/` |
| 5. Agent Runtime | ✅ implementado (loop + checkpoint) | `app/agents/` |
| Painel do Agente (UI §9) | ✅ implementado | `app/web/agent_routes.py`, `app/web/templates/agent_panel.html` |

**Suíte de testes:** 122 testes passando (`python -m pytest tests -q`).

Prova de funcionamento ponta a ponta: o challenge **#2 (`IDP - Sorteios Especiais`)** foi
resolvido pelo fluxo real (RNG semeado pelo unix epoch, bola 42 → `Random(seed).Next() % 1000 + 1`).

### O que ainda não existe (backlog acordado)

- Persistir sessões de agente no banco (hoje vivem em memória e somem no restart do servidor).
- Retry automático quando o provider NIM devolve 503.
- Enforçar `limits.max_attempts` / `limits.max_minutes` (hoje são carregados e ignorados).
- Wiring do `planner.py` no loop do orquestrador.
- Endpoint de inventário de ferramentas (`Tool Doctor` existe como módulo, falta expor).
- Decompilador .NET / Ghidra / pwntools na allowlist; sandbox Docker/Kali.
- Providers extras (Anthropic/OpenAI/9router já declarados em `config/models.yaml`, sem uso ainda).
- UI de configurações (aba Agents/Tools).

Detalhes em [`docs/DESENVOLVIMENTO.md`](docs/DESENVOLVIMENTO.md#dívida-técnica-conhecida).

---

## Requisitos

- **Python 3.12+** (o ambiente validado roda 3.14).
- **Windows** nesta fase (o ToolRunner usa `CREATE_NO_WINDOW` e caminhos nativos). Linux/macOS
  funcionam para o tracking e os agentes, mas o sandbox de ferramentas não foi validado.
- Ferramentas no `PATH` conforme `config/tools.yaml`: `nmap`, `ncat`, `curl`, `strings`, `gdb`,
  `objdump`, `sqlmap`, `python`.
- Uma chave de API da **NVIDIA** (`NVIDIA_API_KEY`) para o Agent Runtime.

---

## Instalação

```powershell
cd "C:\Users\Administrator\Documents\GitHub\CTF FRAMEWORK"
python -m pip install -e .
```

Dependências principais: `fastapi`, `uvicorn[standard]`, `sqlmodel`, `jinja2`,
`python-multipart`, `pydantic`, `pyyaml`, `pydantic-ai-slim[openai]`.
Para testes: `python -m pip install -e .[dev]` (pytest + httpx).

### Chave do modelo

O runtime lê a chave da variável de ambiente declarada em `config/models.yaml`
(`NVIDIA_API_KEY` para o provider `nvidia`). Se você guarda a chave no `auth.json` do OpenCode:

```powershell
$env:NVIDIA_API_KEY = (Get-Content "$env:USERPROFILE\.local\share\opencode\auth.json" -Raw |
  ConvertFrom-Json).nvidia.key
```

Sem a chave, a UI continua funcionando: o painel mostra `idle` e o botão **Iniciar agente**
retorna 503 com a mensagem do provider indisponível.

---

## Rodar

```powershell
python -m uvicorn app.main:app --port 8777
```

Ou dê duplo clique em `iniciar_orchestrator.bat` (sobe em `http://127.0.0.1:8000`, valida Python
e dependências antes).

Abra `http://127.0.0.1:8777/`. O banco (`ctf.db`) e o `storage/` são criados no primeiro acesso.

---

## Fluxo de uso

1. **Dashboard (`/`)** — cria um evento (ex.: `IDP CTF 03`).
2. **Evento (`/events/{id}`)** — cadastra o challenge: título, categoria, pontos, dificuldade,
   descrição, `target` (`host:porta`) e tags. O `target` é o campo de segurança: o ToolRunner só
   executa contra ele.
3. **Challenge (`/challenges/{id}`)** — anexa arquivos do desafio (`type=download`), registra
   tentativas manuais e notas versionadas.
4. **Painel do Agente (`/challenges/{id}/agent`)** — clica em **Iniciar agente**. O framework roda
   o recon determinístico (strings nos anexos, nmap no target), chama o júri e **para no
   checkpoint**.
5. **Checkpoint** — três ações:
   - **Aprovar** → executa o próximo passo decidido pelo júri (despacha um especialista ou valida
     a flag). Um ciclo completo pode levar minutos.
   - **Rejeitar** → encerra a sessão de vez (`state=rejected`).
   - **Ajustar com instrução** → injeta sua orientação e pede uma nova decisão ao júri.
6. **Registrar flag** — cola a flag no card lateral. O framework valida o regex do evento
   (`IDP\{.*\}`, `CTF\{.*\}` etc.) e duplicidade, marca o challenge como `solved` e mostra o
   resultado. A submissão na plataforma é sua.

O feed do painel atualiza por **polling HTMX a cada 1,5 s** (dois parciais independentes). Não há
SSE nem WebSocket nesta fase.

---

## Arquitetura em uma tela

```
┌──────────────────────── FastAPI (um processo) ────────────────────────┐
│  UI Jinja2 + HTMX            REST /api/*                              │
│        │                          │                                   │
│        └──────────┬───────────────┘                                   │
│                   ▼                                                   │
│            services/  (tracking, storage)  ◄── PE (memória única)      │
│                   │                                                   │
│   ┌───────────────┼────────────────┬──────────────────┐               │
│   ▼               ▼                ▼                  ▼               │
│ Agent Runtime  ToolRunner      Knowledge          SQLite (ctf.db)     │
│ (pydantic-ai)  (subprocess)    (FTS5)             + knowledge/ (md)   │
│   │               │                │                                  │
│   └── checkpoint humano ───────────┘                                  │
└───────────────────────────────────────────────────────────────────────┘
```

Loop de agentes: **OP → PL → RC → Júri → especialista (um por vez) → SU**, com o PE (tracking)
como única memória e um checkpoint humano por ciclo.

Detalhes completos em [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

---

## Estrutura do repositório

```
app/
  main.py            # create_app(): routers, static, storage
  config.py          # paths e env vars (CTF_DB_PATH, CTF_STORAGE_DIR, CTF_FLAG_FORMAT)
  database.py        # engine SQLite, create_all + migração leve de colunas
  models.py          # Event, Challenge, Attempt, Note, Attachment (+ enums)
  templates.py       # Jinja2 + filtro badge_for
  api/               # REST: events, challenges, knowledge
  agents/            # runtime: orchestrator, jury, specialists, recon, submission, context
  knowledge/         # parser de frontmatter + índice FTS5
  services/          # tracking (regras de negócio) e storage (arquivos)
  tooling/           # config, runner (sandbox) e doctor (inventário)
  web/               # rotas HTML + templates + painel do agente
  static/theme.css   # tokens visuais (preto/vermelho, verde só na flag)
agents/personas/     # personas .md (op, planner, recon, jury, submission)
config/              # agents.yaml, models.yaml, tools.yaml
knowledge/           # repos vendorizados (ctf-skills, dispatch) + knowledge.lock
scripts/             # smoke tests, sync de knowledge, CLI de busca
tests/               # 122 testes (pytest)
docs/                # specs, plans e esta documentação
ctf.db               # banco local (gitignored)
storage/             # anexos, outputs de tools e sandbox (gitignored)
```

---

## Configuração

| Arquivo | O que define |
|---|---|
| `config/models.yaml` | providers (base_url + env var) e papéis de modelo `strong` / `fast` com fallback |
| `config/agents.yaml` | papel → persona + papel de modelo + tools; `limits` de tentativas/tempo |
| `config/tools.yaml` | allowlist de ferramentas, `guard` (`strict`/`none`), timeouts e limite de output |
| `agents/personas/*.md` | texto de sistema de cada agente |
| variáveis de ambiente | `NVIDIA_API_KEY`, `CTF_DB_PATH`, `CTF_STORAGE_DIR`, `CTF_FLAG_FORMAT` |

Modelos em uso (só NVIDIA nesta fase):

- `strong` → `nvidia/nemotron-3-ultra-550b-a55b` (fallback: `nvidia/nemotron-3.5-super-vl-120b-a12b`)
- `fast` → `nvidia/nemotron-3.5-super-vl-120b-a12b`

---

## Testes

```powershell
python -m pytest tests -q
```

Use `tests` explicitamente: rodar `pytest -q` na raiz tenta coletar
`knowledge/ctf-skills/tests` (repositório vendorizado) e quebra a coleta.

Cobertura por área: tracking, storage, schema/migração, ToolRunner (guard/exec/tracking),
knowledge (parse/index), configs e modelos de agente, payloads, loop do júri, submissão, API REST,
UI e painel do agente (incluindo um e2e scripted com LLM fake).

---

## Documentação

- [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md) — desenho interno: dados, serviços, sandbox,
  knowledge, runtime de agentes, checkpoint, UI.
- [`docs/GUIA-OPERACIONAL.md`](docs/GUIA-OPERACIONAL.md) — instalar, subir, operar o painel,
  diagnosticar problemas, backup e manutenção.
- [`docs/REFERENCIA-API.md`](docs/REFERENCIA-API.md) — todas as rotas REST, HTML e HTMX.
- [`docs/DESENVOLVIMENTO.md`](docs/DESENVOLVIMENTO.md) — fluxo de branches/TDD, como estender
  (tool, persona, especialista, provider) e dívida técnica.
- [`docs/HISTORICO.md`](docs/HISTORICO.md) — decisões de produto, marcos e evidências.
- Specs e planos originais em [`docs/specs/`](docs/specs) e [`docs/plans/`](docs/plans).
- Diagrama de orquestração (Mermaid) em `docs/agents-architecture.mmd`.

---

## Segurança e limites (por desenho)

- O LLM **nunca** monta shell cru: ele escolhe `tool` + `args`; o framework valida e executa
  `argv` sem shell.
- Argumentos com metacaracteres (`; | & $ ` \n \r`) são rejeitados no guard `strict`.
- Qualquer IP/domínio nos argumentos precisa ser `localhost`/loopback ou o host de
  `challenge.target`. Alvo fora do challenge é bloqueado.
- Timeout e limite de output por execução; workdir isolado por challenge.
- Nenhum passo de ataque roda sem aprovação humana explícita.
- `python` está com `guard: none` (precisa de argumentos livres para scripts). É o ponto mais
  permissivo da allowlist — trate como confiança total no agente.
