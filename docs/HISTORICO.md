# Histórico e decisões

Registro cronológico do que foi construído, das decisões de produto que moldaram o framework e das
evidências de funcionamento. O histórico detalhado de implementação está nos planos em
`docs/plans/`; aqui fica o "porquê" e o estado de cada marco.

---

## Linha do tempo

### 2026-11-16 — fundação

- **Spec de design aprovado** (`docs/specs/2026-11-16-ctf-orchestrator-design.md`): monolito
  FastAPI, tracking SQLite, ToolRunner, knowledge vendorizado, Agent Runtime com checkpoint humano.
- **Integração com CTFd cancelada.** Decisão do operador: o framework não se conecta a nenhuma
  plataforma; cadastro e submissão são 100% manuais. O campo `Event.url` permanece como metadado.
- **Inventário da máquina** para a allowlist: `nmap`, `ncat`, `curl`, `strings`, `gdb`, `objdump`,
  `sqlmap`, `python` presentes; WSL disponível (distro `docker-desktop`).
- **Sandbox nativo Windows** escolhido para a fase 1 (Docker/Kali adiado).
- **Subsistema 1 (tracking) implementado** em TDD: modelos, serviços, REST e UI preto/vermelho.
  Planos: `2026-11-16-core-tracking.md`.
- **Subsistema 3 (ToolRunner) implementado**: allowlist em YAML, guards (metacaracteres + alvo),
  timeout/truncamento, workdir por challenge, persistência de Attachment + Attempt, Tool Doctor.
- **Subsistema 4 (Knowledge) implementado**: vendoring content-only de `ljagiello/ctf-skills` e
  `Felipebc2/Dispatch`, `knowledge.lock` por commit, parser de frontmatter, índice FTS5, REST e CLI.
  Primeiro sync: `2026-11-16T22:20:05Z`.
- **Subsistema 5 (Agent Runtime) implementado**: papéis OP/PL/RC/JU/S1-S6/SU, contratos Pydantic
  (`JuryDecision`, `TaskOrder`, `SpecialistReport`), builders de contexto com budget, recon
  determinístico, validação de flag, sessão com checkpoint por ciclo e REST (`start/approve/reject`).
- **Modelos migrados de Gemini para NVIDIA NIM** (`nemotron` strong/fast) — o smoke test de modelo
  foi repontado e o caminho Google ficou apenas como suporte opcional.
- **Validação real**: challenge `IDP - Sorteios Especiais` resolvido pelo fluxo do framework
  (ver "Evidências").

### 2026-11-17 — Painel do Agente

- Spec (`docs/specs/2026-11-17-agent-panel-design.md`) e plano (`docs/plans/2026-11-17-agent-panel.md`)
  do painel: página própria, polling HTMX, checkpoint com três ações, flag manual.
- Implementação em 9 commits (`6e3a227` … `d8a8272`):
  - `orchestrator.terminate()` (único método novo do runtime — "Rejeitar" encerra de fato);
  - `get_orchestrator` compartilhado entre UI e REST;
  - filtro Jinja `badge_for`;
  - página `agent_panel.html` + parciais `_agent_feed.html` / `_agent_side.html`;
  - ações `start / approve / terminate / adjust`;
  - registro manual de flag com parcial `_flag_result.html`;
  - CSS do grid/checkpoint/indicadores.
- **Bug 1**: polling quebrava quando o runtime estava indisponível (start devolvia 500). Corrigido
  em `346ecda` — `_sessions_of` tolerante, `503` explícito, painel segue em `idle`.
- **Bug 2**: o parcial lateral era renderizado com `challenge_id` vazio, gerando URLs
  `challenges//agent/...` e **404 silencioso nos botões**. Corrigido em `1056255` com
  `{% with challenge_id = challenge.id %}` + caixa de erro visível (`htmx:responseError`),
  `hx-disabled-elt` e spinners.
- Suíte fechou em **122 testes**.

### 2026-11-20 — documentação e mudança de casa

- Documentação completa (`README.md` + `docs/ARQUITETURA.md`, `GUIA-OPERACIONAL.md`,
  `REFERENCIA-API.md`, `DESENVOLVIMENTO.md`, `HISTORICO.md`).
- Projeto movido para `C:\Users\Administrator\Documents\GitHub\CTF FRAMEWORK`.

---

## Decisões de produto (ADRs curtos)

