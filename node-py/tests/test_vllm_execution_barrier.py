"""Receipt vectors, not GPU qualification.

These unit controls isolate nonce/rank/configuration refusals that a real GPU
cannot deliberately emit. Deployment still requires real engine IPC evidence.
"""
import unittest
from types import SimpleNamespace

from livestack_node.decisions.contract import ContractError
from livestack_node.decisions.vllm_execution_barrier import abort_and_confirm


class Engine:
    def __init__(self, wrong_nonce=False):
        cls = type('AsyncMPClient', (), {'__module__': 'vllm.v1.engine.core_client'})
        self.engine_core = cls()
        self.vllm_config = SimpleNamespace(max_concurrent_batches=1,
            scheduler_config=SimpleNamespace(async_scheduling=False),
            parallel_config=SimpleNamespace(data_parallel_size=1, pipeline_parallel_size=1,
                tensor_parallel_size=1, worker_extension_cls=
                'livestack_node.decisions.vllm_execution_barrier.ExtractionExecutionBarrier'))
        self.events = []
        self.wrong_nonce = wrong_nonce

    async def abort(self, request_id):
        self.events.append(('abort', request_id))

    async def collective_rpc(self, method, *, timeout, args):
        self.events.append(('barrier', method))
        return [{'nonce': '0'*32 if self.wrong_nonce else args[0], 'worker_pid': 123,
                 'device': 'cuda:0', 'synchronized': True}]


class BarrierVectors(unittest.IsolatedAsyncioTestCase):
    async def test_correlated_receipt_requires_abort_before_barrier(self):
        engine = Engine()
        receipts = await abort_and_confirm(engine, 'chatcmpl-test', workers=1)
        self.assertEqual([row[0] for row in engine.events], ['abort', 'barrier'])
        self.assertEqual(len(receipts), 1)

    async def test_uncorrelated_receipt_is_not_cessation(self):
        with self.assertRaisesRegex(ContractError, 'receipt is invalid'):
            await abort_and_confirm(Engine(wrong_nonce=True), 'chatcmpl-test', workers=1)

    async def test_deferred_batches_refuse_before_abort(self):
        for field in ['async', 'batches', 'parallel']:
            engine = Engine()
            if field == 'async': engine.vllm_config.scheduler_config.async_scheduling = True
            if field == 'batches': engine.vllm_config.max_concurrent_batches = 2
            if field == 'parallel': engine.vllm_config.parallel_config.data_parallel_size = 2
            with self.assertRaisesRegex(ContractError, 'qualified synchronous'):
                await abort_and_confirm(engine, 'chatcmpl-test', workers=1)
            self.assertEqual(engine.events, [])

    async def test_unknown_engine_refuses_without_gpu_work(self):
        engine = Engine(); engine.engine_core = object()
        with self.assertRaisesRegex(ContractError, 'single-engine IPC'):
            await abort_and_confirm(engine, 'chatcmpl-test', workers=1)
        self.assertEqual(engine.events, [])


if __name__ == '__main__': unittest.main()
