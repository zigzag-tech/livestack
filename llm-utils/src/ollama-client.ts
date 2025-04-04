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
async function waitForEnterKey(): Promise<void> {
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

const OLLAMA_RESPONSE_CACHE_DIR = path.join(process.cwd(), '_ollama_response_cache');

/**
 * Creates a hash of the Ollama request to use as a cache key
 */
function createOllamaRequestHash(messages: ChatMessage[], options: Omit<OllamaOptions, 'stream'>): string {
  // Create a string representation of the input that includes all relevant data
  const inputStringBase = JSON.stringify({
    messages: messages.map(msg => ({
      ...msg,
      // Remove images array as we'll handle it separately
      images: undefined
    })),
    options: {
      ...options,
      // Exclude stream as it's always false for JSON responses
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
function ensureOllamaCacheDir(): string {
  fs.mkdirSync(OLLAMA_RESPONSE_CACHE_DIR, { recursive: true });
  return OLLAMA_RESPONSE_CACHE_DIR;
}

/**
 * Gets the path to the cache file for a specific Ollama request hash
 */
function getOllamaCachePath(requestHash: string): string {
  return path.join(ensureOllamaCacheDir(), `response_${requestHash}.json`);
}

/**
 * Checks if a cached response exists for the given hash
 */
function hasCachedResponse(requestHash: string): boolean {
  const cachePath = getOllamaCachePath(requestHash);
  return fs.existsSync(cachePath);
}

/**
 * Reads a cached response from disk
 */
function readCachedResponse<T>(requestHash: string): T {
  const cachePath = getOllamaCachePath(requestHash);
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
function writeResponseCache<T>(requestHash: string, response: T): void {
  const cachePath = getOllamaCachePath(requestHash);
  try {
    fs.writeFileSync(cachePath, JSON.stringify(response, null, 2), 'utf-8');
    // console.log(`Ollama response cache written to: ${cachePath}`);
  } catch (error) {
    console.error(`Error writing Ollama response cache: ${error}`);
  }
}

/**
 * Response wrapper type for JSON responses from Ollama
 */
export type OllamaJSONResult<T> = 
  | { status: 'success'; content: T }
  | { status: 'failed' };

const MAX_JSON_PARSE_ATTEMPTS = 3;

/**
 * Strips any <think>...</think> tags from text
 */
function stripThinkTags(text: string): string {
  return text.replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}

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
  options, 
  cache = true, 
  logStream = true, 
  printPrompt = false,
  requireConfirmation = false,
  schema,
}: {
  messages: ChatMessage[]; 
  options: Omit<OllamaOptions, 'stream'>; 
  cache?: boolean; 
  logStream?: boolean; 
  printPrompt?: boolean;
  requireConfirmation?: boolean;
  schema?: z.ZodType<T>;
}): Promise<{
  stream: AsyncGenerator<string, void, unknown>,
  resultPromise: Promise<OllamaJSONResult<T>>
}> {
  // --- Promise Setup --- 
  let resultPromiseResolve!: (value: OllamaJSONResult<T>) => void; // Use definite assignment assertion
  let resultPromiseReject!: (reason: any) => void;             // Use definite assignment assertion
  const resultPromise = new Promise<OllamaJSONResult<T>>((resolve, reject) => {
    resultPromiseResolve = resolve;
    resultPromiseReject = reject;
  });

  // --- Get Client (Separate Try/Catch) --- 
  let client: Ollama;
  let releaseClient: (() => void) | null = null;
  try {
    const { client: acquiredClient, releaseClient: acquiredReleaseClient } = await getAvailableOllama();
    client = acquiredClient;
    releaseClient = acquiredReleaseClient;
  } catch (clientError) {
    console.error(`${RED}[generateJSONResponseOllama] Failed to get Ollama client: ${clientError}${RESET}`);
    resultPromiseResolve({ status: 'failed' }); // Resolve as failed
    const dummyStream = async function*() {}();
    return { stream: dummyStream, resultPromise }; // Return early
  }

  // --- Main Logic (Try/Catch) --- 
  const funcStartTime = Date.now();
  try {
    // --- Cache Check --- 
    if (cache) {
      const requestHash = createOllamaRequestHash(messages, options);
      if (hasCachedResponse(requestHash)) {
        try {
          const cachedResponse = readCachedResponse<any>(requestHash);
          if (schema) {
            const validatedResponse = schema.parse(cachedResponse);
            const dummyStream = async function*() {}();
            if (releaseClient) releaseClient(); // Release client early
            resultPromiseResolve({ status: 'success', content: validatedResponse }); 
            return { stream: dummyStream, resultPromise };
          } else {
            const dummyStream = async function*() {}();
            if (releaseClient) releaseClient(); // Release client early
            resultPromiseResolve({ status: 'success', content: cachedResponse }); 
            return { stream: dummyStream, resultPromise };
          }
        } catch (validationOrReadError) {
          console.warn('Error reading or validating cached response:', validationOrReadError);
          // Fall through
        }
      }
    }

    // --- Prompt Display & Confirmation --- 
    if(printPrompt || requireConfirmation) {
        const formattedPrompts = messages.map(message => {
          return message.role + ': ' + message.content.replace(/^```json\s*|\s*```$/g, '') + 
            (message.images && message.images.length > 0 ? ` [Contains ${message.images.length} image(s)]` : '');
        }).join('\n\n');
        console.log(`${GREEN}Prompt:${RESET}`, formattedPrompts);
    }
    
    if(requireConfirmation) {
      try {
        await waitForEnterKey();
      } catch (error) {
        console.error('[generateJSONResponseOllama] User cancelled.');
        const dummyStream = async function*() {}();
        if (releaseClient) releaseClient(); // Release client on cancellation
        resultPromiseResolve({ status: 'failed' }); 
        return { stream: dummyStream, resultPromise };
      }
    }

    // --- Stream Generation and Processing --- 
    let attempt = 0;
    let currentMessages = [...messages];
    let finalValidatedResponse: T | null = null;
    let fullResponseAccumulated = '';

    while (attempt < MAX_JSON_PARSE_ATTEMPTS && finalValidatedResponse === null) {
        attempt++;
        fullResponseAccumulated = ''; // Reset for each attempt
        let currentAttemptClient = client; // Use initial client for first attempt
        let currentAttemptReleaseClient = releaseClient;
  
        if (attempt > 1) {
          console.log(`${CYAN}[generateJSONResponseOllama] Attempt ${attempt} to get valid JSON from LLM${RESET}`);
          // Update messages for retry attempts
          if (attempt === 2) {
            currentMessages = [
              { role: 'system', content: 'You MUST respond with valid, parseable JSON. Wrap your response in ```json and ``` tags. Your previous response could not be parsed.' },
              ...messages
            ];
          } else { // attempt === 3
            currentMessages = [
              { role: 'system', content: 'CRITICAL: This is the final attempt. You MUST respond with ONLY valid, parseable JSON without any additional text. Wrap your response in ```json and ``` tags.' },
              ...messages
            ];
          }
  
          // Release previous client and get a new one for retry
          // Note: We don't release the *initial* client (releaseClient) here, only the one from the previous attempt.
          if (currentAttemptReleaseClient && currentAttemptClient !== client) {
            currentAttemptReleaseClient();
          }
          const { client: retryClient, releaseClient: retryReleaseClient } = await getAvailableOllama();
          currentAttemptClient = retryClient;
          currentAttemptReleaseClient = retryReleaseClient;
        }
  
        try {
          // Create the raw stream for the current attempt
          const responseStream = await currentAttemptClient.chat({
            stream: true,
            messages: currentMessages, 
            model: options.model,
            format: options.format === 'json' ? 'json' : undefined,
            options: {
              temperature: options.temperature,
              top_p: options.top_p,
              top_k: options.top_k,
              ...Object.fromEntries(
                Object.entries(options).filter(([key]) =>
                  !['model', 'stream', 'temperature', 'top_p', 'top_k', 'format', 'stripThinkTag'].includes(key)
                )
              )
            }
          });
  
          // Process the stream for this attempt
          for await (const part of responseStream) {
            if (part.message) {
              const content = part.message.content;
              fullResponseAccumulated += content; // Accumulate for final parsing
              // Log stream content if requested (primarily for debugging)
              if (logStream) {
                  process.stdout.write(`${CYAN}${content}${RESET}`);
              }
            }
          }
          if (logStream) {
              process.stdout.write('\n'); // Newline after logging stream
          }
  
          // Strip think tags if enabled
          if (options.stripThinkTag !== false) {
            fullResponseAccumulated = stripThinkTags(fullResponseAccumulated);
          }
  
          // Attempt to parse the accumulated JSON
          let parsedJson: any = null;
          try {
            let jsonString = extractJsonFromResponse(fullResponseAccumulated);
            if (!jsonString) {
              console.warn(`[generateJSONResponseOllama] No JSON block found in response on attempt ${attempt}. Attempting repair on entire response.`);
              jsonString = fullResponseAccumulated;
            } 
            parsedJson = JSON.parse(jsonrepair(jsonString));
          } catch (parseError) {
            console.warn(`${RED}[generateJSONResponseOllama] Error parsing JSON from LLM attempt ${attempt}: ${parseError}${RESET}`);
            console.warn(`${RED}[generateJSONResponseOllama] Full response on attempt ${attempt}:${RESET}\n`, fullResponseAccumulated);
            if (attempt >= MAX_JSON_PARSE_ATTEMPTS) {
              throw new Error(`Failed to parse JSON after ${MAX_JSON_PARSE_ATTEMPTS} attempts.`);
            }
            continue; // Go to the next attempt
          }
  
          // Validate with Zod schema if provided
          if (schema) {
            try {
              finalValidatedResponse = schema.parse(parsedJson);
            } catch (validationError) {
              console.error(`${RED}[generateJSONResponseOllama] Zod schema validation failed on attempt ${attempt}:${RESET}`);
              if (validationError instanceof z.ZodError) {
                 console.error(`${RED}${JSON.stringify(validationError.format(), null, 2)}${RESET}`);
              } else {
                 console.error(`${RED}${validationError}${RESET}`);
              }
              if (attempt >= MAX_JSON_PARSE_ATTEMPTS) {
                throw new Error(`Failed schema validation after ${MAX_JSON_PARSE_ATTEMPTS} attempts.`);
              }
              continue; // Go to the next attempt
            }
          } else {
            finalValidatedResponse = parsedJson as T; // Assume type T if no schema
          }
  
          // If we reached here, parsing and validation (if applicable) succeeded
          break; // Exit the retry loop
  
        } finally {
           // Release client used for this attempt IF it was a retry client
           if (currentAttemptReleaseClient && currentAttemptClient !== client) {
               currentAttemptReleaseClient();
           }
        }
    }

    // Check if we succeeded after all attempts
    if (finalValidatedResponse === null) {
        throw new Error("Could not obtain valid JSON response after all attempts (safeguard)." );
    }

    // --- Success Path --- 
    if (cache) {
      const requestHash = createOllamaRequestHash(messages, options);
      writeResponseCache(requestHash, finalValidatedResponse); 
    }
    resultPromiseResolve({ status: 'success', content: finalValidatedResponse });
    
    const finalStream = async function*() {
        yield fullResponseAccumulated;
    }();

    if (releaseClient) {
        releaseClient();
        releaseClient = null; // Mark as released
    }
    return { stream: finalStream, resultPromise };

  } catch (error) {
    console.error(`${RED}[generateJSONResponseOllama] Error during generation/processing: ${error}${RESET}`);
    resultPromiseResolve({ status: 'failed' }); 
    
    if (releaseClient) {
      releaseClient(); // Ensure release on error
    }
    
    const dummyStream = async function*() {}();
    return { stream: dummyStream, resultPromise };
  }
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
    const { client, releaseClient: acquiredReleaseClient } = await getAvailableOllama();
    releaseClient = acquiredReleaseClient;
    // If JSON response is requested, use the JSON-specific function
    if (options.json) {
      const jsonResponse = await generateJSONResponseOllama<any>({ messages, options });
      const result = await jsonResponse.resultPromise;
      if (result.status === 'failed') {
        // Attempt to return the raw content from the stream if result failed
        let rawContent = '';
        try {
          for await (const chunk of jsonResponse.stream) {
            rawContent += chunk;
          }
        } catch (streamError) {
          console.warn('Error reading stream after JSON generation failed:', streamError);
        }
        return rawContent || '{}'; // Return raw content or empty JSON object
      }
      return JSON.stringify(result.content, null, 2);
    }

    // Check cache if enabled (defaults to true)
    const cacheEnabled = options.cache !== false;
    if (cacheEnabled) {
      const requestHash = createOllamaRequestHash(messages, options);
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
              !['model', 'stream', 'json', 'cache', 'stripThinkTag'].includes(key)
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
              !['model', 'stream', 'json', 'cache', 'stripThinkTag'].includes(key)
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
      const requestHash = createOllamaRequestHash(messages, options);
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
