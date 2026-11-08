"""The players' real addresses on Paper (docs/ARCHITECTURE.md 4.22).

Every player reaches Minecraft through the VPS relay and the hosting PC's tunnel to 127.0.0.1,
so without help Minecraft sees all of them as 127.0.0.1: an IP ban would ban everybody, or nobody.
On Paper (1.19 and later), ``proxies.proxy-protocol`` makes it read a PROXY protocol header at
the start of each connection; the hosting PC writes one in each tunnel
(:mod:`mdh_shared.protocol.proxy`), with the address the VPS saw.

The setting is forced on before every start, like the other settings this application manages:
from then on, a connection without a header is refused, and Minecraft only listens on 127.0.0.1,
where only the tunnels connect. Fabric and vanilla do not read the header: nothing changes there.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mdh_client.minecraft.anticheat import paper_supported
from mdh_shared.models.enums import Loader

_KEY = re.compile(r"^(\s+)proxy-protocol:.*$")


def supported(loader: Loader | str, mc_version: str) -> bool:
    """Paper keeps this setting in config/paper-global.yml since Minecraft 1.19."""
    return Loader(loader) == Loader.PAPER and paper_supported(mc_version)


def edit_global(text: str) -> str:
    """``paper-global.yml`` with ``proxies.proxy-protocol: true`` (the rest as Paper wrote it)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "proxies:"), None)
    if start is None:
        return text + ("" if not text or text.endswith("\n") else "\n") + "proxies:\n  proxy-protocol: true\n"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() and not lines[i].lstrip().startswith("#") and not lines[i].startswith(" ")),
               len(lines))
    for i in range(start + 1, end):
        m = _KEY.match(lines[i])
        if m and len(m.group(1)) == 2:
            if lines[i].strip() == "proxy-protocol: true":
                return text
            lines[i] = "  proxy-protocol: true"
            break
    else:
        lines.insert(start + 1, "  proxy-protocol: true")
    return "\n".join(lines) + "\n"


def configure(server_dir: Path, loader: Loader | str, mc_version: str) -> bool:
    """Before a start: True when this server reads the header (the tunnels must then send it)."""
    if not supported(loader, mc_version):
        return False
    path = server_dir / "config" / "paper-global.yml"
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        text = ""  # first start: Paper adds everything else, for its own version
    new = edit_global(text)
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new, "utf-8")
        os.replace(tmp, path)
    return True
