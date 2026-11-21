# Guia operacional

Como instalar, subir, operar e diagnosticar o CTF Orchestrator no dia a dia (incluindo durante
uma competição).

---

## 1. Pré-requisitos

| Item | Verificação |
|---|---|
| Python 3.12+ | `python --version` |
| pip + venv (opcional) | `python -m pip --version` |
| Ferramentas no `PATH` | `nmap --version`, `strings --version`, `curl --version`, `sqlmap --version`... |
| Chave NVIDIA | `$env:NVIDIA_API_KEY` definida |

Ferramentas ausentes não impedem o app de subir: o recon registra `skipped (tool 'x' not found on
PATH)` e o especialista recebe erro de guard, mas o tracking, o júri e a UI continuam funcionando.

---

## 2. Instalação

```powershell
cd "C:\Users\Administrator\Documents\GitHub\CTF FRAMEWORK"
python -m pip install -e .
```

Se você moveu a pasta depois de instalar, **reinstale** — o `pip install -e` guarda o caminho
absoluto do projeto:

```powershell
python -m pip install -e .
```

Verificação rápida:

```powershell
python -c "import app, fastapi, sqlmodel, pydantic_ai; print('ok')"
```

---

## 3. Variáveis de ambiente

| Variável | Padrão | Uso |
|---|---|---|
| `NVIDIA_API_KEY` | — | chave do provider `nvidia` (obrigatória para agentes) |
| `NINE_ROUTER_API_KEY` | — | provider alternativo já declarado em `config/models.yaml` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | providers declarados, sem uso nesta fase |
| `CTF_DB_PATH` | `<projeto>/ctf.db` | caminho do banco SQLite |
| `CTF_STORAGE_DIR` | `<projeto>/storage` | anexos, outputs e sandbox |
| `CTF_FLAG_FORMAT` | `flag{...}` | default global (o formato efetivo vem do evento) |

Carregar a chave a partir do `auth.json` do OpenCode:

```powershell
$env:NVIDIA_API_KEY = (Get-Content "$env:USERPROFILE\.local\share\opencode\auth.json" -Raw |
  ConvertFrom-Json).nvidia.key
```

A variável vale só para a sessão do PowerShell. Para persistir:
`[Environment]::SetEnvironmentVariable('NVIDIA_API_KEY', $key, 'User')`.

---

## 4. Subir o servidor

**Opção A — comando único (Windows / Linux / macOS):**

```bash
koth
# ou headless:
koth serve --port 8777 --no-browser
# ou diagnóstico de ferramentas / Docker / provedores:
koth doctor
```

**Opção B — uvicorn direto (para debug com `--reload`):**

```bash
python -m uvicorn app.main:app --port 8777 --reload
```

**Opção C — Docker Sandbox (opcional):**

```bash
docker build -t koth-sandbox:latest -f Dockerfile.sandbox .
```

Depois ative `docker` em **Configurações → Sandbox & Limites** (`/settings?tab=runtime`).

