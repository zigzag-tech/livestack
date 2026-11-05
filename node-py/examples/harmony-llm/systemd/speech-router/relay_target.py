#!/usr/bin/env python3
"""add|remove the relay target harmony-asr in the relay settings, touching nothing else.
Run as root. Prints only what it did, never a value from the file (it holds secrets)."""
import json, os, sys, tempfile
CFG = "/etc/benchday/relay/regional-relay-zz-tower2-cn.json"
ENTRY = "harmony-asr=http://127.0.0.1:8189"
op = sys.argv[1]
st = os.stat(CFG)
d = json.load(open(CFG))
cur = d["settings"].get("speechRelayAsrTargets", "")
items = [x for x in cur.split(",") if x]
has = ENTRY in items
if op == "add" and not has:
    items.append(ENTRY)
elif op == "remove" and has:
    items.remove(ENTRY)
else:
    print("no change (%s; target %s)" % (op, "present" if has else "absent"))
    sys.exit(0)
d["settings"]["speechRelayAsrTargets"] = ",".join(items)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CFG))
with os.fdopen(fd, "w") as f:
    json.dump(d, f, indent=2)
os.chmod(tmp, st.st_mode & 0o7777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, CFG)
print("%s: harmony-asr target; asr targets now %d" % (op, len(items)))
