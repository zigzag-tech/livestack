import { webSockets } from "@libp2p/websockets";
import { multiaddr } from "@multiformats/multiaddr";
const bootstrapMultiaddr = `/ip4/0.0.0.0/tcp/65448/p2p/QmBootstrapNodeID`;

export async function startBootstrapNode() {
  try {
    const libP2p = await import("libp2p");
    const { tcp } = await import("@libp2p/tcp");
    const { mplex } = await import("@libp2p/mplex");
    const { noise } = await import("@chainsafe/libp2p-noise");
    const { kadDHT } = await import("@libp2p/kad-dht");
    // const { webSockets } = await import("libp2p/websockets");
    const { bootstrap } = await import("@libp2p/bootstrap");

    const node = await libP2p.createLibp2p({
      addresses: {
        listen: ["/ip4/0.0.0.0/tcp/65448"], // Note: Ensure this port is open for WebSocket connections
      },
      transports: [
        tcp(),
        // webSockets(),
      ],
      streamMuxers: [mplex()],
      connectionEncryption: [noise()],
      services: {
        dht: kadDHT({}),
      },
      peerDiscovery: [
        bootstrap({
          list: [bootstrapMultiaddr],
          // tagName: `myapp-${networkId}`, // Adding tagName similar to the client's config
        }),
      ],
    });

    await node.start();
    console.log("Bootstrap node started.");
    console.log("Listening on:");
    node.getMultiaddrs().forEach((addr) => {
      console.log(addr.toString());
    });
  } catch (err) {
    console.error("Error starting the node", err);
    // Additional error handling or fallback logic can be added here.
  }
}
