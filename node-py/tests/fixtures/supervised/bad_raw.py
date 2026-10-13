import subprocess
import time
from livestack_node.workloads.supervised.supervised import supervised


@supervised({'max_silence_seconds': 60})
def handler(ctx):
    time.sleep(1)
    subprocess.run(['true'])
