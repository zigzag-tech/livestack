import assert from "node:assert/strict";
import test from "node:test";

import {
  buildAliyunEciCreateContainerGroupRequest,
  aliyunEciEndpointForRegion,
  createAliyunEciWorker,
  deleteAliyunEciWorker,
  flattenAliyunEciCreateContainerGroupRequest,
  HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE,
  listAliyunEciWorkers,
  redactAliyunEciRequestForDryRun,
  type AliyunEciClient,
  type AliyunEciCreateContainerGroupRequest,
  type AliyunEciDescribeContainerGroupsRequest,
} from "./index";

test("builds Heyuan ECI worker requests with stable identity and required env", () => {
  const request = buildAliyunEciCreateContainerGroupRequest({
    name: "unchain-render-0001",
    image: "registry.cn-heyuan.aliyuncs.com/unchain/render-worker:sha",
    command: ["node"],
    args: ["worker.js"],
    env: {
      LIVESTACK_VAULT_SERVER_URL: "100.64.0.1:50504",
      OPTIONAL_EMPTY: undefined,
    },
    clientToken: "token-1",
    tags: {
      project: "unchain",
    },
  });

  assert.equal(request.RegionId, "cn-heyuan");
  assert.equal(request.ContainerGroupName, "unchain-render-0001");
  assert.equal(request.RestartPolicy, "Never");
  assert.equal(request.Cpu, 8);
  assert.equal(request.Memory, 32);
  assert.equal(request.ActiveDeadlineSeconds, 14400);
  assert.equal(request.ClientToken, "token-1");
  assert.deepEqual(request.Container, [{
    name: "livestack-worker",
    image: "registry.cn-heyuan.aliyuncs.com/unchain/render-worker:sha",
    cpu: 8,
    memoryGiB: 32,
    imagePullPolicy: "Always",
    command: ["node"],
    args: ["worker.js"],
    env: {
      LIVESTACK_HOST_ID: "aliyun-cn-heyuan/eci.8vcpu-32gib/unchain-render-0001",
      ALIYUN_REGION_ID: "cn-heyuan",
      LIVESTACK_WORKER_IDLE_SHUTDOWN_SECONDS: "300",
      LIVESTACK_VAULT_SERVER_URL: "100.64.0.1:50504",
      OPTIONAL_EMPTY: undefined,
    },
  }]);
  assert.deepEqual(request.Tags, [
    { Key: "app", Value: "livestack" },
    { Key: "region", Value: "cn-heyuan" },
    { Key: "backend", Value: "eci" },
    { Key: "shape", Value: "eci.8vcpu-32gib" },
    { Key: "workload", Value: "livestack-worker" },
    { Key: "sizingPolicy", Value: "eci-8vcpu-32gib" },
    { Key: "project", Value: "unchain" },
  ]);

  assert.deepEqual(flattenAliyunEciCreateContainerGroupRequest(request), {
    RegionId: "cn-heyuan",
    ContainerGroupName: "unchain-render-0001",
    RestartPolicy: "Never",
    Cpu: "8",
    Memory: "32",
    ActiveDeadlineSeconds: "14400",
    ClientToken: "token-1",
    "Container.1.Name": "livestack-worker",
    "Container.1.Image": "registry.cn-heyuan.aliyuncs.com/unchain/render-worker:sha",
    "Container.1.Cpu": "8",
    "Container.1.Memory": "32",
    "Container.1.ImagePullPolicy": "Always",
    "Container.1.Command.1": "node",
    "Container.1.Arg.1": "worker.js",
    "Container.1.EnvironmentVar.1.Key": "LIVESTACK_HOST_ID",
    "Container.1.EnvironmentVar.1.Value": "aliyun-cn-heyuan/eci.8vcpu-32gib/unchain-render-0001",
    "Container.1.EnvironmentVar.2.Key": "ALIYUN_REGION_ID",
    "Container.1.EnvironmentVar.2.Value": "cn-heyuan",
    "Container.1.EnvironmentVar.3.Key": "LIVESTACK_WORKER_IDLE_SHUTDOWN_SECONDS",
    "Container.1.EnvironmentVar.3.Value": "300",
    "Container.1.EnvironmentVar.4.Key": "LIVESTACK_VAULT_SERVER_URL",
    "Container.1.EnvironmentVar.4.Value": "100.64.0.1:50504",
    "Tag.1.Key": "app",
    "Tag.1.Value": "livestack",
    "Tag.2.Key": "region",
    "Tag.2.Value": "cn-heyuan",
    "Tag.3.Key": "backend",
    "Tag.3.Value": "eci",
    "Tag.4.Key": "shape",
    "Tag.4.Value": "eci.8vcpu-32gib",
    "Tag.5.Key": "workload",
    "Tag.5.Value": "livestack-worker",
    "Tag.6.Key": "sizingPolicy",
    "Tag.6.Value": "eci-8vcpu-32gib",
    "Tag.7.Key": "project",
    "Tag.7.Value": "unchain",
  });
});

test("requires an explicit ECI image when the profile does not provide one", () => {
  assert.throws(
    () => buildAliyunEciCreateContainerGroupRequest(),
    /Missing Aliyun ECI worker image/,
  );
  assert.equal(aliyunEciEndpointForRegion("cn-heyuan"), "https://eci.cn-heyuan.aliyuncs.com/");
});

