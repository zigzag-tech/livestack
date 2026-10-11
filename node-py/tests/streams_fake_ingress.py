"""A fake benchday daemon ingress that speaks the REAL frames (4-byte BE length-prefixed JSON).

It mirrors daemon/src/streams/ingress.rs for hello / publish / register / serve / write_result and the
unsolicited `write` and `mode` frames. It is a protocol mirror, not a mock of the SDK: the code under
test connects through the real unix socket with the real vendored SDK.
"""
import asyncio
import json
import struct


async def read_frame(reader):
    (length,) = struct.unpack(">I", await reader.readexactly(4))
    return json.loads(await reader.readexactly(length))


def frame(value):
    body = json.dumps(value).encode()
    return struct.pack(">I", len(body)) + body


class FakeIngress:
    def __init__(self, path, realm_epoch=7, shadow=()):
        self.path, self.realm_epoch, self.shadow = path, realm_epoch, set(shadow)
        self.server = None
        self.publishes, self.registers, self.serves = [], [], []
        self.writers, self.serving = [], {}
        self._pending, self._n = {}, 0

    async def start(self):
        self.server = await asyncio.start_unix_server(self.handle, self.path)

    async def stop(self):
        self.server.close()
        for writer in self.writers:
            writer.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        self.writers.append(writer)
        try:
            while True:
                row = await read_frame(reader)
                kind = row["type"]
                if kind == "hello":
                    writer.write(frame({"type": "hello_ok", "daemon": "d1", "leases": []}))
                elif kind == "publish":
                    self.publishes.append(row)
                    writer.write(frame({"type": "published", "key": "k", "epoch": "1", "seq": str(len(self.publishes)),
                                        "leased": True, "delivered": 1,
                                        **({"shadowed": True} if row["contract"] in self.shadow else {})}))
                elif kind == "register":
                    self.registers.append(row["contract"])
                    mode = "shadow" if row["contract"] in self.shadow else "active"
                    writer.write(frame({"type": "registered", "contract": row["contract"], "mode": mode, "epoch": self.realm_epoch}))
                elif kind == "serve":
                    self.serves.append(row["contract"])
                    self.serving[row["contract"]] = writer
                    writer.write(frame({"type": "serving", "contract": row["contract"], "epoch": self.realm_epoch}))
                elif kind == "intent_partial":
                    part = row["partial"]
                    self.publishes.append({"type": "publish", "contract": part["contract"], "subject": part["subject"],
                                           "body": part["body"], "partial": part})
                    writer.write(frame({"type": "published", "contract": part["contract"],
                                        "key": part["subject"]["intent_id"], "forwarded": True}))
                elif kind == "write_result":
                    future = self._pending.pop(row["req"], None)
                    if future and not future.done():
                        future.set_result(row["partial"])
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def write(self, contract, op, intent_id, payload=None, principal="agent:x", timeout=3):
        """Forward a write to the serving authority exactly as the daemon does; return its partial."""
        for _ in range(100):
            if contract in self.serving:
                break
            await asyncio.sleep(0.02)
        self._n += 1
        req = f"w{self._n}"
        future = asyncio.get_event_loop().create_future()
        self._pending[req] = future
        self.serving[contract].write(frame({"type": "write", "req": req, "contract": contract, "op": op,
                                            "intent_id": intent_id, "principal": principal, "payload": payload or {}}))
        return await asyncio.wait_for(future, timeout)

    def bodies(self, contract, intent_id=None):
        return [p["body"] for p in self.publishes if p["contract"] == contract
                and (intent_id is None or p["subject"].get("intent_id") == intent_id)]

    async def settle(self, predicate, timeout=3):
        for _ in range(int(timeout / 0.02)):
            if predicate():
                return True
            await asyncio.sleep(0.02)
        return False
