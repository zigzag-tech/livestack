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
export declare const HEYUAN_G8I_2XLARGE_RENDER_PROFILE: AliyunEcsWorkerProfile;
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
    Tags: Array<{
        Key: string;
        Value: string;
    }>;
}
export interface AliyunEcsDescribeInstancesRequest {
    RegionId: string;
    InstanceIds?: string[];
    Tags?: Array<{
        Key: string;
        Value: string;
    }>;
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
        Tag?: Array<{
            TagKey?: string;
            Key?: string;
            TagValue?: string;
            Value?: string;
        }>;
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
export declare function aliyunWorkerHostId(profile: AliyunEcsWorkerProfile, instanceId: string): string;
export declare function createAliyunEcsWorkers(client: AliyunEcsClient, input: {
    profile?: AliyunEcsWorkerProfile;
    amount: number;
    namePrefix?: string;
    userData?: string;
    now?: Date;
    tags?: Record<string, string>;
}): Promise<AliyunWorkerInstance[]>;
export declare function listAliyunEcsWorkers(client: AliyunEcsClient, input?: {
    profile?: AliyunEcsWorkerProfile;
    regionId?: string;
    instanceIds?: string[];
    tags?: Record<string, string>;
}): Promise<AliyunWorkerInstance[]>;
export declare function deleteAliyunEcsWorkers(client: AliyunEcsClient, input: {
    profile?: AliyunEcsWorkerProfile;
    regionId?: string;
    instanceIds: string[];
    force?: boolean;
}): Promise<void>;
export declare function buildAliyunRunInstancesRequest(input: {
    profile: AliyunEcsWorkerProfile;
    amount: number;
    namePrefix: string;
    userData?: string;
    now?: Date;
    tags?: Record<string, string>;
}): AliyunEcsRunInstancesRequest;
export declare function buildAliyunWorkerBootstrapUserData(input: {
    profile?: AliyunEcsWorkerProfile;
    repoUrl: string;
    branch?: string;
    packageManagerCommand?: string;
    workerCommand?: string;
    env?: Record<string, string>;
}): string;
export declare function createAliyunEcsClientFromEnv(env?: NodeJS.ProcessEnv): AliyunEcsClient;
export declare class AliyunEcsRpcClient implements AliyunEcsClient {
    private readonly options;
    private readonly endpoint;
    private readonly fetchImpl;
    private readonly now;
    private readonly nonce;
    constructor(options: {
        accessKeyId: string;
        accessKeySecret: string;
        endpoint?: string;
        fetch?: typeof fetch;
        now?: () => Date;
        nonce?: () => string;
    });
    runInstances(request: AliyunEcsRunInstancesRequest): Promise<AliyunEcsRunInstancesResult>;
    describeInstances(request: AliyunEcsDescribeInstancesRequest): Promise<AliyunEcsDescribeInstancesResult>;
    deleteInstances(request: {
        RegionId: string;
        InstanceIds: string[];
        Force?: boolean;
    }): Promise<void>;
    private call;
}
export declare function signAliyunRpcRequest(input: {
    accessKeyId: string;
    accessKeySecret: string;
    method: "GET" | "POST";
    params: Record<string, string>;
}): Record<string, string>;