| Data | Decisão | Contexto / consequência |
|---|---|---|
| 2026-11-16 | **Sem integração com plataformas** | Cadastro e submissão manuais. Elimina dependência de API de CTFd e o risco de submissão automática errada. |
| 2026-11-16 | **O framework é o host dos agentes** | Prompts, personas, conhecimento e checkpoint vivem aqui. MCP vira interface opcional futura, nunca o núcleo. |
| 2026-11-16 | **Checkpoint humano por ciclo** | Nenhum ataque roda sem aprovação. Custo: cada ciclo exige um clique; benefício: controle total do operador. |
| 2026-11-16 | **ToolRunner nativo Windows** | Ferramentas já instaladas no host, zero fricção no evento. Docker/Kali fica para quando precisar de ghidra/pwntools. |
| 2026-11-16 | **`target` do challenge como fronteira de segurança** | O sandbox só aceita IP/domínio igual ao `target` (+ loopback). Alvo fora do challenge é bloqueado no guard. |
| 2026-11-16 | **Só NVIDIA (strong/fast)** | Chave disponível, endpoint OpenAI-compatible, sem surpresa de provider. Fallback só troca de modelo. |
| 2026-11-16 | **Knowledge vendorizado por commit** | Reprodutibilidade: o índice não muda no meio do evento; atualização é manual. |
| 2026-11-16 | **Submissão manual da flag** | O framework valida regex + duplicidade e exibe a flag; o operador submete. |
| 2026-11-17 | **Painel em página própria, polling 1.5 s** | Ciclo lento (minutos) torna SSE/WebSocket desnecessários; polling sobrevive a restart e é trivial de depurar. |
| 2026-11-17 | **"Ajustar" reusa `reject(feedback)`; "Rejeitar" = `terminate()`** | Ajustar pede nova decisão com a instrução; Rejeitar encerra. Dois botões, semânticas distintas. |
| 2026-11-17 | **Sessões em memória no MVP** | Simplicidade; persistência é o próximo item do backlog. |
| 2026-11-17 | **Polling pausa com checkpoint visível / foco na lateral** | Evita que o swap de HTML "coma" cliques e digitação. |

---

## Evidências de funcionamento

### Challenge #2 — `IDP - Sorteios Especiais` (resolvido)

- Evento `idp-ctf-03-local`, challenge de 500 pts.
- Caminho: recon → júri → especialista `crypto` → validação de flag.
- Exploit: o RNG do jogo era semeado com o **unix epoch**; a bola 42 saía de
  `Random(seed).Next() % 1000 + 1` no segundo escolhido.
- Artefatos do processo: `scripts/orchestrator_real_test.py` e o harness
  `scripts/win_game/` (não versionado por completo — `bin/` e `obj/` no `.gitignore`).

### Challenge #3 — `Double Wordle` (em validação)

- Categoria `crypto`, 500 pts, anexo `transmissao.txt` (238 bytes), sem `target`.
- Sessão real executada (`73ac39eb`), júri pediu especialista **`forensics`** — confirmando que a
  categoria existe e é roteada.
- Estado: aguardando validação ao vivo do ciclo de aprovação no painel (sessões em memória somem
  a cada restart do servidor; é preciso reiniciar o agente).

### Números do repositório

- 60 commits até `1056255` (+ os de documentação).
- 122 testes passando.
- 41 MB no total; 15 MB de conhecimento vendorizado.
- 8 ferramentas na allowlist; 6 categorias de especialista.

---

## Perguntas em aberto

1. **Persistir sessões**: tabela própria ou serializar `AgentSession` em JSON? (afeta migração e
   retomada após restart)
2. **Ciclo assíncrono**: thread por sessão, `asyncio.to_thread` ou fila simples? (o polling já está
   pronto para qualquer um)
3. **Custo**: gravar tokens por ciclo no `Attempt`? (pydantic-ai expõe `usage`)
4. **Sandbox**: quando migrar para Docker/Kali e como manter a allowlist nativa como fallback?
5. **Especialistas**: personas dedicadas mudam o resultado? (hoje todos usam o texto default)
6. **Planner**: usar o ranking de pontos/dificuldade para sugerir o próximo challenge no dashboard?

---

## Referências internas

| Documento | Conteúdo |
|---|---|
| `docs/specs/2026-11-16-ctf-orchestrator-design.md` | spec original (propósito, arquitetura, dados, políticas) |
| `docs/specs/2026-11-17-agent-panel-design.md` | spec do painel do agente |
| `docs/plans/2026-11-16-core-tracking.md` | plano do núcleo de tracking |
| `docs/plans/2026-11-16-tool-runner.md` | plano do ToolRunner |
| `docs/plans/2026-11-16-knowledge.md` | plano do knowledge subsystem |
| `docs/plans/2026-11-16-agent-runtime.md` | plano do Agent Runtime |
| `docs/plans/2026-11-17-agent-panel.md` | plano do painel do agente |
| `docs/agents-architecture.mmd` | diagrama Mermaid da orquestração |
