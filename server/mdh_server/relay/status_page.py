"""Public status page of a server, served by the relay itself (docs/ARCHITECTURE.md 4.9).

Friends open ``http://<the server's Minecraft address>:25565/`` in a browser: no application,
no certificate warning (plain HTTP on the port already open for Minecraft, nothing secret on
the page), no change to Nginx or the firewall. Off unless a group admin turns it on, since it
shows who is playing. Personal addresses (secrets) are never shown.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from urllib.parse import unquote

MAX_REQUEST_BYTES = 8192
#: World map (docs/ARCHITECTURE.md 4.11): a browser connection with no new request for this
#: long is closed, and one connection carries at most this much.
MAP_IDLE_S = 30.0
#: After the browser's last request, how long its answer may still take.
MAP_LAST_ANSWER_S = 5.0
MAP_MAX_BYTES = 256 * 1024**2
#: Request headers passed on to BlueMap (caching and compression); nothing that identifies the
#: visitor, no cookie.
MAP_HEADERS = ("accept", "accept-encoding", "accept-language", "if-none-match", "if-modified-since",
               "cache-control")
_MAP_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,2047}$")
HEADERS = (
    "Content-Type: text/html; charset=utf-8\r\n"
    "Cache-Control: no-store\r\n"
    # No script, no external resource: the page cannot be used for anything else.
    "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'\r\n"
    "X-Content-Type-Options: nosniff\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "Connection: close\r\n"
)


@dataclass(frozen=True)
class PageInfo:
    name: str
    online: bool
    state_text: str
    players: list[str]
    players_count: int | None
    address: str | None
    version: str
    needs_personal_address: bool
    has_map: bool = False


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    hostname: str
    headers: list[tuple[str, str]]


async def read_request(reader: asyncio.StreamReader, start: bytes | None, timeout: float) -> HttpRequest | None:
    """The head of one HTTP request (``start``: its first bytes, already read). None when the
    connection ends, takes too long, or sends something that is not a small, plain request."""
    try:
        head = (start or b"") + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError, ConnectionError):
        return None
    if len(head) > MAX_REQUEST_BYTES:
        return None
    request_line, *lines = head.decode("latin-1").split("\r\n")
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    headers = [(name.strip(), value.strip()) for name, sep, value in (h.partition(":") for h in lines) if sep]
    host = next((v for n, v in headers if n.lower() == "host"), "")
    hostname = host.rsplit(":", 1)[0].strip("[]").lower() if not host.endswith("]") else host.strip("[]")
    return HttpRequest(parts[0], parts[1], hostname, headers)


def map_request(request: HttpRequest) -> bytes | None:
    """The request as BlueMap gets it: ``/map/x`` becomes ``/x``. None if it is not a plain
    GET or HEAD of a file of the map."""
    if request.method not in ("GET", "HEAD") or not request.path.startswith("/map/"):
        return None
    path = request.path[len("/map"):]
    plain = unquote(path.split("?", 1)[0])
    if not _MAP_PATH.fullmatch(path) or "\\" in plain or "/../" in plain + "/" or "\x00" in plain:
        return None
    lines = [f"{request.method} {path} HTTP/1.1", "Host: localhost"]
    lines += [f"{name}: {value}" for name, value in request.headers
              if name.lower() in MAP_HEADERS and value.isprintable()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def redirect(location: str) -> bytes:
    return (f"HTTP/1.1 301 Moved Permanently\r\nLocation: {location}\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n").encode("ascii")


def response(status: str, body: bytes, *, head_only: bool = False) -> bytes:
    head = f"HTTP/1.1 {status}\r\n{HEADERS}Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return head if head_only else head + body


def render(info: PageInfo) -> bytes:
    e = html.escape
    count = len(info.players) if info.players else info.players_count
    if not info.online:
        players = ""
    elif info.players:
        players = "<ul>" + "".join(f"<li>{e(p)}</li>" for p in info.players) + "</ul>"
    else:
        players = f"<p class='muted'>{'Personne pour le moment.' if not count else f'{count} joueur(s).'}</p>"
    if info.needs_personal_address and info.address:
        address = (f"<p class='label'>Première connexion (Multijoueur → Ajouter un serveur)</p>"
                   f"<p class='address'>{e(info.address)}</p>"
                   "<p class='muted'>Chaque joueur a sa propre adresse. Connecte-toi d'abord à celle-ci : "
                   "ta demande part aux administrateurs. Une fois acceptée, reconnecte-toi ici, et ton "
                   "adresse personnelle s'affichera.</p>")
    elif info.needs_personal_address:
        address = ("<p class='muted'>Ce serveur accepte les comptes non officiels : chaque joueur a "
                   "sa propre adresse, à demander à un administrateur du groupe.</p>")
    elif info.address:
        address = (f"<p class='label'>Adresse à mettre dans Minecraft (Multijoueur → Ajouter un serveur)</p>"
                   f"<p class='address'>{e(info.address)}</p>")
    else:
        address = ""
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>{e(info.name)}</title>
<style>
:root {{ --bg: #f5f6f8; --card: #ffffff; --text: #1d2330; --muted: #667085; --ok: #2f9e6a; --off: #98a2b3; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #14171c; --card: #1e232b; --text: #e8eaed; --muted: #9aa3b2; --ok: #3fb27f; --off: #5c6573; }}
}}
body {{ margin: 0; background: var(--bg); color: var(--text);
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 560px; margin: 0 auto; padding: 32px 16px; }}
.card {{ background: var(--card); border-radius: 14px; padding: 24px; box-shadow: 0 1px 3px #0002; }}
h1 {{ margin: 0 0 8px; font-size: 1.6rem; }}
.state {{ display: inline-flex; align-items: center; gap: 8px; font-weight: 600; }}
.dot {{ width: 12px; height: 12px; border-radius: 50%; background: {'var(--ok)' if info.online else 'var(--off)'}; }}
h2 {{ font-size: 1rem; margin: 24px 0 8px; }}
ul {{ margin: 0; padding-left: 20px; }}
.muted, .label {{ color: var(--muted); margin: 4px 0; }}
.address {{ font: 600 1.15rem ui-monospace, Consolas, monospace; user-select: all; word-break: break-all;
           background: var(--bg); padding: 10px 12px; border-radius: 8px; margin: 4px 0; }}
a {{ color: inherit; font-weight: 600; }}
footer {{ color: var(--muted); font-size: .85rem; margin-top: 16px; text-align: center; }}
</style></head>
<body><main><div class="card">
<h1>{e(info.name)}</h1>
<p class="state"><span class="dot"></span>{e(info.state_text)}</p>
<p class="muted">Minecraft {e(info.version)}</p>
{f'<h2>Joueurs connectés ({count or 0})</h2>' + players if info.online else ''}
{address}
{"<p><a href='map/'>Voir la carte du monde</a></p>" if info.has_map and info.online else ""}
</div><footer>Mise à jour toutes les 30 secondes · Minecraft Distributed Hosting</footer></main></body></html>
""".encode()


NOT_FOUND = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Introuvable</title></head>
<body style="font-family: system-ui, sans-serif; padding: 32px">
<p>Pas de page de statut à cette adresse.</p></body></html>""".encode()


def _map_page(text: str, refresh_s: int) -> bytes:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Carte du monde</title>
<meta http-equiv="refresh" content="{refresh_s}"></head>
<body style="font-family: system-ui, sans-serif; padding: 32px; max-width: 640px">
<p>{text}</p><p><a href="/">Retour au statut</a></p>
</body></html>""".encode()


MAP_OFFLINE = _map_page("Le serveur est arrêté : la carte du monde s'affiche quand il est en ligne.", 30)
MAP_NOT_ACTIVE = _map_page(
    "Le serveur est en ligne, mais la carte n'y est pas encore active. Elle démarre avec le serveur : "
    "si elle vient d'être activée, un administrateur doit redémarrer le serveur depuis l'application "
    "(bouton Redémarrer).", 30)
MAP_STARTING = _map_page("La carte démarre sur le PC hôte (environ une minute après le serveur). "
                         "Cette page se recharge toute seule.", 15)
