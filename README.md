# KOTH — King Of The Hill

<p align="center">
  <img src="app/static/brand/koth_brand_lockup.png" alt="KOTH — King Of The Hill" width="100%">
</p>

<p align="center">
  <strong>Framework Orquestrador Autônomo &amp; Supervisionado para CTFs</strong><br>
  FastAPI · HTMX · SQLite (FTS5) · Pydantic-AI · Sandbox Docker Kali · MCP
</p>

---

## Visão geral

O **KOTH (King Of The Hill)** é um workspace local de operações ofensivas e investigação para competições de **Capture The Flag (CTF)**, projetado para um único operador. O framework atua como **host completo dos agentes**: monta contexto, consulta o Arsenal FTS5, aplica guardas de execução em sandbox, gerencia checkpoints humanos, consolida provas reproduzíveis (`writeup.md` + `.zip`) e registra todo o ciclo de vida no SQLite.

### Principais capacidades

- **Terminal OP (`/`)** — interface inicial estilo CLI para conversar com o Orquestrador (`[KOTH // OP]`), lançar desafios em linguagem natural ou via `/solve`, priorizar com `/rank`, consultar `/events`, `/status` e `/help`.
- **Dashboard Arena (`/dashboard`)** — fila operacional de challenges abertos com filtro instantâneo por categoria/texto, telemetria de pontos e sessões ao vivo, progresso por evento, atividade recente e criação/edição/exclusão completa.
- **Orquestração multiagente inteligente (`OP → PL → RC → Júri ↔ Especialistas → SU`)**:
  - **7 especialistas** (`web`, `pwn`, `crypto`, `reversing`, `forensics` multimodal com imagem, `osint` e `misc`) com aliases de categoria (`reverse`, `stego`, `pcap`, `ai-ml`, `custom`);
  - **Modo Full Auto** (com fast-path de validação de flag assim que o especialista resolve) ou **Supervisionado** (checkpoint humano com *Aprovar*, *Aprovar + Full Auto*, *Ajustar* e *Rejeitar*);
  - **Recon adaptativo** automático (`file`, `strings`, `checksec`, `exiftool`, `binwalk`, `tshark`, `nmap`, `curl -sI`) + pivô automático após falhas consecutivas;
  - **Recuperação de sessões** pós-reinício e telemetria de tokens por sessão.
- **Sandbox Docker Kali (`koth-sandbox:latest`)** — 26 ferramentas allowlisted (`nmap`, `ncat`, `curl`, `ffuf`, `gobuster`, `whatweb`, `sqlmap`, `jq`, `file`, `strings`, `readelf`, `objdump`, `gdb`, `radare2`, `ltrace`, `strace`, `checksec`, `ROPgadget`, `binwalk`, `exiftool`, `tshark`, `foremost`, `steghide`, `openssl`, `john`, `python` com `pwntools`, `pycryptodome`, `z3-solver`, `sympy`, `scapy`) com target guard e staging automático em `/work`.
- **Ecossistema MCP (cliente e servidor)**:
  - Conecta servidores MCP externos (`radare2`, `ghidra`, `gdb`, `playwright`, `fetch`, `hexstrike` ou customizados `stdio`/`http`/`sse`) em `config/mcp.yaml` diretamente aos especialistas;
  - Expõe o próprio sandbox e Arsenal do KOTH via `koth mcp` (`FastMCP` stdio) para uso externo no Claude Code, Codex CLI, Gemini CLI ou Cursor.
- **Writeups, Provas & Autoaprendizado FTS5**:
  - Ao concluir o desafio, gera `writeup.md` com comandos executados, saídas capturadas e scripts de `/work`;
  - Exporta `writeup.md`, pacote `proof-bundle.zip` (writeup + `attempts.json` + scripts + outputs) e compilado por evento;
  - Botão **Salvar no Arsenal FTS5** grava técnicas resolvidas em `knowledge/local/` e reindexa a busca para os próximos desafios.

> **Submissão de flag é 100% manual.** O framework valida formato (regex do evento) e duplicidade e exibe a flag pronta para entrega na plataforma do CTF — sem integração automática com CTFd/HTB.

---

## Arquitetura

