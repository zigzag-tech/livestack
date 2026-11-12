"""Native ranking projection controls using the real ranker.

Malformed-result guards deliberately patch its return boundary: a correct
ranker cannot naturally emit malformed fields, so integration positives alone
cannot demonstrate that these producer guards refuse a future bad result.
Live realm admission remains a separate daemon/Hub isolated-E2E requirement.
"""
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from livestack_node.streams import fleet_rank_stream as source
from livestack_node.streams.runtime import StreamsRuntime
from test_streams_producers import REAL_FLEET


class FleetRankProjectionTests(unittest.TestCase):
    triple = ("llm", "na", "host:tower")

    def body(self, view=None, now=120.1):
        return source.ranking_body(copy.deepcopy(REAL_FLEET if view is None else view), self.triple, now)

    def test_native_body_identity_and_stable_content_sensitive_decision(self):
        first = self.body()
        self.assertEqual(source.CONTRACT, "harmony.fleet.rank/1")
        self.assertEqual(first["region"], "na")
        self.assertRegex(first["decision_id"], r"^fr_[a-f0-9]{64}$")
        self.assertEqual(self.body(), first)
        same_second = self.body(now=120.9)
        self.assertNotEqual(first["decision_id"], same_second["decision_id"])
        view = copy.deepcopy(REAL_FLEET)
        view["hosts"]["tower"]["nodes"][0]["peer"] = "http://different/livestack"
        changed = self.body(view)
        self.assertEqual(first["generated_at"], changed["generated_at"])
        self.assertNotEqual(first["decision_id"], changed["decision_id"])

    def test_configuration_refuses_second_vantage_and_preserves_first_32_pairs(self):
        logs = []
        pairs = source.parse_pairs("llm:na=host:first,llm:na=host:second", logs.append)
        self.assertEqual(pairs, [("llm", "na", "host:first")])
        self.assertTrue(any("fleet_rank_duplicate_pair" in row for row in logs))
        pairs = source.parse_pairs(",".join(f"llm:r{i}=host:tower" for i in range(33)), logs.append)
        self.assertEqual([pair[1] for pair in pairs], [f"r{i}" for i in range(32)])
        self.assertTrue(any("fleet_rank_pair_limit" in row for row in logs))
        runtime = StreamsRuntime(None, logs.append, "tower").attach(
            fleet_rank_pairs=[("llm", f"r{i}", "host:tower") for i in range(33)])
        self.assertEqual(len(runtime.fleet_rank_pairs), 32)

    def test_configuration_input_fields_and_controls_are_bounded(self):
        logs = []
        for config in ["x" * 32769, f"{'k' * 33}:na=host:tower", "llm:na=host:\x01bad"]:
            self.assertEqual(source.parse_pairs(config, logs.append), [])
        self.assertTrue(all(logs))
        self.assertEqual(source.bounded_pairs([("llm", "na", "one"), ("llm", "na", "two")], logs.append),
                         [("llm", "na", "one")])

    def test_typed_preferences_refuse_hard_eligibility_maps_and_contradictory_metrics(self):
        self.assertTrue(source._valid_preference_clause({"attribute": "asr.streaming", "value": "native"}))
        self.assertTrue(source._valid_preference_clause({"attribute": "asr.languages", "value": ["en", "zh"]}))
        self.assertTrue(source._valid_preference_clause({"metric": "llm.decode_tok_s", "direction": "max"}))
        for clause in [{"class": "embed", "model": "google/embeddinggemma-2"},
                       {"metric": "llm.params_b", "direction": "min"},
                       {"metric": "asr.quality", "direction": "max"},
                       {"attribute": "asr.streaming", "value": "x" * 129},
                       {"attribute": "asr.languages", "value": [""]}]:
            with self.subTest(clause=clause):
                self.assertFalse(source._valid_preference_clause(clause))

    def test_defensive_body_and_receipt_validation_names_every_refusal(self):
        valid = source.fleet_rank.rank(copy.deepcopy(REAL_FLEET), "llm", vantage="host:tower", now=120.1)
        cases = []
        bad = copy.deepcopy(valid); bad["prefer"] = [{"model": "ineligible-map"}]; cases.append(bad)
        bad = copy.deepcopy(valid); bad["targets"][0]["preferences"] = [{"clause": {"attribute": "model", "value": "x"}, "comparable": True}]; cases.append(bad)
        bad = copy.deepcopy(valid); bad["targets"][0]["target_id"] = "x" * 257; cases.append(bad)
        bad = copy.deepcopy(valid); bad["targets"] *= 65; cases.append(bad)
        bad = copy.deepcopy(valid); bad["generated_at"] = float("nan"); cases.append(bad)
        cases.extend([None, {}, {"targets": []}])
        for result in cases:
            with self.subTest(result=str(result)[:100]), patch.object(source.fleet_rank, "rank", return_value=result):
                with self.assertRaises(source.RankingFailure) as refusal:
                    source.ranking_body({}, self.triple, 120.1)
                self.assertTrue(refusal.exception.code.startswith("fleet_rank_"))

    def test_actual_empty_result_is_absence_and_compute_exception_is_failure(self):
        with self.assertRaises(source.RankingAbsent):
            self.body({"hosts": {}})
        with patch.object(source.fleet_rank, "rank", side_effect=RuntimeError("broken")):
            with self.assertRaises(source.RankingFailure) as refusal:
                self.body()
        self.assertEqual(refusal.exception.code, "fleet_rank_compute_failed")
