#!/usr/bin/env python3
"""Backport "the door keeps reading while it admits" onto a deployed facade release
whose WebSocket handler still admits inline with `await node.begin(...)` (the
e2fb4bbace24e144 shape). Asserts the exact text it edits and refuses anything
else, like backport-process-node-holds.py.

    backport-process-node-read-while-admitting.py <in process_node.py> <out process_node.py>

Why (2026-11-17 14:05 CST, via hto-histo-na -> zz-tower0): the facade answered
`started`, then blocked in admission while a cold worker loaded for 12 s and read
nothing. The client's unacked window (25 chunks) filled; 25 of 134 chunks left the
phone and the session failed over to a batch upload. Main's fix: livestack
process_node.py, "While admission ... runs, the door KEEPS READING".
"""
import sys

src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()

def swap(old, new):
    global s
    if s.count(old) != 1:
        sys.exit(f"refusing: expected exactly one occurrence of:\n{old}")
    s = s.replace(old, new)

swap("""            await node.begin(node.config['default_unit'])
            try:
                backend_url=""", """            admission=asyncio.create_task(node.begin(node.config['default_unit']))
            held=[]
            acked=-1
            pending=None

            async def take(message):
                nonlocal acked
                frame=message.get('bytes')
                if (session is not None and frame is not None and frame[:4]==b'BASR'
                        and len(frame)>=16):
                    node.cache_frame(session,frame)
                    while acked+1 in session['frames']:acked+=1
                    if acked>=0:
                        await ws.send_json({'type':'ack','protocol':1,
                                            'sessionId':json.loads(start)['sessionId'] if start else None,
                                            'ackSeq':acked})
                    return
                if len(held)>=256:raise RuntimeError('ASR held control capacity exceeded')
                held.append(message)

            async def drop_admission():
                # The socket is gone (or this handler failed) while admitting: do
                # not keep a grant nobody will use.
                if not admission.done():
                    admission.cancel()
                    try:await admission
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():raise
                    except Exception:pass
                elif not admission.cancelled() and admission.exception() is None:
                    await node.finish()

            # The door said `started`; it KEEPS READING while a cold worker loads,
            # acking frames into the bounded resume cache and holding control
            # messages, instead of letting the client's unacked window fill.
            try:
                await take(first)
                while not admission.done():
                    if pending is None:pending=asyncio.create_task(ws.receive())
                    done,_=await asyncio.wait({admission,pending},return_when=asyncio.FIRST_COMPLETED)
                    if pending in done:
                        message=pending.result();pending=None
                        if message['type']=='websocket.disconnect':
                            await drop_admission();return
                        await take(message)
            except BaseException:
                if pending is not None:pending.cancel()
                await drop_admission();raise
            await admission
            try:
                backend_url=""")

swap("""                    async def upstream():
                        while True:
                            message=await ws.receive()
                            if message['type']=='websocket.disconnect':return
                            await send(message)""", """                    async def upstream():
                        nonlocal pending
                        while True:
                            if pending is not None:
                                message=await pending;pending=None
                            else:message=await ws.receive()
                            if message['type']=='websocket.disconnect':return
                            await send(message)""")

swap("""                            for seq,frame in sorted(tuple(session['frames'].items())):await backend.send(frame)
                        await send(first)""", """                            for seq,frame in sorted(tuple(session['frames'].items())):await backend.send(frame)
                        # `first` and every frame read while admitting are in the
                        # replay above; held control messages follow in order.
                        for message in held:await send(message)""")

open(dst, "w").write(s)
print("patched", dst)
