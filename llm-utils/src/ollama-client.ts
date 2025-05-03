import * as fs from 'fs';
import * as path from 'path';
import { getAvailableOllama } from './multi-ollama'; // Import the new function
import * as crypto from 'crypto';
import { z } from 'zod';
import { Ollama } from 'ollama'; // Import the Ollama type
import { jsonrepair } from 'jsonrepair'

// ANSI color codes
const CYAN = '\x1b[36m';
const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const RESET = '\x1b[0m';

// Remove the old direct Ollama initialization
// const OLLAMA_HOST = process.env.OLLAMA_HOST || 'http://127.0.0.1:11434';
// const ollama = new Ollama({ host: OLLAMA_HOST })

/**
 * Utility function to wait for user to press Enter
 * Returns a promise that resolves when Enter is pressed, rejects otherwise
 */
export async function waitForEnterKey(): Promise<void> {
  return new Promise((resolve, reject) => {
    // Set up stdin to read input
    process.stdin.setRawMode(true);
    process.stdin.resume();
    process.stdin.setEncoding('utf8');

    console.log(`${GREEN}Press Enter to continue or any other key to cancel...${RESET}`);

    const onData = (key: string) => {
      // Ctrl+C or q to exit
      if (key === '\u0003' || key === 'q') {
        process.stdin.setRawMode(false);
        process.stdin.pause();
        process.stdin.removeListener('data', onData);
        reject(new Error('User cancelled operation'));
        return;
      }

      // Enter key
      if (key === '\r' || key === '\n') {
        process.stdin.setRawMode(false);
        process.stdin.pause();
        process.stdin.removeListener('data', onData);
        resolve();
      }
    };

    process.stdin.on('data', onData);
  });
}

/**
 * Interface for a chat message
 */
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
  images?: string[]; // Add support for image paths
}

/**
 * Options for the Ollama API call
 */
export interface OllamaOptions {
  model: string;
  temperature?: number;
  top_p?: number;
  top_k?: number;
  stream?: boolean;
  stripThinkTag?: boolean;
  [key: string]: any; // Allow for additional options
}

export const OLLAMA_RESPONSE_CACHE_DIR = path.join(process.cwd(), '_ollama_response_cache');

/**
 * Creates a hash of the Ollama request to use as a cache key
 */
export function createRequestHash(messages: ChatMessage[], options: Record<string, any>): string {
  // Create a string representation of the input that includes all relevant data
  const inputStringBase = JSON.stringify({
    messages: messages.map(msg => ({
      ...msg,
      // Remove images array as we'll handle it separately
      images: undefined
    })),
    // Use all options provided for hashing, assuming they affect the output
    options: {
      ...options,
      // Exclude stream as it's often implicitly handled or irrelevant for caching content
      stream: undefined
    }
  });

  // Process images if any message contains them
  const imageHashes: string[] = [];
  for (const message of messages) {
    if (message.images && message.images.length > 0) {
      for (const imagePath of message.images) {
        try {
          // Read image file synchronously
          const imageBuffer = fs.readFileSync(imagePath);
          // Create hash of image content
          const imageHash = crypto.createHash('sha256').update(imageBuffer).digest('hex');
          imageHashes.push(`${imagePath}:${imageHash}`);
        } catch (error) {
          console.error(`Error hashing image ${imagePath}:`, error);
          // Include the path without hash if we can't read the file
          imageHashes.push(`${imagePath}:error`);
        }
      }
    }
  }

  // Combine base input string with image hashes
  const fullInputString = inputStringBase + (imageHashes.length > 0 ? JSON.stringify(imageHashes) : '');

  // Create a SHA-256 hash of the input string
  return crypto.createHash('sha256').update(fullInputString).digest('hex');
}

/**
 * Ensures the Ollama response cache directory exists
 */
export function ensureCacheDir(cacheDir: string): string {
  fs.mkdirSync(cacheDir, { recursive: true });
  return cacheDir;
}

/**
 * Gets the path to the cache file for a specific Ollama request hash
 */
export function getResponseCachePath(requestHash: string, cacheDir: string = OLLAMA_RESPONSE_CACHE_DIR): string {
  return path.join(ensureCacheDir(cacheDir), `response_${requestHash}.json`);
}

/**
 * Checks if a cached response exists for the given hash
 */
