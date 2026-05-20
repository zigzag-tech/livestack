import { createHmac, randomUUID } from "node:crypto";

export interface AliyunEcsWorkerProfile {
  regionId: string;
  instanceType: string;
  imageFamily: string;
  systemDiskCategory: string;
  systemDiskSizeGiB: number;
  autoReleaseHours: number;
  maxParallelWorkers: number;
  idleShutdownSeconds: number;
  labels?: Record<string, string>;
}

export const HEYUAN_G8I_2XLARGE_RENDER_PROFILE: AliyunEcsWorkerProfile = {
  regionId: "cn-heyuan",
  instanceType: "ecs.g8i.2xlarge",
  imageFamily: "ubuntu_24_04_x64",
  systemDiskCategory: "cloud_essd",
  systemDiskSizeGiB: 80,
  autoReleaseHours: 4,
  idleShutdownSeconds: 300,
  maxParallelWorkers: 4,
  labels: {
    workload: "remotion-render",
    sizingPolicy: "scale-out-g8i-2xlarge",
  },
};

export interface AliyunEcsRunInstancesRequest {
  RegionId: string;
  InstanceType: string;
  ImageFamily: string;
  Amount: number;
  InstanceName: string;
  HostName: string;
  AutoReleaseTime: string;
  SystemDisk: {
    Category: string;
    Size: number;
  };
  UserData?: string;
  Tags: Array<{ Key: string; Value: string }>;
}

export interface AliyunEcsDescribeInstancesRequest {
  RegionId: string;
  InstanceIds?: string[];
  Tags?: Array<{ Key: string; Value: string }>;
  PageNumber?: number;
  PageSize?: number;
}

export interface AliyunEcsInstanceDescription {
  InstanceId: string;
  InstanceName?: string;
  RegionId?: string;
  InstanceType?: string;
  Status?: string;
  CreationTime?: string;
  ExpiredTime?: string;
  Tags?: {
    Tag?: Array<{ TagKey?: string; Key?: string; TagValue?: string; Value?: string }>;
  };
}

export interface AliyunEcsRunInstancesResult {
  InstanceIdSets: {
    InstanceIdSet: string[];
  };
}

export interface AliyunEcsDescribeInstancesResult {
  Instances: {
    Instance: AliyunEcsInstanceDescription[];
  };
  PageNumber?: number;
  PageSize?: number;
  TotalCount?: number;
}

export interface AliyunEcsClient {
  runInstances(request: AliyunEcsRunInstancesRequest): Promise<AliyunEcsRunInstancesResult>;
  describeInstances(request: AliyunEcsDescribeInstancesRequest): Promise<AliyunEcsDescribeInstancesResult>;
  deleteInstances(request: {
    RegionId: string;
    InstanceIds: string[];
    Force?: boolean;
  }): Promise<void>;
}

export interface AliyunEciWorkerProfile {
  regionId: string;
  zoneId?: string;
  vSwitchId?: string;
  securityGroupId?: string;
  instanceType: string;
  containerImage?: string;
  containerName?: string;
  restartPolicy: "Never" | "OnFailure" | "Always";
  activeDeadlineSeconds?: number;
  vcpu: number;
  memoryGiB: number;
  maxParallelWorkers: number;
  idleShutdownSeconds: number;
  labels?: Record<string, string>;
}

export const HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE: AliyunEciWorkerProfile = {
  regionId: "cn-heyuan",
  instanceType: "eci.8vcpu-32gib",
  containerName: "livestack-worker",
  restartPolicy: "Never",
  activeDeadlineSeconds: 4 * 60 * 60,
  vcpu: 8,
  memoryGiB: 32,
  idleShutdownSeconds: 300,
  maxParallelWorkers: 4,
  labels: {
    workload: "livestack-worker",
    sizingPolicy: "eci-8vcpu-32gib",
  },
};

export interface AliyunEciContainer {
  name: string;
  image: string;
  cpu: number;
  memoryGiB: number;
  imagePullPolicy?: "Always" | "IfNotPresent" | "Never";
  command?: string[];
  args?: string[];
  env?: Record<string, string | undefined>;
}

