"""CPU canonical planner controls; not a resource lease or live GPU proof."""
import unittest
from dataclasses import replace
from livestack_node.planner import Device, Unit, WorldState, Placement, Residency, Grant
from livestack_node.qualification_capacity import plan_qualification_capacity


class QualificationCapacityTests(unittest.TestCase):
    def setUp(self):
        self.device = Device('joe-gpu1', 'joe', {'vram_bytes': 24})
        self.original = Unit('original', {'vram_bytes': 8}, residency=Residency.HARD_PIN,
                             min_resident=1, servable_on=frozenset({'joe-gpu1'}))
        self.unit = Unit('qualified', {'vram_bytes': 4}, servable_on=frozenset({'joe-gpu1'}))
        self.world = WorldState(devices=(self.device,), units={'original': self.original},
            placements=(Placement('original', 'joe-gpu1', loaded_at=1, busy=True, leases=1),),
            measured_free={'joe-gpu1': {'vram_bytes': 16}}, now=100)

    def test_spare_capacity_plans_exact_target_without_mutating_original(self):
        result = plan_qualification_capacity(self.world, self.unit, 'joe-gpu1', 'job', 'owner')
        self.assertEqual(len(result.of(Grant)), 1)
        self.assertEqual(self.world.placements[0].leases, 1)
        self.assertNotIn('qualified', self.world.units)

    def test_missing_nan_or_insufficient_measurement_refuses(self):
        for measurement in ({}, {'vram_bytes': float('nan')}, {'vram_bytes': 3}, {'vram_bytes': True}):
            with self.assertRaises(ValueError):
                plan_qualification_capacity(replace(self.world, measured_free={'joe-gpu1': measurement}), self.unit, 'joe-gpu1', 'job', 'owner')

    def test_wrong_device_or_ambiguous_unit_refuses(self):
        with self.assertRaises(ValueError):
            plan_qualification_capacity(self.world, self.unit, 'joe-gpu0', 'job', 'owner')
        with self.assertRaises(ValueError):
            plan_qualification_capacity(replace(self.world, units={**self.world.units, 'qualified': replace(self.unit, footprint={'vram_bytes': 1})}), self.unit, 'joe-gpu1', 'job', 'owner')

    def test_unrelated_restoration_is_refused_before_dispatch(self):
        missing = Unit('unrelated', {'vram_bytes': 2}, residency=Residency.HARD_PIN, min_resident=1,
                       servable_on=frozenset({'joe-gpu1'}))
        with self.assertRaisesRegex(ValueError, 'unrelated_residency'):
            plan_qualification_capacity(replace(self.world, units={**self.world.units, 'unrelated': missing}), self.unit, 'joe-gpu1', 'job', 'owner')

if __name__ == '__main__':
    unittest.main()
