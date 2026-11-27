from dataclasses import dataclass, field
from pathlib import Path
import re
import socket
import zipfile

from sqlmodel import Session, select

from ..models import Attachment, AttachmentType, AttemptResult, AttemptType, Challenge
from ..services import tracking
from ..services.storage import StorageService

RECON_TOOLS = ("strings", "nmap")

_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x7fELF", "ELF binary"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit LE binary"),
    (b"\xfe\xed\xfa\xcf", "Mach-O 64-bit BE binary"),
    (b"\xce\xfa\xed\xfe", "Mach-O 32-bit LE binary"),
    (b"\xfe\xed\xfa\xce", "Mach-O 32-bit BE binary"),
    (b"\xca\xfe\xba\xbe", "Mach-O Universal / Java Class"),
    (b"dex\n", "Android DEX bytecode"),
    (b"\x00asm", "WebAssembly binary"),
    (b"MZ", "PE/DOS executable (MZ)"),
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"%PDF-", "PDF document"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x1f\x8b\x08", "GZIP compressed"),
    (b"SQLite format 3\x00", "SQLite 3 database"),
    (b"\xd4\xc3\xb2\xa1", "PCAP capture (little-endian)"),
    (b"\xa1\xb2\xc3\xd4", "PCAP capture (big-endian)"),
    (b"\x0a\x0d\x0d\x0a", "PCAPNG capture"),
]

_URL_RE = re.compile(r"https?://[a-zA-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_IGNORED_URL_HOSTS = (
    "w3.org",
    "apple.com",
    "schemas.xmlsoap.org",
    "schemas.microsoft.com",
    "purl.org",
    "creativecommons.org",
    "plone.org",
    "zope.dev",
    "python.org",
    "nmap.org",
    "github.com/nmap",
    "example.com",
    "localhost",
)


def detect_file_type(path: Path) -> str:
    """Deterministic file type detection from magic bytes / text sniff."""
    try:
        head = path.read_bytes()[:512]
    except OSError as exc:
        return f"unreadable ({exc})"
    if not head:
        return "empty file"
    for magic, label in _MAGIC_SIGNATURES:
        if head.startswith(magic):
            return label
    if len(head) >= 265 and head[257:262] == b"ustar":
        return "TAR archive"
    try:
        head.decode("utf-8")
        return "UTF-8/ASCII text"
    except UnicodeDecodeError:
        return "binary data"


def _detect_magic_bytes(head: bytes) -> str | None:
    for magic, label in _MAGIC_SIGNATURES:
        if head.startswith(magic):
            return label
    return None


def _inspect_zip_archive(path: Path) -> tuple[list[str], list[str]]:
    """Inspect ZIP/IPA/APK/JAR entries without extracting, identifying inner executables."""
    members: list[str] = []
    inner_bins: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = [zi for zi in zf.infolist() if not zi.is_dir()]
            for zi in infos[:25]:
                members.append(f"{zi.filename} ({zi.file_size}b)")
                if zi.file_size >= 16 and (
                    zi.filename.endswith((".so", ".dylib", ".dll", ".exe", ".dex", ".bin"))
                    or "/MacOS/" in zi.filename
                    or (".app/" in zi.filename and "." not in Path(zi.filename).name)
                ):
                    try:
                        with zf.open(zi, "r") as fh:
                            hdr = fh.read(16)
                        lbl = _detect_magic_bytes(hdr)
                        if lbl:
                            inner_bins.append(f"{zi.filename}: {lbl} ({zi.file_size}b)")
                    except Exception:
                        pass
            if len(infos) > 25:
                members.append(f"... (+{len(infos) - 25} more entries)")
    except Exception:
        pass
    return members, inner_bins