export interface AliyunEciCreateContainerGroupRequest {
  RegionId: string;
  ZoneId?: string;
  VSwitchId?: string;
  SecurityGroupId?: string;
  ContainerGroupName: string;
  RestartPolicy: "Never" | "OnFailure" | "Always";
  Cpu: number;
  Memory: number;
  ActiveDeadlineSeconds?: number;
  ClientToken?: string;
  Container: AliyunEciContainer[];
  Tags: Array<{ Key: string; Value: string }>;
}

export interface AliyunEciCreateContainerGroupResult {
  ContainerGroupId: string;
}

export interface AliyunEciDescribeContainerGroupsRequest {
  RegionId: string;
  ContainerGroupIds?: string[];
  Limit?: number;
}

export interface AliyunEciContainerGroupDescription {
  ContainerGroupId: string;
  ContainerGroupName?: string;
  RegionId?: string;
  Status?: string;
  CreationTime?: string;
  Tags?: {
    Tag?: Array<{ Key?: string; TagKey?: string; Value?: string; TagValue?: string }>;
  };
}

export interface AliyunEciDescribeContainerGroupsResult {
  ContainerGroups: {
    ContainerGroup: AliyunEciContainerGroupDescription[];
  };
  NextToken?: string;
}

export interface AliyunEciClient {
  createContainerGroup(request: AliyunEciCreateContainerGroupRequest): Promise<AliyunEciCreateContainerGroupResult>;
  describeContainerGroups(request: AliyunEciDescribeContainerGroupsRequest): Promise<AliyunEciDescribeContainerGroupsResult>;
  deleteContainerGroup(request: {
    RegionId: string;
    ContainerGroupId: string;
  }): Promise<void>;
}

export interface AliyunEciWorker {
  containerGroupId: string;
  hostId: string;
  regionId: string;
  instanceType: string;
  status?: string;
  name?: string;
  createdAt?: string;
  tags?: Record<string, string>;
}

export interface AliyunWorkerInstance {
  instanceId: string;
  hostId: string;
  regionId: string;
  instanceType: string;
  status?: string;
  name?: string;
  createdAt?: string;
  expiresAt?: string;
  tags?: Record<string, string>;
}

export function aliyunWorkerHostId(profile: AliyunEcsWorkerProfile, instanceId: string): string {
  return `aliyun-${profile.regionId}/${profile.instanceType}/${instanceId}`;
}

export function aliyunEciWorkerHostId(profile: AliyunEciWorkerProfile, containerGroupId: string): string {
  return `aliyun-${profile.regionId}/${profile.instanceType}/${containerGroupId}`;
}

export async function createAliyunEcsWorkers(
  client: AliyunEcsClient,
  input: {
    profile?: AliyunEcsWorkerProfile;
    amount: number;
    namePrefix?: string;
    userData?: string;
    now?: Date;
    tags?: Record<string, string>;
  },
): Promise<AliyunWorkerInstance[]> {
  const profile = input.profile ?? HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
  const amount = Math.max(1, Math.min(input.amount, profile.maxParallelWorkers));
  const result = await client.runInstances(buildAliyunRunInstancesRequest({
    profile,
    amount,
    namePrefix: input.namePrefix ?? "livestack-worker",
    userData: input.userData,
    now: input.now,
    tags: input.tags,
  }));
  return result.InstanceIdSets.InstanceIdSet.map((instanceId) => ({
    instanceId,
    hostId: aliyunWorkerHostId(profile, instanceId),
    regionId: profile.regionId,
    instanceType: profile.instanceType,
  }));
}

export async function listAliyunEcsWorkers(
  client: AliyunEcsClient,
  input: {
    profile?: AliyunEcsWorkerProfile;
    regionId?: string;
    instanceIds?: string[];
    tags?: Record<string, string>;
  } = {},
): Promise<AliyunWorkerInstance[]> {
  const profile = input.profile ?? HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
  const result = await client.describeInstances({
    RegionId: input.regionId ?? profile.regionId,
    ...(input.instanceIds && input.instanceIds.length > 0 ? { InstanceIds: input.instanceIds } : {}),
    Tags: Object.entries({
      app: "livestack",
      ...input.tags,
    }).map(([Key, Value]) => ({ Key, Value })),
    PageSize: 100,
  });
  return result.Instances.Instance.map((instance) => {
    const regionId = instance.RegionId ?? input.regionId ?? profile.regionId;
    const instanceType = instance.InstanceType ?? profile.instanceType;
    const hostProfile = { ...profile, regionId, instanceType };
    return {
      instanceId: instance.InstanceId,
      hostId: aliyunWorkerHostId(hostProfile, instance.InstanceId),
      regionId,
      instanceType,
      ...(instance.Status ? { status: instance.Status } : {}),
      ...(instance.InstanceName ? { name: instance.InstanceName } : {}),
      ...(instance.CreationTime ? { createdAt: instance.CreationTime } : {}),
      ...(instance.ExpiredTime ? { expiresAt: instance.ExpiredTime } : {}),
      tags: normalizeTags(instance.Tags),
    };
  });
}

