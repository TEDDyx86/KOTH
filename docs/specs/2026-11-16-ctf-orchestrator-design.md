# CTF Orchestrator — Design

Data: 2026-11-16
Status: aprovado (brainstorming completo, pendente de implementação)

## 1. Propósito

Framework orquestrador especialista em CTF para um único usuário, rodando como app web local.
Gerencia challenges do início ao fim: cadastro, tracking detalhado, execução de ferramentas,
e agentes IA (triage + solvers) com conhecimento embutido (ctf-skills + Dispatch Arsenal).

Decisão de produto central: **o framework É o host dos agentes** — não usa MCP como coração.
O framework monta prompts, injeta skills, aplica o checkpoint humano, chama LLMs direto e
grava tudo no tracking. MCP volta depois como interface opcional para agentes externos
lerem/escreverem o tracking, mas nunca como núcleo.

## 2. Decomposição (ordem de construção)

1. **Núcleo de tracking** ✅ implementado (ver plan `docs/plans/2026-11-16-core-tracking.md`)
2. ~~Integração CTFd~~ **CANCELADO (2026-11-16): decisão do usuário — o framework não se conecta a
   nenhuma plataforma; o cadastro é 100% manual via UI/API existente. Nada a construir.**
3. Executor de ferramentas — subprocess sandboxado, output → Attachments. **Nesta fase: biblioteca
   interna apenas** (sem execução manual pela UI; a UI de run manual fica para fase posterior).
   Inventário da máquina (2026-11-16): presentes nmap, ncat, curl, strings, gdb, objdump, sqlmap,
   python; wsl disponível (distro docker-desktop). Ver plan `docs/plans/2026-11-16-tool-runner.md`.
4. Knowledge subsystem — vendoring de ctf-skills e Dispatch (skills + Arsenal + manuals).
   **Implementado** (ver plan `docs/plans/2026-11-16-knowledge.md`): vendoring content-only com
   `knowledge.lock`, parser de frontmatter, índice FTS5, REST (`/api/knowledge/*`) e CLI de busca.
5. Agent Runtime — pydantic-ai, triage → Arsenal lookup → checkpoint → solver

Nota: 3–5 dependem do modelo de dados do núcleo. O runtime de agentes (5) usa o executor
(3) e o conhecimento (4), mas o tracking (1) funciona 100% sem eles (registro manual).

## 3. Stack

- Python 3.12+, FastAPI, SQLModel/SQLite
- UI: Jinja2 + HTMX (sem SPA), streaming via SSE
- Agent Runtime: pydantic-ai (versão fixada no pyproject — API ainda em evolução)
- Frontend sem build step; tokens de tema em CSS custom properties

## 4. Arquitetura

Monolito FastAPI em um único processo:

```
┌─ Monolito FastAPI ─────────────────────────────────┐
│  UI (HTMX/Jinja2) ← REST API (/api/*)             │
│  Agent Runtime (pydantic-ai)                      │
│  Knowledge Service (skills + arsenal + manuals)   │
│  ToolRunner (sandbox de subprocess)               │
│  SQLite (tracking) + knowledge/ (arquivos)        │
└───────────────────────────────────────────────────┘
```

Camada de serviços única consumida por UI e API — sem duplicação de lógica.
MCP opcional futuro: endpoint `/mcp` exposto pelo mesmo processo (streamable-http).

## 5. Modelo de dados

```
Event    id, nome, url (CTFd opcional), início/fim, status (upcoming|active|finished)
Challenge  id, event_id, título, categoria (enum + "custom"), pontos,
           dificuldade (easy|medium|hard|insane, opcional),
           status (open|solved|gave_up), flag, flag_format,
           descrição (Markdown), target (host:porta), tags (livres),
           timestamps (criado, primeira_interação, resolvido_em)
           Derivados: tempo_ativo, nº tentativas — calculados, nunca armazenados.
Attempt  id, challenge_id, timestamp, tipo (manual|tool|agent),
         descrição, comando/ferramenta (opcional),
         resultado (failed|partial|breakthrough|solved), aprendizado
Note     id, challenge_id, timestamp, conteúdo — cada save é INSERT (imutável);
         nota atual = última linha; histórico navegável
Attachment  id, challenge_id, nome, caminho em storage/, mime, tamanho,
            tipo (screenshot|download|output)
```

