"""Authenticating HTTP forward-proxy shim for the CrowdVolt Chrome.

Chrome's --proxy-server flag cannot carry credentials: user:pass in the
URL is silently dropped and Chrome answers the upstream's 407 with an
interactive auth dialog — useless in a systemd-launched browser. This
shim listens on localhost with no auth and relays every request to the
paid residential upstream with Proxy-Authorization injected:

    Chrome --proxy-server=http://127.0.0.1:3128
        -> cvproxy.py -- CONNECT + Proxy-Authorization --> residential upstream

Config (environment, or /opt/kartis/.env.cvproxy loaded by the systemd
unit — the CLI falls back to reading that file too):

    KARTIS_CVPROXY_UPSTREAM   http://user:pass@gate.provider.com:8080
                              (percent-encode ':' or '@' inside user/pass)
    KARTIS_CVPROXY_LISTEN     127.0.0.1:3128 (default)

Only HTTP upstreams are spoken — every static-residential provider sells
an HTTP(S)-proxy port; if you were handed socks5://, use their HTTP port
instead. HTTPS sites (all of CrowdVolt) tunnel via CONNECT; plain-http
requests are relayed with the auth header added but keep-alive reuse is
not re-authenticated — irrelevant for the CV traffic this exists for.

Probe the upstream and print the egress IP without touching Chrome:

    .venv/bin/python cvproxy.py --test
"""
import base64
import os
import socket
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

ENV_FILE = Path(__file__).resolve().parent / ".env.cvproxy"
BUFSIZE = 65536
HEAD_LIMIT = 65536


def _load_env_file():
    """systemd passes the env in; for CLI runs pull it from .env.cvproxy."""
    if os.environ.get("KARTIS_CVPROXY_UPSTREAM") or not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _upstream():
    raw = os.environ.get("KARTIS_CVPROXY_UPSTREAM", "").strip()
    if not raw:
        raise SystemExit(
            "KARTIS_CVPROXY_UPSTREAM is not set — put the provider proxy in "
            f"{ENV_FILE} as KARTIS_CVPROXY_UPSTREAM=http://user:pass@host:port")
    u = urlsplit(raw if "//" in raw else "http://" + raw)
    if u.scheme not in ("", "http"):
        raise SystemExit(
            f"upstream scheme {u.scheme!r} unsupported — use the provider's "
            "HTTP proxy endpoint (not socks)")
    if not u.hostname or not u.port:
        raise SystemExit(f"can't parse host:port out of KARTIS_CVPROXY_UPSTREAM={raw!r}")
    auth = None
    if u.username:
        cred = f"{unquote(u.username)}:{unquote(u.password or '')}"
        auth = base64.b64encode(cred.encode()).decode()
    return u.hostname, u.port, auth


def _read_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(BUFSIZE)
        if not chunk:
            return None
        data += chunk
        if len(data) > HEAD_LIMIT:
            return None
    return data


def _with_auth(head, auth):
    """Insert Proxy-Authorization after the request line. `head` may carry
    body bytes past the blank line — they pass through untouched."""
    if not auth:
        return head
    line, rest = head.split(b"\r\n", 1)
    return line + b"\r\nProxy-Authorization: Basic " + auth.encode() + b"\r\n" + rest


def _pump(src, dst):
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client, upstream_addr, auth):
    up = None
    try:
        head = _read_head(client)
        if not head:
            return
        up = socket.create_connection(upstream_addr, timeout=30)
        up.sendall(_with_auth(head, auth))
        t = threading.Thread(target=_pump, args=(client, up), daemon=True)
        t.start()
        _pump(up, client)
        t.join()
    except OSError as e:
        req = b""
        try:
            req = head.split(b"\r\n", 1)[0][:120]
        except Exception:
            pass
        print(f"[cvproxy] relay error {type(e).__name__}: {e} ({req!r})", flush=True)
    finally:
        for s in (client, up):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def serve():
    host, port, auth = _upstream()
    listen = os.environ.get("KARTIS_CVPROXY_LISTEN", "127.0.0.1:3128")
    lhost, _, lport = listen.rpartition(":")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lhost or "127.0.0.1", int(lport)))
    srv.listen(64)
    print(f"[cvproxy] listening on {listen}, upstream {host}:{port} "
          f"(auth {'on' if auth else 'off'})", flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle, args=(client, (host, port), auth),
                         daemon=True).start()


def test():
    """CONNECT to api.ipify.org through the upstream and print the egress
    IP — proves the purchased proxy + credentials before Chrome touches it."""
    import ssl
    host, port, auth = _upstream()
    print(f"upstream: {host}:{port} (auth {'on' if auth else 'off'})")
    up = socket.create_connection((host, port), timeout=30)
    req = "CONNECT api.ipify.org:443 HTTP/1.1\r\nHost: api.ipify.org:443\r\n"
    if auth:
        req += f"Proxy-Authorization: Basic {auth}\r\n"
    up.sendall((req + "\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = up.recv(4096)
        if not chunk:
            break
        resp += chunk
    status = resp.split(b"\r\n", 1)[0].decode(errors="replace")
    print("upstream says:", status)
    if " 200" not in status:
        print(resp.decode(errors="replace"))
        return 1
    s = ssl.create_default_context().wrap_socket(up, server_hostname="api.ipify.org")
    s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
    out = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        out += chunk
    print("egress IP:", out.split(b"\r\n\r\n", 1)[-1].decode(errors="replace").strip())
    return 0


if __name__ == "__main__":
    _load_env_file()
    sys.exit(test() if "--test" in sys.argv else serve())