```text
┌─────────────────────────── KOTH (FastAPI · um processo) ───────────────────────────┐
│  UI Jinja2 + HTMX (/, /dashboard, /events, /challenges, /settings)  │  REST /api/* │
└──────────────────────────────────────┬─────────────────────────────────────────────┘
                                       ▼
                     services/tracking.py + storage.py  (SQLite ctf.db + storage/)
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
  Agent Runtime                  ToolRunner + MCP              Knowledge FTS5
  (OP · PL · RC · Jury ·         (koth-sandbox:latest          (ctf-skills + dispatch
   7 Especialistas · Writeup)     guard strict + koth mcp)      + knowledge/local/)
```

### Fluxo operacional

1. **Reconhecimento (`RC`):** faz staging dos downloads no workdir isolado (`/work`), detecta magic bytes e roda sondas adaptativas conforme tipo de arquivo/alvo.
2. **Decisão (`Júri`):** modelo `strong` analisa o contexto completo e emite **1 ação por ciclo** (`DISPATCH`, `RECHECK`, `SUBMIT`, `WAIT_USER`, `GIVE_UP`).
3. **Execução (`S1–S7`):** especialista usa `run_tool`, `run_python_script`, `list_sandbox_files`, `read_sandbox_file`, `search_arsenal` e servidores MCP ativos dentro do orçamento (`max_tool_runs`).
4. **Validação & Provas (`SU` + `Writeup`):** `validate_flag` confere regex e unicidade; `writeup.py` gera a documentação técnica reproduzível e o pacote de evidências.

Documentação técnica complementar:
- [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md) — desenho interno de dados, runtime, sandbox e UI
- [`docs/REFERENCIA-API.md`](docs/REFERENCIA-API.md) — rotas REST, HTML e HTMX
- [`docs/GUIA-OPERACIONAL.md`](docs/GUIA-OPERACIONAL.md) — operação, diagnóstico e backup
- [`docs/DESENVOLVIMENTO.md`](docs/DESENVOLVIMENTO.md) — fluxo de branches, TDD e extensibilidade

---

## Estrutura do repositório

```text
app/
  cli.py                 # Entrypoint `koth` (serve, doctor, mcp)
  main.py                # Factory FastAPI, routers, static e storage
  config.py              # Paths e env vars (CTF_DB_PATH, CTF_STORAGE_DIR, CTF_FLAG_FORMAT)
  database.py            # SQLite engine + migração automática de colunas (ensure_schema)
  models.py              # Event, Challenge, Attempt, Note, Attachment, AgentSessionRecord, CliMessageRecord
  agents/                # orchestrator, jury, planner, recon, specialists, cli_op, cli_providers,
                         # providers, context, submission, writeup, payloads, models, config, api
  knowledge/             # Parser de frontmatter + índice SQLite FTS5
  services/              # tracking (CRUD + cascata) e storage (arquivos locais)
  tooling/               # config, runner (guard + docker), doctor, sandbox, mcp_servers, koth_mcp
  api/                   # REST: events, challenges, knowledge
  web/                   # Rotas HTML/HTMX, templates Jinja2 e static/ (theme.css + brand/)
agents/personas/         # Prompts de sistema (op, planner, recon, jury, submission, specialist_*)
config/                  # agents.yaml, models.yaml, tools.yaml, mcp.yaml
knowledge/               # Bases vendorizadas (ctf-skills, dispatch) + local/ (técnicas aprendidas)
tests/                   # 164 testes (pytest)
Dockerfile.sandbox       # Imagem Kali Rolling com 26 ferramentas + libs Python CTF
```

---

## Provedores de IA suportados

Configure diretamente pela UI em **`/settings`** (sem precisar editar YAML na mão):

- **Via API Key** (salva localmente em `storage/provider_keys.json`, gitignored, com descoberta dinâmica de modelos):
  `NVIDIA NIM`, `OpenAI`, `Anthropic`, `Google Gemini`, `OpenRouter`, `Groq`, `DeepSeek`, `Ollama (local)` e endpoints OpenAI-compatíveis customizados.
- **Via CLI de assinatura** (sem custo extra de API, usando login local existente):
  `Claude Code CLI` (`claude auth login`), `OpenAI Codex CLI` (`codex login`) e `Google Gemini CLI` (`gemini`).

---

## Tutorial: Como executar localmente

### Pré-requisitos