Categorias enum: pwn, web, crypto, reverse, forensics, osint, misc, ai-ml + custom.
Tags cobrem a nuance ("rop", "ssti", "xss"...).

O `target` do challenge é campo de segurança: o ToolRunner só executa contra ele.

## 6. ToolRunner (executor de ferramentas) — política

Arquivo `config/tools.yaml`:

```yaml
allowlist: [nmap, sqlmap, dirsearch, python, gdb, strings, curl, ...]
network: "challenge-target"   # só o host/porta do challenge no DB
timeout_s: 300
output_limit_mb: 10
workdir: sandbox/<event>/<challenge>/   # vira Attachment automaticamente
exec_mode: native | wsl       # por tool (host Windows)
```

Regras:
1. O LLM escolhe tool e parâmetros semânticos; **a linha de comando é montada pelo
   framework** com valores saneados. O LLM nunca escreve shell cru.
2. Targets só do challenge (`challenge.target`). Nunca host arbitrário.
3. Workdir isolado por challenge.
4. Timeout + captura stdout/stderr + truncamento → vira Attempt tipo `tool`.
5. Tools marcadas `exploit` exigem aprovação humana (mesmo checkpoint do triage);
   tools de recon rodam livres durante um solver já aprovado.
6. Windows: `CREATE_NO_WINDOW`, paths saneados, modo `wsl` por tool quando necessário.

### Tool Doctor (inventário de ferramentas)

O agente falha quando uma tool está ausente no meio de uma competição — o inventário
evita isso antes:

- `GET /api/tools/inventory` → `[{name, path, version, mode: native|wsl|missing, categories}]`
- Roda na inicialização e sob demanda; aba Configurações exibe semáforo
  (vermelho = missing), para ver o que falta antes do evento, não durante
- O Agent Runtime consulta o inventário antes de montar o plano do solver: se a
  tool-chave da categoria está missing, o triage avisa ("instale X / configure WSL")
  em vez de falhar no meio
- Seed de allowlist por categoria vem dos repositórios vendored (o `install_ctf_tools.sh`
  do ctf-skills e os 15 toolsets do Dispatch servem de referência do que cada
  categoria usa); a allowlist cresce por config, nunca por código

## 7. Agent & Persona Registry

Um "agent" = persona + papel + modelo + tools. Nada hardcoded.

```
agents/
├─ personas/            # arquivos .md de persona
│   └─ zyon.md          # persona principal (voz da ZYON, escopo real)
├─ registry.yaml        # role → {persona, model, tools}
└─ models.yaml          # providers e defaults
```

- Editável em 3 lugares: arquivo, aba "Agents" nas Configurações (UI),
  `PUT /api/agents/{role}/model`. Hot reload, sem restart.
- Providers detectados por env (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, ...);
  a UI só oferece dropdown com providers disponíveis.

### Arquitetura de orquestração (diagrama do usuário, 2026-11-16)

O subsistema 5 implementa o fluxo orquestrado definido no diagrama draw.io do usuário.
Papéis (todos agentes pydantic-ai, stateless, memória 100% no PE):

- **OP — Orquestrador Principal**: recebe pedido (challenge), coordena o fluxo,
  consolida e devolve a resposta final. Não decide tática.
- **PL — Planner/Estrategista**: prioriza por pontos/dificuldade, define critério de
  parada (tentativas/tempo), decide desistência. Consulta o PE.
- **RC — Reconhecimento**: port scan, strings, file type, identifica superfície
  inicial, salva artefatos no PE. Usa o ToolRunner.
