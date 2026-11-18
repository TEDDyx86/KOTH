# Spec — UI do Painel do Agente (checkpoint humano)

**Data:** 2026-11-17 · **Status:** aprovada (aguardando review do usuário)
**Depende de:** Agent Runtime (subsistema 5), `theme.css`, HTMX 2.0.4

## 1. Objetivo

Checkpoint humano visual para o orchestrator: acompanhar em tempo real o ciclo
OP→PL→RC→Júri→especialistas, interagir com Aprovar / Rejeitar / Ajustar em cada
parada, e registrar a flag manualmente — tudo no visual do tema (preto degradê,
vermelho `#e01b24`, verde `#2ea043` apenas para flag confirmada).

## 2. Decisões do brainstorming

| Decisão | Escolha |
|---|---|
| Localização | Página própria `GET /challenges/{id}/agent`, link "Painel do Agente" no header do `challenge.html` |
| Streaming | Polling HTMX nativo (`hx-trigger="every 1.5s"`), sem SSE/WebSocket |
| Checkpoint | 3 ações separadas: Aprovar (continua), Rejeitar (encerra a sessão), Ajustar (instrução injetada como diretriz no próximo passo) |
| Submissão de flag | 100% manual, no painel, via `validate_flag` (regex do evento + duplicidade) |

## 3. Mapeamento design ↔ código atual

- `Orchestrator` (app/agents/orchestrator.py) já para em **cada** decisão:
  `start()` e cada `approve()` terminam em `state="awaiting_approval"` com
  `pending_decision`. O painel não controla o loop — só reage ao estado.
- **Aprovar** → `POST /api/agent/sessions/{sid}/approve` (lógica existente).
- **Ajustar** → `orch.reject(feedback)`: re-decide com `extra_feedback` —
  exatamente "instrução como diretriz no próximo passo". Reuso direto.
- **Rejeitar (encerrar)** → método **novo** `Orchestrator.terminate(session_id,
  reason)`: `state → "rejected"`, registra nota `[agent terminated]`. (O
  `reject` atual re-decide; não serve para encerrar.)
- Sessões vivem em `app.state._orchestrator.sessions` (memória). Tanto
  `agents/api.py` quanto as rotas web precisam do mesmo helper — extrair
  `get_orchestrator(request)` para módulo compartilhado e usar dos dois lados.
- **Lookup por challenge, não por session_id**: `GET /challenges/{id}/agent/feed`
  encontra a sessão mais recente do challenge no `orch.sessions`. A URL do
  painel nunca precisa saber o sid.
- `start()` é **síncrono** (recon_bundle + Júri chamam o LLM — minutos). MVP:
  botão de start trava com spinner (`hx-indicator`); o feed, que já polla,
  mostra `running` assim que a sessão é registrada. (Execução em thread de
  fundo fica no backlog.)

## 4. Página e layout

`agent_panel.html` estende `base.html`. Grid de duas colunas:

```
[  FEED (esq, ~70%)                 ][  LATERAL (dir, ~30%)              ]
[  stream de ciclos/eventos        ][  card status + start             ]
[  bloco por ciclo, mono           ][  card checkpoint (3 botões)      ]
[                                  ][  card flag manual                ]
```

Mobile (max-width 900px): coluna única, lateral em cima.

## 5. Endpoints HTMX (novos em `web/routes.py`)

| Método | Rota | Retorna |
|---|---|---|
| GET | `/challenges/{id}/agent` | página do painel |
| GET | `/challenges/{id}/agent/feed` | parcial HTML do feed (polling) |
| GET | `/challenges/{id}/agent/side` | parcial HTML dos cards laterais (polling) |
| POST | `/challenges/{id}/agent/start` | parcial do card lateral (cria sessão) |
| POST | `/challenges/{id}/agent/sessions/{sid}/approve` | parcial lateral |
| POST | `/challenges/{id}/agent/sessions/{sid}/terminate` | parcial lateral |
| POST | `/challenges/{id}/agent/sessions/{sid}/adjust` | parcial lateral (campo `instruction`) |
| POST | `/challenges/{id}/flag` | parcial do card de flag (badges/erro) |

Os três POSTs de checkpoint delegam para `orch.approve/terminate/reject` —
mesma lógica já exposta em `agents/api.py`; as rotas REST permanecem intactas.

## 6. Feed — formato dos eventos

Cada sessão = lista cronológica. Render:

- `<div class="cycle">` por ciclo com header `Ciclo N — {categoria}`
- Linhas mono rotuladas: **Plano** (next_step do Júri), **Comando** (tool +
  args do relatório do especialista), **Output** (≤2000 chars, resto em
  `<details>` "expandir"), **Findings** (bullets), **Veredito** (action + reason)
- Findings acumulados (`sess.findings`) aparecem consolidados no topo da
  sessão, em bloco próprio
- Cores: vermelho `--accent` para comandos/falhas, `--warning: #d29922`
  (token novo) para checkpoint aguardando, `--flag-ok` só flag confirmada
- Sem sessão ainda: placeholder "Nenhuma sessão. Inicie o agente →"

## 7. Cards laterais

- **Status**: estado da sessão (idle / running / aguardando checkpoint /
  solved / gave_up / rejected) com badge colorida; botão "Iniciar agente"
  (hx-post start, spinner via hx-indicator); desabilitado se já existe
  sessão ativa
- **Checkpoint** (só quando `awaiting_approval`): action do Júri
  (DISPATCH/SUBMIT/RECHECK/GIVE_UP), categoria + objetivo do especialista,
  flag candidate quando houver, e os 3 botões:
  - `Aprovar` — primário vermelho
  - `Ajustar` — abre `<input>` de instrução + confirmar (hx-post adjust)
  - `Rejeitar` — termina a sessão (hx-post terminate)
- **Flag manual**: input + "Registrar flag". Aceito → badge verde com a flag,
  `tracking.update_challenge(flag, status=solved)`, feed ganha linha de
  flag. Recusado → erro inline vermelho com o motivo. Sempre registra
  Attempt (o `validate_flag` já faz).

## 8. Não-objetivos (MVP)

- Persistência de sessão no DB (sessões somem no restart — backlog acordado)
- Start em thread de fundo
- Gráficos/timeline visual (abordagem C cortada)
- Qualquer conexão com plataformas de CTF

## 9. Estilo

Reuso de `theme.css` (cards, badges, mono). Único token novo:
`--warning: #d29922` (checkpoint pendente / running).

## 10. Testes (TDD)

1. `terminate()` no Orchestrator: estado → rejected, nota registrada, decisão
   pendente descartada
2. Rotas web: feed/side retornam 200 com marcação correta para cada estado
   (sem sessão / running / awaiting_approval / solved), usando orchestrator
   fake via override de `get_orchestrator`
3. approve/terminate/adjust: 404 sem sessão, 409 em estado inválido,
   delegação correta
4. Flag manual: aceito (badge + update_challenge), formato inválido,
   duplicado
5. Suite existente (106 testes) permanece verde