1. **Python 3.12+** instalado e no `PATH` (Windows, Linux ou macOS).
2. **Git** para clonar o repositório.
3. **Docker Desktop / Docker Engine** *(recomendado)* para isolar as 26 ferramentas Kali no container `koth-sandbox:latest` — ou ferramentas nativas equivalentes no `PATH`.
4. **Pelo menos 1 provedor de IA:** uma API key (OpenRouter, Groq, OpenAI, Anthropic, Gemini, NVIDIA NIM, Ollama local) **ou** um CLI autenticado (`claude`, `codex` ou `gemini`).

---

### Passo 1 — Clonar o repositório e criar ambiente virtual

**Windows (PowerShell):**

```powershell
git clone https://github.com/TEDDyx86/KOTH.git
cd KOTH
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

**Linux / macOS (Bash/Zsh):**

```bash
git clone https://github.com/TEDDyx86/KOTH.git
cd KOTH
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

---

### Passo 2 — Construir o sandbox Docker Kali *(recomendado)*

Certifique-se de que o **Docker Desktop** (ou `dockerd`) esteja em execução e faça o build da imagem com todas as ferramentas de CTF:

```bash
docker build -f Dockerfile.sandbox -t koth-sandbox:latest .
```

> O `config/tools.yaml` já vem pré-configurado para `mode: docker` apontando para `koth-sandbox:latest`. Se preferir rodar apenas ferramentas instaladas direto no seu sistema operacional, você pode alternar para `native` em **`/settings?tab=runtime`**.

---

### Passo 3 — Verificar o ambiente (`koth doctor`)

Execute o diagnóstico completo para validar Docker, ferramentas allowlisted, provedores de IA e servidores MCP:

```bash
koth doctor
```

*(Caso o script `koth` não esteja no `PATH` fora da venv, use `python -m app.cli doctor`.)*

---

### Passo 4 — Subir o KOTH e configurar sua IA

Inicie o servidor local:

```bash
koth
```

- O painel abrirá automaticamente em **`http://127.0.0.1:8777/`** (use `koth serve --no-browser` para não abrir o navegador ou `--port 9000` para trocar a porta).
- Acesse **`http://127.0.0.1:8777/settings`**:
  1. Na aba **Provedores & Auth**, cole sua API Key (ex.: OpenRouter, Groq, OpenAI, Gemini, NVIDIA NIM, Ollama) **ou** ative um provedor CLI (`Claude Code`, `Codex`, `Gemini CLI`) e clique em **Testar**.
  2. Na aba **Modelos & IA**, selecione os modelos `strong` (Júri/OP), `fast` (Especialistas) e opcionalmente um `fallback`.
  3. Na aba **Sandbox & Limites**, confira o **Tool Doctor**, ative servidores **MCP** opcionais se desejar e ajuste `max_attempts` / `max_minutes`.

---

### Passo 5 — Resolver seu primeiro desafio

- **Pelo Terminal OP (`/`):** envie anexos e digite o enunciado em linguagem natural ou use:
  ```text
  /solve MeuWeb | cat=web | pts=300 | target=127.0.0.1:8080 | encontrar flag em /admin
  ```
- **Pelo Dashboard (`/dashboard`):** clique em **`+ Novo challenge`**, anexe os arquivos e clique em **`Criar e rodar Full Auto →`** (ou **`Criar e abrir Agente`** para acompanhar passo a passo no modo supervisionado).
- Ao final, baixe as provas em **`writeup.md`** ou **`proof-bundle.zip`** e clique em **Salvar no Arsenal FTS5** para reutilizar a técnica nos próximos desafios.

---

### Passo 6 — Rodar a suíte de testes e usar como servidor MCP *(opcional)*

Para validar os **164 testes** da aplicação:

```bash
python -m pytest tests -q
```

> **Importante:** informe `tests` explicitamente ao rodar `pytest` para não coletar os testes dos repositórios vendorizados dentro de `knowledge/`.

Para expor o sandbox e a base FTS5 do KOTH como **servidor MCP stdio** para clientes externos (Claude Code, Gemini CLI, Codex, Cursor):

```bash
koth mcp
```

*(Ou `python -m app.cli mcp` — o JSON pronto para copiar fica em `/settings?tab=runtime`.)*
