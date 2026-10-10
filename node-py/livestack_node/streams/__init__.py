"""Harmony's native stream producers (openspec services-own-their-streams, tasks 3.1/3.2/4.1/4.3).

Everything here is OFF unless `HARMONY_STREAMS=1` (see `hostd_streams`). The modules other than
`hostd_streams` are standard library only and import nothing outside this directory, so benchday's
isolated lane can copy the directory and drive it against the real daemon (`python3 -m <dir>.lane`).
"""
