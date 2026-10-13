from livestack_node.workloads.supervised.supervised import supervised

# A single 300 s silent wait inside a contract that allows 60 s: the structure the proof must reject.
SCENARIO = {'wait': {'duration_s': 300}}


@supervised({'max_silence_seconds': 60})
def handler(ctx):
    ctx.wait(lambda: 'x', 'one very long step')