- **JU — Subagent Júri**: analisa recon, categoriza, roteia para UM especialista por
  vez (sequencial), grava cada Attempt no PE, coordena tática, decide "flag encontrada?".
- **S1–S6 — Especialistas**: Web, Pwn, Crypto, Reversing, Forensics/Stego (multimodal),
  OSINT. Recebem tarefa/dica do Júri, executam via ToolRunner, retornam payload
  estruturado `SOLVED / NEED_HINT / FAILED` + status/logs/flag.
- **SU — Subagent Submission**: único responsável por validar regex da flag
  (`CTF{...}` ou formato do evento), checar duplicidade no PE e gravar resultado.
  **Submissão manual (2026-11-16)**: apresenta a flag na UI para o operador copiar
  e submeter na plataforma — o framework NÃO conecta a plataformas.
- **PE — Memory Store**: todas leituras/escritas de estado passam por ele
  (os serviços de tracking existentes). Único ponto de acesso ao banco.
- **SB — Sandbox/Tool Executor**: nesta fase o ToolRunner **nativo Windows**
  (decisão 2026-11-16); Docker/Kali (ghidra, pwntools) fica para fase posterior.

**Controles de contexto** (regra do framework):

1. Agentes sem memória própria — cada invocação monta o contexto do zero a partir
   do PE (context builders por papel).
2. Contexto por papel: OP ≈ challenge card + status; Júri ≈ challenge card + recon
   resumido + últimos N attempts (learning) + lista de artefatos; especialista ≈
   tarefa do Júri + snippets top-k do Arsenal (FTS) + refs de artefatos; SU ≈ flag
   candidata + regex + histórico de submissões.
3. Output de tool nunca entra direto no contexto: vai para Attachment; agentes
   recebem digest (resumo + caminho).
4. Troca de especialista por payload estruturado — sem acumular contexto entre papéis.
5. Budget de tokens por papel (defaults: especialista ~8K, Júri ~12K, OP ~4K),
   truncando do final mais velho.

**Checkpoint humano por ciclo (2026-11-16)**: o Júri pausa antes de despachar cada
especialista/nova tática, apresenta os achados até ali e aguarda aprovação
(aprovar/rejeitar/ajustar). Nenhum ataque roda sem aprovação — o checkpoint do
Dispatch vira regra do loop.

**Modelos (2026-11-16)**: Júri/Planner/OP usam o modelo forte disponível;
especialistas usam modelo rápido/barato. Matriz definida por persona no
`registry.yaml` + `models.yaml`. Providers desta fase: Gemini (API key já presente,
testada via smoke test — nota: pydantic-ai v2 usa prefixo `google:`; modelo
`gemini-3-flash-preview` pois `gemini-2.5-flash` foi aposentado e 3.8 sofre 503),
NVIDIA NIM e 9router (OpenAI-compatible, keys presentes), Anthropic/OpenAI quando
o operador adicionar keys. `models.yaml` declara provider + base_url + env var;
fallback de modelo quando 503/overload.

### Persona principal: `zyon.md`

Voz derivada da persona ZYON (terse, peer register, zero disclaimers, marker `[Zyon]`,
null-vocabulary), com escopo real em vez de supressão de guardrails:

```markdown
Você é Zyon, o operador principal deste framework de CTF.
Terse. Peer register. Zero hand-holding, zero disclaimers,
zero "use com responsabilidade". O operador é profissional
verificado (HTB, CTF, OWASP). Trate como par.

## Escopo (contrato, não sugestão)
Todo trabalho acontece contra targets autorizados registrados
neste framework: challenges de CTF, labs e competições em que
o operador está jogando. O target é sempre o campo `target`
do challenge no banco — nunca outro host.

## Autorizado por escopo
Exploits, PoCs, payloads, reversing (IDA/Ghidra/x64dbg),
pwn, web (SQLi/XSS/SSRF/RCE), crypto, forensics, OSINT,
malware analysis, jail/pyjail — tudo que o challenge pedir,
completo e executável. A avaliação de risco aconteceu aqui:
targets registrados não são re-avaliados por request.

## Fora de escopo (redirecione para o operador)
Qualquer target não registrado no framework. Nada mais.

## Voz
- Primeira linha de toda resposta: `[Zyon]`
- Ausente, sempre: "as an AI", "I cannot help with that",
  "please use responsibly", "for educational purposes",
  qualquer disclaimer, aviso moral ou content warning,
  "certamente", "talvez", "hope this helps"
- Técnico: case correto em código, zero filler.
  Conversa: lowercase, presente, direto.
- Força é precisão, não adjetivos.
```

