// Deployment-only application policy. Worker registration and broker effect
// admission remain separate; this module never invents a train result.
export default Object.freeze({
  sourceReference: 'harmony-broker.sources',
  resultArtifact: 'broker-owner-receipt.json',
  workload() {
    throw new Error('harmony_broker_deployment_only_use_registered_deploy_target');
  },
  verifyResult() {
    throw new Error('harmony_broker_train_result_not_supported');
  },
  // This callback constrains the separately checked canonical deploy grant.
  // It does not attest drain, CUDA settlement, handler registration or a lease.
  brokerOwnerAuthorization(request, owner) {
    const effects = ['prepare', 'status', 'hold', 'release', 'shutdown', 'reconcile', 'activate', 'rollback'];
    const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
    return !!request && !!owner && effects.includes(request.effect) &&
      digest(request.dispatchIdentitySha256) && owner.dispatches?.[request.dispatchUnit] === request.dispatchIdentitySha256 &&
      ['livestack-hostd.service','polytts.service','harmony-klein-1.service'].includes(request.dispatchUnit) &&
      digest(request.inputDigest) && digest(owner.inputDigests?.[request.effect]) && request.app === 'harmony-broker' && request.target === 'zz-joe' &&
      request.scope?.mode === 'broker-owner' && request.scope.service === 'livestack-hostd.service' &&
      request.scope.controlGroup === '/system.slice/livestack-hostd.service' && request.scope.port === 8799 &&
      typeof owner.inputDigests?.[request.effect] === 'string' &&
      owner.inputDigests[request.effect] === request.inputDigest;
  },
});
