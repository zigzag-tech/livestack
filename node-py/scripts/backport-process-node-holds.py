"""Apply livestack 91b49f4b (hold tokens, synchronous finish, holds bounded by their request task) to an older
process-node facade release: `python3 backport-process-node-holds.py <release>/process_node.py` edits it in place
and refuses (AssertionError) if the file does not have a shape this was written for. Used on xc-tower-ubuntu
2026-11-03 for releases e2fb4bbace24e144, 3e4360c1bbc9b020, depth-lease, image-wait and admit-wait
(node-py/docs/host-broker-release-deploy.md)."""
import re,sys
p=sys.argv[1];s=open(p).read()
def rep(a,b,count=1):
    global s
    n=s.count(a);assert n==count,(a,n);s=s.replace(a,b)
FINISH_NOTE="""        # Synchronous on purpose: it runs in `finally` blocks that may already
        # be cancelled, where awaiting self.lock is cancelled before it returns (livestack 91b49f4b).
"""
rep("""        self.active = 0
""","""        # One entry per admitted request, keyed by token, valued by the task
        # that owns the request. See `active`.
        self.holds = {}
""")
rep("""    def _protected(self):""","""    @property
    def active(self):
        \"\"\"Requests in flight: what makes every unit of this worker busy.

        A hold lives no longer than the request task that took it (livestack
        91b49f4b): cleanup that never ran would otherwise keep the device busy
        until the facade restarts.
        \"\"\"
        for token, owner in list(self.holds.items()):
            if owner.done():
                del self.holds[token]
                log.warning('released a hold whose request ended without returning it')
        return len(self.holds)

    def _protected(self):""")
rep("""            self.active += 1
            self.last_used = time.monotonic()
""","""            token = uuid.uuid4().hex
            self.holds[token] = asyncio.current_task()
            self.last_used = time.monotonic()
            return token
""")
if '    async def attach(self):' in s:
    # The engine-route shape (release admit-wait): a read-only request attaches to a running worker
    # without a grant, and finish() takes `used`.
    rep("""            if not self._alive(): return False
            self.active += 1
            return True
""","""            if not self._alive(): return None
            token = uuid.uuid4().hex
            self.holds[token] = asyncio.current_task()
            return token
""")
    rep("""    async def finish(self, used=True):
        async with self.lock:
            self.active -= 1
            # A status poll is not use: it must not keep an idle model resident.
            if used: self.last_used = time.monotonic()
""","""    def finish(self, token, used=True):
"""+FINISH_NOTE+"""        # A status poll is not use: it must not keep an idle model resident.
        if self.holds.pop(token, None) is not None and used: self.last_used = time.monotonic()
""")
    rep("""        used=not (read_only(request.method,path) and await node.attach())
        if used:
            unit=await node.choose('/'+path,body,request.headers.get('content-type',''))
            await node.begin(""","""        hold=await node.attach() if read_only(request.method,path) else None
        used=hold is None
        if used:
            unit=await node.choose('/'+path,body,request.headers.get('content-type',''))
            hold=await node.begin(""")
    rep("""            finally:
                await response.aclose()
                await node.finish(used)""","""            finally:
                node.finish(hold,used)
                await response.aclose()""")
    rep("await node.finish(used);raise","node.finish(hold,used);raise")
    begins=2
else:
    s,n=re.subn(r"    async def finish\(self\):\n        async with self.lock:\n            self.active -= 1\n(            self.last_used = [^\n]*\n)",
                lambda m:"    def finish(self, token):\n"+FINISH_NOTE+"        if self.holds.pop(token, None) is not None:\n"+m.group(1),s)
    assert n==1
    rep("""            finally:
                await response.aclose()
                await node.finish()""","""            finally:
                node.finish(hold)
                await response.aclose()""")
    begins=3
s,n=re.subn(r"(\n *)await node\.begin\(",r"\1hold=await node.begin(",s);assert n==begins,n
s,n=re.subn(r"await node\.finish\(\)",r"node.finish(hold)",s);assert n==begins,n
assert 'self.active' not in s.replace('self.active or','').replace("'in_flight':self.active",''),'stray active'
if 'getLogger' not in s:
    rep("import json\n","import json\nimport logging\n")
    rep("\nclass ProcessNode","\nlog = logging.getLogger('livestack.process_node')\n\n\nclass ProcessNode")
open(p,'w').write(s)
