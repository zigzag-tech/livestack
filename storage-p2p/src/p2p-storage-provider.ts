import {
  IStorageProvider,
  InferRestoredFileType,
  LargeFileWithoutValue,
  OriginalType,
} from "@livestack/core";
import { createLibp2p } from "libp2p";
import { tcp } from "@libp2p/tcp";
import { mplex } from "@libp2p/mplex";
import { noise } from "@chainsafe/libp2p-noise";
import { kadDHT } from "@libp2p/kad-dht";
import { pubsubPeerDiscovery } from "@libp2p/pubsub-peer-discovery";
import { toString as uint8ArrayToString } from "uint8arrays/to-string";
import { fromString as uint8ArrayFromString } from "uint8arrays/from-string";
import { bootstrap } from "@libp2p/bootstrap";
import { concat } from "uint8arrays/concat";

let vaultServerURL = "localhost:50504";

if (process.env.LIVESTACK_VALULT_SERVER_URL) {
  vaultServerURL = process.env.LIVESTACK_VALULT_SERVER_URL;
}

const vaultServerHost = vaultServerURL.split(":")[0];

export async function getP2PStorageProvider(
  networkId: string
): Promise<IStorageProvider> {
  const bootstrapMultiaddr = `/ip4/${vaultServerHost}/tcp/65448/p2p/QmBootstrapNodeID`;

  const node = await createLibp2p({
    addresses: {
      listen: ["/ip4/0.0.0.0/tcp/0"],
    },
    transports: [tcp()],
    streamMuxers: [mplex()],
    connectionEncryption: [noise()],

    peerDiscovery: [
      bootstrap({
        list: [bootstrapMultiaddr],
        tagName: `myapp-${networkId}`,
      }),
      pubsubPeerDiscovery({
        topics: [`${networkId}.storage-p2p`],
      }),
    ],
    services: {
      dht: kadDHT({
        // protocolPrefix: `/myapp-${networkId}/kad/1.0.0`,
      }),
    },
  });

  await node.start();

  return {
    putToStorage: async (destination, data) => {
      const encodedData = uint8ArrayFromString(data.toString());
      await node.contentRouting.put(
        uint8ArrayFromString(destination),
        encodedData
      );
    },
    fetchFromStorage: async <T extends OriginalType>(
      f: LargeFileWithoutValue<T>
    ): Promise<InferRestoredFileType<T>> => {
      const asyncIterable = node.contentRouting.get(
        uint8ArrayFromString(f.path)
      );
      const result = await asyncIterable;
      const data = concat([result]);

      if (f.originalType === "string") {
        return uint8ArrayToString(data) as InferRestoredFileType<T>;
      } else if (f.originalType === "buffer") {
        return data as unknown as InferRestoredFileType<T>;
      } else if (f.originalType === "array-buffer") {
        return data.buffer as InferRestoredFileType<T>;
      } else if (f.originalType === "blob") {
        return new Blob([data]) as InferRestoredFileType<T>;
      } else if (f.originalType === "file") {
        return new File([data], f.path) as InferRestoredFileType<T>;
      } else if (f.originalType === "stream") {
        const { Readable } = await import("stream");
        return Readable.from(data) as unknown as InferRestoredFileType<T>;
      } else {
        throw new Error("Unsupported originalType " + f.originalType);
      }
    },
  };
}