def _extract_discovered_urls(lines: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        for raw in _URL_RE.findall(ln):
            url = raw.rstrip(".,;:)'\"`>\\")
            low = url.lower()
            if any(h in low for h in _IGNORED_URL_HOSTS):
                continue
            if url not in seen:
                seen.add(url)
                found.append(url)
            if len(found) >= 10:
                return found
    return found


def probe_tcp_banner(host: str, port: int, *, timeout_s: float = 2.0, max_bytes: int = 4096) -> str | None:
    """Quick raw TCP banner grab for non-HTTP socket services (jail/pwn/crypto)."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as sock:
            sock.settimeout(timeout_s)
            chunks: list[bytes] = []
            total = 0
            while total < max_bytes:
                try:
                    data = sock.recv(min(1024, max_bytes - total))
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if len(data) < 1024:
                    # Wait briefly once more only if very small initial chunk
                    sock.settimeout(0.25)
            raw = b"".join(chunks)
            text = raw.decode("utf-8", errors="replace").strip()
            return text[:1500] if text else None
    except OSError:
        return None


@dataclass
class ReconDigest:
    target: str | None
    files: list[dict] = field(default_factory=list)
    summary: str = ""
    discovered_urls: list[str] = field(default_factory=list)


def _run_optional_probe(runner, challenge, tool: str, args: list[str],
                      *, timeout_s: int = 20) -> str | None:
    """Run an adaptive recon probe only when `tool` is configured on runner.settings."""
    settings = getattr(runner, "settings", None)
    tools = getattr(settings, "tools", None) if settings is not None else None
    if not tools or tool not in tools:
        return None
    try:
        res = runner.run_raw(challenge, tool, args, timeout_s=timeout_s, output_limit_mb=1)
        out = (res.stdout or "").strip()
        return out if out else None
    except Exception:
        return None


def recon_bundle(runner, session: Session, challenge: Challenge,
                 storage: StorageService | None = None) -> ReconDigest:
    """Adaptive deterministic recon: file type + strings + ZIP/IPA inspection + URL discovery +
    TCP banner grab + targeted probes (checksec/file/exiftool/binwalk/tshark/curl/nmap)."""
    digests: list[dict] = []
    summaries: list[str] = []
    discovered_urls: list[str] = []

    effective_storage = storage or getattr(runner, "storage", None)
    if runner is not None and hasattr(runner, "stage_downloads"):
        runner.stage_downloads(session, challenge)

    artifacts = list(session.exec(select(Attachment).where(
        Attachment.challenge_id == challenge.id,
        Attachment.type == AttachmentType.download,
    )).all())

    for att in artifacts:
        base_dir = effective_storage.storage_dir if effective_storage else Path("storage")
        path = base_dir / att.path
        if not path.exists():
            continue
        ftype = detect_file_type(path)
        summaries.append(f"file {att.name}: {ftype} ({att.size} bytes)")
        entry: dict = {"name": att.name, "file_type": ftype, "strings": []}
        staged_name = Path(att.name).name

        if ftype == "ZIP archive" or Path(att.name).suffix.lower() in (".zip", ".ipa", ".apk", ".jar"):
            members, inner_bins = _inspect_zip_archive(path)
            if members:
                entry["zip_members"] = members
                summaries.append(f"zip-contents {att.name}: " + ", ".join(members[:10]))
            if inner_bins:
                entry["inner_binaries"] = inner_bins
                summaries.append(f"inner-binaries {att.name}: " + " | ".join(inner_bins[:6]))

        try:
            res = runner.run_raw(challenge, "strings",
                                 [staged_name], timeout_s=30, output_limit_mb=1)
        except Exception as exc:  # tool missing / guard — recon must not die
            summaries.append(f"strings {att.name}: skipped ({exc})")
            digests.append(entry)
            continue
        out = (res.stdout or "").strip()
        all_lines = [ln for ln in out.splitlines() if ln.strip()]
        interesting = all_lines[:40]
        entry["strings"] = interesting[:40]
        digests.append(entry)
        if interesting:
            summaries.append(f"strings {att.name}: " + " | ".join(interesting[:8]))

        urls = _extract_discovered_urls(all_lines)
        for u in urls:
            if u not in discovered_urls:
                discovered_urls.append(u)
        if urls:
            summaries.append(f"discovered-urls {att.name}: " + ", ".join(urls[:5]))

        # Adaptive type-specific probes when configured on ToolRunner
        if "ELF" in ftype or "Mach-O" in ftype:
            f_out = _run_optional_probe(runner, challenge, "file", [staged_name], timeout_s=15)
            if f_out:
                summaries.append(f"file-detail {att.name}: {f_out.splitlines()[0][:180]}")
            if "ELF" in ftype:
                chk = _run_optional_probe(runner, challenge, "checksec", [f"--file={staged_name}"], timeout_s=20)
                if chk:
                    summaries.append(f"checksec {att.name}: {' | '.join(chk.splitlines()[:6])[:300]}")
        elif "image" in ftype:
            ex = _run_optional_probe(runner, challenge, "exiftool", [staged_name], timeout_s=20)
            if ex:
                ex_lines = [ln.strip() for ln in ex.splitlines() if ln.strip()][:8]
                summaries.append(f"exiftool {att.name}: {' | '.join(ex_lines)[:300]}")
            bw = _run_optional_probe(runner, challenge, "binwalk", [staged_name], timeout_s=20)
            if bw:
                bw_lines = [ln.strip() for ln in bw.splitlines() if ln.strip()][:6]
                summaries.append(f"binwalk {att.name}: {' | '.join(bw_lines)[:260]}")
        elif "PCAP" in ftype:
            ts = _run_optional_probe(runner, challenge, "tshark", ["-r", staged_name, "-q", "-z", "io,phs"], timeout_s=25)
            if ts:
                ts_lines = [ln.strip() for ln in ts.splitlines() if ln.strip()][:10]
                summaries.append(f"tshark {att.name}: {' | '.join(ts_lines)[:300]}")

    target = challenge.target
    if target:
        host_port = target.split("://")[-1].split("/")[0]
        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
        else:
            host, port_str = host_port, ""
        explicit_port = port_str.strip() if port_str.strip().isdigit() else None
        lower_t = target.lower()
        is_web_hint = (
            lower_t.startswith(("http://", "https://"))
            or any(f":{p}" in lower_t for p in ("80", "443", "5000", "8000", "8080", "8443", "3000"))
        )

        if explicit_port and not lower_t.startswith(("http://", "https://")):
            banner = probe_tcp_banner(host, int(explicit_port))
            if banner:
                banner_preview = banner.replace("\r\n", "\n")[:600]
                summaries.append(f"tcp-banner {host}:{explicit_port}: {banner_preview}")
                digests.append({"name": "tcp_banner", "banner": banner_preview})

        nmap_args = (
            ["-Pn", "-sV", "--version-intensity", "2", "--host-timeout", "20s", "-p", explicit_port, host]
            if explicit_port
            else ["--top-ports", "100", host]
        )
        nmap_timeout = 30 if explicit_port else 120
        try:
            res = runner.run_raw(challenge, "nmap",
                                 nmap_args,
                                 timeout_s=nmap_timeout, output_limit_mb=1)
            open_lines = [ln for ln in res.stdout.splitlines() if "/tcp" in ln][:20]
            summaries.append(f"nmap {target}: " + ("; ".join(open_lines) if open_lines
                            else (res.stdout or "")[:200]))
            digests.append({"name": "nmap", "ports": open_lines})
            if explicit_port and open_lines and all(
                f"{explicit_port}/tcp" in ln and ("closed" in ln or "filtered" in ln)
                for ln in open_lines
            ):
                summaries.append(
                    f"target-port-status: {host}:{explicit_port} is closed/filtered — "
                    "instance may be stopped or moved to a new port"
                )
        except Exception as exc:
            summaries.append(f"nmap {target}: skipped ({exc})")

        if is_web_hint:
            probe_url = target if lower_t.startswith(("http://", "https://")) else f"http://{target}"
            hdr = _run_optional_probe(runner, challenge, "curl", ["-sI", "--max-time", "8", probe_url], timeout_s=12)
            if hdr:
                hdr_lines = [ln.strip() for ln in hdr.splitlines() if ln.strip()][:8]
                summaries.append(f"http-headers {probe_url}: {' | '.join(hdr_lines)[:300]}")
    else:
        summaries.append("no target set — nmap skipped")

    result_kind = (
        AttemptResult.breakthrough
        if any(d.get("strings") or d.get("ports") or d.get("banner") for d in digests)
        else AttemptResult.partial
    )
    tracking.record_attempt(
        session, challenge.id, AttemptType.agent, "recon bundle (file/strings/nmap)",
        result_kind,
        tool_command="recon_bundle",
        learning="\n".join(summaries)[:2000])

    return ReconDigest(
        target=target,
        files=digests,
        summary="\n".join(summaries),
        discovered_urls=discovered_urls,
    )
