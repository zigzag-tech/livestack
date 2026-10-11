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
});