export async function deleteAliyunEcsWorkers(
  client: AliyunEcsClient,
  input: {
    profile?: AliyunEcsWorkerProfile;
    regionId?: string;
    instanceIds: string[];
    force?: boolean;
  },
): Promise<void> {
  if (input.instanceIds.length === 0) return;
  const profile = input.profile ?? HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
  await client.deleteInstances({
    RegionId: input.regionId ?? profile.regionId,
    InstanceIds: input.instanceIds,
    Force: input.force ?? true,
  });
}

export async function createAliyunEciWorker(
  client: AliyunEciClient,
  input: {
    profile?: AliyunEciWorkerProfile;
    name?: string;
    image?: string;
    command?: string[];
    args?: string[];
    env?: Record<string, string | undefined>;
    clientToken?: string;
    tags?: Record<string, string>;
  } = {},
): Promise<AliyunEciWorker> {
  const profile = input.profile ?? HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE;
  const request = buildAliyunEciCreateContainerGroupRequest(input);
  const result = await client.createContainerGroup(request);
  return {
    containerGroupId: result.ContainerGroupId,
    hostId: aliyunEciWorkerHostId(profile, request.ContainerGroupName),
    regionId: profile.regionId,
    instanceType: profile.instanceType,
  };
}

export async function listAliyunEciWorkers(
  client: AliyunEciClient,
  input: {
    profile?: AliyunEciWorkerProfile;
    regionId?: string;
    containerGroupIds?: string[];
  } = {},
): Promise<AliyunEciWorker[]> {
  const profile = input.profile ?? HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE;
  const result = await client.describeContainerGroups({
    RegionId: input.regionId ?? profile.regionId,
    ...(input.containerGroupIds && input.containerGroupIds.length > 0
      ? { ContainerGroupIds: input.containerGroupIds }
      : {}),
    Limit: 100,
  });
  return result.ContainerGroups.ContainerGroup.map((group) => {
    const regionId = group.RegionId ?? input.regionId ?? profile.regionId;
    const hostProfile = { ...profile, regionId };
    return {
      containerGroupId: group.ContainerGroupId,
      hostId: aliyunEciWorkerHostId(hostProfile, group.ContainerGroupId),
      regionId,
      instanceType: profile.instanceType,
      ...(group.Status ? { status: group.Status } : {}),
      ...(group.ContainerGroupName ? { name: group.ContainerGroupName } : {}),
      ...(group.CreationTime ? { createdAt: group.CreationTime } : {}),
      tags: normalizeEciTags(group.Tags),
    };
  });
}

export async function deleteAliyunEciWorker(
  client: AliyunEciClient,
  input: {
    profile?: AliyunEciWorkerProfile;
    regionId?: string;
    containerGroupId: string;
  },
): Promise<void> {
  const profile = input.profile ?? HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE;
  await client.deleteContainerGroup({
    RegionId: input.regionId ?? profile.regionId,
    ContainerGroupId: input.containerGroupId,
  });
}

export function buildAliyunRunInstancesRequest(input: {
  profile: AliyunEcsWorkerProfile;
  amount: number;
  namePrefix: string;
  userData?: string;
  now?: Date;
  tags?: Record<string, string>;
}): AliyunEcsRunInstancesRequest {
  const autoReleaseTime = new Date(
    (input.now ?? new Date()).getTime() + input.profile.autoReleaseHours * 60 * 60 * 1000,
  ).toISOString();
  return {
    RegionId: input.profile.regionId,
    InstanceType: input.profile.instanceType,
    ImageFamily: input.profile.imageFamily,
    Amount: input.amount,
    InstanceName: input.namePrefix,
    HostName: input.namePrefix,
    AutoReleaseTime: autoReleaseTime,
    SystemDisk: {
      Category: input.profile.systemDiskCategory,
      Size: input.profile.systemDiskSizeGiB,
    },
    ...(input.userData ? { UserData: Buffer.from(input.userData, "utf8").toString("base64") } : {}),
    Tags: Object.entries({
      app: "livestack",
      region: input.profile.regionId,
      instanceType: input.profile.instanceType,
      ...input.profile.labels,
      ...input.tags,
    }).map(([Key, Value]) => ({ Key, Value })),
  };
}

