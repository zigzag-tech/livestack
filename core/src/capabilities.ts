export interface LivestackCapability {
  kind: string;
  hostId: string;
  slots: number;
  labels?: Record<string, string>;
  leaseTtlSeconds?: number;
}

export interface CapabilityRequirement {
  kind: string;
  labels?: Record<string, string>;
  hostIds?: string[];
}

export interface CapabilityLease {
  leaseId: string;
  capabilityKind: string;
  hostId: string;
  ownerId: string;
  acquiredAt: string;
  heartbeatAt: string;
  expiresAt: string;
  labels?: Record<string, string>;
}

export interface CapabilityLeaseStore {
  register(capability: LivestackCapability): void;
  unregister(hostId: string, kind?: string): void;
  listCapabilities(): LivestackCapability[];
  listLeases(): CapabilityLease[];
  acquireLease(input: {
    requirement: CapabilityRequirement;
    ownerId: string;
    ttlSeconds?: number;
    now?: Date;
  }): CapabilityLease | null;
  heartbeatLease(leaseId: string, input?: {
    ttlSeconds?: number;
    now?: Date;
  }): CapabilityLease | null;
  releaseLease(leaseId: string): boolean;
  reapExpired(now?: Date): CapabilityLease[];
}

export class InMemoryCapabilityLeaseStore implements CapabilityLeaseStore {
  private capabilities = new Map<string, LivestackCapability>();
  private leases = new Map<string, CapabilityLease>();
  private nextLeaseId = 1;

  register(capability: LivestackCapability): void {
    if (capability.slots < 1 || !Number.isInteger(capability.slots)) {
      throw new Error(`Capability ${capability.kind} on ${capability.hostId} must have a positive integer slot count.`);
    }
    this.capabilities.set(capabilityKey(capability.hostId, capability.kind), {
      ...capability,
      labels: { ...(capability.labels ?? {}) },
    });
  }

  unregister(hostId: string, kind?: string): void {
    for (const key of this.capabilities.keys()) {
      const [capabilityHostId, capabilityKind] = splitCapabilityKey(key);
      if (capabilityHostId === hostId && (!kind || capabilityKind === kind)) {
        this.capabilities.delete(key);
      }
    }
    for (const [leaseId, lease] of this.leases) {
      if (lease.hostId === hostId && (!kind || lease.capabilityKind === kind)) {
        this.leases.delete(leaseId);
      }
    }
  }

  listCapabilities(): LivestackCapability[] {
    return Array.from(this.capabilities.values()).map((capability) => ({
      ...capability,
      labels: { ...(capability.labels ?? {}) },
    }));
  }

  listLeases(): CapabilityLease[] {
    return Array.from(this.leases.values()).map(cloneLease);
  }

  acquireLease(input: {
    requirement: CapabilityRequirement;
    ownerId: string;
    ttlSeconds?: number;
    now?: Date;
  }): CapabilityLease | null {
    const now = input.now ?? new Date();
    this.reapExpired(now);

    const capability = this.listCapabilities()
      .filter((candidate) => matchesRequirement(candidate, input.requirement))
      .sort(compareCapabilityCandidates)
      .find((candidate) => this.activeLeaseCount(candidate.hostId, candidate.kind) < candidate.slots);
    if (!capability) return null;

    const ttlSeconds = input.ttlSeconds ?? capability.leaseTtlSeconds ?? 300;
    const lease: CapabilityLease = {
      leaseId: `lease-${this.nextLeaseId++}`,
      capabilityKind: capability.kind,
      hostId: capability.hostId,
      ownerId: input.ownerId,
      acquiredAt: now.toISOString(),
      heartbeatAt: now.toISOString(),
      expiresAt: new Date(now.getTime() + ttlSeconds * 1000).toISOString(),
      labels: { ...(capability.labels ?? {}) },
    };
    this.leases.set(lease.leaseId, lease);
    return cloneLease(lease);
  }

  heartbeatLease(leaseId: string, input: {
    ttlSeconds?: number;
    now?: Date;
  } = {}): CapabilityLease | null {
    const lease = this.leases.get(leaseId);
    if (!lease) return null;
    const capability = this.capabilities.get(capabilityKey(lease.hostId, lease.capabilityKind));
    const now = input.now ?? new Date();
    const ttlSeconds = input.ttlSeconds ?? capability?.leaseTtlSeconds ?? 300;
    const nextLease = {
      ...lease,
      heartbeatAt: now.toISOString(),
      expiresAt: new Date(now.getTime() + ttlSeconds * 1000).toISOString(),
    };
    this.leases.set(leaseId, nextLease);
    return cloneLease(nextLease);
  }

  releaseLease(leaseId: string): boolean {
    return this.leases.delete(leaseId);
  }

  reapExpired(now: Date = new Date()): CapabilityLease[] {
    const expired: CapabilityLease[] = [];
    for (const [leaseId, lease] of this.leases) {
      if (Date.parse(lease.expiresAt) <= now.getTime()) {
        expired.push(cloneLease(lease));
        this.leases.delete(leaseId);
      }
    }
    return expired;
  }

  private activeLeaseCount(hostId: string, kind: string): number {
    return Array.from(this.leases.values()).filter((lease) =>
      lease.hostId === hostId && lease.capabilityKind === kind
    ).length;
  }
}

export function matchesRequirement(
  capability: LivestackCapability,
  requirement: CapabilityRequirement,
): boolean {
  if (capability.kind !== requirement.kind) return false;
  if (requirement.hostIds && !requirement.hostIds.includes(capability.hostId)) return false;
  return Object.entries(requirement.labels ?? {}).every(([key, value]) =>
    capability.labels?.[key] === value
  );
}

function compareCapabilityCandidates(a: LivestackCapability, b: LivestackCapability): number {
  const kind = a.kind.localeCompare(b.kind);
  if (kind !== 0) return kind;
  return a.hostId.localeCompare(b.hostId);
}

function capabilityKey(hostId: string, kind: string): string {
  return `${hostId}\0${kind}`;
}

function splitCapabilityKey(key: string): [string, string] {
  const index = key.indexOf("\0");
  return [key.slice(0, index), key.slice(index + 1)];
}

function cloneLease(lease: CapabilityLease): CapabilityLease {
  return {
    ...lease,
    labels: { ...(lease.labels ?? {}) },
  };
}
