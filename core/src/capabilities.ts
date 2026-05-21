import { z } from "zod";

export const WorkerCapabilitySchema = z.object({
  kind: z.string().min(1),
  hostId: z.string().min(1),
  slots: z.number().int().positive().default(1),
  labels: z.record(z.string(), z.string()).default({}),
  leaseTtlSeconds: z.number().int().positive().optional(),
  localService: z.object({
    baseUrl: z.string().url(),
    healthPath: z.string().startsWith("/").default("/health"),
  }).optional(),
});

export type WorkerCapability = z.infer<typeof WorkerCapabilitySchema>;

export const CapabilityRequirementSchema = z.object({
  kind: z.string().min(1),
  minSlots: z.number().int().positive().default(1),
  labels: z.record(z.string(), z.string()).default({}),
  hostIds: z.array(z.string().min(1)).optional(),
});

export type CapabilityRequirement = z.input<typeof CapabilityRequirementSchema>;

export interface LivestackCapability {
  kind: string;
  hostId: string;
  slots: number;
  labels?: Record<string, string>;
  leaseTtlSeconds?: number;
  localService?: {
    baseUrl: string;
    healthPath?: string;
  };
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
  rawRequirement: CapabilityRequirement | Record<string, unknown>,
): boolean {
  const requirement = CapabilityRequirementSchema.parse(rawRequirement);
  if (capability.kind !== requirement.kind) return false;
  if (capability.slots < requirement.minSlots) return false;
  if (requirement.hostIds && !requirement.hostIds.includes(capability.hostId)) return false;
  return Object.entries(requirement.labels ?? {}).every(([key, value]) =>
    capability.labels?.[key] === value
  );
}

export function matchesCapability(
  capability: WorkerCapability,
  requirement: CapabilityRequirement | Record<string, unknown>,
): boolean {
  return matchesRequirement(capability, requirement);
}

export function findMatchingCapabilities(
  capabilities: WorkerCapability[],
  requirement: CapabilityRequirement | Record<string, unknown>,
): WorkerCapability[] {
  const parsed = CapabilityRequirementSchema.parse(requirement);
  return capabilities.filter((capability) => matchesCapability(capability, parsed));
}

export class CapabilityError extends Error {
  public readonly code: "MISSING_CAPABILITY";
  public readonly requirement: CapabilityRequirement;

  constructor(
    message: string,
    code: "MISSING_CAPABILITY",
    requirement: CapabilityRequirement,
  ) {
    super(message);
    this.name = "CapabilityError";
    this.code = code;
    this.requirement = requirement;
  }
}

export function assertCapabilitiesAvailable(
  capabilities: WorkerCapability[],
  rawRequirements: Array<CapabilityRequirement | Record<string, unknown>>,
): void {
  for (const rawRequirement of rawRequirements) {
    const requirement = CapabilityRequirementSchema.parse(rawRequirement);
    const matches = findMatchingCapabilities(capabilities, requirement);
    if (matches.length === 0) {
      throw new CapabilityError(
        `No worker capability satisfies ${requirement.kind} ${JSON.stringify(requirement.labels)}`,
        "MISSING_CAPABILITY",
        requirement,
      );
    }
  }
}

export function parseWorkerCapabilitiesJson(
  value: string | undefined,
  defaultHostId?: string,
): WorkerCapability[] {
  if (!value?.trim()) return [];
  let raw: unknown;
  try {
    raw = JSON.parse(value);
  } catch (error) {
    throw new Error(`Worker capabilities must be a JSON array: ${(error as Error).message}`);
  }
  if (!Array.isArray(raw)) {
    throw new Error("Worker capabilities must be a JSON array");
  }
  return raw.map((capability) => {
    if (!capability || typeof capability !== "object") {
      throw new Error("Worker capability entries must be objects");
    }
    return WorkerCapabilitySchema.parse({
      ...(capability as Record<string, unknown>),
      ...(!("hostId" in capability) && defaultHostId ? { hostId: defaultHostId } : {}),
    });
  });
}

export interface ServiceLease {
  leaseId: string;
  capability: WorkerCapability;
  projectId: string;
  transformId?: string;
  acquiredAt: string;
  expiresAt: string;
  releasedAt?: string;
  status: "active" | "released" | "expired";
}

export interface AcquireServiceLeaseInput {
  projectId: string;
  requirement: CapabilityRequirement | Record<string, unknown>;
  transformId?: string;
  now?: Date;
}

export class ServiceLeaseError extends Error {
  public readonly code: "NO_CAPACITY" | "LEASE_NOT_FOUND";

  constructor(
    message: string,
    code: "NO_CAPACITY" | "LEASE_NOT_FOUND",
  ) {
    super(message);
    this.name = "ServiceLeaseError";
    this.code = code;
  }
}

export interface ServiceLeaseManager {
  acquire(input: AcquireServiceLeaseInput): ServiceLease | Promise<ServiceLease>;
  heartbeat(leaseId: string, now?: Date): ServiceLease | Promise<ServiceLease>;
  release(leaseId: string, now?: Date): ServiceLease | Promise<ServiceLease>;
  listActive(now?: Date): ServiceLease[] | Promise<ServiceLease[]>;
}

