# Referência de rotas

Tudo roda no mesmo processo e na mesma porta (ex.: `http://127.0.0.1:8777`).

- **REST** (`/api/*`): JSON, para scripts e integrações.
- **HTML** (`/`, `/events/...`, `/challenges/...`): páginas Jinja2.
- **HTMX**: rotas que devolvem **fragmentos HTML** consumidos pelo painel do agente.

---

## 1. Saúde

### `GET /api/health`

```json
{"status": "ok"}
```

---

## 2. Eventos — `/api/events`

### `POST /api/events`

Corpo (`EventCreate`):

```json
{"name": "IDP CTF 03", "url": null, "starts_at": null, "ends_at": null}
```

Resposta: `Event` completo (`id`, `name`, `url`, `flag_format`, `starts_at`, `ends_at`, `status`,
`created_at`).

### `GET /api/events`

Lista de `Event`, ordenada por `created_at`.

### `PATCH /api/events/{event_id}/status`

```json
{"status": "active"}
```

Valores: `upcoming`, `active`, `finished`. `404` se o evento não existir.

---

## 3. Challenges — `/api`

### `POST /api/events/{event_id}/challenges`

Corpo (`ChallengeCreate`):

```json
{
  "title": "Double Wordle",
  "category": "crypto",
  "points": 500,
  "difficulty": "hard",
  "description": "...",
  "target": "10.10.10.5:1337",
  "tags": "rng,prng"
}
```

`404` se o evento não existir.

### `GET /api/events/{event_id}/challenges`

Query opcional: `category`, `status` (`open|solved|gave_up`), `tag` (match exato dentro do CSV de
tags).

### `GET /api/challenges/{challenge_id}`

```json
{
  "challenge": { "...": "..." },
  "summary": {"attempt_count": 3, "active_seconds": 412.5},
  "attempts": [ "..." ],
  "notes": [ "..." ]
}
```

`404` se não existir.

### `PATCH /api/challenges/{challenge_id}`

```json
{"status": "solved", "flag": "IDP{...}"}
```

Ambos os campos são opcionais. `status=solved` preenche `solved_at` se estiver vazio.

### `POST /api/challenges/{challenge_id}/attempts`

Corpo (`AttemptCreate`):

```json
{
  "type": "manual",
  "description": "testei seed = epoch",
  "result": "breakthrough",
  "tool_command": "python solve.py",
  "learning": "RNG = Random(seed)",
  "flag": null
}
```

- `type`: `manual | tool | agent | submission`
- `result`: `failed | partial | breakthrough | solved`

`result=solved` marca o challenge como resolvido e grava `flag` (se enviada).

### `POST /api/challenges/{challenge_id}/notes`

```json
{"content": "hipótese: MT19937 com seed fixa"}
```

Nota é **append-only**; a "nota atual" é a mais recente.

### `GET /api/challenges/{challenge_id}/notes`

Histórico, mais recente primeiro.

### `POST /api/challenges/{challenge_id}/attachments`

`multipart/form-data`:

| Campo | Tipo | Default |
|---|---|---|
| `file` | arquivo | obrigatório |
| `type` | `screenshot \| download \| output` | `download` |

Grava em `storage/<challenge_id>/<8hex>_<nome>` e devolve o `Attachment`.

### `GET /api/challenges/{challenge_id}/attachments`

Lista de `Attachment` do challenge.

---

## 4. Knowledge — `/api/knowledge`

### `POST /api/knowledge/rebuild`

Reconstrói o índice FTS5 a partir de `knowledge/`.

```json
{"indexed": 412}
```

### `GET /api/knowledge/search?q=...&limit=10&category=crypto`

`limit` entre 1 e 50. Devolve lista de hits:

```json
[
  {
    "source": "dispatch-arsenal",
    "category": "CRYPTO",
    "title": "MT19937 seed recovery",
    "path": "dispatch/Arsenal/CRYPTO/mt19937.md",
    "snippet": "...[[trecho]]..."
  }
]
```

### `GET /api/knowledge/skills`

Lista as skills indexadas (`source='skill'`): `[{category, title, path}]`.

---

## 5. Agent Runtime — `/api/agent`

Formato de sessão (`AgentSession.to_dict()`):

```json
{
  "id": "9f2c...",
  "challenge_id": 3,
  "state": "awaiting_approval",
  "pending_decision": {
    "action": "DISPATCH",
    "order": {"category": "forensics", "task": "...", "hints": [], "max_tool_runs": 5},
    "flag_candidate": null,
    "reason": "...",
    "findings": ["..."]
  },
  "findings": ["..."],
  "reports": [
    {"status": "NEED_HINT", "flag_candidate": null, "findings": ["..."],
     "next_step": "...", "tools_used": ["strings"]}
  ],
  "created_at": "2026-11-20T12:00:00+00:00"
}
```

### `POST /api/agent/{challenge_id}/start`

Cria a sessão, roda o recon e chama o júri (ou executa o loop completo se `mode="auto"`).
Corpo opcional:

```json
{"mode": "supervised", "background": false}
```