export function hasCachedResponse(requestHash: string, cacheDir?: string): boolean {
  const cachePath = getResponseCachePath(requestHash, cacheDir);
  return fs.existsSync(cachePath);
}

/**
 * Reads a cached response from disk
 */
export function readCachedResponse<T>(requestHash: string, cacheDir?: string): T {
  const cachePath = getResponseCachePath(requestHash, cacheDir);
  try {
    const cacheContent = fs.readFileSync(cachePath, 'utf-8');
    return JSON.parse(cacheContent);
  } catch (error) {
    console.error(`Error reading Ollama response cache: ${error}`);
    throw error;
  }
}

/**
 * Writes an Ollama response to the cache
 */
export function writeResponseCache<T>(requestHash: string, response: T, cacheDir?: string): void {
  const cachePath = getResponseCachePath(requestHash, cacheDir);
  try {
    fs.writeFileSync(cachePath, JSON.stringify(response, null, 2), 'utf-8');
    // console.log(`Ollama response cache written to: ${cachePath}`);
  } catch (error) {
    console.error(`Error writing Ollama response cache: ${error}`);
  }
}

/**
 * Response wrapper type for JSON responses from LLMs
 */
export type LLMJSONResult<T> =
  | { status: 'success'; content: T }
  | { status: 'failed' };

export const MAX_JSON_PARSE_ATTEMPTS = 3;

/**
 * Strips any <think>...</think> tags from text
 */
