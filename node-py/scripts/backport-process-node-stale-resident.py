"""Apply the process-node half of livestack 6f5f19fe (stale-resident grants) to an older facade release that
already carries the hold-token backport (backport-process-node-holds.py):
`python3 backport-process-node-stale-resident.py <release>/process_node.py` edits it in place and refuses
(AssertionError) if the file does not have a shape this was written for.

- the residence report says `residence_authority: owner`, so a host broker carrying 6f5f19fe stops
  remembering a unit this facade stopped as resident at once, instead of granting without a warm for 90 s;
- `begin()` warms under a grant that reaches it while the unit is not resident, instead of answering
  503 "Harmony grant did not warm this provider".

Used on xc-tower-ubuntu 2026-11-06 for the harmony-image facade (release 5619d4e8d1e093c4), whose qwen
requests were refused that way after hostd evicted qwen for drama3 (node-py/docs/host-broker-release-deploy.md)."""
import sys
p=sys.argv[1];s=open(p).read()
def rep(a,b,count=1):
    global s
    n=s.count(a);assert n==count,(a,n);s=s.replace(a,b)
rep("""        async with self.lock:
            await self._refresh()
            if unit in self.resident:
                self.last_used = time.monotonic()
                return {'resident':sorted(self.resident),'device':self.config['device_id']}
            if self._protected():
                raise HTTPException(409, 'worker is busy; retry admission')
            if headers: self.control_headers = headers
            try:
                await self._start()
                result = await self._request('POST', '/livestack/model/warm',
                    json={'unit':unit,'device':self.config['device_id'], **({'budget':budget} if budget else {})},
                    headers=self.control_headers, timeout=self.config.get('warm_timeout',600))
                if unit not in result.get('resident', []):
                    raise RuntimeError('worker_did_not_acknowledge_residency')
                self.resident = set(result['resident'])
                self.last_used = time.monotonic()
                return result
            except BaseException:
                if not self._protected(): await self._stop()
                raise
""","""        async with self.lock:
            return await self._warm_locked(unit, headers, budget)

    async def _warm_locked(self, unit, headers=None, budget=None):
        \"\"\"Make `unit` resident. The caller holds self.lock.\"\"\"
        await self._refresh()
        if unit in self.resident:
            self.last_used = time.monotonic()
            return {'resident':sorted(self.resident),'device':self.config['device_id']}
        if self._protected():
            raise HTTPException(409, 'worker is busy; retry admission')
        if headers: self.control_headers = headers
        try:
            await self._start()
            result = await self._request('POST', '/livestack/model/warm',
                json={'unit':unit,'device':self.config['device_id'], **({'budget':budget} if budget else {})},
                headers=self.control_headers, timeout=self.config.get('warm_timeout',600))
            if unit not in result.get('resident', []):
                raise RuntimeError('worker_did_not_acknowledge_residency')
            self.resident = set(result['resident'])
            self.last_used = time.monotonic()
            return result
        except BaseException:
            if not self._protected(): await self._stop()
            raise
""")
rep("""            if not self._alive() or unit not in self.resident:
                raise HTTPException(503,'Harmony grant did not warm this provider')
""","""            if not self._alive() or unit not in self.resident:
                # The broker granted without a warm because it still believed the unit
                # resident (livestack 6f5f19fe). The grant stands: load it under it.
                try: await self._warm_locked(unit)
                except Exception:
                    raise HTTPException(503,'Harmony grant did not warm this provider') from None
""")
rep("""        result = dict(host_id=self.config['host_id'],node_id=self.config['node_id'],
""","""        result = dict(host_id=self.config['host_id'],node_id=self.config['node_id'],
                      residence_authority='owner',
""")
open(p,"w").write(s)
