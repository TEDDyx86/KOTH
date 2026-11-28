from dataclasses import dataclass, field
import base64
import html
import json
from pathlib import Path
import re
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

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
    structured: dict = field(default_factory=dict)
    hypotheses: list[str] = field(default_factory=list)


_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
_ENDPOINT_RE = re.compile(
    r"""["'](/(?:api|v\d+|admin|auth|login|register|graphql|flag|debug|internal|secret|token|user)[a-zA-Z0-9_./?=&%-]*)["']"""
)
_FORM_ACTION_RE = re.compile(r"""<form[^>]*action=["']([^"']*)["']""", re.I)
_HIDDEN_INPUT_RE = re.compile(r"""<input[^>]*type=["']hidden["'][^>]*>""", re.I)
_INPUT_NAME_RE = re.compile(r"""name=["']([^"']+)["']""", re.I)
_PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+?)-----")
_RSA_PARAM_RE = re.compile(r"(?:^|[\s,;({])([nec]|p|q|dp|dq|phi)\s*[:=]\s*(0x[0-9a-fA-F]+|\d{1,})", re.M)
_B64_CAND_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{12,512}(?![A-Za-z0-9+/=_-])")
_HEX_CAND_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}){8,256}\b")
_B32_CAND_RE = re.compile(r"(?<![A-Z2-7=])[A-Z2-7]{16,256}={0,6}(?![A-Z2-7=])")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _decode_jwt_unverified(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        hdr = json.loads(_b64url_decode(parts[0]).decode("utf-8", errors="strict"))
        pay = json.loads(_b64url_decode(parts[1]).decode("utf-8", errors="strict"))
        if isinstance(hdr, dict) and isinstance(pay, dict):
            return {"token": token[:48] + ("..." if len(token) > 48 else ""), "header": hdr, "payload": pay}
    except Exception:
        return None
    return None


def _is_printable_text(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        txt = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for ch in txt if 32 <= ord(ch) <= 126 or ch in "\r\n\t")
    if printable / max(len(txt), 1) >= 0.90 and len(txt.strip()) >= 4:
        return txt.strip()
    return None


def _http_fetch_default(url: str) -> tuple[dict[str, str], str]:
    req = urllib.request.Request(url, headers={"User-Agent": "KOTH-Recon3/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read(262144).decode("utf-8", errors="replace")
            return hdrs, body
    except urllib.error.HTTPError as err:
        hdrs = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        try:
            body = err.read(65536).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return hdrs, body
    except Exception:
        return {}, ""


def extract_web_intel(url: str, *, fetcher=None) -> dict:
    """Deterministic HTTP + HTML/JS/JWT/robots/sitemap recon with 0 LLM tokens."""
    norm_url = url if url.lower().startswith(("http://", "https://")) else f"http://{url}"
    parsed = urllib.parse.urlparse(norm_url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else norm_url.rstrip("/")
    fetch = fetcher or _http_fetch_default

    headers, body = fetch(norm_url)
    robots_hdrs, robots_txt = fetch(f"{origin}/robots.txt")
    _sm_hdrs, sitemap_txt = fetch(f"{origin}/sitemap.xml")

    stack: list[str] = []
    server_hdr = headers.get("server", "")
    powered = headers.get("x-powered-by", "")
    blob_low = f"{server_hdr} {powered} {body[:16000]}".lower()
    for sig, label in (
        ("werkzeug", "Flask/Werkzeug"),
        ("next_data", "Next.js"),
        ("__next_data__", "Next.js"),
        ("csrfmiddlewaretoken", "Django"),
        ("express", "Express.js"),
        ("phpsessid", "PHP"),
        ("x-powered-by: php", "PHP"),
        ("nginx", "nginx"),
        ("apache", "Apache"),
    ):
        if sig in blob_low and label not in stack:
            stack.append(label)

    comments = [
        " ".join(html.unescape(c).split())[:180]
        for c in _HTML_COMMENT_RE.findall(body)
        if c.strip()
    ][:8]
    scripts = list(dict.fromkeys(_SCRIPT_SRC_RE.findall(body)))[:12]
    endpoints = list(dict.fromkeys(_ENDPOINT_RE.findall(body)))[:15]
    forms = [a or "(self)" for a in _FORM_ACTION_RE.findall(body)][:10]
    hidden_inputs = []
    for tag in _HIDDEN_INPUT_RE.findall(body)[:12]:
        m = _INPUT_NAME_RE.search(tag)
        if m and m.group(1) not in hidden_inputs:
            hidden_inputs.append(m.group(1))

    cookie_hdr = headers.get("set-cookie", "")
    cookies = [c.split("=", 1)[0].strip() for c in cookie_hdr.split(",") if "=" in c][:8]

    jwts: list[dict] = []
    seen_jwt: set[str] = set()
    for tok in _JWT_RE.findall(f"{cookie_hdr}\n{body}\n{json.dumps(headers)}"):
        if tok in seen_jwt:
            continue
        seen_jwt.add(tok)
        decoded = _decode_jwt_unverified(tok)
        if decoded:
            jwts.append(decoded)
        if len(jwts) >= 5:
            break

    robots_paths: list[str] = []
    if robots_txt and ("disallow:" in robots_txt.lower() or "allow:" in robots_txt.lower()):
        for ln in robots_txt.splitlines():
            m = re.match(r"^\s*(?:Dis)?[Aa]llow:\s*(\S+)", ln)
            if m and m.group(1) not in robots_paths:
                robots_paths.append(m.group(1))
            if len(robots_paths) >= 12:
                break

    sitemap_urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap_txt or "", re.I)[:10]

    hypotheses: list[str] = []
    if endpoints:
        hypotheses.append(f"Testar endpoints JS/HTML descobertos: {', '.join(endpoints[:5])}")
    if robots_paths:
        hypotheses.append(f"Verificar rotas de robots.txt: {', '.join(robots_paths[:5])}")
    if jwts:
        j0 = jwts[0]
        alg = j0["header"].get("alg", "?")
        hypotheses.append(f"Analisar JWT decodificado (alg={alg}, payload={json.dumps(j0['payload'])[:120]})")
    if comments:
        hypotheses.append(f"Revisar comentário HTML: {comments[0][:100]}")
    if hidden_inputs:
        hypotheses.append(f"Verificar campos hidden de formulário: {', '.join(hidden_inputs[:5])}")

    summary_parts: list[str] = []
    if stack:
        summary_parts.append(f"web-stack: {', '.join(stack)}")
    if endpoints:
        summary_parts.append(f"web-endpoints: {', '.join(endpoints[:8])}")
    if robots_paths:
        summary_parts.append(f"robots-paths: {', '.join(robots_paths[:6])}")
    if comments:
        summary_parts.append(f"html-comments: {' | '.join(comments[:3])[:220]}")
    if jwts:
        summary_parts.append(f"web-jwt: alg={jwts[0]['header'].get('alg')} payload={json.dumps(jwts[0]['payload'])[:180]}")

    return {
        "url": norm_url,
        "stack": stack,
        "cookies": cookies,
        "jwts": jwts,
        "comments": comments,
        "scripts": scripts,
        "endpoints": endpoints,
        "forms": forms,
        "hidden_inputs": hidden_inputs,
        "robots_paths": robots_paths,
        "sitemap_urls": sitemap_urls,
        "hypotheses": hypotheses,
        "summary_lines": summary_parts,
    }


def _caesar_shift(text: str, shift: int) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - 97 + shift) % 26 + 97))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - 65 + shift) % 26 + 65))
        else:
            out.append(ch)
    return "".join(out)


