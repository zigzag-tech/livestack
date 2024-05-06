import type {
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
// import { pubsubPeerDiscovery } from "@libp2p/pubsub-peer-discovery";
// import { toString as uint8ArrayToString } from "uint8arrays/to-string.node";
// import { fromString as uint8ArrayFromString } from "uint8arrays/from-string";
import { bootstrap } from "@libp2p/bootstrap";
import { multiaddr } from "multiaddr";
import corePkg from "@livestack/core";
const { calculateHash } = corePkg;
import dns from "dns";
// import { concat } from "uint8arrays/concat";

let vaultServerURL = "localhost:50504";

if (process.env.LIVESTACK_VALULT_SERVER_URL) {
  vaultServerURL = process.env.LIVESTACK_VALULT_SERVER_URL;
}

export async function getP2PStorageProvider(
  networkId: string
): Promise<IStorageProvider> {
  const vaultServerHost = await new Promise<string>((resolve, reject) => {
    const [host, port] = vaultServerURL.split(":");
    dns.lookup(host, 4, (err, address) => {
      if (err) {
        reject(err);
      } else {
        resolve(address);
      }
    });
  });
  console.log("vaultServerHost resolved to", vaultServerHost);

  // const { createLibp2p } = await import("libp2p");
  // const { tcp } = await import("@libp2p/tcp");
  // const { mplex } = await import("@libp2p/mplex");
  // const { noise } = await import("@chainsafe/libp2p-noise");
  // const { kadDHT } = await import("@libp2p/kad-dht");
  // const { pubsubPeerDiscovery } = await import("@libp2p/pubsub-peer-discovery");
  // const { bootstrap } = await import("@libp2p/bootstrap");
  const uint8arrays = await import("uint8arrays");

  // const { fromString: uint8ArrayFromString } = await import(
  //   "uint8arrays/from-string"
  // );
  // const { toString: uint8ArrayToString } = await import(
  //   "uint8arrays/to-string.node"
  // );
  const { concat } = await import("uint8arrays/concat");

  const bootstrapMultiaddr = `/ip4/${vaultServerHost}/tcp/65448/p2p/QmYJyUMAcXEw1b5bFfbBbzYu5wyyjLMRHXGUkCXpag74Fu`;
  console.log("bootstrapMultiaddr", "'" + bootstrapMultiaddr + "'");
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
        // tagName: `myapp-${networkId}`,
      }),
      // pubsubPeerDiscovery({
      //   topics: [`${networkId}.storage-p2p`],
      // }),
    ],
    services: {
      dht: kadDHT({
        // protocolPrefix: `/myapp-${networkId}/kad/1.0.0`,
      }),
    },
  });

  await node.start();
  await node.dial(multiaddr(bootstrapMultiaddr));

  console.log("P2P client storage node started.");
  console.log("Listening on:");
  node.getMultiaddrs().forEach((addr) => {
    console.log(addr.toString());
  });

  return {
    putToStorage: async (destination, data) => {
      const encodedData = uint8arrays.fromString(data.toString());
      console.log("putToStorage", destination, encodedData.length);
      const destUint = uint8arrays.fromString(destination);
      await node.contentRouting.put(destUint, encodedData);
      const hash = await calculateHash(data.toString());
      console.log(hash);
      return { hash };
    },
    fetchFromStorage: async <T extends OriginalType>(
      f: LargeFileWithoutValue<T>
    ): Promise<InferRestoredFileType<T>> => {
      const asyncIterable = node.contentRouting.get(
        uint8arrays.fromString(f.path)
      );
      const result = await asyncIterable;
      const data = concat([result]);

      if (f.originalType === "string") {
        return uint8arrays.toString(data) as InferRestoredFileType<T>;
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