- `201` → sessão (`awaiting_approval`, `running` em background, ou `solved`/`gave_up` em `auto`)
- `404` → challenge inexistente
- `503` → runtime indisponível (provider sem chave): `{"detail": "provider 'nvidia' unavailable: env 'NVIDIA_API_KEY' not set"}`

### `GET /api/agent/sessions/{session_id}`

Estado atual da sessão (carregada da memória ou hidratada do SQLite). `404` se o id não existir.

### `POST /api/agent/sessions/{session_id}/approve`

Corpo opcional: `{"note": "...", "background": false}`. Executa **uma** decisão pendente (despacha especialista, valida
flag, ou encerra por `GIVE_UP`) e devolve a sessão atualizada.

- `409` → `"session <id> is not awaiting approval"`
- `404` → sessão desconhecida

### `POST /api/agent/sessions/{session_id}/reject`

```json
{"feedback": "não é RNG, olhe o padding do AES"}
```

Grava a nota `[agent rejected] ...` e pede nova decisão ao júri **com** o feedback. Devolve `409` se a sessão não estiver em `awaiting_approval`.

### `POST /api/agent/sessions/{session_id}/terminate`

Corpo opcional: `{"reason": "..."}`. Encerra a sessão definitivamente (`state=rejected`), consolida `final_report` e grava nota `[agent terminated] ...`.

### `GET /api/agent/events/{event_id}/rank`

Devolve o ranking determinístico de challenges do evento via `planner_rank` (pontos + dificuldade + status).

### `GET /api/agent/tools/inventory`

Devolve o inventário do `Tool Doctor` (`[{name, mode, path, present}]`).

---

## 6. Páginas HTML

| Método | Rota | Ação |
|---|---|---|
| GET | `/` | dashboard (eventos, challenges abertos, tentativas recentes) |
| POST | `/events` | cria evento (form `name`) → redirect 303 |
| GET | `/events/{event_id}` | página do evento (query `category`, `status`) |
| POST | `/events/{event_id}/challenges` | cria challenge (form) → redirect 303 |
| GET | `/challenges/{challenge_id}` | página do challenge |
| POST | `/challenges/{challenge_id}/attempts` | registra tentativa manual (form) → redirect 303 |
| POST | `/challenges/{challenge_id}/notes` | adiciona nota (form) → redirect 303 |
| POST | `/challenges/{challenge_id}/attachments` | upload (`file`, `type`) → redirect 303 |
| GET | `/challenges/{challenge_id}/agent` | **Painel do Agente** |

Formulários usam `application/x-www-form-urlencoded` (exceto upload, que é `multipart/form-data`).

---

## 7. Endpoints HTMX do painel

Todos devolvem **fragmentos HTML** (`_agent_feed.html`, `_agent_side.html`,
`_flag_result.html`), não JSON.

| Método | Rota | Devolve |
|---|---|---|
| GET | `/challenges/{id}/agent/feed` | `_agent_feed.html` — sessão, findings, ciclos |
| GET | `/challenges/{id}/agent/side` | `_agent_side.html` — status, checkpoint, botões |
| POST | `/challenges/{id}/agent/start` | `_agent_side.html` atualizado (`503` se runtime indisponível, `409` se já há sessão ativa) |
| POST | `/challenges/{id}/agent/sessions/{sid}/approve` | `_agent_side.html` (`409` se não está aguardando) |
| POST | `/challenges/{id}/agent/sessions/{sid}/terminate` | `_agent_side.html` (`state=rejected`) |
| POST | `/challenges/{id}/agent/sessions/{sid}/adjust` | `_agent_side.html` (form `instruction`) |
| POST | `/challenges/{id}/flag` | `_flag_result.html` (form `flag`) |

Observações de implementação:

- O painel **não** carrega `session_id` na URL: as rotas `feed`/`side` usam a sessão mais recente
  daquele challenge (`latest_session`).
- `feed` e `side` toleram runtime indisponível: renderizam `idle` em vez de 500.
- O polling da lateral pausa quando há checkpoint visível ou foco dentro dela.

---

## 8. Exemplos (PowerShell)

```powershell
$base = "http://127.0.0.1:8777"

# evento
Invoke-RestMethod "$base/api/events" -Method Post -ContentType "application/json" `
  -Body '{"name":"IDP CTF 03"}'

# challenge
Invoke-RestMethod "$base/api/events/3/challenges" -Method Post -ContentType "application/json" `
  -Body '{"title":"Double Wordle","category":"crypto","points":500,"target":"10.10.10.5"}'

# busca no knowledge
Invoke-RestMethod "$base/api/knowledge/search?q=prng%20seed&limit=3"

# sessão de agente
Invoke-RestMethod "$base/api/agent/3/start" -Method Post

# aprovar o checkpoint
Invoke-RestMethod "$base/api/agent/sessions/<session_id>/approve" -Method Post `
  -ContentType "application/json" -Body '{"note":"ok"}'
```

Em `curl` (se disponível): `curl -s -X POST $base/api/agent/3/start`.