Checagem de saúde:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8777/api/health | Select-Object -Expand Content
# {"status":"ok"}
```

---

## 5. Operação — passo a passo

### 5.1 Criar evento

Dashboard (`/`) → campo **nome** → *Criar*. O `flag_format` do evento é o regex usado na validação
da flag; se ficar vazio, o framework usa `CTF\{.*\}`.

Formatos vistos em eventos reais: `IDP\{.*\}` (evento IDP), `CTF\{.*\}`, `flag\{.*\}`.

### 5.2 Cadastrar challenge

Em `/events/{id}`: título, categoria (`web`, `pwn`, `crypto`, `reverse`, `forensics`, `osint`,
`misc`), pontos, dificuldade, descrição e **target** (`host:porta` ou só `host`).

O `target` é o contrato de segurança: o ToolRunner **só** aceita IP/domínio igual ao host dele
(mais loopback). Deixe vazio para desafios offline (só anexos).

### 5.3 Anexar arquivos

Em `/challenges/{id}` → upload com `type=download`. Esses anexos são exatamente o que o recon vai
passar por `strings` quando o agente iniciar.

### 5.4 Iniciar o agente

Abra `/challenges/{id}/agent` e clique em **Iniciar agente**.

- O botão desabilita e aparece "trabalhando (recon + júri, pode levar minutos)…".
- O `start` é **síncrono**: a requisição só responde quando o júri devolve a primeira decisão.
- Ao terminar, a lateral mostra o **checkpoint** com `action` + `reason` (e a ordem de tarefa, se
  houver).

### 5.5 Checkpoint

| Botão | Efeito | Quando usar |
|---|---|---|
| **Aprovar** | Executa a decisão pendente (despacha especialista ou valida flag) e volta ao checkpoint com a nova decisão | concorda com o plano do júri |
| **Rejeitar** | Encerra a sessão (`rejected`), sem novo ciclo | o caminho está errado / quer parar |
| **Ajustar com instrução** | Grava sua instrução como nota e pede nova decisão **com** ela no prompt | quer redirecionar sem matar a sessão |

**Aprovar pode demorar minutos** (um especialista LLM + várias execuções de ferramenta + júri). O
spinner "executando ciclo (especialista + júri, pode levar minutos)…" fica visível e os botões
ficam desabilitados. Não clique duas vezes.

Durante a execução o polling da lateral **pausa** (há checkpoint na tela / foco no formulário), o
que é esperado.

### 5.6 Registrar a flag

Card **Registrar flag** na lateral: cole a flag e envie. Resultados possíveis:

- **aceita** → challenge `solved`, badge verde, `Attempt(type=submission, result=solved)`;
- **formato inválido** → mensagem com o regex esperado, `Attempt(failed)`;
- **duplicada** → "duplicate submission", `Attempt(failed)`.

Depois disso, copie a flag e submeta **você** na plataforma do evento.

---

## 6. Interpretando o painel

| Sinal | Significado |
|---|---|
| `idle — nenhuma sessão` | nenhuma sessão para este challenge (ou o servidor reiniciou) |
| badge `running` (amarelo) | ciclo em andamento |
| badge `awaiting_approval` (amarelo) + caixa tracejada | checkpoint esperando você |
| badge `solved` (verde) | flag validada |
| badge cinza | `gave_up` / `rejected` |
| `próximo passo (júri): ...` no feed | decisão pendente resumida |
| "Ciclo N — especialista" | relatório de um dispatch já executado |
| "Findings acumulados" | fatos que o loop carrega entre ciclos |

---

## 7. Diagnóstico

### O painel não abre / 404

Confirme o challenge: `http://127.0.0.1:8777/challenges/3/agent`. Se `/challenges/3` funciona e
`/agent` não, o problema é a rota do painel — verifique se o servidor é o processo certo (veja a
porta na barra de endereço).

### Botão não faz nada

1. Abra o DevTools → aba Network: o POST saiu? Qual status?
2. A caixa `#agent-error-card` deve mostrar `erro <status> — <corpo>`.
3. Verifique o log do uvicorn (o terminal onde ele roda) — tracebacks aparecem lá.

Histórico: já houve um bug em que o parcial lateral era renderizado com `challenge_id` vazio,
gerando URLs `challenges//agent/...` e 404 silencioso. Corrigido em `1056255`.

### `503 agent runtime unavailable`

Provider sem chave. A mensagem traz o nome da env var que falta:

```
provider 'nvidia' unavailable: env 'NVIDIA_API_KEY' not set
```

Carregue a chave **no mesmo terminal** em que o uvicorn roda e reinicie o servidor.

### `409 session already active`

Já existe sessão `running`/`awaiting_approval` para o challenge. Volte ao painel e continue de lá,
ou encerre a sessão com **Rejeitar**. Não há endpoint REST de terminate — só o painel.

### `409 session ... is not awaiting approval`

Você clicou Aprovar em uma sessão que já avançou (duplo clique, outra aba, ou estado `solved`).
Recarregue o painel.

### Aprovar "trava"

É síncrono e pode levar minutos: o especialista faz chamadas ao LLM e roda ferramentas (nmap com
timeout de 120 s, por exemplo). Acompanhe o log do uvicorn. Se passar de ~10 min, o provider pode
estar lento/instável — Rejeitar e reiniciar a sessão é o caminho.

### Sessão sumiu depois de reiniciar o servidor

Esperado: `Orchestrator.sessions` é memória de processo. O tracking (banco) permanece; o painel
volta para `idle`. Persistir sessões está no backlog.

### `AttributeError: in_progress` ao iniciar em challenge novo