export interface ServiceLeaseStore {
  put(lease: ServiceLease): Promise<ServiceLease>;
  get(leaseId: string): Promise<ServiceLease | undefined>;
  listActive(now?: Date): Promise<ServiceLease[]>;
}

export class InMemoryServiceLeaseManager implements ServiceLeaseManager {
  private readonly leaseStore = new InMemoryCapabilityLeaseStore();
  private readonly serviceLeases = new Map<string, ServiceLease>();
  private readonly capabilities: WorkerCapability[];

  constructor(capabilities: WorkerCapability[]) {
    this.capabilities = capabilities.map((capability) => WorkerCapabilitySchema.parse(capability));
    for (const capability of this.capabilities) {
      this.leaseStore.register(capability);
    }
  }

  acquire(input: AcquireServiceLeaseInput): ServiceLease {
    const now = input.now ?? new Date();
    this.expireLeases(now);
    const requirement = CapabilityRequirementSchema.parse(input.requirement);
    const coreLease = this.leaseStore.acquireLease({
      requirement,
      ownerId: [input.projectId, input.transformId].filter(Boolean).join(":"),
      now,
    });
    if (!coreLease) {
      throw new ServiceLeaseError(`No capacity available for ${requirement.kind}`, "NO_CAPACITY");
    }
    const capability = this.findCapabilityForLease(coreLease);
    const lease: ServiceLease = {
      leaseId: coreLease.leaseId,
      capability,
      projectId: input.projectId,
      ...(input.transformId ? { transformId: input.transformId } : {}),
      acquiredAt: coreLease.acquiredAt,
      expiresAt: coreLease.expiresAt,
      status: "active",
    };
    this.serviceLeases.set(lease.leaseId, lease);
    return lease;
  }

  heartbeat(leaseId: string, now = new Date()): ServiceLease {
    const lease = this.requireLease(leaseId);
    if (lease.status !== "active") return lease;
    const coreLease = this.leaseStore.heartbeatLease(leaseId, { now });
    if (!coreLease) {
      throw new ServiceLeaseError(`Unknown service lease ${leaseId}`, "LEASE_NOT_FOUND");
    }
    const next = {
      ...lease,
      expiresAt: coreLease.expiresAt,
    };
    this.serviceLeases.set(leaseId, next);
    return next;
  }

  release(leaseId: string, now = new Date()): ServiceLease {
    const lease = this.requireLease(leaseId);
    if (lease.status !== "active") return lease;
    this.leaseStore.releaseLease(leaseId);
    const next: ServiceLease = {
      ...lease,
      releasedAt: now.toISOString(),
      status: "released",
    };
    this.serviceLeases.set(leaseId, next);
    return next;
  }

  listActive(now = new Date()): ServiceLease[] {
    this.expireLeases(now);
    return [...this.serviceLeases.values()].filter((lease) => lease.status === "active");
  }

  private expireLeases(now: Date): void {
    const expired = new Set(this.leaseStore.reapExpired(now).map((lease) => lease.leaseId));
    for (const lease of this.serviceLeases.values()) {
      if (lease.status !== "active") continue;
      if (!expired.has(lease.leaseId) && Date.parse(lease.expiresAt) > now.getTime()) continue;
      this.serviceLeases.set(lease.leaseId, { ...lease, status: "expired" });
    }
  }

  private requireLease(leaseId: string): ServiceLease {
    const lease = this.serviceLeases.get(leaseId);
    if (!lease) {
      throw new ServiceLeaseError(`Unknown service lease ${leaseId}`, "LEASE_NOT_FOUND");
    }
    return lease;
  }

  private findCapabilityForLease(lease: CapabilityLease): WorkerCapability {
    const capability = this.capabilities.find((candidate) =>
      candidate.kind === lease.capabilityKind &&
      candidate.hostId === lease.hostId &&
      JSON.stringify(candidate.labels) === JSON.stringify(lease.labels ?? {})
    );
    if (!capability) {
      throw new ServiceLeaseError(`No registered capability for lease ${lease.leaseId}`, "LEASE_NOT_FOUND");
    }
    return capability;
  }
}

export class PersistedServiceLeaseManager implements ServiceLeaseManager {
  private readonly delegate: ServiceLeaseManager;
  private readonly store: ServiceLeaseStore;

  constructor(
    delegate: ServiceLeaseManager,
    store: ServiceLeaseStore,
  ) {
    this.delegate = delegate;
    this.store = store;
  }

  async acquire(input: AcquireServiceLeaseInput): Promise<ServiceLease> {
    return this.store.put(await this.delegate.acquire(input));
  }

  async heartbeat(leaseId: string, now = new Date()): Promise<ServiceLease> {
    return this.store.put(await this.delegate.heartbeat(leaseId, now));
  }

  async release(leaseId: string, now = new Date()): Promise<ServiceLease> {
    return this.store.put(await this.delegate.release(leaseId, now));
  }

  async listActive(now = new Date()): Promise<ServiceLease[]> {
    const leases = await this.delegate.listActive(now);
    await Promise.all(leases.map((lease) => this.store.put(lease)));
    return this.store.listActive(now);
  }
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