export function buildAliyunEciCreateContainerGroupRequest(input: {
  profile?: AliyunEciWorkerProfile;
  name?: string;
  image?: string;
  command?: string[];
  args?: string[];
  env?: Record<string, string | undefined>;
  clientToken?: string;
  tags?: Record<string, string>;
} = {}): AliyunEciCreateContainerGroupRequest {
  const profile = input.profile ?? HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE;
  const image = input.image ?? profile.containerImage;
  if (!image) {
    throw new Error("Missing Aliyun ECI worker image.");
  }
  const name = input.name ?? `livestack-worker-${Date.now()}`;
  const hostId = aliyunEciWorkerHostId(profile, name);
  return {
    RegionId: profile.regionId,
    ...(profile.zoneId ? { ZoneId: profile.zoneId } : {}),
    ...(profile.vSwitchId ? { VSwitchId: profile.vSwitchId } : {}),
    ...(profile.securityGroupId ? { SecurityGroupId: profile.securityGroupId } : {}),
    ContainerGroupName: name,
    RestartPolicy: profile.restartPolicy,
    Cpu: profile.vcpu,
    Memory: profile.memoryGiB,
    ...(profile.activeDeadlineSeconds ? { ActiveDeadlineSeconds: profile.activeDeadlineSeconds } : {}),
    ...(input.clientToken ? { ClientToken: input.clientToken } : {}),
    Container: [{
      name: profile.containerName ?? "livestack-worker",
      image,
      cpu: profile.vcpu,
      memoryGiB: profile.memoryGiB,
      imagePullPolicy: "Always",
      command: input.command,
      args: input.args,
      env: {
        LIVESTACK_HOST_ID: hostId,
        ALIYUN_REGION_ID: profile.regionId,
        LIVESTACK_WORKER_IDLE_SHUTDOWN_SECONDS: String(profile.idleShutdownSeconds),
        ...input.env,
      },
    }],
    Tags: Object.entries({
      app: "livestack",
      region: profile.regionId,
      backend: "eci",
      shape: profile.instanceType,
      ...profile.labels,
      ...input.tags,
    }).map(([Key, Value]) => ({ Key, Value })),
  };
}

export function buildAliyunWorkerBootstrapUserData(input: {
  profile?: AliyunEcsWorkerProfile;
  repoUrl: string;
  branch?: string;
  packageManagerCommand?: string;
  workerCommand?: string;
  env?: Record<string, string>;
}): string {
  const profile = input.profile ?? HEYUAN_G8I_2XLARGE_RENDER_PROFILE;
  const hostPrefix = `aliyun-${profile.regionId}/${profile.instanceType}/`;
  return [
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "mkdir -p /root/.livestack",
    "ALIYUN_INSTANCE_ID=\"$(curl -fsS --connect-timeout 2 http://100.100.100.200/latest/meta-data/instance-id || hostname)\"",
    `LIVESTACK_HOST_ID=${shellQuote(hostPrefix)}"$ALIYUN_INSTANCE_ID"`,
    "printf '%s\\n' \"$LIVESTACK_HOST_ID\" > /root/.livestack/host-id",
    "export LIVESTACK_HOST_ID",
    ...Object.entries(input.env ?? {}).map(([key, value]) => `export ${key}=${shellQuote(value)}`),
    "mkdir -p /opt/livestack-worker",
    "cd /opt/livestack-worker",
    `git clone --depth 1 --branch ${shellQuote(input.branch ?? "main")} ${shellQuote(input.repoUrl)} app`,
    "cd app",
    input.packageManagerCommand ?? "npm install",
    "set +e",
    input.workerCommand ?? "npm run worker",
    "worker_status=$?",
    "shutdown -h now || true",
    "exit \"$worker_status\"",
  ].join("\n");
}

