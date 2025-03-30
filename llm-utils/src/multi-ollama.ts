import { Ollama, ChatRequest, ChatResponse, EmbeddingsRequest, EmbeddingsResponse, AbortableAsyncIterator } from 'ollama';

interface OllamaInstanceWrapper {
  instance: Ollama;
  host: string;
  busy: boolean;
}

// No longer need AutoReleaseOllamaClient type, we return a proxied Ollama instance

let ollamaInstances: OllamaInstanceWrapper[] = [];
let waitingResolvers: ((wrapper: OllamaInstanceWrapper) => void)[] = [];

function initializeOllamaInstances() {
  const hostsEnv = process.env.OLLAMA_HOSTS;
  const singleHostEnv = process.env.OLLAMA_HOST;

  let hosts: string[] = [];

  if (hostsEnv) {
    hosts = hostsEnv.split(',').map(h => h.trim()).filter(h => h.length > 0);
    if (hosts.length > 0) {
      console.log(`Initializing Ollama with multiple hosts: ${hosts.join(', ')}`);
    }
  }

  if (hosts.length === 0 && singleHostEnv) {
    const singleHost = singleHostEnv.trim();
    if (singleHost.length > 0) {
      hosts = [singleHost];
      console.log(`Initializing Ollama with single host (from OLLAMA_HOST): ${hosts[0]}`);
    }
  }

  if (hosts.length === 0) {
    throw new Error('Ollama host configuration error: Neither OLLAMA_HOSTS nor OLLAMA_HOST environment variables are set or valid.');
  }

  try {
    ollamaInstances = hosts.map(host => ({
      instance: new Ollama({ host }),
      host,
      busy: false,
    }));
  } catch (error) {
    throw error;
  }

  console.log(`Initialized ${ollamaInstances.length} Ollama instances.`);

}

initializeOllamaInstances();

// Return type is now Promise<Ollama>, but it's a proxied instance
async function getAvailableOllama(): Promise<{ client: Ollama; releaseClient: () => void }> {
  const getWrapper = (): Promise<OllamaInstanceWrapper> => {
    const availableInstance = ollamaInstances.find(inst => !inst.busy);
    if (availableInstance) {
      availableInstance.busy = true;
      return Promise.resolve(availableInstance);
    } else {
      return new Promise((resolve) => {
        waitingResolvers.push((instanceWrapper) => {
          resolve(instanceWrapper);
        });
      });
    }
  };

  const wrapper = await getWrapper();
  const originalOllamaInstance = wrapper.instance;

  let released = false; // Flag to prevent double release
  const release = () => {
    if (released) return; // Already released
    released = true;
    wrapper.busy = false;
    if (waitingResolvers.length > 0) {
      const resolver = waitingResolvers.shift();
      const newlyAvailable = ollamaInstances.find(inst => !inst.busy);
      if (newlyAvailable && resolver) {
        newlyAvailable.busy = true;
        resolver(newlyAvailable);
      }
    }
  };

  // Create a Proxy around the original Ollama instance
  const proxyClient = new Proxy(originalOllamaInstance, {
    get(target, prop, receiver) {
      // Intercept 'chat' method
      if (prop === 'chat') {
        // Return a new function that wraps the original chat method
        return async (request: ChatRequest): Promise<ChatResponse | AbortableAsyncIterator<ChatResponse>> => {
          if (request.stream === true) {
            // Handle streaming case
            let stream: AbortableAsyncIterator<ChatResponse> | null = null;
            try {
              stream = await target.chat(request as ChatRequest & { stream: true });

              // Wrap the generator to release on completion/error
              const wrappedGenerator = (async function* () {
                try {
                  for await (const chunk of stream!) { yield chunk; }
                } finally { release(); }
              })();

              // Construct the object to return for the stream case
              const streamWrapper = {
                ...stream, // Spread original properties (best effort)
                [Symbol.asyncIterator]: () => wrappedGenerator,
                abort: () => {
                  if (stream) {
                    try { stream.abort(); } catch (e) { console.error("Error in original abort:", e); }
                    finally { release(); } // Ensure release on abort
                  } else {
                    release(); // Release if abort called before stream obtained
                  }
                },
              };
              // Force cast the wrapper object to the required type at the return point
              return streamWrapper as any as AbortableAsyncIterator<ChatResponse>;
            } catch (error) {
              release(); // Release on error getting stream
              throw error;
            }
          } else {
            // Handle non-streaming case
            try {
              const response = await target.chat(request as ChatRequest & { stream?: false | undefined });
              release(); // Release immediately
              return response;
            } catch (error) {
              release(); // Release on error
              throw error;
            }
          }
        };
      }

      // Intercept 'embeddings' method
      if (prop === 'embeddings') {
        // Return a new function that wraps the original embeddings method
        return async (request: EmbeddingsRequest): Promise<EmbeddingsResponse> => {
          try {
            return await target.embeddings(request);
          } finally {
            release(); // Release after completion or error
          }
        };
      }

      // For any other property, just reflect the original
      // Use Reflect.get for correct receiver binding
      return Reflect.get(target, prop, receiver);
    }
  });

  return { client: proxyClient, releaseClient: release };
}

export { getAvailableOllama };
