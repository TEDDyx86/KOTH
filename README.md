# KOTH — King Of The Hill

<p align="center">
  <img src="app/static/brand/koth_brand_lockup.png" alt="KOTH — King Of The Hill" width="100%">
</p>

Framework orquestrador especialista em CTF, para um único operador, rodando como app web local
(**FastAPI + HTMX + SQLite**), com **tela inicial estilo CLI** para conversar diretamente com o
Orquestrador (`[KOTH // OP]`), lançar desafios em **Full Auto** ou **Supervisionado** e escalar
até a flag. Ele cobre o ciclo de vida de um desafio do início ao fim:

- **Terminal OP (`/`)**: chat estilo CLI para enviar desafios com anexos, rodar `/solve`, `/rank`, `/status`, `/events`;
- **tracking** de eventos, challenges, tentativas, notas e artefatos;
- **execução de ferramentas** em sandbox com allowlist, guard de alvo, staging de anexos e timeout;
- **conhecimento embutido** (skills e Arsenal de CTF indexados em FTS5);
- **agentes de IA** (OP, Planner, Recon, Júri e 6 especialistas por categoria, com Forensics multimodal)
  em modo **Full Auto** ou com **checkpoint humano** por ciclo.

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

**Suíte de testes:** 148 testes passando (`python -m pytest tests -q`).

Prova de funcionamento ponta a ponta: o challenge **#2 (`IDP - Sorteios Especiais`)** foi
resolvido pelo fluxo real (RNG semeado pelo unix epoch, bola 42 → `Random(seed).Next() % 1000 + 1`).

### O que ainda falta no backlog

- UI de edição avançada de personas diretamente pelo navegador (hoje editáveis em `agents/personas/*.md`).
- Retry automático dedicado no cliente HTTP além do fallback de modelo por erro transitório.

Detalhes em [`docs/DESENVOLVIMENTO.md`](docs/DESENVOLVIMENTO.md#dívida-técnica-conhecida).

---

## Requisitos

- **Python 3.12+** (Windows, Linux ou macOS).
- **Ferramentas:** você pode usar **Docker** com o [`Dockerfile.sandbox`](Dockerfile.sandbox) incluído (recomendado para portabilidade e isolamento) **ou** ferramentas nativas no `PATH` (`nmap`, `ncat`, `curl`, `strings`, `gdb`, `objdump`, `sqlmap`, `python`).
- **IA (qualquer uma das opções):**
  - API key de um provedor suportado (**NVIDIA NIM**, **OpenAI**, **Anthropic**, **Google Gemini**, **OpenRouter**, **Groq**, **DeepSeek**, **Ollama local** ou **Custom/9router**) — configurável direto na UI em `/settings`; **ou**
  - **CLI oficial autenticado** na sua máquina (`claude` para Claude Pro/Max, `codex` para ChatGPT Plus/Pro, ou `gemini` para Google AI).

---

## Instalação e execução rápida

```bash
git clone https://github.com/TEDDyx86/KOTH.git
cd KOTH
python -m pip install -e .
koth
```

O comando `koth` sobe o servidor em `http://127.0.0.1:8777/` e abre o navegador automaticamente (use `koth serve --no-browser` para modo headless ou `koth doctor` para diagnosticar ferramentas, Docker e provedores).

Para testes:

```bash
python -m pip install -e .[dev]
python -m pytest tests -q
```

### Sandbox Docker (opcional, recomendado)

```bash
docker build -t koth-sandbox:latest -f Dockerfile.sandbox .
```

Depois ative o modo `docker` em **Configurações → Sandbox & Limites** (`/settings?tab=runtime`).

---

## Fluxo de uso

1. **Terminal OP (`/`)** — tela inicial em formato CLI para conversar com o Orquestrador (`[KOTH // OP]`), colar o enunciado do desafio com anexos ou usar `/solve`, `/rank`, `/events`, `/status`, `/help`.
2. **Dashboard (`/dashboard`)** — criação rápida de desafios em 1 passo (com upload múltiplo e botão **Criar e rodar Full Auto →**), progresso dos eventos e atividade recente.
3. **Painel do Agente (`/challenges/{id}/agent`)** — acompanha o loop em **Full Auto** (sem pausa) ou **Supervisionado** (checkpoint humano com **Aprovar**, **Aprovar + Full Auto**, **Ajustar** e **Rejeitar**), além de validação manual da flag.
4. **Configurações (`/settings`)** — 4 abas: **Provedores & Auth** (API keys locais + CLIs de assinatura), **Modelos & IA** (descoberta automática de modelos por provedor), **Sandbox & Limites** (Docker/Native + Planner + Knowledge FTS5) e **Identidade Visual**.

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
tests/               # 148 testes (pytest)
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
