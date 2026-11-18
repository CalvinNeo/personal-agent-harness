"""Per-call loopback SOCKS transport; Chromium keeps native TLS, DNS stays pinned."""
from __future__ import annotations

import asyncio
import contextlib
import socket

import douyin
from public_web import WebError, public_addresses


class PublicEgress:
    def __init__(self):
        self.server = None
        self.tasks = set()
        self.transferred = 0
        self.failure = None
        self.addresses = {}
        self.resolvers = {}
        self.dns_slots = asyncio.Semaphore(3)

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0, limit=65536)
        port = self.server.sockets[0].getsockname()[1]
        self.url = "socks5://127.0.0.1:" + str(port)
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def connect(self, host, port):
        if port not in {80, 443}:
            raise WebError("standard_ports_only")
        douyin.resource_url(("https://" if port == 443 else "http://") + host + ":" + str(port) + "/")
        key = (host, port)
        async with self.resolvers.setdefault(key, asyncio.Lock()):
            if key not in self.addresses:
                async with self.dns_slots:
                    self.addresses[key] = await asyncio.to_thread(public_addresses, host, port)
            addresses = self.addresses[key]
        for family, socktype, protocol, address in addresses:
            connection = socket.socket(family, socktype, protocol)
            connection.setblocking(False)
            try:
                await asyncio.wait_for(asyncio.get_running_loop().sock_connect(connection, address), 5)
                return await asyncio.open_connection(sock=connection)
            except (OSError, TimeoutError):
                connection.close()
            except BaseException:
                connection.close()
                raise
        raise WebError("douyin_public_site_unavailable")

    async def relay(self, reader, writer):
        while data := await reader.read(65536):
            self.transferred += len(data)
            if self.transferred > douyin.MAX_TRANSFER:
                self.failure = "douyin_transfer_limit"
                raise WebError(self.failure)
            writer.write(data)
            await writer.drain()

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        try:
            if len(self.tasks) > 128 or self.failure:
                return
            async with asyncio.timeout(12):
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                if version != 5 or 0 not in methods:
                    return
                writer.write(b"\x05\x00")
                await writer.drain()
                version, command, reserved, address_type = await reader.readexactly(4)
                if (version, command, reserved, address_type) != (5, 1, 0, 3):
                    return
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode("ascii")
                port = int.from_bytes(await reader.readexactly(2), "big")
                remote, upstream = await self.connect(host, port)
                writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
            transfers = [asyncio.create_task(self.relay(reader, upstream)),
                         asyncio.create_task(self.relay(remote, writer))]
            try:
                await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for transfer in transfers:
                    transfer.cancel()
                await asyncio.gather(*transfers, return_exceptions=True)
        except (WebError, OSError, UnicodeError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(OSError, TimeoutError):
                        await asyncio.wait_for(stream.wait_closed(), 1)
            self.tasks.discard(task)
