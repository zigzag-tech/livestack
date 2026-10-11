import test from 'node:test';
import assert from 'node:assert/strict';
import adapter from '../zzops-adapter.mjs';

test('policy refuses malformed effects and digests before permission dispatch', () => {
  const digest = 'a'.repeat(64);
  const scope = { mode: 'broker-owner', service: 'livestack-hostd.service', controlGroup: '/system.slice/livestack-hostd.service', port: 8799 };
  const request = { app: 'harmony-broker', target: 'zz-joe', effect: 'prepare', inputDigest: digest, scope };
  const owner = { inputDigests: { prepare: digest } };
  assert.equal(adapter.brokerOwnerAuthorization(request, owner), true);
  for (const inputDigest of ['', 'true', 'A'.repeat(64), {}, null]) {
    assert.equal(adapter.brokerOwnerAuthorization({ ...request, inputDigest }, { inputDigests: { prepare: inputDigest } }), false);
  }
  assert.equal(adapter.brokerOwnerAuthorization({ ...request, effect: 'invented' }, { inputDigests: { invented: digest } }), false);
  assert.equal(adapter.brokerOwnerAuthorization({ ...request, target: 'other' }, owner), false);
  assert.equal(adapter.brokerOwnerAuthorization(null, owner), false);
  assert.equal(adapter.brokerOwnerAuthorization(request, null), false);
});
