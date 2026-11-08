"""A stand-in for a Minecraft server: same console behaviour, no Java needed.

Modes (env FAKE_MC_MODE): normal | crash (exits with code 1 shortly after "Done") | hang (never
ready) | eula (behaves like a real server without eula=true). Writes world/level.dat like a real
server and records received commands in commands.log.
"""

import os
import sys
import threading
import time
from pathlib import Path

mode = os.environ.get("FAKE_MC_MODE", "normal")
cwd = Path.cwd()
# Like the JVM started with -Dfile.encoding/-Dstdout.encoding=UTF-8.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def out(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')} INFO]: {msg}", flush=True)


def feedback(msg: str) -> None:
    """Command feedback, logged the way Minecraft 26.x does it."""
    out(f"System chat: {msg}")


eula = (cwd / "eula.txt").read_text("latin-1") if (cwd / "eula.txt").exists() else ""
if "eula=true" not in eula or mode == "eula":
    out("Failed to load eula.txt")
    out("You need to agree to the EULA in order to run the server. Go to eula.txt for more info.")
    sys.exit(0)

out("Starting minecraft server version fake")
(cwd / "world" / "region").mkdir(parents=True, exist_ok=True)
(cwd / "world" / "data").mkdir(exist_ok=True)
if not (cwd / "world" / "level.dat").exists():
    (cwd / "world" / "level.dat").write_bytes(b"fake level")
region = cwd / "world" / "region" / "r.0.0.mca"
if not region.exists():
    region.write_bytes(os.urandom(2 * 1024 * 1024 + 4096))  # 3 blocks of 1 MiB
pending: dict[str, str] = {}  # changes "in memory", written to disk on save / stop


def save_world() -> None:
    for name, value in pending.items():
        (cwd / "world" / "data" / name).write_text(value, "utf-8")
    pending.clear()
    with region.open("r+b") as fh:  # a chunk rewritten in the middle of the region file
        fh.seek(1024 * 1024 + 8192)
        fh.write(os.urandom(4096))


