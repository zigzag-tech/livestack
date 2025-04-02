import * as fs from 'fs';
import * as path from 'path';
import { getAvailableOllama } from './multi-ollama'; // Import the new function
import * as crypto from 'crypto';
import { z } from 'zod';
import { Ollama } from 'ollama'; // Import the Ollama type

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
export type OllamaJSONResponse<T> = 
  | { status: 'success'; result: T; stream?: AsyncGenerator<string, void, unknown> }
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
 * @returns A wrapped response containing either the parsed JSON of type T or a failure status
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
}): Promise<OllamaJSONResponse<T>> {
  // Get the auto-releasing client
  let releaseClient: (() => void) | null = null;
  const funcStartTime = Date.now();
  try {
    // Get the client and its specific release function
    const { client, releaseClient: acquiredReleaseClient } = await getAvailableOllama();
    releaseClient = acquiredReleaseClient; // Assign release func to outer scope

    // Check cache if enabled
    if (cache) {
      const requestHash = createOllamaRequestHash(messages, options);
      if (hasCachedResponse(requestHash)) {
        const cachedResponse = readCachedResponse<any>(requestHash);
        if (schema) {
          try {
            const validatedResponse = schema.parse(cachedResponse);
            return { status: 'success', result: validatedResponse };
          } catch (validationError) {
            if (validationError instanceof z.ZodError) {
              console.warn('Cached response failed schema validation:');
              console.error(`${RED}${JSON.stringify(validationError.format(), null, 2)}${RESET}`);
            } else {
              console.warn('Cached response failed schema validation:', validationError);
            }
            // Fall through to generate new response
          }
        } else {
          return { status: 'success', result: cachedResponse };
        }
      }
    }

    if(printPrompt || requireConfirmation) {
      // use a different color (green) for the prompt
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
        return { status: 'failed' };
      }
    }

    let fullResponse = '';
    let streamResult: AsyncGenerator<string, void, unknown> | undefined;
    let parsedJson: T | null = null;
    let parseAttempts = 0;

    // Make up to MAX_JSON_PARSE_ATTEMPTS to get valid JSON from LLM
    while (parseAttempts < MAX_JSON_PARSE_ATTEMPTS && parsedJson === null) {
      parseAttempts++;
      
      if (parseAttempts > 1) {
        console.log(`${CYAN}[generateJSONResponseOllama] Making attempt ${parseAttempts} to get valid JSON from LLM${RESET}`);
        
        // For retries, we can add a system message to encourage valid JSON output
        if (parseAttempts === 2) {
          messages = [
            { role: 'system', content: 'You MUST respond with valid, parseable JSON. Wrap your response in ```json and ``` tags. Your previous response could not be parsed.' },
            ...messages
          ];
        } else if (parseAttempts === 3) {
          messages = [
            { role: 'system', content: 'CRITICAL: This is the final attempt. You MUST respond with ONLY valid, parseable JSON without any additional text. Wrap your response in ```json and ``` tags.' },
            ...messages
          ];
        }
      }

      try {
        fullResponse = '';
        let chunkCounter = 0;
        const responseStream = await client.chat({
          stream: true, // Always stream for JSON extraction
          messages, // Convert message format if needed
          model: options.model,
          format: options.format === 'json' ? 'json' : undefined,
          options: {
            temperature: options.temperature,
            top_p: options.top_p,
            top_k: options.top_k,
            // Include any other specific Ollama options passed in
            ...Object.fromEntries(
              Object.entries(options).filter(([key]) =>
                !['model', 'stream', 'temperature', 'top_p', 'top_k', 'format', 'stripThinkTag'].includes(key)
              )
            )
          }
        });

        const streamProcessor = async function*() {
          for await (const part of responseStream) {
            chunkCounter++;
            if (part.message) {
              const content = part.message.content;
              fullResponse += content;
              if (logStream) {
                process.stdout.write(`${CYAN}${content}${RESET}`);
              }
              // Yield the content for potential external consumers of the stream
              yield content;
            }
          }
          if (logStream && chunkCounter > 0) {
            process.stdout.write('\n'); // Add newline after streaming is done
          }
        };

        streamResult = streamProcessor();
        // Consume the stream fully to build fullResponse
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        for await (const _ of streamResult) {}

        // Strip think tags if enabled (defaults to true)
        if (options.stripThinkTag !== false) {
          const originalLength = fullResponse.length;
          fullResponse = stripThinkTags(fullResponse);
          if(fullResponse.length < originalLength) {
            // Optional: Log if tags were stripped
          }
        }

        // Attempt to parse the JSON from this LLM attempt
        try {
          let jsonString = extractJsonFromResponse(fullResponse);
          if (!jsonString) {
            console.warn(`[generateJSONResponseOllama] No JSON block found in response on attempt ${parseAttempts}. Attempting to parse entire response.`);
            // Optionally try parsing the whole thing if no block found
            jsonString = fullResponse;
          }
          parsedJson = JSON.parse(jsonString);
          // If we get here, parsing succeeded
        } catch (parseError) {
          console.warn(`${RED}[generateJSONResponseOllama] Error parsing JSON from LLM attempt ${parseAttempts}: ${parseError}${RESET}`);
          // Continue to next attempt if we have retries left
          if (parseAttempts >= MAX_JSON_PARSE_ATTEMPTS) {
            console.error(`${RED}[generateJSONResponseOllama] Failed to get valid JSON after ${MAX_JSON_PARSE_ATTEMPTS} LLM attempts.${RESET}`);
            return { status: 'failed' };
          }
        }
      } catch (apiError) {
        console.error(`${RED}[generateJSONResponseOllama] Error during Ollama API call on attempt ${parseAttempts}: ${apiError}${RESET}`);
        if (parseAttempts >= MAX_JSON_PARSE_ATTEMPTS) {
          return { status: 'failed' };
        }
        // Continue to next attempt
      }
    }

    if (!parsedJson) {
      console.error('[generateJSONResponseOllama] Could not obtain parsed JSON after all LLM attempts.');
      return { status: 'failed' };
    }

    let validatedResponse: T = parsedJson;
    // Validate with Zod schema if provided
    if (schema) {
      try {
        validatedResponse = schema.parse(parsedJson);
      } catch (validationError) {
        if (validationError instanceof z.ZodError) {
          console.error(`${RED}[generateJSONResponseOllama] Zod schema validation failed:${RESET}`);
          console.error(`${RED}${JSON.stringify(validationError.format(), null, 2)}${RESET}`);
        } else {
          console.error(`${RED}[generateJSONResponseOllama] Zod schema validation failed: ${validationError}${RESET}`);
        }
        return { status: 'failed' };
      }
    }

    // Write to cache if enabled
    if (cache) {
      const requestHash = createOllamaRequestHash(messages, options);
      writeResponseCache(requestHash, validatedResponse); // Cache the validated response
    }

    // Return success with the validated response (or parsed if no schema)
    // Also return the stream generator in case the caller wants to replay it
    return { status: 'success', result: validatedResponse };

  } finally {
    // Ensure the client is released back to the pool
    if (releaseClient) {
      releaseClient();
    } else {
      console.warn('[generateJSONResponseOllama] releaseClient function was not available in finally block, cannot release.');
    }
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
      if (jsonResponse.status === 'failed') {
        return '{}'; // Return empty JSON object if parsing failed
      }
      return JSON.stringify(jsonResponse.result, null, 2);
    }

    // Check cache if enabled (defaults to true)
    const cacheEnabled = options.cache !== false;
    if (cacheEnabled) {
      const requestHash = createOllamaRequestHash(messages, options);
      if (hasCachedResponse(requestHash)) {
        const cachedResponse = readCachedResponse<string>(requestHash);
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
              !['model', 'stream'].includes(key)
            )
          )
        },
        messages: ollama_messages,
        stream: true
      });

      let fullResponse = '';
      for await (const part of response) {
        process.stdout.write(`${CYAN}${part.message.content}${RESET}`);
        fullResponse += part.message.content;
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
    } else {
      // Handle non-streaming response
      const response = await client.chat({
        model: options.model,
        options: {
          temperature: options.temperature ?? 0.7,
          top_p: options.top_p,
          top_k: options.top_k,
          // Include any other options
          ...Object.fromEntries(
            Object.entries(options).filter(([key]) => 
              !['model', 'stream'].includes(key)
            )
          )
        },
        messages: ollama_messages,
        stream: false
      });
      
      let content = response.message.content;
      
      // Strip think tags if enabled (defaults to true)
      if (options.stripThinkTag !== false) {
        content = stripThinkTags(content);
      }

      // Cache the response if enabled
      if (cacheEnabled) {
        const requestHash = createOllamaRequestHash(messages, options);
        writeResponseCache(requestHash, content);
      }
      
      return content;
    }
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
