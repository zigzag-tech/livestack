import  { Ollama } from 'ollama';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import { z } from 'zod';

// ANSI color codes
const CYAN = '\x1b[36m';
const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const RESET = '\x1b[0m';

const OLLAMA_HOST = process.env.OLLAMA_HOST || 'http://127.0.0.1:11434';
const ollama = new Ollama({ host: OLLAMA_HOST })

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

  try {
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
    
    // If confirmation is required, wait for user to press Enter
    if (requireConfirmation) {
      try {
        await waitForEnterKey();
        console.log(`${GREEN}Proceeding with Ollama request...${RESET}`);
      } catch (error) {
        console.log(`${RED}Operation cancelled by user${RESET}`);
        throw error;
      }
    }

    for (let attempt = 1; attempt <= MAX_JSON_PARSE_ATTEMPTS; attempt++) {
      let fullResponse = '';

      try {
        // Force JSON format but enable streaming for console output
        // Create a deep copy of the messages to add images properly
        const ollama_messages = messages.map(msg => {
          const msgCopy = { ...msg };
          
          // If the message has images, prepare them for ollama API
          if (msg.images && msg.images.length > 0) {
            // Keep the images array as is, the Ollama client will handle the conversion
            // No special processing needed here
          }
          
          return msgCopy;
        });

        // Call Ollama with the prepared messages
        const response = await ollama.chat({
          model: options.model,
          format: 'json',
          options: {
            temperature: options.temperature ?? 0.2, // Lower default temperature for JSON responses
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
          stream: true // Enable streaming for console output
        });

        if (logStream) {
          process.stdout.write(`\nStreaming response${attempt > 1 ? ` (attempt ${attempt}/${MAX_JSON_PARSE_ATTEMPTS})` : ''}: `);
        }
        
        // Buffer to store the stream chunks for later replay
        const streamParts: string[] = [];
        
        // Create the stream generator that will be returned
        const streamGenerator = (async function* () {
          for (const part of streamParts) {
            yield part;
          }
        })();
        
        // Collect streamed response while printing to console
        for await (const part of response) {
          const content = part.message.content;
          if (logStream) {
            process.stdout.write(`${CYAN}${content}${RESET}`);
          }
          // Store the content for later replay through the generator
          streamParts.push(content);
          fullResponse += content;
        }
        if (logStream) {
          process.stdout.write('\n'); // Add newline after streaming
        }

        // Strip think tags if enabled (defaults to true)
        if (options.stripThinkTag !== false) {
          fullResponse = stripThinkTags(fullResponse);
        }

        // Parse the complete response as JSON
        const parsedResponse = JSON.parse(fullResponse);
        
        // Validate against schema if provided
        if (schema) {
          const validatedResponse = schema.parse(parsedResponse);
          if (cache) {
            const requestHash = createOllamaRequestHash(messages, options);
            writeResponseCache(requestHash, validatedResponse);
          }
          return { status: 'success', result: validatedResponse, stream: streamGenerator };
        } else {
          if (cache) {
            const requestHash = createOllamaRequestHash(messages, options);
            writeResponseCache(requestHash, parsedResponse);
          }
          return { status: 'success', result: parsedResponse as T, stream: streamGenerator };
        }
      } catch (parseError) {
        console.error(parseError, "data", fullResponse);
        console.warn(`Error parsing JSON response (attempt ${attempt}/${MAX_JSON_PARSE_ATTEMPTS}).`);
        console.warn("Full response:", fullResponse);
        if (attempt === MAX_JSON_PARSE_ATTEMPTS) {
          console.error('All attempts to parse JSON response failed');
          return { status: 'failed' };
        }
        // Wait briefly before retrying
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }

    return { status: 'failed' };
  } catch (error) {
    console.error('Error generating JSON response from Ollama:', error);
    console.error("Messages:", messages);
    return { status: 'failed' };
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
  try {
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
      const response = await ollama.chat({
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
      const response = await ollama.chat({
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

/**
 * Calculates the Levenshtein distance between two strings
 * 
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns The Levenshtein distance (number of edits needed to transform str1 into str2)
 */
export function levenshteinDistance(str1: string, str2: string): number {
  const m = str1.length;
  const n = str2.length;
  const dp: number[][] = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));

  for (let i = 0; i <= m; i++) dp[i][0] = i;
  for (let j = 0; j <= n; j++) dp[0][j] = j;

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (str1[i - 1] === str2[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1];
      } else {
        dp[i][j] = Math.min(
          dp[i - 1][j - 1] + 1,  // substitution
          dp[i - 1][j] + 1,      // deletion
          dp[i][j - 1] + 1       // insertion
        );
      }
    }
  }
  return dp[m][n];
}

/**
 * Calculates the similarity ratio between two strings (0 to 1)
 * 
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns A value between 0 (completely different) and 1 (identical)
 */
export function stringSimilarity(str1: string, str2: string): number {
  const maxLength = Math.max(str1.length, str2.length);
  if (maxLength === 0) return 1.0; // Both strings are empty
  const distance = levenshteinDistance(str1, str2);
  return 1 - distance / maxLength;
} 

const EMBEDDING_MODEL = 'nomic-embed-text'; // Ollama embedding model

/**
 * Generate embedding for a text description using Ollama
 * @param text Text to generate embeddings for
 * @returns Array of embedding values
 */
 async function genEmbeddingOllama(text: string): Promise<number[]> {
  try {
    const response = await ollama.embeddings({
      model: EMBEDDING_MODEL,
      prompt: text,
    });
    
    return response.embedding;
  } catch (error) {
    console.error('Error generating embedding:', error);
    throw error;
  }
}



export class EmbeddingCache {
  private cacheDir: string;
  private cache: Map<string, number[]>;

  constructor(cacheDir: string = '.cache/embeddings') {
    this.cacheDir = cacheDir;
    this.cache = new Map();
    this.initializeCache();
  }

  private initializeCache() {
    // Create cache directory if it doesn't exist
    if (!fs.existsSync(this.cacheDir)) {
      fs.mkdirSync(this.cacheDir, { recursive: true });
    }
  }

  private getHash(text: string): string {
    return crypto.createHash('sha256').update(text).digest('hex');
  }

  private getCacheFilePath(hash: string): string {
    return path.join(this.cacheDir, `${hash}.bin`);
  }

  private arrayToBuffer(arr: number[]): Buffer {
    const buffer = Buffer.alloc(arr.length * Float32Array.BYTES_PER_ELEMENT);
    const view = new Float32Array(buffer.buffer);
    arr.forEach((val, i) => view[i] = val);
    return buffer;
  }

  private bufferToArray(buffer: Buffer): number[] {
    const view = new Float32Array(buffer.buffer, buffer.byteOffset, buffer.length / Float32Array.BYTES_PER_ELEMENT);
    return Array.from(view);
  }

  async getEmbedding(text: string): Promise<number[]> {
    const hash = this.getHash(text);

    // Check in-memory cache first
    if (this.cache.has(hash)) {
      return this.cache.get(hash)!;
    }

    // Check file cache
    const cacheFilePath = this.getCacheFilePath(hash);
    if (fs.existsSync(cacheFilePath)) {
      const buffer = fs.readFileSync(cacheFilePath);
      const embedding = this.bufferToArray(buffer);
      this.cache.set(hash, embedding);
      return embedding;
    }

    // Generate new embedding
    const embedding = await genEmbeddingOllama(text);
    
    // Save to both caches
    this.cache.set(hash, embedding);
    const buffer = this.arrayToBuffer(embedding);
    fs.writeFileSync(cacheFilePath, buffer);

    return embedding;
  }

  /**
   * Calculate cosine similarity between two embeddings
   */
  getCosineSimilarity(embedding1: number[], embedding2: number[]): number {
    if (!Array.isArray(embedding1) || !Array.isArray(embedding2) || 
        embedding1.length !== embedding2.length || embedding1.length === 0) {
      console.warn('Invalid embeddings provided for cosine similarity calculation');
      return 0;
    }
    
    let dotProduct = 0;
    let normA = 0;
    let normB = 0;
    
    for (let i = 0; i < embedding1.length; i++) {
      if (!Number.isFinite(embedding1[i]) || !Number.isFinite(embedding2[i])) continue;
      dotProduct += embedding1[i] * embedding2[i];
      normA += embedding1[i] * embedding1[i];
      normB += embedding2[i] * embedding2[i];
    }
    
    if (normA === 0 || normB === 0) {
      console.warn('Zero norm detected in cosine similarity calculation');
      return 0;
    }
    
    const similarity = dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
    return Math.max(-1, Math.min(1, similarity));
  }
}
const cache = new EmbeddingCache();

// cached version of the embedding function
export async function generateEmbedding(text: string): Promise<number[]> {
  return cache.getEmbedding(text);
}


export async function getCosineSimilarity(embedding1: number[], embedding2: number[]): Promise<number> {
  return cache.getCosineSimilarity(embedding1, embedding2);
}