test("adds private registry credentials to ECI requests and redacts dry-run passwords", () => {
  const request = buildAliyunEciCreateContainerGroupRequest({
    profile: {
      ...HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE,
      containerImage: "registry.cn-heyuan.aliyuncs.com/unchain/render-worker:sha",
      imageRegistryCredential: {
        server: "registry.cn-heyuan.aliyuncs.com",
        userName: "render-user",
        password: "render-password",
      },
    },
    name: "unchain-render-0001",
  });

  assert.deepEqual(request.ImageRegistryCredential, [{
    server: "registry.cn-heyuan.aliyuncs.com",
    userName: "render-user",
    password: "render-password",
  }]);
  assert.deepEqual(redactAliyunEciRequestForDryRun(request).ImageRegistryCredential, [{
    server: "registry.cn-heyuan.aliyuncs.com",
    userName: "render-user",
    password: "<redacted>",
  }]);
  assert.equal(
    flattenAliyunEciCreateContainerGroupRequest(request)["ImageRegistryCredential.1.Password"],
    "render-password",
  );
});

test("creates, lists, and deletes ECI workers through the client wrapper", async () => {
  const client = new MemoryEciClient();
  const created = await createAliyunEciWorker(client, {
    name: "unchain-render-0002",
    image: "registry.cn-heyuan.aliyuncs.com/unchain/render-worker:sha",
  });

  assert.deepEqual(created, {
    containerGroupId: "eci-1",
    hostId: "aliyun-cn-heyuan/eci.8vcpu-32gib/unchain-render-0002",
    regionId: "cn-heyuan",
    instanceType: "eci.8vcpu-32gib",
  });
  assert.equal(client.createdRequests[0]?.ContainerGroupName, "unchain-render-0002");

  const listed = await listAliyunEciWorkers(client);
  assert.deepEqual(listed, [{
    containerGroupId: "eci-1",
    hostId: "aliyun-cn-heyuan/eci.8vcpu-32gib/unchain-render-0002",
    regionId: "cn-heyuan",
    instanceType: "eci.8vcpu-32gib",
    status: "Running",
    name: "unchain-render-0002",
    createdAt: "2026-05-20T00:00:00Z",
    tags: {
      app: "livestack",
      shape: "eci.8vcpu-32gib",
    },
  }]);

  await deleteAliyunEciWorker(client, { containerGroupId: "eci-1" });
  assert.deepEqual(client.deleted, [{ RegionId: "cn-heyuan", ContainerGroupId: "eci-1" }]);
});

test("accepts empty Aliyun ECI list responses where ContainerGroups is an array", async () => {
  const client: AliyunEciClient = {
    async createContainerGroup() {
      throw new Error("not used");
    },
    async describeContainerGroups() {
      return {
        ContainerGroups: [] as any,
      };
    },
    async deleteContainerGroup() {
      throw new Error("not used");
    },
  };

  assert.deepEqual(await listAliyunEciWorkers(client), []);
});

class MemoryEciClient implements AliyunEciClient {
  readonly createdRequests: AliyunEciCreateContainerGroupRequest[] = [];
  readonly deleted: Array<{ RegionId: string; ContainerGroupId: string }> = [];

  async createContainerGroup(
    request: AliyunEciCreateContainerGroupRequest,
  ): Promise<{ ContainerGroupId: string }> {
    this.createdRequests.push(request);
    return { ContainerGroupId: "eci-1" };
  }

  async describeContainerGroups(
    request: AliyunEciDescribeContainerGroupsRequest,
  ): Promise<{
    ContainerGroups: {
      ContainerGroup: Array<{
        ContainerGroupId: string;
        ContainerGroupName: string;
        RegionId: string;
        Status: string;
        CreationTime: string;
        Tags: { Tag: Array<{ Key: string; Value: string }> };
      }>;
    };
  }> {
    assert.deepEqual(request, {
      RegionId: HEYUAN_ECI_8VCPU_32GIB_WORKER_PROFILE.regionId,
      Tags: [
        { Key: "app", Value: "livestack" },
        { Key: "region", Value: "cn-heyuan" },
        { Key: "backend", Value: "eci" },
        { Key: "shape", Value: "eci.8vcpu-32gib" },
        { Key: "workload", Value: "livestack-worker" },
        { Key: "sizingPolicy", Value: "eci-8vcpu-32gib" },
      ],
      Limit: 20,
    });
    return {
      ContainerGroups: {
        ContainerGroup: [{
          ContainerGroupId: "eci-1",
          ContainerGroupName: "unchain-render-0002",
          RegionId: "cn-heyuan",
          Status: "Running",
          CreationTime: "2026-05-20T00:00:00Z",
          Tags: {
            Tag: [
              { Key: "app", Value: "livestack" },
              { Key: "shape", Value: "eci.8vcpu-32gib" },
            ],
          },
        }],
      },
    };
  }

  async deleteContainerGroup(request: {
    RegionId: string;
    ContainerGroupId: string;
  }): Promise<void> {
    this.deleted.push(request);
  }
}