export function stripThinkTags(text: string): string {
  return text.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

/**
 * Interface for the function that makes the actual API call to the LLM backend.
 * It should handle client acquisition/release internally if necessary.
 */
export type ApiCallFn = (
  messages: ChatMessage[],
  options: Record<string, any> // Backend-specific options passed from the main function
) => Promise<AsyncGenerator<string, void, unknown>>; // Returns the raw content stream part by part


/**
 * Handles the common logic for generating a JSON response from an LLM,
 * including caching, retries, prompt handling, JSON parsing, and validation.
 *
 * @param messages - Array of chat messages.
 * @param options - Backend-specific options for the LLM API call.
 * @param apiCallFn - Function to execute the actual LLM API call and return a stream.
 * @param cacheOptions - Configuration for caching.
 * @param promptOptions - Configuration for displaying prompts and requiring confirmation.
 * @param jsonParsingOptions - Configuration for JSON extraction, repair, and validation.
 * @param logStream - Whether to log the raw stream to console.
 * @returns An object containing the stream (yielding the final full response) and a promise for the structured result.
 */
export async function handleLLMJsonResponseGeneration<T>({
  messages,
  options,
  apiCallFn,
  cacheOptions,
  promptOptions,
  jsonParsingOptions,
  logStream = true,
}: {
  messages: ChatMessage[];
  options: Record<string, any>; // Backend-specific options
  apiCallFn: ApiCallFn;
  cacheOptions: {
    enabled: boolean;
    requestHash: string; // Pre-calculated hash
    cacheDir?: string;
  };
  promptOptions: {
    printPrompt: boolean;
    requireConfirmation: boolean;
  };
  jsonParsingOptions: {
    maxAttempts: number;
    schema?: z.ZodType<T>;
    stripTags?: boolean; // Use stripThinkTags specifically
  };
  logStream?: boolean;
}): Promise<{
  stream: AsyncGenerator<string, void, unknown>,
  resultPromise: Promise<LLMJSONResult<T>>
}> {
  // --- Promise Setup ---
  let resultPromiseResolve!: (value: LLMJSONResult<T>) => void;
  let resultPromiseReject!: (reason: any) => void; // Not currently used but good practice
  const resultPromise = new Promise<LLMJSONResult<T>>((resolve, reject) => {
    resultPromiseResolve = resolve;
    resultPromiseReject = reject;
  });

  // --- Async IIFE to handle the generation logic ---
  (async () => {
    try {
      // --- Cache Check ---
      if (cacheOptions.enabled && hasCachedResponse(cacheOptions.requestHash, cacheOptions.cacheDir)) {
        try {
          const cachedResponse = readCachedResponse<any>(cacheOptions.requestHash, cacheOptions.cacheDir);
          if (jsonParsingOptions.schema) {
            const validatedResponse = jsonParsingOptions.schema.parse(cachedResponse);
            const dummyStream = async function* () { }();
            resultPromiseResolve({ status: 'success', content: validatedResponse });
            return; // Exit IIFE
          } else {
            const dummyStream = async function* () { }();
            resultPromiseResolve({ status: 'success', content: cachedResponse });
            return; // Exit IIFE
          }
        } catch (validationOrReadError) {
          console.warn('Error reading or validating cached response:', validationOrReadError);
          // Fall through to generate response
        }
      }

      // --- Prompt Display & Confirmation ---
      if (promptOptions.printPrompt || promptOptions.requireConfirmation) {
        const formattedPrompts = messages.map(message => {
          return message.role + ': ' + message.content.replace(/^```json\s*|\s*```$/g, '') +
            (message.images && message.images.length > 0 ? ` [Contains ${message.images.length} image(s)]` : '');
        }).join('\n\n');
        console.log(`${GREEN}Prompt:${RESET}`, formattedPrompts);
      }

      if (promptOptions.requireConfirmation) {
        try {
          await waitForEnterKey();
        } catch (error) {
          console.error('[handleLLMJsonResponseGeneration] User cancelled.');
          resultPromiseResolve({ status: 'failed' });
          return; // Exit IIFE
        }
      }

      // --- Stream Generation and Processing ---
      let attempt = 0;
      let currentMessages = [...messages];
      let finalValidatedResponse: T | null = null;
      let fullResponseAccumulated = '';

      while (attempt < jsonParsingOptions.maxAttempts && finalValidatedResponse === null) {
        attempt++;
        fullResponseAccumulated = ''; // Reset for each attempt

        if (attempt > 1) {
          console.log(`${CYAN}[handleLLMJsonResponseGeneration] Attempt ${attempt} to get valid JSON from LLM${RESET}`);
          // Update messages for retry attempts (Generic retry strategy)
          if (attempt === 2) {
            currentMessages = [
              { role: 'system', content: 'You MUST respond with valid, parseable JSON. Wrap your response in ```json and ``` tags. Your previous response could not be parsed.' },
              ...messages // Use original messages for subsequent retries
            ];
          } else { // attempt >= 3
            currentMessages = [
              { role: 'system', content: `CRITICAL: Attempt ${attempt}/${jsonParsingOptions.maxAttempts}. You MUST respond with ONLY valid, parseable JSON without any additional text. Wrap your response in \`\`\`json and \`\`\` tags.` },
              ...messages
            ];
          }
        }

        try {
          // Call the provided API function
          const responseStream = await apiCallFn(currentMessages, options);

          // Process the stream for this attempt
          for await (const part of responseStream) {
            fullResponseAccumulated += part; // Accumulate for final parsing
            // Log stream content if requested
            if (logStream) {
              process.stdout.write(`${CYAN}${part}${RESET}`);
            }
          }
          if (logStream) {
            process.stdout.write('\n'); // Newline after logging stream
          }

          // Strip think tags if enabled
          if (jsonParsingOptions.stripTags !== false) { // Default to true
            fullResponseAccumulated = stripThinkTags(fullResponseAccumulated);
          }

          // Attempt to parse the accumulated JSON
          let parsedJson: any = null;
          try {
            let jsonString = extractJsonFromResponse(fullResponseAccumulated);
            if (!jsonString) {
              console.warn(`[handleLLMJsonResponseGeneration] No JSON block found in response on attempt ${attempt}. Attempting repair on entire response.`);
              jsonString = fullResponseAccumulated; // Try repairing the whole thing
            }
            // Use jsonrepair for robustness
            parsedJson = JSON.parse(jsonrepair(jsonString));
          } catch (parseError) {
            console.warn(`${RED}[handleLLMJsonResponseGeneration] Error parsing/repairing JSON from LLM attempt ${attempt}: ${parseError}${RESET}`);
            console.warn(`${RED}[handleLLMJsonResponseGeneration] Full response on attempt ${attempt}:${RESET}\n`, fullResponseAccumulated);
            if (attempt >= jsonParsingOptions.maxAttempts) {
              throw new Error(`Failed to parse JSON after ${jsonParsingOptions.maxAttempts} attempts.`);
            }
            continue; // Go to the next attempt
          }

          // Validate with Zod schema if provided
          if (jsonParsingOptions.schema) {
            try {
              finalValidatedResponse = jsonParsingOptions.schema.parse(parsedJson);
            } catch (validationError) {
              console.error(`${RED}[handleLLMJsonResponseGeneration] Zod schema validation failed on attempt ${attempt}:${RESET}`);
              if (validationError instanceof z.ZodError) {
                console.error(`${RED}${JSON.stringify(validationError.format(), null, 2)}${RESET}`);
              } else {
                console.error(`${RED}${validationError}${RESET}`);
              }
              if (attempt >= jsonParsingOptions.maxAttempts) {
                throw new Error(`Failed schema validation after ${jsonParsingOptions.maxAttempts} attempts.`);
              }
              continue; // Go to the next attempt
            }
          } else {
            finalValidatedResponse = parsedJson as T; // Assume type T if no schema
          }

          // If we reached here, parsing and validation (if applicable) succeeded
          break; // Exit the retry loop

        } catch (apiError) {
          // Catch errors during the apiCallFn execution or stream processing
          console.error(`${RED}[handleLLMJsonResponseGeneration] Error during API call/stream processing on attempt ${attempt}: ${apiError}${RESET}`);
          if (attempt >= jsonParsingOptions.maxAttempts) {
            throw new Error(`API call/stream processing failed after ${jsonParsingOptions.maxAttempts} attempts.`);
          }
          // Optionally wait before retrying
          // await new Promise(resolve => setTimeout(resolve, 1000)); // Example delay
          continue; // Go to the next attempt
        }
      } // End while loop

      // Check if we succeeded after all attempts
      if (finalValidatedResponse === null) {
        throw new Error("Could not obtain valid JSON response after all attempts (safeguard).");
      }

      // --- Success Path ---
      if (cacheOptions.enabled) {
        writeResponseCache(cacheOptions.requestHash, finalValidatedResponse, cacheOptions.cacheDir);
      }
      resultPromiseResolve({ status: 'success', content: finalValidatedResponse });

    } catch (error) {
      console.error(`${RED}[handleLLMJsonResponseGeneration] Overall error: ${error}${RESET}`);
      resultPromiseResolve({ status: 'failed' });
    }
  })(); // Immediately invoke the async IIFE

  // --- Stream Return ---
  // This stream yields the *final* accumulated response once the resultPromise resolves successfully.
  // It doesn't stream during generation in this refactored version, as accumulation happens inside.
  const finalStream = async function* () {
    const result = await resultPromise;
    if (result.status === 'success') {
      // Yield the final validated object as a string, or the raw accumulated string?
      // Let's yield the stringified final object for consistency with expected JSON output.
      yield JSON.stringify(result.content, null, 2);
    } else {
      // Yield nothing or an error message if failed? Yielding nothing for now.
    }
  }();

  return { stream: finalStream, resultPromise };
}


/**
 * Specific implementation of ApiCallFn for Ollama using multi-ollama balancing.
 */
const _ollamaChatApiCall: ApiCallFn = async (
  messages: ChatMessage[],
  options: Record<string, any> // Use Record<string, any> to match ApiCallFn type
): Promise<AsyncGenerator<string, void, unknown>> => { // Return a Promise<AsyncGenerator>

  // Define the async generator function internally
  async function* ollamaStreamGenerator(): AsyncGenerator<string, void, unknown> {
    let client: Ollama | null = null;
    let releaseClient: (() => void) | null = null;

    try {
      const { client: acquiredClient, releaseClient: acquiredReleaseClient } = await getAvailableOllama();
      client = acquiredClient;
      releaseClient = acquiredReleaseClient;

      // Create the raw stream
      const responseStream = await client.chat({
        stream: true, // Always stream for the handler to consume
        messages: messages,
        model: options.model, // Access properties directly, assuming they exist
        format: options.format === 'json' ? 'json' : undefined, // Pass format if specified
        options: { // Pass other relevant options
          temperature: options.temperature,
          top_p: options.top_p,
          top_k: options.top_k,
          num_ctx: options.num_ctx, // Example: ensure common options are passed
          // Include any other arbitrary options passed in 'options' object
          ...Object.fromEntries(
            Object.entries(options).filter(([key]) =>
              !['model', 'stream', 'temperature', 'top_p', 'top_k', 'format', 'stripThinkTag', 'cache', 'json', 'printPrompt', 'requireConfirmation'].includes(key) && // Filter out handled/meta options
              options[key] !== undefined // Filter out undefined values
            )
          )
        }
      });

      // Yield content parts from the stream
      for await (const part of responseStream) {
        if (part.message) {
          yield part.message.content;
        }
      }
    } catch (error) {
      console.error(`${RED}[_ollamaChatApiCall] Error during Ollama API call: ${error}${RESET}`);
      // Re-throw the error to be caught by the handler's retry logic
      // Ensure the generator throws, which will reject the promise returned by handleLLMJsonResponseGeneration eventually
      throw error;
    } finally {
      // Ensure client is released
      if (releaseClient) {
        releaseClient();
      } else if (client) {
        // This case shouldn't happen with getAvailableOllama, but good practice
        console.warn('Client acquired but no release function provided.');
      }
    }
  }

  // Return the generator instance from the async function
  return ollamaStreamGenerator();
};

/**
 * Generates a JSON response from Ollama using the provided messages
 *
 * @param messages - Array of chat messages to send to Ollama
 * @param options - Configuration options for the Ollama API call
 * @param cache - Whether to use caching (defaults to true)
 * @param logStream - Whether to log the streaming response to console (defaults to true)
 * @param schema - Optional Zod schema for response validation and type inference
 * @returns An object containing the stream for immediate consumption and a promise that resolves when processing is complete
 */
export async function generateJSONResponseOllama<T>({
  messages,
  options, // This should be OllamaOptions specifically
  cache = true,
  logStream = true,
  printPrompt = false,
  requireConfirmation = false,
  schema,
}: {
  messages: ChatMessage[];
  options: Omit<OllamaOptions, 'stream'>; // OllamaOptions, stream is handled internally
  cache?: boolean;
  logStream?: boolean;
  printPrompt?: boolean;
  requireConfirmation?: boolean;
  schema?: z.ZodType<T>;
}): Promise<{
  stream: AsyncGenerator<string, void, unknown>,
  resultPromise: Promise<LLMJSONResult<T>>
}> {
  const funcStartTime = Date.now(); // Keep timing if needed

  // Calculate cache hash based on Ollama options
  const requestHash = createRequestHash(messages, options);

  // Call the generic handler with Ollama-specific API call function and options
  const result = await handleLLMJsonResponseGeneration<T>({
    messages,
    options, // Pass OllamaOptions directly
    apiCallFn: _ollamaChatApiCall,
    cacheOptions: {
      enabled: cache,
      requestHash: requestHash,
      cacheDir: OLLAMA_RESPONSE_CACHE_DIR, // Use Ollama-specific cache dir
    },
    promptOptions: {
      printPrompt,
      requireConfirmation,
    },
    jsonParsingOptions: {
      maxAttempts: MAX_JSON_PARSE_ATTEMPTS,
      schema,
      stripTags: options.stripThinkTag !== false, // Use stripThinkTag option
    },
    logStream,
  });

  // Optional: Add timing log if desired
  result.resultPromise.then(() => {
    const duration = Date.now() - funcStartTime;
    // console.log(`[generateJSONResponseOllama] Total time: ${duration}ms`);
  }).catch(() => {/* ignore */ }); // Prevent unhandled rejection if promise fails


  return result;
}

/**
 * Generates a response from Ollama using the provided messages
 * 
 * @param messages - Array of chat messages to send to Ollama
 * @param options - Configuration options for the Ollama API call
 * @returns The full response text from Ollama
 */
export async function generateResponseOllama(
  { messages, options }: { messages: ChatMessage[]; options: OllamaOptions & { json?: boolean; cache?: boolean; }; }): Promise<string> {
  // Get the auto-releasing client
  let releaseClient: (() => void) | null = null;
  const funcStartTime = Date.now(); // Add timing for this function too if desired
  try {
    // If JSON response is requested, use the JSON-specific function
    if (options.json) {
      // Use the refactored function
      const jsonResponse = await generateJSONResponseOllama<any>({
        messages,
        options, // Pass OllamaOptions
        cache: options.cache !== false,
        schema: undefined, // No schema by default for this generic call
        printPrompt: false, // Defaults for generic call
        requireConfirmation: false,
      });
      const result = await jsonResponse.resultPromise;
      if (result.status === 'failed') {
        // Attempt to return the raw content from the stream if result failed
        let rawContent = '';
        try {
          // The stream from generateJSONResponseOllama now yields the final stringified JSON
          for await (const chunk of jsonResponse.stream) {
            rawContent += chunk;
          }
          // If rawContent is empty, means the promise failed before yielding
          if (!rawContent) {
            console.warn('[generateResponseOllama] JSON generation failed, returning empty object string.');
            return '{}';
          }
        } catch (streamError) {
          console.warn('Error reading stream after JSON generation failed:', streamError);
          return '{}'; // Return empty JSON object on stream error
        }
        // If we got content, it should be the stringified JSON already
        return rawContent;
      }
      // Success case, result.content is the parsed object, stringify it
      return JSON.stringify(result.content, null, 2);
    }

    // --- Non-JSON response handling (remains mostly the same) ---
    const { client, releaseClient: acquiredReleaseClient } = await getAvailableOllama();
    releaseClient = acquiredReleaseClient;

    // Check cache if enabled (defaults to true)
    const cacheEnabled = options.cache !== false;
    const requestHash = createRequestHash(messages, options); // Use generic hash function
    if (cacheEnabled) {
      if (hasCachedResponse(requestHash)) {
        const cachedResponse = readCachedResponse<string>(requestHash);
        if (releaseClient) releaseClient(); // Release client early for cache hit
        return cachedResponse;
      }
    }

    // Set default stream to true if not specified
    const streamEnabled = options.stream !== false;

    // Create a deep copy of the messages to add images properly
    const ollama_messages = messages.map(msg => {
      const msgCopy = { ...msg };
      // No special processing needed for images as the Ollama client handles them
      return msgCopy;
    });

    let fullResponse = '';
    if (streamEnabled) {
      // Handle streaming response
      const response = await client.chat({
        model: options.model,
        options: {
          temperature: options.temperature ?? 0.0,
          top_p: options.top_p,
          top_k: options.top_k,
          num_ctx: 1024 * 6,
          // Include any other options
          ...Object.fromEntries(
            Object.entries(options).filter(([key]) =>
              !['model', 'stream', 'json', 'cache', 'stripThinkTag'].includes(key) && options[key] !== undefined
            )
          )
        },
        messages: ollama_messages,
        stream: true
      });

      for await (const part of response) {
        const content = part.message.content;
        process.stdout.write(`${CYAN}${content}${RESET}`);
        fullResponse += content;
      }
      process.stdout.write('\n'); // Add newline after streaming

    } else {
      // Handle non-streaming response
      const response = await client.chat({
        model: options.model,
        options: {
          temperature: options.temperature ?? 0.7,
          top_p: options.top_p,
          top_k: options.top_k,
          num_ctx: 1024 * 6,
          // Include any other options
          ...Object.fromEntries(
            Object.entries(options).filter(([key]) =>
              !['model', 'stream', 'json', 'cache', 'stripThinkTag'].includes(key) && options[key] !== undefined
            )
          )
        },
        messages: ollama_messages,
        stream: false
      });

      fullResponse = response.message.content;
    }

    // Strip think tags if enabled (defaults to true)
    if (options.stripThinkTag !== false) {
      fullResponse = stripThinkTags(fullResponse);
    }

    // Cache the response if enabled
    if (cacheEnabled) {
      writeResponseCache(requestHash, fullResponse);
    }

    return fullResponse;

  } catch (error) {
    console.error('Error generating response from Ollama:', error);
    console.error("Messages:", messages);
    throw error;
  } finally {
    if (releaseClient) {
      releaseClient();
    } else {
      // This path might be hit if getAvailableOllama fails initially in non-JSON case
      console.warn('[generateResponseOllama] releaseClient function was not available in finally block, cannot release.');
    }
  }
}

/**
 * Extracts JSON content from a response string
 * 
 * @param response - The full response text from Ollama
 * @returns The extracted JSON content, or null if no valid JSON was found
 */
export function extractJsonFromResponse(response: string): string | null {
  const allJsonMatches = Array.from(response.matchAll(/```json([\s\S]*?)```/g) ?? []);
  const lastJsonMatch = allJsonMatches[allJsonMatches.length - 1] ?? null;
  return lastJsonMatch?.[1]?.trim() ?? null;
}

export function extractLineByLineFromResponse(response: string): string | null {
  const allLineByLineMatches = Array.from(response.matchAll(/```line_by_line([\s\S]*?)```/g) ?? []);
  const lastLineByLineMatch = allLineByLineMatches[allLineByLineMatches.length - 1] ?? null;
  return lastLineByLineMatch?.[1]?.trim() ?? null;
}
