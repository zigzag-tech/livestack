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
