import { StreamServiceImplementation } from "@livestack/vault-interface";
import {
  StreamPubMessage,
  SubRequest,
  ServerStreamingMethodResult,
} from "@livestack/vault-interface/src/generated/stream";
import { CallContext } from "nice-grpc-common";
import { createClient } from "redis";
import { v4 as uuidv4 } from "uuid";
import path from "path";
import { saveLargeFilesToStorage, identifyLargeFilesToSave, restoreLargeValues } from "../storage/cloudStorage";
import { addDatapoint } from "../db/data_points";
import { ensureStreamRec } from "../db/streams";
import { createClient } from "redis";

class StreamServiceByProject implements StreamServiceImplementation {
  async pub(
    request: StreamPubMessage,
    context: CallContext
  ): Promise<{ messageId: string }> {
    const { projectId, uniqueName, jobId, outputTag, messageIdOverride } = request;
    const channelId = `${uniqueName}`;
    const pubClient = await createClient().connect();

    const message = JSON.parse(request.dataStr); // Assuming dataStr is a JSON string of the message

    let { largeFilesToSave, newObj } = identifyLargeFilesToSave(message);

    // Save large files to storage and update message with storage references
    if (largeFilesToSave.length > 0) {
      const fullPathLargeFilesToSave = largeFilesToSave.map((x) => ({
        ...x,
        path: path.join(projectId, uniqueName, x.path),
      }));
      await saveLargeFilesToStorage(fullPathLargeFilesToSave);
      message = newObj;
    }

    // Ensure stream record exists in the database
    await ensureStreamRec({
      projectId,
      streamId: uniqueName,
      dbConn: context.dbConn, // Assuming dbConn is part of the context
    });

    const datapointId = messageIdOverride || uuidv4();
    await addDatapoint({
      streamId: uniqueName,
      projectId,
      dbConn: context.dbConn, // Assuming dbConn is part of the context
      jobInfo: jobId ? { jobId, outputTag } : undefined,
      data: message,
      datapointId,
    });

    // Publish the message to the Redis stream
    const messageId = await pubClient.xAdd(channelId, '*', 'data', JSON.stringify(message));

    await pubClient.disconnect();

    return { messageId };
  }
  sub(
    request: SubRequest,
    context: CallContext
  ): ServerStreamingMethodResult<{
    timestamp?: number | undefined;
    messageId?: string | undefined;
    dataStr?: string | undefined;
  }> {
    throw new Error("Method not implemented.");
  }
}

const _projectServiceMap: Record<string, StreamServiceByProject> = {};

export function getStreamService() {
  if (!_projectServiceMap["default"]) {
    _projectServiceMap["default"] = new StreamServiceByProject();
  }
  return _projectServiceMap["default"];
}