export function createAliyunEcsClientFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): AliyunEcsClient {
  const accessKeyId = env.ALIBABA_CLOUD_ACCESS_KEY_ID ?? env.ALIYUN_ACCESS_KEY_ID;
  const accessKeySecret = env.ALIBABA_CLOUD_ACCESS_KEY_SECRET ?? env.ALIYUN_ACCESS_KEY_SECRET;
  if (!accessKeyId || !accessKeySecret) {
    throw new Error("Missing Aliyun credentials.");
  }
  return new AliyunEcsRpcClient({
    accessKeyId,
    accessKeySecret,
    endpoint: env.ALIYUN_ECS_ENDPOINT,
  });
}

export function createAliyunEciClientFromEnv(
  env: NodeJS.ProcessEnv = process.env,
): AliyunEciClient {
  const accessKeyId = env.ALIBABA_CLOUD_ACCESS_KEY_ID ?? env.ALIYUN_ACCESS_KEY_ID;
  const accessKeySecret = env.ALIBABA_CLOUD_ACCESS_KEY_SECRET ?? env.ALIYUN_ACCESS_KEY_SECRET;
  if (!accessKeyId || !accessKeySecret) {
    throw new Error("Missing Aliyun credentials.");
  }
  return new AliyunEciRpcClient({
    accessKeyId,
    accessKeySecret,
    endpoint: env.ALIYUN_ECI_ENDPOINT,
  });
}

export class AliyunEcsRpcClient implements AliyunEcsClient {
  private readonly endpoint: string;
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => Date;
  private readonly nonce: () => string;

  constructor(private readonly options: {
    accessKeyId: string;
    accessKeySecret: string;
    endpoint?: string;
    fetch?: typeof fetch;
    now?: () => Date;
    nonce?: () => string;
  }) {
    this.endpoint = options.endpoint ?? "https://ecs.aliyuncs.com/";
    this.fetchImpl = options.fetch ?? fetch;
    this.now = options.now ?? (() => new Date());
    this.nonce = options.nonce ?? (() => randomUUID());
  }

  async runInstances(request: AliyunEcsRunInstancesRequest): Promise<AliyunEcsRunInstancesResult> {
    return this.call<AliyunEcsRunInstancesResult>("RunInstances", flattenRunInstancesRequest(request));
  }

  async describeInstances(request: AliyunEcsDescribeInstancesRequest): Promise<AliyunEcsDescribeInstancesResult> {
    return this.call<AliyunEcsDescribeInstancesResult>("DescribeInstances", flattenDescribeInstancesRequest(request));
  }

  async deleteInstances(request: {
    RegionId: string;
    InstanceIds: string[];
    Force?: boolean;
  }): Promise<void> {
    await this.call("DeleteInstances", flattenDeleteInstancesRequest(request));
  }

  private async call<T>(action: string, params: Record<string, string>): Promise<T> {
    const signedParams = signAliyunRpcRequest({
      accessKeyId: this.options.accessKeyId,
      accessKeySecret: this.options.accessKeySecret,
      method: "POST",
      params: {
        ...params,
        Action: action,
        Version: "2014-05-26",
        Format: "JSON",
        SignatureMethod: "HMAC-SHA1",
        SignatureNonce: this.nonce(),
        SignatureVersion: "1.0",
        Timestamp: this.now().toISOString(),
      },
    });
    const response = await this.fetchImpl(this.endpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(signedParams).toString(),
    });
    const body = await response.text();
    const parsed = body.trim() ? JSON.parse(body) : {};
    if (!response.ok) {
      const message = typeof parsed === "object" && parsed && "Message" in parsed
        ? String((parsed as { Message: unknown }).Message)
        : body.slice(0, 200);
      throw new Error(`Aliyun ECS ${action} failed (${response.status}): ${message}`);
    }
    return parsed as T;
  }
}

export class AliyunEciRpcClient implements AliyunEciClient {
  private readonly endpoint: string;
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => Date;
  private readonly nonce: () => string;

