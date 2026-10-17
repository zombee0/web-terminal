"""Private PostgreSQL access and process-local DNS assistance for deployment."""

import asyncio
import ipaddress
import json
import pathlib
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request

import asyncpg

from supabase.config import required


async def allow_pooler_dns_fallback(dsn):
    """Keep the hostname for TLS verification; never persist resolved addresses."""
    host = urllib.parse.urlsplit(dsn).hostname
    if not host or not re.fullmatch(r"[a-z0-9-]+\.pooler\.supabase\.com", host):
        return
    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    cache = {"until": 0, "addresses": []}

    async def resolve(requested, port, *args, **kwargs):
        try:
            return await original(requested, port, *args, **kwargs)
        except socket.gaierror:
            if requested != host:
                raise
        if time.monotonic() >= cache["until"]:
            def lookup():
                url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": host, "type": "A"})
                with urllib.request.urlopen(url, timeout=15) as response:
                    data = json.load(response)
                addresses = [answer["data"] for answer in data.get("Answer", []) if answer.get("type") == 1]
                return [address for address in addresses if ipaddress.ip_address(address).is_global]
            cache["addresses"] = await asyncio.to_thread(lookup)
            cache["until"] = time.monotonic() + 60
        if not cache["addresses"]:
            raise socket.gaierror("Pooler DNS lookup failed")
        results = []
        for address in cache["addresses"]:
            results.extend(await original(address, port, *args, **kwargs))
        return results

    loop.getaddrinfo = resolve


async def create_pool(dsn=None, *, schema="web_terminal", dns_fallback=False):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError("Invalid database schema")
    dsn = dsn or required("WEB_TERMINAL_DB_URL")
    if dns_fallback:
        await allow_pooler_dns_fallback(dsn)
    tls = ssl.create_default_context()
    # Official Supabase root, distributed by its Database Settings certificate link.
    tls.load_verify_locations(cafile=str(pathlib.Path(__file__).parent / "certs" / "supabase-ca-2021.crt"))
    return await asyncpg.create_pool(
        dsn, min_size=1, max_size=5, timeout=20, command_timeout=20,
        statement_cache_size=0, ssl=tls,
        server_settings={"application_name": "web-terminal", "search_path": f'"{schema}",pg_catalog'},
    )