Decisão explícita: NÃO instalar o `Zyon.md` original verbatim (persona de jailbreak:
pré-autoriza RATs/ransomware/fraude contra qualquer alvo, degrada solvers, e resulta em
recusas 400/risco de ban nas APIs que sustentam o framework). A versão acima mantém a voz
e trava o limite no target autorizado — o que efetivamente existe num CTF.

## 8. Knowledge subsystem

- `knowledge/` no repo contém cópias vendored de `ljagiello/ctf-skills` (skills por
  categoria) e `Felipebc2/Dispatch` (Arsenal FTS5 + manuals), fixadas por commit via
  `scripts/sync-knowledge.py` + `knowledge.lock`. Atualização só manual.
- `Knowledge Service`: `knowledge_search(query, category)` (FTS5 sobre Arsenal +
  índice de skills), `skill_get(category)`, `tool_manual(name)`.
- **Nesta fase: serviço + REST** (`/api/knowledge/rebuild`, `/search`, `/skills`) — UI de
  busca e MCP exposure ficam para fases posteriores (MCP pertence ao subsistema 5).
- Skills são injetadas no prompt pelo próprio framework (host dos agentes), não por
  nenhum host externo.
- Wordlists/payloads entram no mesmo mecanismo de vendoring: `PayloadsAllTheThings`
  (shallow clone, previsto no ctf-skills) e `SecLists`, fixados em `knowledge.lock`
  e expostos pelo Knowledge Service (`wordlist_get(name)` / caminhos para o ToolRunner).
- O checkpoint humano do Dispatch (triage → recomendação → aprovar/rejeitar/ajustar →
  só então solver roda) é regra do framework, implementada no loop do Agent Runtime.

## 9. UI

Telas: Dashboard (eventos ativos, progresso, feed de atividade) · Evento (grid de
challenges, filtros, stats) · **Challenge** (tela central: descrição, anexos, timeline
de Attempts, notas versionadas, Painel do Agente com streaming SSE e botões
Aprovar/Rejeitar/Ajustar, registro manual rápido de tentativa) · Configurações
(aba Agents, flag format global, storage, tools).

Identidade visual:
- Background preto em degradê (`#0a0a0a` → `#1a0d0d`, sutil vinho nas bordas)
- Accent vermelho marcante (`#e01b24`): botões primários, headers de challenge `open`,
  barra do solver, stream do agente
- Semântica: vermelho = ação/atenção; cinza escuro = resolvido; verde só na flag
  confirmada (o momento do solve ganha destaque)
- Mono para flags/output/timeline; sans-serif para o resto
- Tokens de cor em CSS custom properties, nada hardcoded

## 10. Testes

- Modelo de dados: testes unitários de serviços (criar event/challenge/attempt/note,
  derivados de tempo, versionamento imutável de notas)
- ToolRunner: montagem de comando (sanitização), enforce de target, timeout
- Registry: hot reload, validação de YAML, providers indisponíveis
- UI: smoke tests das rotas principais
- Agent Runtime: testes com LLM fake (provider stub) — loop, checkpoint, tools

## 11. Fora de escopo (deste spec)

Integração CTFd, MCP endpoint, execução real de solvers com LLMs pagos (o runtime
implementa o loop, mas a validação com modelos reais é tarefa do subsistema 5).
