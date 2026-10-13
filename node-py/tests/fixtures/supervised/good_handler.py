from livestack_node.workloads.supervised.supervised import supervised

CONTRACT = {'max_silence_seconds': 60, 'min_interval_seconds': 1}
SCENARIO = {'exec': {'duration_s': 600, 'chunk_gap_s': 20}, 'wait': {'duration_s': 40}}


@supervised(CONTRACT)
def handler(ctx):
    ctx.phase('prepare', lambda: ctx.wait(lambda: 'ready', 'cache lookup'))
    ctx.each(['a', 'b', 'c'], lambda item: ctx.exec('build', [item]), unit='targets')