  constructor(private readonly options: {
    accessKeyId: string;
    accessKeySecret: string;
    endpoint?: string;
    fetch?: typeof fetch;
    now?: () => Date;
    nonce?: () => string;
  }) {
    this.endpoint = options.endpoint ?? "https://eci.aliyuncs.com/";
    this.fetchImpl = options.fetch ?? fetch;
    this.now = options.now ?? (() => new Date());
    this.nonce = options.nonce ?? (() => randomUUID());
  }

  async createContainerGroup(
    request: AliyunEciCreateContainerGroupRequest,
  ): Promise<AliyunEciCreateContainerGroupResult> {
    return this.call<AliyunEciCreateContainerGroupResult>(
      "CreateContainerGroup",
      flattenAliyunEciCreateContainerGroupRequest(request),
    );
  }

  async describeContainerGroups(
    request: AliyunEciDescribeContainerGroupsRequest,
  ): Promise<AliyunEciDescribeContainerGroupsResult> {
    return this.call<AliyunEciDescribeContainerGroupsResult>(
      "DescribeContainerGroups",
      flattenAliyunEciDescribeContainerGroupsRequest(request),
    );
  }

  async deleteContainerGroup(request: {
    RegionId: string;
    ContainerGroupId: string;
  }): Promise<void> {
    await this.call("DeleteContainerGroup", request);
  }

  private async call<T>(action: string, params: Record<string, string>): Promise<T> {
    const signedParams = signAliyunRpcRequest({
      accessKeyId: this.options.accessKeyId,
      accessKeySecret: this.options.accessKeySecret,
      method: "POST",
      params: {
        ...params,
        Action: action,
        Version: "2018-08-08",
        Format: "JSON",
        SignatureMethod: "HMAC-SHA1",
        SignatureNonce: this.nonce(),
        SignatureVersion: "1.0",
        Timestamp: this.now().toISOString(),
      },
    });
    const response = await this.fetchImpl(this.endpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(signedParams).toString(),
    });
    const body = await response.text();
    const parsed = body.trim() ? JSON.parse(body) : {};
    if (!response.ok) {
      const message = typeof parsed === "object" && parsed && "Message" in parsed
        ? String((parsed as { Message: unknown }).Message)
        : body.slice(0, 200);
      throw new Error(`Aliyun ECI ${action} failed (${response.status}): ${message}`);
    }
    return parsed as T;
  }
}

export function signAliyunRpcRequest(input: {
  accessKeyId: string;
  accessKeySecret: string;
  method: "GET" | "POST";
  params: Record<string, string>;
}): Record<string, string> {
  const params = {
    ...input.params,
    AccessKeyId: input.accessKeyId,
  };
  const canonical = canonicalizeAliyunParams(params);
  const stringToSign = [
    input.method,
    percentEncode("/"),
    percentEncode(canonical),
  ].join("&");
  const signature = createHmac("sha1", `${input.accessKeySecret}&`)
    .update(stringToSign)
    .digest("base64");
  return {
    ...params,
    Signature: signature,
  };
}

function flattenRunInstancesRequest(request: AliyunEcsRunInstancesRequest): Record<string, string> {
  return {
    RegionId: request.RegionId,
    InstanceType: request.InstanceType,
    ImageFamily: request.ImageFamily,
    Amount: String(request.Amount),
    InstanceName: request.InstanceName,
    HostName: request.HostName,
    AutoReleaseTime: request.AutoReleaseTime,
    "SystemDisk.Category": request.SystemDisk.Category,
    "SystemDisk.Size": String(request.SystemDisk.Size),
    ...(request.UserData ? { UserData: request.UserData } : {}),
    ...flattenTags(request.Tags),
  };
}

export function flattenAliyunEciCreateContainerGroupRequest(
  request: AliyunEciCreateContainerGroupRequest,
): Record<string, string> {
  return {
    RegionId: request.RegionId,
    ...(request.ZoneId ? { ZoneId: request.ZoneId } : {}),
    ...(request.VSwitchId ? { VSwitchId: request.VSwitchId } : {}),
    ...(request.SecurityGroupId ? { SecurityGroupId: request.SecurityGroupId } : {}),
    ContainerGroupName: request.ContainerGroupName,
    RestartPolicy: request.RestartPolicy,
    Cpu: String(request.Cpu),
    Memory: String(request.Memory),
    ...(request.ActiveDeadlineSeconds ? { ActiveDeadlineSeconds: String(request.ActiveDeadlineSeconds) } : {}),
    ...(request.ClientToken ? { ClientToken: request.ClientToken } : {}),
    ...flattenEciContainers(request.Container),
    ...flattenTags(request.Tags),
  };
}

