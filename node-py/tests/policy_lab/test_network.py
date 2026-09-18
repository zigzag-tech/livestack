from fractions import Fraction

from livestack_node.policy_lab.network import DirectedLink, NetworkModel


def test_s12_two_transfers_conserve_bytes_under_shared_bottleneck():
    network = NetworkModel(
        [DirectedLink("wan", "a", "b", bits_per_second=8_000_000, propagation_us=0)]
    )
    network.start_transfer("one", source="a", destination="b", path=("wan",), byte_count=1_000_000, at_us=0)
    network.advance_to(500_000)
    assert network.transfer("one").remaining_bits == Fraction(4_000_000)

    network.start_transfer("two", source="a", destination="b", path=("wan",), byte_count=1_000_000, at_us=500_000)
    assert network.rates_bps() == {"one": Fraction(4_000_000), "two": Fraction(4_000_000)}
    network.advance_to(1_500_000)
    assert network.transfer("one").data_finished_at_us == 1_500_000
    assert network.transfer("two").remaining_bits == Fraction(4_000_000)
    network.advance_to(2_000_000)
    assert network.transfer("two").data_finished_at_us == 2_000_000
    assert network.total_delivered_bytes == 2_000_000


def test_endpoint_caps_and_shared_contention_group_bound_aggregate_rate():
    network = NetworkModel(
        [
            DirectedLink("left", "a", "relay", 20_000_000, 0, contention_group="wan"),
            DirectedLink("right", "relay", "b", 20_000_000, 0, contention_group="wan"),
        ],
        egress_caps={"a": 6_000_000},
    )
    network.start_transfer("x", source="a", destination="b", path=("left", "right"), byte_count=1000, at_us=0)
    network.start_transfer("y", source="a", destination="b", path=("left", "right"), byte_count=1000, at_us=0)
    rates = network.rates_bps()
    assert sum(rates.values()) == 6_000_000


def test_s13_remote_coordinator_charges_control_but_payload_uses_local_route():
    network = NetworkModel(
        [DirectedLink("local", "client", "worker", 8_000_000, 10_000)]
    )
    decision_ready = network.control_admission_ready(at_us=0, control_rtt_us=80_000)
    transfer = network.start_transfer(
        "payload",
        source="client",
        destination="worker",
        path=("local",),
        byte_count=1000,
        at_us=decision_ready,
        connection_rtt_us=20_000,
        route_id="local-direct",
    )
    assert transfer.data_start_us == 100_000
    assert transfer.propagation_us == 10_000
    assert transfer.path == ("local",)


def test_connection_reuse_and_cache_hit_avoid_artificial_per_chunk_rtt():
    network = NetworkModel(
        [DirectedLink("route", "a", "b", 8_000_000, 5_000)]
    )
    first = network.start_transfer(
        "first", source="a", destination="b", path=("route",), byte_count=100, at_us=0,
        connection_rtt_us=20_000, route_id="stream"
    )
    second = network.start_transfer(
        "second", source="a", destination="b", path=("route",), byte_count=100, at_us=20_000,
        connection_rtt_us=20_000, route_id="stream"
    )
    assert first.data_start_us == 20_000
    assert second.data_start_us == 20_000

    network.record_cache("digest", "b")
    assert network.required_transfer("digest", destination="b") is False
    assert network.required_transfer("other", destination="b") is True
