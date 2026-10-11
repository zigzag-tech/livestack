"""Fixed source-bound retirement diagnostics; this CLI never stops a unit."""
import argparse
import json
import os
from pathlib import Path
import stat

from .broker_predecessor import inspect_predecessor, verify_retained_files
from .broker_retirement import BrokerRetirementJournal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', required=True)
    parser.add_argument('--operation', required=True, choices=('prepare', 'status', 'reconcile'))
    parser.add_argument('--unit')
    args = parser.parse_args()
    path = Path(args.configuration)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
            raise PermissionError('broker_registered_private_configuration_required')
        content = os.read(fd, 65537)
        if len(content) > 65536:
            raise ValueError('broker_configuration_bound')
    finally:
        os.close(fd)
    configuration = json.loads(content)
    if not isinstance(configuration, dict) or set(configuration) != {'version', 'plan', 'registeredInventory', 'journalPath'} or configuration['version'] != 1:
        raise ValueError('broker_closed_retirement_configuration_required')
    plan = configuration['plan']; inventory = configuration['registeredInventory']
    verify_retained_files(plan, inventory)
    journal = BrokerRetirementJournal(configuration['journalPath'], plan, inventory)
    if args.operation == 'prepare':
        if args.unit is not None:
            raise ValueError('broker_prepare_all_registered_units_required')
        result = {'planSha256': journal.digest,
                  'predecessors': [inspect_predecessor(row) for row in plan['units']],
                  'prepared': True, 'activationAllowed': False, 'modelEffectsAllowed': False}
    elif args.operation == 'status':
        if args.unit is not None:
            raise ValueError('broker_status_all_registered_units_required')
        result = journal.status()
    else:
        if args.unit is None:
            raise ValueError('broker_exact_reconciliation_unit_required')
        result = journal.reconcile(args.unit)
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