Bug conhecido em `app/agents/recon.py` (o enum `ChallengeStatus` não tem `in_progress`). Só ocorre
quando o challenge nunca teve tentativa/nota (`first_interaction is None`). Contorno imediato:
registre qualquer tentativa manual no challenge antes de iniciar o agente. Correção definitiva na
lista de dívida técnica.

### `tool 'x' not found on PATH`

Instale a ferramenta ou remova-a de `config/tools.yaml`. No Windows, confirme que o terminal que
roda o uvicorn enxerga o `PATH` correto.

### `host 'x' is not the challenge target`

O agente tentou um alvo diferente de `challenge.target`. Preencha/ajuste o campo `target` do
challenge (host exato, sem esquema: `10.10.10.5`, `app.ctf.local`).

### `shell metacharacter in arg`

Guard `strict` bloqueou `;`, `|`, `&`, `$`, backtick ou quebra de linha. O modelo deve chamar a
ferramenta com argumentos simples. Se a ferramenta precisa de shell (pipes), ela não é compatível
com `guard: strict` — use `python` ou escreva um script.

### Busca de conhecimento vazia

```powershell
python scripts/knowledge-search.py "sql injection"
```

Se responder `(index empty - rebuilding...)`, o índice foi criado. Para forçar:

```powershell
Invoke-WebRequest -UseBasicParsing -Method POST http://127.0.0.1:8777/api/knowledge/rebuild
```

Se `knowledge/` estiver vazio, rode `python scripts/sync_knowledge.py` (precisa de rede e git).

### `pytest -q` quebra a coleta

Use o caminho explícito:

```powershell
python -m pytest tests -q
```

Sem isso, o pytest tenta coletar `knowledge/ctf-skills/tests` (repo vendorizado) e falha.

### Porta em uso

```powershell
Get-NetTCPConnection -LocalPort 8777 | Select-Object OwningProcess
Stop-Process -Id <pid>
```

Ou suba em outra porta (`--port 8778`).

### Acentuação estranha no terminal

Os arquivos são UTF-8. O PowerShell 5.1 lê como ANSI e mostra `Ã©`/`�?"`. Isso é só exibição; use
`Get-Content -Encoding UTF8` ou um editor UTF-8.

---

## 8. Manutenção

### Backup

```powershell
Copy-Item ctf.db "backup-ctf-$(Get-Date -Format yyyyMMdd-HHmm).db"
Copy-Item storage "backup-storage-$(Get-Date -Format yyyyMMdd-HHmm)" -Recurse
```

`ctf.db` e `storage/` são gitignored — o backup é sua responsabilidade.

### Atualizar conhecimento vendorizado

```powershell
python scripts/sync_knowledge.py
Invoke-WebRequest -UseBasicParsing -Method POST http://127.0.0.1:8777/api/knowledge/rebuild
```

O `knowledge/knowledge.lock` registra repo, ref, commit e caminho. Atualização é sempre manual
(nada de auto-update no boot) para não mudar o comportamento no meio de um evento.

### Resetar o ambiente

```powershell
Remove-Item ctf.db
Remove-Item storage -Recurse -Force
```

O banco é recriado no próximo request (`create_all` + `ensure_schema`).

### Rodar a suíte

```powershell
python -m pytest tests -q
# 122 passed
```

---

## 9. Checklist antes de um evento

- [ ] `python -m pytest tests -q` verde
- [ ] `NVIDIA_API_KEY` carregada e smoke test do modelo OK (`python scripts/model_smoke_test.py`)
- [ ] inventário de ferramentas conferido (`strings`, `nmap`, `curl`, `sqlmap`, `python`...)
- [ ] `knowledge.lock` atualizado e índice reconstruído
- [ ] evento criado com `flag_format` correto
- [ ] desafios cadastrados com `target` quando aplicável
- [ ] anexos enviados como `type=download`
- [ ] backup do `ctf.db` anterior

---

## 10. Limitações conhecidas (nesta fase)

- Sessões de agente em memória (somem no restart).
- `approve`/`start` síncronos (a requisição espera o ciclo inteiro).
- `limits.max_attempts` / `max_minutes` não enforçados.
- `planner.py` não está ligado ao loop (priorização ainda não usada).
- Sem endpoint/UI de inventário de ferramentas.
- Sem persistência de custo/tokens por ciclo.
- Sandbox nativo Windows; Docker/Kali e decompilador .NET no backlog.
- `python` com `guard: none` (confiança total no agente).
- Bug latente de `in_progress` no recon (ver §7).
