import hashlib
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from livestack_node.owned_executor_jobs import JobConflict, OwnedExecutorJobs


class OwnedExecutorJobsTest(unittest.TestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.barriers = []
        self.jobs = OwnedExecutorJobs(self.executor, lambda: self.barriers.append('settled'))
        self.job_id = str(uuid.uuid4())
        self.token = 'test-control-secret-' * 3

    def tearDown(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def submit(self, worker, fingerprint='a' * 64):
        return self.jobs.submit(self.job_id, 'owner-a', self.token, fingerprint, 'fake', worker)

    def finish(self):
        # A task scheduled after the owned task cannot start until that task's
        # settlement callback finished on the shared serial executor.
        self.executor.submit(lambda: None).result(timeout=2)
        return self.jobs.status(self.job_id, self.token)

    def test_cancelled_does_not_mean_stopped_until_executor_and_barrier_settle(self):
        entered, return_now = threading.Event(), threading.Event()
        def work(signal):
            entered.set()
            return_now.wait(timeout=2)
            signal.check()
            return b'must-not-be-returned'
        self.submit(work)
        self.assertTrue(entered.wait(timeout=2))
        requested = self.jobs.cancel(self.job_id, self.token, 'owner-a')
        self.assertEqual(requested['state'], 'cancel_requested')
        self.assertTrue(requested['executor_active'])
        self.assertFalse(requested['physical_settled'])
        self.assertIsNone(requested['settled_at'])
        return_now.set()
        settled = self.finish()
        self.assertEqual(settled['state'], 'cancelled')
        self.assertFalse(settled['executor_active'])
        self.assertTrue(settled['physical_settled'])
        self.assertEqual(self.barriers, ['settled'])
        with self.assertRaises(JobConflict):
            self.jobs.result(self.job_id, self.token)

    def test_cancel_before_submit_tombstone_forbids_late_acceptance(self):
        receipt = self.jobs.cancel(self.job_id, self.token, 'owner-a')
        calls = []
        self.submit(lambda signal: calls.append('gpu') or b'wav')
        settled = self.finish()
        self.assertEqual(settled['state'], 'cancelled')
        self.assertEqual(calls, [])
        self.assertEqual(self.barriers, [])
        self.assertTrue(receipt['physical_settled'])

    def test_owned_retry_is_idempotent_and_changed_request_or_owner_is_refused(self):
        calls = []
        self.submit(lambda signal: calls.append('gpu') or b'original-wav')
        self.finish()
        self.submit(lambda signal: calls.append('duplicate') or b'wrong-wav')
        settled = self.finish()
        self.assertEqual(calls, ['gpu'])
        self.assertEqual(settled['result_sha256'], hashlib.sha256(b'original-wav').hexdigest())
        with self.assertRaises(JobConflict):
            self.submit(lambda signal: b'wrong', fingerprint='b' * 64)
        with self.assertRaises(KeyError):
            self.jobs.cancel(self.job_id, self.token, 'owner-b')
        with self.assertRaises(KeyError):
            self.jobs.status(self.job_id, 'different-secret' * 3)
        self.assertNotIn('control_token', settled)
        self.assertNotIn(self.token, str(settled))

    def test_cancels_only_the_named_queued_job_and_leaves_shared_work_running(self):
        started, unblock = threading.Event(), threading.Event()
        shared = self.executor.submit(lambda: (started.set(), unblock.wait(timeout=2)))
        self.assertTrue(started.wait(timeout=2))
        calls = []
        self.submit(lambda signal: calls.append('owned') or b'wav')
        receipt = self.jobs.cancel(self.job_id, self.token, 'owner-a')
        self.assertEqual(receipt['state'], 'cancelled')
        self.assertFalse(shared.done())
        unblock.set()
        self.finish()
        self.assertEqual(calls, [])

    def test_failed_accelerator_barrier_never_claims_physical_settlement(self):
        def failed_barrier():
            raise RuntimeError('synchronize failed')
        self.jobs = OwnedExecutorJobs(self.executor, failed_barrier)
        self.submit(lambda signal: b'wav')
        receipt = self.finish()
        self.assertEqual(receipt['state'], 'failed')
        self.assertFalse(receipt['physical_settled'])
        self.assertIsNone(receipt['settled_at'])
        with self.assertRaises(JobConflict):
            self.jobs.result(self.job_id, self.token)


if __name__ == '__main__':
    unittest.main()