function flattenAliyunEciDescribeContainerGroupsRequest(
  request: AliyunEciDescribeContainerGroupsRequest,
): Record<string, string> {
  return {
    RegionId: request.RegionId,
    ...(request.ContainerGroupIds ? { ContainerGroupIds: JSON.stringify(request.ContainerGroupIds) } : {}),
    ...(request.Limit ? { Limit: String(request.Limit) } : {}),
  };
}

function flattenEciContainers(containers: AliyunEciContainer[]): Record<string, string> {
  const params: Record<string, string> = {};
  containers.forEach((container, containerIndex) => {
    const prefix = `Container.${containerIndex + 1}`;
    params[`${prefix}.Name`] = container.name;
    params[`${prefix}.Image`] = container.image;
    params[`${prefix}.Cpu`] = String(container.cpu);
    params[`${prefix}.Memory`] = String(container.memoryGiB);
    if (container.imagePullPolicy) params[`${prefix}.ImagePullPolicy`] = container.imagePullPolicy;
    container.command?.forEach((command, commandIndex) => {
      params[`${prefix}.Command.${commandIndex + 1}`] = command;
    });
    container.args?.forEach((arg, argIndex) => {
      params[`${prefix}.Arg.${argIndex + 1}`] = arg;
    });
    let envIndex = 1;
    Object.entries(container.env ?? {}).forEach(([key, value]) => {
      if (value === undefined) return;
      params[`${prefix}.EnvironmentVar.${envIndex}.Key`] = key;
      params[`${prefix}.EnvironmentVar.${envIndex}.Value`] = value;
      envIndex += 1;
    });
  });
  return params;
}

function flattenDescribeInstancesRequest(request: AliyunEcsDescribeInstancesRequest): Record<string, string> {
  return {
    RegionId: request.RegionId,
    ...(request.InstanceIds && request.InstanceIds.length > 0 ? { InstanceIds: JSON.stringify(request.InstanceIds) } : {}),
    ...(request.PageNumber ? { PageNumber: String(request.PageNumber) } : {}),
    ...(request.PageSize ? { PageSize: String(request.PageSize) } : {}),
    ...flattenTags(request.Tags ?? []),
  };
}

function flattenDeleteInstancesRequest(input: {
  RegionId: string;
  InstanceIds: string[];
  Force?: boolean;
}): Record<string, string> {
  return {
    RegionId: input.RegionId,
    Force: String(input.Force ?? true),
    ...Object.fromEntries(input.InstanceIds.map((id, index) => [`InstanceId.${index + 1}`, id])),
  };
}

function flattenTags(tags: Array<{ Key: string; Value: string }>): Record<string, string> {
  return Object.fromEntries(tags.flatMap((tag, index) => {
    const ordinal = index + 1;
    return [
      [`Tag.${ordinal}.Key`, tag.Key],
      [`Tag.${ordinal}.Value`, tag.Value],
    ];
  }));
}

function normalizeTags(tags: AliyunEcsInstanceDescription["Tags"]): Record<string, string> {
  return Object.fromEntries((tags?.Tag ?? []).flatMap((tag) => {
    const key = tag.TagKey ?? tag.Key;
    const value = tag.TagValue ?? tag.Value;
    return key && value ? [[key, value]] : [];
  }));
}

function normalizeEciTags(tags: AliyunEciContainerGroupDescription["Tags"]): Record<string, string> {
  return Object.fromEntries((tags?.Tag ?? []).flatMap((tag) => {
    const key = tag.TagKey ?? tag.Key;
    const value = tag.TagValue ?? tag.Value;
    return key && value ? [[key, value]] : [];
  }));
}

function canonicalizeAliyunParams(params: Record<string, string>): string {
  return Object.keys(params).sort().map((key) => (
    `${percentEncode(key)}=${percentEncode(params[key])}`
  )).join("&");
}

function percentEncode(value: string): string {
  return encodeURIComponent(value)
    .replace(/\+/g, "%20")
    .replace(/\*/g, "%2A")
    .replace(/%7E/g, "~");
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}