def listen_for_players() -> None:
    """Minimal network side on server-port: server-list status, login (logged like Minecraft:
    "<name> joined the game"), then echo of whatever the player sends."""
    import json
    import socket

    props = dict(line.split("=", 1) for line in (cwd / "server.properties").read_text("utf-8").splitlines()
                 if "=" in line and not line.startswith("#")) if (cwd / "server.properties").exists() else {}
    try:
        server = socket.create_server(("127.0.0.1", int(props.get("server-port", 25565))))
    except OSError:
        return  # port taken (e.g. a real server on this PC): console-only fake

    def varint(n: int) -> bytes:
        b = bytearray()
        while True:
            if n < 0x80:
                return bytes(b + bytes([n]))
            b.append((n & 0x7F) | 0x80)
            n >>= 7

    def recv_exact(conn, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    def read_varint(conn) -> int:
        value, shift = 0, 0
        while True:
            byte = recv_exact(conn, 1)[0]
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    def read_packet(conn) -> tuple[int, bytes]:
        body = recv_exact(conn, read_varint(conn))
        return body[0], body[1:]  # packet ids used here are < 0x80

    def send_packet(conn, pid: int, payload: bytes) -> None:
        body = varint(pid) + payload
        conn.sendall(varint(len(body)) + body)

    def string(s: str) -> bytes:
        data = s.encode("utf-8")
        return varint(len(data)) + data

    # Like Paper with proxies.proxy-protocol: a PROXY v2 header first, or the connection is refused.
    paper_global = cwd / "config" / "paper-global.yml"
    proxy_protocol = paper_global.exists() and "  proxy-protocol: true" in paper_global.read_text("utf-8")

    def read_proxy_header(conn) -> str:
        head = recv_exact(conn, 16)
        if head[:12] != b"\r\n\r\n\x00\r\nQUIT\n":
            raise ConnectionError("no PROXY protocol header")
        body = recv_exact(conn, int.from_bytes(head[14:16], "big"))
        if head[12] == 0x20:                                 # LOCAL: the connection's own address
            return "/127.0.0.1"
        import ipaddress
        size = 4 if head[13] == 0x11 else 16
        ip = ipaddress.ip_address(body[:size])
        return f"/{ip}:{int.from_bytes(body[2 * size:2 * size + 2], 'big')}"

    def handle(conn) -> None:
        with conn:
            try:
                address = read_proxy_header(conn) if proxy_protocol else "/127.0.0.1"
                _, handshake = read_packet(conn)
                if handshake[-1] == 1:                       # status
                    read_packet(conn)
                    doc = {"version": {"name": "fake", "protocol": 0}, "players": {"max": 20, "online": 1},
                           "description": {"text": f"FAKE {cwd.parent.parent.name}"}}
                    send_packet(conn, 0x00, string(json.dumps(doc)))
                    _, payload = read_packet(conn)
                    send_packet(conn, 0x01, payload)
                    return
                if handshake[-1] == 3:                       # /transfer, without accepts-transfers
                    out("Disconnecting: transfers are disabled on this server")
                    return
                _, login = read_packet(conn)                 # login start: name first
                name = login[1:1 + login[0]].decode("utf-8")
                out(f"{name}[{address}] logged in with entity id 1 at (0.5, 64.0, 0.5)")
                out(f"{name} joined the game")
                send_packet(conn, 0x02, string(name))        # stands for "login success"
                try:
                    while data := conn.recv(65536):
                        conn.sendall(data)
                finally:
                    out(f"{name} left the game")
            except (ConnectionError, OSError, IndexError):
                pass

    def accept_loop() -> None:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()


if mode == "hang":
    time.sleep(3600)
listen_for_players()
time.sleep(0.2)
out('Done (0.213s)! For help, type "help"')
out("Steve joined the game")

if mode == "crash":
    def crash() -> None:
        time.sleep(0.5)
        print("Exception in server tick loop", flush=True)
        os._exit(1)

    threading.Thread(target=crash, daemon=True).start()

chunky_world, chunky_cancel = "minecraft:overworld", False
log = (cwd / "commands.log").open("a", encoding="utf-8")
for raw in sys.stdin:
    cmd = raw.strip()
    log.write(cmd + "\n")
    log.flush()
    if cmd.startswith("mark "):          # test helper: "play" (change the world in memory)
        key, _, value = cmd[5:].partition("=")
        pending[key] = value
        out(f"Marked {key}")
    elif cmd == "save-off":
        feedback("Automatic saving is now disabled")
    elif cmd == "save-on":
        feedback("Automatic saving is now enabled")
    elif cmd == "stop":
        save_world()
        out("Stopping the server")
        out("Saving worlds")
        time.sleep(0.1)
        sys.exit(0)
    elif cmd == "save-all flush":
        save_world()
        out("Saving the game (this may take a moment!)")
        feedback("Saved the game")
    elif cmd == "list":
        feedback("There are 1 of a max of 20 players online: Steve")
    elif cmd == "tps":
        out("TPS from last 1m, 5m, 15m: 19.9, 20.0, 20.0")
    elif cmd.startswith("say "):
        out(f"[Server] {cmd[4:]}")
    elif cmd.startswith(("whitelist add ", "whitelist remove ")):
        import json
        wl_path = cwd / "whitelist.json"
        entries = json.loads(wl_path.read_text("utf-8")) if wl_path.exists() else []
        name = cmd.split(" ", 2)[2]
        entries = [e for e in entries if e["name"].lower() != name.lower()]
        if cmd.startswith("whitelist add "):
            entries.append({"uuid": "00000000-0000-3000-8000-000000000000", "name": name})
            out(f"Added {name} to the whitelist")
        else:
            out(f"Removed {name} from the whitelist")
        wl_path.write_text(json.dumps(entries), "utf-8")
    elif cmd == "whitelist reload":
        feedback("Reloaded the whitelist")
    elif cmd == "leave":              # test helper: the only player logs out
        out("Steve left the game")
    elif cmd == "chat":
        out("<Léa> Ça marche, à tout à l'heure !")
    elif cmd.startswith("talk "):            # test helper: "talk Steve hello" = Steve says hello
        who, _, text = cmd[5:].partition(" ")
        out(f"<{who}> {text}")
    elif cmd.startswith("chunky "):          # Chunky 1.5.3's answers (lang/en.json)
        sub = cmd[7:]
        if sub.startswith("world "):
            chunky_world = sub[6:]
            out(f"[Chunky] World changed to {chunky_world}.")
        elif sub == "start":
            out(f"[Chunky] Task started in {chunky_world} for the square region centered at 0, 0 with radius 1000.")
            out(f"[Chunky] Task running for {chunky_world}. Processed: 1000 chunks (12.50%), ETA: 0:01:30, "
                "Rate: 50.0 cps, Current: 1, 2")
        elif sub == "cancel":
            chunky_cancel = True
            out("[Chunky] Cancelled tasks cannot be continued. If you are sure you want to cancel, type "
                "'/chunky confirm'.")
        elif sub == "confirm" and chunky_cancel:
            chunky_cancel = False
            out(f"[Chunky] Task cancelled for {chunky_world}.")
        elif sub == "confirm":
            out("[Chunky] Nothing to confirm!")
        else:
            out(f"[Chunky] {sub}: done")
    elif cmd.startswith("spark profiler start"):   # spark 1.10.187's answers
        out("[⚡] Starting a new profiler, please wait...")
        out("[⚡] Profiler is now running!")

        def spark_done() -> None:
            time.sleep(0.5)
            out("[⚡] The active profiler has completed! Uploading results...")
            out("[⚡] Profiler stopped & upload complete!")
            out("[⚡] https://spark.lucko.me/AbCdEf1234")
        threading.Thread(target=spark_done, daemon=True).start()
    elif cmd.startswith("gamerule "):        # 26.3's answer (commands.gamerule.set)
        _, rule, value = cmd.split()
        feedback(f"Game rule {rule} is now set to {value}")
    elif cmd.startswith("transfer "):        # the players' game reconnects there (the tests do it)
        feedback(f"Transferring {cmd.split()[3]} to {cmd.split()[1]}:{cmd.split()[2]}")
    elif cmd == "crashmod":                  # test helper: a mod crashes the server tick
        reports = cwd / "crash-reports"
        reports.mkdir(exist_ok=True)
        (reports / f"crash-{time.strftime('%Y-%m-%d_%H.%M.%S')}-server.txt").write_text(
            "---- Minecraft Crash Report ----\nDescription: Exception in server tick loop\n\n"
            "java.lang.IllegalStateException: boom\n"
            "\tat net.minecraft.server.level.ServerLevel.tick(ServerLevel.java:10)\n"
            "\tat net.caffeinemc.mods.lithium.ai.Pathing.find(Pathing.java:40)\n", "utf-8")
        print("Exception in server tick loop", flush=True)
        os._exit(1)
    elif cmd.startswith(("kick ", "ban ", "pardon ", "op ", "deop ")):
        verb, _, who = cmd.partition(" ")
        feedback(f"{verb} {who}: done")
    else:
        out(f"Unknown or incomplete command: {cmd}")