def _decode_blob_layers(token: str) -> tuple[str, str] | None:
    cur = token
    chain: list[str] = []
    for _ in range(2):
        nxt: str | None = None
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}){6,256}", cur):
            try:
                txt = _is_printable_text(bytes.fromhex(cur))
                if txt:
                    nxt = txt
                    chain.append("hex")
            except Exception:
                pass
        if nxt is None and re.fullmatch(r"[A-Z2-7]{16,256}={0,6}", cur):
            try:
                txt = _is_printable_text(base64.b32decode(cur, casefold=False))
                if txt:
                    nxt = txt
                    chain.append("b32")
            except Exception:
                pass
        if nxt is None and len(cur) >= 12 and len(cur) % 4 == 0:
            try:
                raw = base64.urlsafe_b64decode(cur) if ("-" in cur or "_" in cur) else base64.b64decode(cur, validate=True)
                txt = _is_printable_text(raw)
                if txt and txt != cur:
                    nxt = txt
                    chain.append("b64")
            except Exception:
                pass
        if nxt is None:
            break
        cur = nxt
    if not chain:
        return None
    return "->".join(chain), cur


def extract_crypto_intel(text: str) -> dict:
    """Deterministic scanner for RSA params, PEMs, JWTs, hashes, encoded layers, and simple ciphers."""
    src = (text or "")[:120_000]
    rsa_params: dict[str, str] = {}
    for k, v in _RSA_PARAM_RE.findall(src):
        if k not in rsa_params:
            rsa_params[k] = v[:80] + ("..." if len(v) > 80 else "")

    pems = list(dict.fromkeys(_PEM_RE.findall(src)))[:6]
    jwts: list[dict] = []
    for tok in _JWT_RE.findall(src)[:5]:
        dec = _decode_jwt_unverified(tok)
        if dec:
            jwts.append(dec)

    hashes: list[dict] = []
    for kind, rx in (
        ("bcrypt", r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
        ("sha256", r"\b[0-9a-fA-F]{64}\b"),
        ("sha1", r"\b[0-9a-fA-F]{40}\b"),
        ("md5", r"\b[0-9a-fA-F]{32}\b"),
    ):
        for h in re.findall(rx, src)[:4]:
            if not any(x["value"] == h for x in hashes):
                hashes.append({"type": kind, "value": h})

    decoded_blobs: list[dict] = []
    seen_tok: set[str] = set()
    for rx in (_HEX_CAND_RE, _B32_CAND_RE, _B64_CAND_RE):
        for tok in rx.findall(src):
            if tok in seen_tok:
                continue
            seen_tok.add(tok)
            res = _decode_blob_layers(tok)
            if res:
                decoded_blobs.append({"encoding": res[0], "decoded": res[1][:160]})
            if len(decoded_blobs) >= 6:
                break

    cipher_hints: list[str] = []
    for ln in src.splitlines()[:40]:
        s = ln.strip()
        if 8 <= len(s) <= 120:
            for sh in range(1, 26):
                cand = _caesar_shift(s, sh)
                low = cand.lower()
                if "flag{" in low or "ctf{" in low or "koth{" in low:
                    cipher_hints.append(f"caesar(+{sh}): {cand[:120]}")
                    break
        if len(cipher_hints) >= 3:
            break
    for hx in _HEX_CAND_RE.findall(src)[:5]:
        if len(hx) <= 200:
            raw = bytes.fromhex(hx)
            for key in range(1, 256):
                xored = bytes(b ^ key for b in raw)
                txt = _is_printable_text(xored)
                if txt and any(m in txt.lower() for m in ("flag{", "ctf{", "koth{")):
                    cipher_hints.append(f"xor(0x{key:02x}): {txt[:120]}")
                    break

    hypotheses: list[str] = []
    if "n" in rsa_params and "c" in rsa_params:
        e_val = rsa_params.get("e", "")
        if e_val in ("3", "0x3"):
            hypotheses.append("RSA com e=3 detectado — testar raiz cúbica inteira / Håstad")
        else:
            hypotheses.append(f"Parâmetros RSA detectados ({', '.join(rsa_params.keys())}) — checar fatoração ou Wiener/Fermat")
    if decoded_blobs:
        hypotheses.append(f"Blob codificado decodificado ({decoded_blobs[0]['encoding']}): {decoded_blobs[0]['decoded'][:100]}")
    if cipher_hints:
        hypotheses.append(f"Cifra simples detectada: {cipher_hints[0]}")
    if jwts:
        hypotheses.append(f"JWT decodificado: payload={json.dumps(jwts[0]['payload'])[:120]}")

    summary_lines: list[str] = []
    if rsa_params:
        summary_lines.append("crypto-rsa: " + ", ".join(f"{k}={v[:28]}" for k, v in rsa_params.items()))
    if pems:
        summary_lines.append("crypto-pem: " + ", ".join(pems))
    if hashes:
        summary_lines.append("crypto-hashes: " + ", ".join(f"{h['type']}:{h['value'][:16]}..." for h in hashes[:5]))
    if decoded_blobs:
        summary_lines.append("crypto-decoded: " + " | ".join(f"{d['encoding']}=>{d['decoded'][:60]}" for d in decoded_blobs[:3]))
    if cipher_hints:
        summary_lines.append("crypto-ciphers: " + " | ".join(cipher_hints))

    return {
        "rsa_params": rsa_params,
        "pems": pems,
        "jwts": jwts,
        "hashes": hashes,
        "decoded_blobs": decoded_blobs,
        "cipher_hints": cipher_hints,
        "hypotheses": hypotheses,
        "summary_lines": summary_lines,
    }


def extract_binary_intel(path: Path, strings_lines: list[str]) -> dict:
    """Pure-Python ELF/PE header parser + clustering of risky/crypto strings."""
    info: dict = {"format": None, "arch": None, "bits": None, "pie": None}
    try:
        hdr = path.read_bytes()[:4096]
    except OSError:
        hdr = b""

    if hdr.startswith(b"\x7fELF") and len(hdr) >= 20:
        info["format"] = "ELF"
        ei_class = hdr[4]
        ei_data = hdr[5]
        endian = "<" if ei_data == 1 else ">"
        info["bits"] = 64 if ei_class == 2 else 32 if ei_class == 1 else None
        e_type, e_mach = struct.unpack(f"{endian}HH", hdr[16:20])
        info["pie"] = e_type == 3
        mach_map = {0x3E: "x86_64", 0x03: "x86", 0xB7: "ARM64", 0x28: "ARM", 0xF3: "RISC-V"}
        info["arch"] = mach_map.get(e_mach, f"0x{e_mach:x}")
    elif hdr.startswith(b"MZ") and len(hdr) >= 0x40:
        info["format"] = "PE"
        pe_off = struct.unpack("<I", hdr[0x3C:0x40])[0]
        if pe_off + 6 <= len(hdr) and hdr[pe_off:pe_off + 4] == b"PE\x00\x00":
            mach = struct.unpack("<H", hdr[pe_off + 4:pe_off + 6])[0]
            info["arch"] = {0x8664: "x64", 0x014C: "x86", 0xAA64: "ARM64"}.get(mach, f"0x{mach:x}")
            info["bits"] = 64 if mach in (0x8664, 0xAA64) else 32

    dangerous_re = re.compile(r"\b(gets|strcpy|strcat|sprintf|scanf|system|execve|popen|printf)\b")
    crypto_re = re.compile(r"\b(aes|rsa|sha\d*|md5|xor|rc4|chacha)\b", re.I)
    fmt_re = re.compile(r"%\d*\$?[spxn]|/flag\b|/bin/sh\b")

    dangerous: list[str] = []
    crypto_clues: list[str] = []
    paths_or_fmt: list[str] = []
    for ln in strings_lines[:500]:
        for m in dangerous_re.findall(ln):
            if m not in dangerous:
                dangerous.append(m)
        if crypto_re.search(ln) and len(crypto_clues) < 8:
            crypto_clues.append(ln[:100])
        if fmt_re.search(ln) and len(paths_or_fmt) < 8:
            paths_or_fmt.append(ln[:100])

    hypotheses: list[str] = []
    if info["format"]:
        hypotheses.append(
            f"Binário {info['format']} {info['arch'] or ''} ({info['bits'] or '?'}b"
            + (f", PIE={info['pie']}" if info["pie"] is not None else "") + ")"
        )
    if dangerous:
        hypotheses.append(f"Símbolos perigosos detectados ({', '.join(dangerous[:6])}) — checar overflow/command injection")
    if paths_or_fmt:
        hypotheses.append(f"Strings de interesse binário: {', '.join(paths_or_fmt[:4])}")

    summary_lines: list[str] = []
    if info["format"]:
        summary_lines.append(
            f"bin-header {path.name}: {info['format']} {info['arch']} {info['bits']}b"
            + (f" PIE={info['pie']}" if info["pie"] is not None else "")
        )
    if dangerous:
        summary_lines.append(f"bin-dangerous: {', '.join(dangerous[:8])}")
    if crypto_clues:
        summary_lines.append(f"bin-crypto-clues: {' | '.join(crypto_clues[:4])[:200]}")

    return {
        **info,
        "dangerous_apis": dangerous,
        "crypto_clues": crypto_clues,
        "paths_or_fmt": paths_or_fmt,
        "hypotheses": hypotheses,
        "summary_lines": summary_lines,
    }


def extract_forensics_intel(path: Path, ftype: str) -> dict:
    """Pure-Python trailing-EOF detector (PNG/JPEG/PDF) and PCAP stream/HTTP scanner."""
    trailing_bytes = 0
    trailing_preview = ""
    png_text: list[str] = []
    pcap_packets = 0
    pcap_artifacts: list[str] = []

    try:
        data = path.read_bytes()[:4 * 1024 * 1024]
    except OSError:
        data = b""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos = 8
        while pos + 8 <= len(data):
            length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
            chunk_end = pos + 12 + length
            if chunk_end > len(data):
                break
            cdata = data[pos + 8:pos + 8 + length]
            if ctype == b"tEXt" and len(png_text) < 8:
                png_text.append(cdata.replace(b"\x00", b"=").decode("latin1", errors="replace")[:120])
            elif ctype == b"zTXt" and b"\x00\x00" in cdata and len(png_text) < 8:
                key, rest = cdata.split(b"\x00\x00", 1)
                try:
                    val = zlib.decompress(rest[:4096]).decode("utf-8", errors="replace")
                except Exception:
                    val = "(compressed)"
                png_text.append(f"{key.decode('latin1', errors='replace')}={val[:100]}")
            pos = chunk_end
            if ctype == b"IEND":
                trailing_bytes = max(0, len(data) - pos)
                if trailing_bytes > 0:
                    trailing_preview = data[pos:pos + 160].decode("latin1", errors="replace")
                break
    elif data.startswith(b"\xff\xd8\xff"):
        idx = data.rfind(b"\xff\xd9")
        if idx != -1 and idx + 2 < len(data):
            trailing_bytes = len(data) - (idx + 2)
            trailing_preview = data[idx + 2:idx + 162].decode("latin1", errors="replace")
    elif data.startswith(b"%PDF-"):
        idx = data.rfind(b"%%EOF")
        if idx != -1:
            after = data[idx + 5:].lstrip(b"\r\n")
            if after:
                trailing_bytes = len(after)
                trailing_preview = after[:160].decode("latin1", errors="replace")
    elif data[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4") and len(data) >= 24:
        endian = "<" if data[:4] == b"\xd4\xc3\xb2\xa1" else ">"
        pos = 24
        seen_art: set[str] = set()
        http_re = re.compile(rb"(?:GET|POST|PUT|HEAD) [^\r\n]{1,120}|(?:Host|Authorization|Cookie): [^\r\n]{1,120}|[A-Za-z0-9_-]{2,16}\{[^}\r\n]{1,80}\}")
        while pos + 16 <= len(data) and pcap_packets < 512:
            _ts_s, _ts_u, incl_len, _orig_len = struct.unpack(f"{endian}IIII", data[pos:pos + 16])
            pos += 16
            if incl_len > 65535 or pos + incl_len > len(data):
                break
            pkt = data[pos:pos + incl_len]
            pos += incl_len
            pcap_packets += 1
            for m in http_re.findall(pkt):
                s = m.decode("utf-8", errors="replace").strip()
                if s not in seen_art:
                    seen_art.add(s)
                    pcap_artifacts.append(s)
                if len(pcap_artifacts) >= 12:
                    break

    hypotheses: list[str] = []
    if trailing_bytes > 0:
        hypotheses.append(f"Dados ocultos pós-EOF em {path.name} ({trailing_bytes} bytes extras — checar polyglot/ZIP)")
    if png_text:
        hypotheses.append(f"Metadados PNG encontrados: {png_text[0][:100]}")
    if pcap_artifacts:
        hypotheses.append(f"PCAP com {pcap_packets} pacotes — artefatos HTTP/flag: {pcap_artifacts[0][:100]}")

    summary_lines: list[str] = []
    if trailing_bytes > 0:
        summary_lines.append(f"forensics-trailing {path.name}: {trailing_bytes}B after EOF ({trailing_preview[:80]!r})")
    if png_text:
        summary_lines.append(f"png-text {path.name}: {' | '.join(png_text[:4])[:220]}")
    if pcap_packets > 0:
        art = " | ".join(pcap_artifacts[:6])[:240] if pcap_artifacts else "sem strings HTTP óbvias"
        summary_lines.append(f"pcap-scan {path.name}: {pcap_packets} pkts · {art}")

    return {
        "trailing_bytes": trailing_bytes,
        "trailing_preview": trailing_preview,
        "png_text": png_text,
        "pcap_packets": pcap_packets,
        "pcap_artifacts": pcap_artifacts,
        "hypotheses": hypotheses,
        "summary_lines": summary_lines,
    }



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
                 storage: StorageService | None = None,
                 *, web_fetcher=None) -> ReconDigest:
    """Adaptive deterministic recon 3.0: file type + strings + ZIP/IPA + URL discovery +
    TCP banner grab + pure-Python Web/Crypto/Binary/Forensics extractors + CLI probes."""
    digests: list[dict] = []
    summaries: list[str] = []
    discovered_urls: list[str] = []
    hypotheses: list[str] = []
    structured: dict = {"files": [], "crypto": [], "binary": [], "forensics": [], "web": None}

    def _add_hyps(items: list[str]) -> None:
        for h in items:
            if h and h not in hypotheses:
                hypotheses.append(h)

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

        all_lines: list[str] = []
        if runner is not None:
            try:
                res = runner.run_raw(challenge, "strings",
                                     [staged_name], timeout_s=30, output_limit_mb=1)
                out = (res.stdout or "").strip()
                all_lines = [ln for ln in out.splitlines() if ln.strip()]
            except Exception as exc:  # tool missing / guard — recon must not die
                summaries.append(f"strings {att.name}: skipped ({exc})")

        interesting = all_lines[:40]
        entry["strings"] = interesting[:40]
        if interesting:
            summaries.append(f"strings {att.name}: " + " | ".join(interesting[:8]))

        urls = _extract_discovered_urls(all_lines)
        for u in urls:
            if u not in discovered_urls:
                discovered_urls.append(u)
        if urls:
            summaries.append(f"discovered-urls {att.name}: " + ", ".join(urls[:5]))

        # Recon 3.0 deterministic pure-Python extractors
        if "text" in ftype.lower() or Path(att.name).suffix.lower() in (".txt", ".py", ".json", ".md", ".log", ".pem"):
            try:
                txt_blob = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                txt_blob = "\n".join(all_lines)
            crypto_info = extract_crypto_intel(txt_blob)
            if any(crypto_info[k] for k in ("rsa_params", "pems", "jwts", "hashes", "decoded_blobs", "cipher_hints")):
                structured["crypto"].append({"file": att.name, **crypto_info})
                summaries.extend(crypto_info["summary_lines"])
                _add_hyps(crypto_info["hypotheses"])

        if "ELF" in ftype or "PE/" in ftype or "Mach-O" in ftype:
            bin_info = extract_binary_intel(path, all_lines)
            structured["binary"].append({"file": att.name, **bin_info})
            summaries.extend(bin_info["summary_lines"])
            _add_hyps(bin_info["hypotheses"])
            if runner is not None:
                f_out = _run_optional_probe(runner, challenge, "file", [staged_name], timeout_s=15)
                if f_out:
                    summaries.append(f"file-detail {att.name}: {f_out.splitlines()[0][:180]}")
                if "ELF" in ftype:
                    chk = _run_optional_probe(runner, challenge, "checksec", [f"--file={staged_name}"], timeout_s=20)
                    if chk:
                        summaries.append(f"checksec {att.name}: {' | '.join(chk.splitlines()[:6])[:300]}")
        elif "image" in ftype or "PDF" in ftype or "PCAP" in ftype:
            for_info = extract_forensics_intel(path, ftype)
            if for_info["summary_lines"]:
                structured["forensics"].append({"file": att.name, **for_info})
                summaries.extend(for_info["summary_lines"])
                _add_hyps(for_info["hypotheses"])
            if runner is not None and "image" in ftype:
                ex = _run_optional_probe(runner, challenge, "exiftool", [staged_name], timeout_s=20)
                if ex:
                    ex_lines = [ln.strip() for ln in ex.splitlines() if ln.strip()][:8]
                    summaries.append(f"exiftool {att.name}: {' | '.join(ex_lines)[:300]}")
                bw = _run_optional_probe(runner, challenge, "binwalk", [staged_name], timeout_s=20)
                if bw:
                    bw_lines = [ln.strip() for ln in bw.splitlines() if ln.strip()][:6]
                    summaries.append(f"binwalk {att.name}: {' | '.join(bw_lines)[:260]}")
            elif runner is not None and "PCAP" in ftype:
                ts = _run_optional_probe(runner, challenge, "tshark", ["-r", staged_name, "-q", "-z", "io,phs"], timeout_s=25)
                if ts:
                    ts_lines = [ln.strip() for ln in ts.splitlines() if ln.strip()][:10]
                    summaries.append(f"tshark {att.name}: {' | '.join(ts_lines)[:300]}")

        digests.append(entry)
        structured["files"].append(entry)

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
                c_banner = extract_crypto_intel(banner)
                if c_banner["summary_lines"]:
                    structured["crypto"].append({"file": "tcp_banner", **c_banner})
                    summaries.extend(c_banner["summary_lines"])
                    _add_hyps(c_banner["hypotheses"])

        nmap_args = (
            ["-Pn", "-sV", "--version-intensity", "2", "--host-timeout", "20s", "-p", explicit_port, host]
            if explicit_port
            else ["--top-ports", "100", host]
        )
        nmap_timeout = 30 if explicit_port else 120
        if runner is not None:
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
            web_info = extract_web_intel(probe_url, fetcher=web_fetcher)
            structured["web"] = web_info
            summaries.extend(web_info["summary_lines"])
            _add_hyps(web_info["hypotheses"])
            if runner is not None:
                hdr = _run_optional_probe(runner, challenge, "curl", ["-sI", "--max-time", "8", probe_url], timeout_s=12)
                if hdr:
                    hdr_lines = [ln.strip() for ln in hdr.splitlines() if ln.strip()][:8]
                    summaries.append(f"http-headers {probe_url}: {' | '.join(hdr_lines)[:300]}")
    else:
        summaries.append("no target set — nmap skipped")

    result_kind = (
        AttemptResult.breakthrough
        if any(d.get("strings") or d.get("ports") or d.get("banner") for d in digests)
        or bool(hypotheses)
        else AttemptResult.partial
    )
    tracking.record_attempt(
        session, challenge.id, AttemptType.agent, "recon bundle 3.0",
        result_kind,
        tool_command="recon_bundle",
        learning="\n".join(summaries)[:2000])

    return ReconDigest(
        target=target,
        files=digests,
        summary="\n".join(summaries),
        discovered_urls=discovered_urls,
        structured=structured,
        hypotheses=hypotheses,
    )
