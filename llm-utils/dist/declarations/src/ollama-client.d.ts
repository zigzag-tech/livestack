import { z } from 'zod';
/**
 * Interface for a chat message
 */
export interface ChatMessage {
    role: 'system' | 'user' | 'assistant';
    content: string;
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
    [key: string]: any;
}
/**
 * Response wrapper type for JSON responses from Ollama
 */
export type OllamaJSONResponse<T> = {
    status: 'success';
    result: T;
    stream?: AsyncGenerator<string, void, unknown>;
} | {
    status: 'failed';
};
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
export declare function generateJSONResponseOllama<T>({ messages, options, cache, logStream, printPrompt, requireConfirmation, schema, }: {
    messages: ChatMessage[];
    options: Omit<OllamaOptions, 'stream'>;
    cache?: boolean;
    logStream?: boolean;
    printPrompt?: boolean;
    requireConfirmation?: boolean;
    schema?: z.ZodType<T>;
}): Promise<OllamaJSONResponse<T>>;
/**
 * Generates a response from Ollama using the provided messages
 *
 * @param messages - Array of chat messages to send to Ollama
 * @param options - Configuration options for the Ollama API call
 * @returns The full response text from Ollama
 */
export declare function generateResponseOllama({ messages, options }: {
    messages: ChatMessage[];
    options: OllamaOptions & {
        json?: boolean;
        cache?: boolean;
    };
}): Promise<string>;
/**
 * Extracts JSON content from a response string
 *
 * @param response - The full response text from Ollama
 * @returns The extracted JSON content, or null if no valid JSON was found
 */
export declare function extractJsonFromResponse(response: string): string | null;
export declare function extractLineByLineFromResponse(response: string): string | null;
/**
 * Calculates the Levenshtein distance between two strings
 *
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns The Levenshtein distance (number of edits needed to transform str1 into str2)
 */
export declare function levenshteinDistance(str1: string, str2: string): number;
/**
 * Calculates the similarity ratio between two strings (0 to 1)
 *
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns A value between 0 (completely different) and 1 (identical)
 */
export declare function stringSimilarity(str1: string, str2: string): number;
export declare class EmbeddingCache {
    private cacheDir;
    private cache;
    constructor(cacheDir?: string);
    private initializeCache;
    private getHash;
    private getCacheFilePath;
    private arrayToBuffer;
    private bufferToArray;
    getEmbedding(text: string): Promise<number[]>;
    /**
     * Calculate cosine similarity between two embeddings
     */
    getCosineSimilarity(embedding1: number[], embedding2: number[]): number;
}
export declare function generateEmbedding(text: string): Promise<number[]>;
export declare function getCosineSimilarity(embedding1: number[], embedding2: number[]): Promise<number>;
