"""Broker-owned private control socket; registration remains an operator task."""
import json
import os
from pathlib import Path
import stat

from .broker_owner import BrokerOwnerGate
from .broker_owner_journal import BrokerOwnerJournal
from .provider_owner_socket import ProviderOwnerSocket


def configure_broker_owner(broker, configuration_path, source_identity, incarnation):
    """Installed source-bound launcher supplies identity; config grants nothing.

    The canonical deployment authorization is checked by the fixed owner
    command before reaching this same-UID private socket. Initial ingress is
    held, and existing remote uncertainty is never reconciled by bootstrap.
    """
    path = Path(configuration_path)
    if not path.is_absolute() or path.resolve() != path:
        raise PermissionError('broker_owner_canonical_configuration_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise PermissionError('broker_owner_private_configuration_required')
        payload = os.read(fd, 16385)
        if len(payload) > 16384:
            raise ValueError('broker_owner_configuration_bound')
    finally:
        os.close(fd)
    selected = json.loads(payload)
    if not isinstance(selected, dict) or set(selected) != {'version', 'controlSocket', 'journalPath', 'sourceIdentity', 'startupHolder'} or selected['version'] != 1:
        raise ValueError('broker_owner_closed_configuration_required')
    if selected['sourceIdentity'] != source_identity or not isinstance(source_identity, dict) or set(source_identity) != {'sourceSha256'}:
        raise ValueError('broker_owner_registered_source_changed')
    holder = selected['startupHolder']
    if not isinstance(holder, str) or not 1 <= len(holder) <= 128:
        raise ValueError('broker_owner_startup_holder_required')
    # Refuse unsupported provisioning before creating a socket or journal.
    if getattr(broker, 'fleet_pools', ()):
        raise ValueError('broker_owner_untracked_provisioning_requires_separate_gate')
    control = ProviderOwnerSocket(selected['controlSocket'], str(path), source_identity,
                                  lambda: gate.shutdown_owned(), lambda: None)
    journal = BrokerOwnerJournal(selected['journalPath'], source_identity['sourceSha256'], incarnation)
    gate = BrokerOwnerGate(broker, control.authorize, journal=journal)
    gate.fence.hold(control._credential, holder)
    control.start(gate.fence)
    return gate, control


def configured_owned_broker_app(broker, qualification_path, reservation_path,
                                owner_path, source_identity, incarnation):
    """The installed launcher supplies the independently verified owner tuple."""
    from .hostd import configured_broker_app
    if not owner_path:
        if source_identity is not None or incarnation is not None:
            raise ValueError('paired_broker_owner_startup_required')
        return configured_broker_app(broker, qualification_path, reservation_path), None
    if not source_identity or not incarnation:
        raise ValueError('paired_broker_owner_startup_required')
    gate, control = configure_broker_owner(broker, owner_path, source_identity, incarnation)
    try:
        app = configured_broker_app(broker, qualification_path, reservation_path, owner_gate=gate)
    except BaseException:
        control.close()
        raise
    return app, control
