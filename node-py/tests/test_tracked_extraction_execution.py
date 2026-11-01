"""Adversarial task/receipt controls, not vLLM or GPU qualification.

These isolate cancellation-resistant rendering and deliberately invalid worker
receipts. Real HTTP ingress controls cannot force these engine-internal states.
"""
import asyncio
import unittest

from livestack_node.decisions.contract import ContractError
from livestack_node.decisions.execution_uncertain import ExecutionUnconfirmed
from livestack_node.decisions.tracked_extraction_execution import TrackedExtractionExecution
from test_vllm_execution_barrier import Engine


class TrackedVectors(unittest.IsolatedAsyncioTestCase):
    async def test_normal_completion_releases_owned_slot(self):
        engine = Engine(); control = TrackedExtractionExecution(engine)
        async def start(identity): return identity
        self.assertEqual(await control.execute('chatcmpl-test', start, timeout_seconds=1), 'chatcmpl-test')
        self.assertIsNone(control.active)
        self.assertEqual(engine.events, [])

    async def test_deadline_collects_renderer_before_gpu_barrier(self):
        engine = Engine(); control = TrackedExtractionExecution(engine)
        async def start(identity):
            try: await asyncio.Event().wait()
            finally: engine.events.append(('renderer-finished', identity))
        with self.assertRaisesRegex(ContractError, 'confirmed cessation'):
            await control.execute('chatcmpl-test', start, timeout_seconds=.01)
        self.assertEqual([x[0] for x in engine.events], ['renderer-finished', 'abort', 'barrier'])
        self.assertIsNone(control.active)

    async def test_bad_gpu_receipt_retains_slot_and_prevents_redispatch(self):
        engine = Engine(wrong_nonce=True); control = TrackedExtractionExecution(engine)
        calls = []
        async def start(identity):
            calls.append(identity); await asyncio.Event().wait()
        with self.assertRaises(ExecutionUnconfirmed):
            await control.execute('chatcmpl-test', start, timeout_seconds=.01)
        self.assertIsNotNone(control.active)
        with self.assertRaisesRegex(ContractError, 'owns its slot'):
            await control.execute('chatcmpl-second', start, timeout_seconds=1)
        self.assertEqual(calls, ['chatcmpl-test'])

    async def test_unsupported_launch_refuses_before_renderer(self):
        engine = Engine(); engine.vllm_config.scheduler_config.async_scheduling = True
        control = TrackedExtractionExecution(engine); calls = []
        async def start(identity): calls.append(identity)
        with self.assertRaisesRegex(ContractError, 'qualified synchronous'):
            await control.execute('chatcmpl-test', start, timeout_seconds=1)
        self.assertEqual(calls, [])

    async def test_uncollected_renderer_cannot_be_followed_by_gpu_barrier(self):
        engine = Engine(); control = TrackedExtractionExecution(engine)
        release = asyncio.Event()
        async def start(identity):
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: await release.wait()
        try:
            with self.assertRaisesRegex(ExecutionUnconfirmed, 'frontend cancellation unconfirmed'):
                await control.execute('chatcmpl-test', start, timeout_seconds=.01)
            self.assertIsNotNone(control.active)
            self.assertEqual(engine.events, [])
        finally:
            release.set()
            if control.active is not None: await control.active[1]


if __name__ == '__main__': unittest.main()
