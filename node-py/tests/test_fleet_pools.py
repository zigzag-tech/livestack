"""Elastic pools: the operator's statement of what may be rented, and where.

The failure mode this configuration has to avoid is the quiet one. A pool list
with a typo that half-parses is a fleet that bursts into the wrong region at the
wrong price and reports nothing — so every case here checks that a bad value
yields NO pools and says so, rather than some pools and silence.
"""
from livestack_node.fleet_pools import Pool, parse_pools, pool_targets, spec_for
from livestack_node.fleet_scheduler import Tier

GOOD = ('[{"id":"heyuan-spot","provider":"aliyun","tier":"SPOT",'
        ' "region":"cn-heyuan","instance_type":"ecs.g8i.2xlarge",'
        ' "cost_per_hour":2.1,"max_instances":2,"kinds":["llm"]},'
        ' {"id":"heyuan-od","provider":"aliyun","tier":"ONDEMAND",'
        '  "region":"cn-heyuan","instance_type":"ecs.g8i.4xlarge",'
        '  "cost_per_hour":4.2,"max_instances":1}]')


def test_a_well_formed_declaration_parses_and_is_announced():
    said = []
    pools = parse_pools(GOOD, log=said.append)
    assert [p.id for p in pools] == ["heyuan-spot", "heyuan-od"]
    assert pools[0].tier is Tier.SPOT and pools[1].tier is Tier.ONDEMAND
    # The startup line names the price and the ceiling: the two numbers an
    # operator needs to recognise a mistake before it is billed.
    assert "¥2.1/h x2" in said[0]


def test_nothing_declared_is_no_pools_and_no_noise():
    said = []
    assert parse_pools("", log=said.append) == ()
    assert said == []


def test_a_malformed_value_yields_no_pools_and_says_why():
    """The precedent is hostd's account quota: systemd ate the quotes, the
    broker crash-looped. A bad pool list must degrade loudly, not partly."""
    said = []
    assert parse_pools('[{"id": bad}]', log=said.append) == ()
    assert "malformed" in said[0] and "cannot burst" in said[0]


def test_a_partly_valid_list_yields_nothing_rather_than_the_valid_half():
    said = []
    assert parse_pools('[{"id":"a","provider":"aliyun","region":"cn"},'
                       ' {"provider":"aliyun","region":"cn"}]',
                       log=said.append) == ()
    assert "malformed" in said[0]


def test_an_unknown_key_is_refused_rather_than_ignored():
    """A misspelled key that is silently dropped is a pool that costs a
    different amount than the unit file says it does."""
    said = []
    assert parse_pools('[{"id":"a","provider":"aliyun","region":"cn",'
                       ' "instance_type":"x","cost_per_hr":9}]',
                       log=said.append) == ()
    assert "cost_per_hr" in said[0]


def test_an_unknown_tier_is_refused():
    assert parse_pools('[{"id":"a","provider":"aliyun","region":"cn",'
                       ' "instance_type":"x","tier":"CHEAP"}]') == ()


def test_duplicate_ids_are_refused():
    said = []
    assert parse_pools('[{"id":"a","provider":"p","region":"cn","instance_type":"x"},'
                       ' {"id":"a","provider":"p","region":"cn","instance_type":"y"}]',
                       log=said.append) == ()
    assert "duplicate" in said[0]


# --- becoming targets -------------------------------------------------------
def test_a_pool_at_its_ceiling_is_excluded_with_the_count():
    pools = parse_pools(GOOD)
    targets, excluded = pool_targets(pools, running_instances={"heyuan-spot": 2})
    assert [t.id for t in targets] == ["heyuan-od"]
    assert excluded[0] == {"pool_id": "heyuan-spot",
                           "reason": "at its ceiling (2 of 2 up)"}


def test_a_pool_outside_the_region_policy_is_excluded_by_name():
    targets, excluded = pool_targets(parse_pools(GOOD), allow_regions=("na",))
    assert targets == ()
    assert all("caller allows na" in e["reason"] for e in excluded)


def test_a_pool_declaring_no_kinds_serves_anything():
    targets, _ = pool_targets(parse_pools(GOOD), kinds=("asr",))
    assert [t.id for t in targets] == ["heyuan-od"]


def test_an_elastic_target_carries_no_distance_and_no_utilization():
    """It does not exist yet. `None` is scored as the worst measured candidate,
    which is the honest reading: a machine that is not up cannot be near."""
    targets, _ = pool_targets(parse_pools(GOOD))
    assert all(t.distance_ms is None and t.utilization is None for t in targets)
    assert all(t.elastic and not t.running for t in targets)


def test_the_spec_carries_the_pool_env_and_the_callers_announce_env():
    pools = parse_pools('[{"id":"a","provider":"aliyun","region":"cn-heyuan",'
                        ' "instance_type":"x","env":{"A":"1"}}]')
    spec = spec_for(pools[0], announce_env={"LIVESTACK_BROKER_URL": "http://b"})
    assert spec.announce_env == {"A": "1", "LIVESTACK_BROKER_URL": "http://b"}
    assert spec.name_prefix == "livestack-a"
