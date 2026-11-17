"""Real-world test: run the orchestrator against the local "IDP Sorteios
Especiais" game (idp-ctf-03-sorteio) — win the draw to capture the flag.

Seeds the REAL database (default_engine) with event + challenge, copies the
game's key files as challenge Attachments, builds the real orchestrator
(Júri = nemotron-3-ultra-550b, especialistas = nemotron-3.5-super-vl-120b)
and runs start → approve (auto-approved cycles) until solved/gave_up.

Usage: python scripts/orchestrator_real_test.py [--max-cycles 8]
"""
import argparse
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AUTH_PATH = Path(os.environ.get("USERPROFILE", "")) / ".local" / "share" / "opencode" / "auth.json"

GAME_DIR = Path(r"C:\Users\Administrator\AppData\Local\Programs\IDP Sorteios Especiais")
GAME_FILES = [
    "IdpSorteiosEspeciais.dll",
    "IdpSorteios.Core.dll",
    "IdpSorteios.Infrastructure.dll",
    "extract.cs",
    "IdpSorteiosEspeciais.deps.json",
]

DESCRIPTION = """Jogo local .NET 10 (WPF): 'IDP - Sorteios Especiais' — um sorteador de
bolas. Para capturar a flag, é preciso VENCER o jogo (o time sorteado ganha e o app
exibe a flag na UI). É um challenge de reversing local; sem rede, sem servidor.

Observações iniciais do operador (recon manual já feito):
- Binários em %LOCALAPPDATA%\\Programs\\IDP Sorteios Especiais
  (IdpSorteiosEspeciais.exe = host; lógica em IdpSorteiosEspeciais.dll,
  IdpSorteios.Core.dll, IdpSorteios.Infrastructure.dll; os arquivos foram
  anexados a este challenge).
- IdpSorteios.Core.dll: classe DrawEngine — CurrentSeed() = unix epoch em
  segundos (TimeProvider.GetUtcNow().ToUnixTimeSeconds()); _random = new
  Random(seed); Draw() = _random.Next() e ToNumber() = n % 1000 + 1; após
  cada Draw() há Reseed() com o relógio atual.
- TeamRound.Draw() compara o número sorteado com 42 (ldc.i4.s 42; ceq) —
  acertar a bola 42 faz IsWinner=true e a flag exibida = Team.Flag.
- Team.Flag vem do times.csv EMBUTIDO como recurso em
  IdpSorteiosEspeciais.dll (ManifestResource "times.csv", formato
  'nome;flag' por linha) — as 12 entradas apontam para IDP{FLAG_FALSA},
  portanto a flag revelada pela UI deve ser essa MESMA string; o objetivo é
  VENCER o jogo e confirmar qual flag a UI exibe ao vencedor.
- Regras do jogo: 3 tentativas por vez (turno) de cada um dos 12 times,
  TurnDuration com deadline; jogador interage por UI (botão Sortear).
Ferramentas permitidas: python (guard none, pip funciona), strings, objdump.
dotnet-ildasm falha nesta máquina; pip install dnfile dncil funciona (um
helper pronto existe em scripts/dotnet_disasm.py no repositório, cwd dos
tools é o sandbox do storage)."""


def load_nvidia_key() -> str:
    import json

    data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    key = data.get("nvidia", {}).get("key")
    if not key:
        raise SystemExit("no nvidia key in auth.json")
    return key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-cycles", type=int, default=8)
    args = parser.parse_args()

    os.environ["NVIDIA_API_KEY"] = load_nvidia_key()
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    from sqlmodel import Session

    from app.agents.api import _make_orchestrator
    from app.database import default_engine
    from app.models import Attachment, AttachmentType, Challenge, ChallengeStatus, Event
    from app.services.storage import StorageService

    engine = default_engine()
    storage = StorageService(ROOT / "storage")
    db = ROOT / "data" / "ctf.db"
    print(f"[db] {db}")

    with Session(engine) as session:
        event = Event(
            name="idp-ctf-03-local", flag_format=r"IDP\{.*\}",
            url=None,
        )
        existing = session.query(Event).filter(Event.name == event.name).first()
        if existing:
            event = existing
            print(f"[event] reusing #{event.id} {event.name}")
        else:
            session.add(event)
            session.commit()
            session.refresh(event)
            print(f"[event] created #{event.id} {event.name}")

        ch = session.query(Challenge).filter(
            Challenge.event_id == event.id,
            Challenge.title == "IDP - Sorteios Especiais").first()
        if ch is None:
            ch = Challenge(event_id=event.id, title="IDP - Sorteios Especiais",
                           category="reversing", points=300,
                           description=DESCRIPTION, target=None,
                           status=ChallengeStatus.open)
            session.add(ch)
            session.commit()
            session.refresh(ch)
            print(f"[challenge] created #{ch.id}")
        else:
            print(f"[challenge] reusing #{ch.id}")

        # attach the game files (skip duplicates by name)
        have = {a.name for a in session.query(Attachment)
                .filter(Attachment.challenge_id == ch.id).all()}
        for name in GAME_FILES:
            if name in have:
                print(f"[attach] {name} already attached")
                continue
            src = GAME_DIR / name
            if not src.exists():
                print(f"[attach] missing on disk: {name}")
                continue
            data = src.read_bytes()
            path = storage.save(ch.id, name, data)
            att = Attachment(challenge_id=ch.id, name=name,
                             path=str(path.relative_to(storage.storage_dir)),
                             mime="application/octet-stream",
                             size=len(data), type=AttachmentType.download)
            session.add(att)
            session.commit()
            print(f"[attach] {name} ({len(data)} bytes)")

        orch = _make_orchestrator(storage)

        print("\n[cycle 0] start (recon + primeira decisão)...")
        try:
            s = orch.start(session, ch.id)
        except Exception:
            traceback.print_exc()
            return 1
        print_session(s)

        cycle = 0
        while s.state not in ("solved", "gave_up") and cycle < args.max_cycles:
            cycle += 1
            decision = s.pending_decision
            action = decision.action if decision else "?"
            print(f"\n[cycle {cycle}] auto-approving {action}...")
            if action == "WAIT_USER":
                # answer ourselves: feed the operator context as feedback
                print("    WAIT_USER → reject with guidance to keep working")
                s = orch.reject(session, s.id,
                                "Continue autônomo: use run_tool (python) para "
                                "implementar a estratégia de vencer o jogo "
                                "(prever o número 42 com o RNG seedado pelo "
                                "relógio, ou automatizar a UI). Não pare.")
            else:
                try:
                    s = orch.approve(session, s.id)
                except Exception:
                    traceback.print_exc()
                    print("    approve failed — breaking loop")
                    break
            print_session(s)
            time.sleep(1)

        final = session.get(Challenge, ch.id)
        print(f"\n[final] challenge status={final.status} flag={final.flag}")
        print(f"[final] agent session state={s.state}")
        return 0 if s.state == "solved" else 1


def print_session(s) -> None:
    print(f"    state={s.state}")
    if s.pending_decision:
        d = s.pending_decision
        print(f"    decision={d.action} reason={d.reason!r}")
        if d.order:
            print(f"    order={d.order.model_dump()}")
        if d.flag_candidate:
            print(f"    flag_candidate={d.flag_candidate!r}")
    for i, r in enumerate(s.reports[-2:], 1):
        print(f"    report[-{i}]={r.model_dump()}")
    print(f"    findings({len(s.findings)})={s.findings[-6:]}")


if __name__ == "__main__":
    sys.exit(main())
