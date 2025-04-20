import * as path from 'path';
import { z } from 'zod';
import { jsonrepair } from 'jsonrepair';
import {
    ChatMessage,
    LLMJSONResult,
    ApiCallFn,
    handleLLMJsonResponseGeneration,
    createRequestHash,
    hasCachedResponse,
    readCachedResponse,
    writeResponseCache,
    extractJsonFromResponse,
    stripThinkTags,
    waitForEnterKey,
    ensureCacheDir,
    getResponseCachePath,
    MAX_JSON_PARSE_ATTEMPTS // Assuming this is exported or define locally
} from './ollama-client';

// ANSI color codes (consider moving to a shared utils file)
const CYAN = '\x1b[36m';
const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const RESET = '\x1b[0m';

// Configuration for Llama CPP Server
const LLAMA_CPP_HOST = process.env.LLAMA_CPP_HOST || 'http://127.0.0.1:8080';
const LLAMA_CPP_COMPLETION_ENDPOINT = `${LLAMA_CPP_HOST}/v1/chat/completions`;
const LLAMA_CPP_RESPONSE_CACHE_DIR = path.join(process.cwd(), '_llama_cpp_response_cache');

/**
 * Interface for Llama CPP specific options
 * Based on common parameters for llama.cpp server's /completion endpoint
 */
export interface LlamaCPPOptions {
    model?: string; // Although model is often fixed per server endpoint
    temperature?: number;
    top_p?: number;
    top_k?: number;
    n_predict?: number; // Max tokens to generate
    stop?: string[]; // Stop sequences
    grammar?: string; // BNF grammar for constrained output (useful for JSON)
    mirostat?: number; // 0 = disabled, 1 = Mirostat, 2 = Mirostat 2.0
    mirostat_tau?: number;
    mirostat_eta?: number;
    repeat_penalty?: number;
    stream?: boolean; // Handled internally by the ApiCallFn
    stripThinkTag?: boolean; // Meta option for handler
    [key: string]: any; // Allow additional parameters
}

/**
 * Interface for simplified Llama CPP options for the simple generator.
 */
export interface SimpleLlamaCPPOptions {
    temperature?: number;
    top_p?: number;
    top_k?: number;
    n_predict?: number; // Max tokens to generate
    stop?: string[]; // Stop sequences
    repeat_penalty?: number;
    // Add other simple parameters supported by /v1/chat/completions if needed
    // Exclude complex ones like grammar, mirostat, stream, json, cache, stripThinkTag etc.
}

/**
 * Specific implementation of ApiCallFn for Llama CPP server's /v1/chat/completions endpoint.
 */
const _llamaCppApiCall: ApiCallFn = async (
    messages: ChatMessage[],
    options: LlamaCPPOptions // Expect Llama CPP specific options
): Promise<AsyncGenerator<string, void, unknown>> => {

    // const prompt = formatMessagesForLlamaCPP(messages); // Removed

    // Prepare payload for Llama CPP /v1/chat/completions endpoint
    const payload = {
        // prompt, // Removed
        messages: messages, // Use the messages array directly
        stream: true, // Always request stream from backend for the handler
        n_predict: options.n_predict ?? -1, // Default to infinite prediction
        temperature: options.temperature ?? 0.7,
        top_p: options.top_p ?? 0.9,
        top_k: options.top_k ?? 40,
        stop: options.stop,
        grammar: options.grammar,
        mirostat: options.mirostat,
        mirostat_tau: options.mirostat_tau,
        mirostat_eta: options.mirostat_eta,
        repeat_penalty: options.repeat_penalty,
        // Add other supported options here
    };

    // Define the async generator function internally
    async function* llamaCppStreamGenerator(): AsyncGenerator<string, void, unknown> {
        try {
            const response = await fetch(LLAMA_CPP_COMPLETION_ENDPOINT, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });

            if (!response.ok) {
                const errorBody = await response.text();
                throw new Error(`Llama CPP request failed: ${response.status} ${response.statusText} - ${errorBody}`);
            }

            if (!response.body) {
                throw new Error('Llama CPP response body is null');
            }

            // Process the Server-Sent Events (SSE) stream
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    break;
                }

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || ''; // Keep incomplete line in buffer

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const jsonData = line.substring(6);
                        try {
                            const parsedData = JSON.parse(jsonData);
                            if (parsedData.content) {
                                yield parsedData.content;
                            }
                            // Handle 'stop' signal if needed, though handler accumulates anyway
                            // if (parsedData.stop) { 
                            //     return; // Or break loop 
                            // }
                        } catch (e) {
                            console.warn(`[llama.cpp stream] Failed to parse JSON chunk: ${jsonData}`, e);
                        }
                    }
                }
            }
            // Process any remaining buffer content (though likely empty for SSE)
            if (buffer.startsWith('data: ')) {
                const jsonData = buffer.substring(6);
                try {
                    const parsedData = JSON.parse(jsonData);
                    if (parsedData.content) {
                        yield parsedData.content;
                    }
                } catch (e) {
                    console.warn(`[llama.cpp stream] Failed to parse final JSON chunk: ${jsonData}`, e);
                }
            }

        } catch (error) {
            console.error(`${RED}[_llamaCppApiCall] Error during Llama CPP API call: ${error}${RESET}`);
            // Re-throw the error to be caught by the handler's retry logic
            throw error;
        }
    }

    // Return the generator instance
    return llamaCppStreamGenerator();
};

/**
 * Generates a JSON response from a Llama CPP server using the provided messages
 * 
 * @param messages - Array of chat messages to send
 * @param options - Configuration options for the Llama CPP API call
 * @param cache - Whether to use caching (defaults to true)
 * @param logStream - Whether to log the streaming response to console (defaults to true)
 * @param schema - Optional Zod schema for response validation and type inference
 * @returns An object containing the stream for immediate consumption and a promise that resolves when processing is complete
 */
export async function generateJSONResponseLlamaCPP<T>({
    messages,
    options, // This should be LlamaCPPOptions specifically
    cache = true,
    logStream = true,
    printPrompt = false,
    requireConfirmation = false,
    schema,
}: {
    messages: ChatMessage[];
    options: Omit<LlamaCPPOptions, 'stream'>; // stream is handled internally
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

    // Ensure grammar forces JSON output if not already set
    const llamaOptions = {
        ...options,
        // Example: Automatically add JSON grammar if schema is present and no grammar provided
        grammar: schema && !options.grammar ? generateJsonGrammar() : options.grammar
    };

    // Calculate cache hash based on Llama CPP options
    // Ensure options used for hashing include the potentially added grammar
    const requestHash = createRequestHash(messages, llamaOptions);

    // Call the generic handler with Llama CPP-specific API call function and options
    const result = await handleLLMJsonResponseGeneration<T>({
        messages,
        options: llamaOptions, // Pass LlamaCPPOptions
        apiCallFn: _llamaCppApiCall,
        cacheOptions: {
            enabled: cache,
            requestHash: requestHash,
            cacheDir: LLAMA_CPP_RESPONSE_CACHE_DIR, // Use Llama CPP-specific cache dir
        },
        promptOptions: {
            printPrompt,
            requireConfirmation,
        },
        jsonParsingOptions: {
            maxAttempts: MAX_JSON_PARSE_ATTEMPTS,
            schema,
            stripTags: options.stripThinkTag !== false, // Use stripThinkTag option from original options
        },
        logStream,
    });

    // Optional: Add timing log if desired
    result.resultPromise.then(() => {
        const duration = Date.now() - funcStartTime;
        // console.log(`[generateJSONResponseLlamaCPP] Total time: ${duration}ms`);
    }).catch(() => { /* ignore */ }); // Prevent unhandled rejection

    return result;
}

/**
 * Basic JSON grammar generator (replace with a proper library or more robust implementation if needed)
 * See: https://github.com/ggerganov/llama.cpp/blob/master/grammars/json.gbnf
 */
function generateJsonGrammar(): string {
    // Simplified grammar - consider using a pre-defined, validated one
    return `
    root   ::= object
    value  ::= object | array | string | number | boolean | null
    object ::= "{" ws ( string ":" ws value ( "," ws string ":" ws value )* )? "}" ws
    array  ::= "[" ws ( value ( "," ws value )* )? "]" ws
    string ::= "\"" ( [^"\\] | "\\" ( ["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] ) )* "\"" ws
    number ::= ( "-" )? ( [0-9] | [1-9] [0-9]* ) ( "." [0-9]+ )? ( [eE] [+-]? [0-9]+ )? ws
    boolean ::= ("true" | "false") ws
    null   ::= "null" ws
    ws ::= [ \t\n]*
    `;
}

/**
 * Generates a response from a Llama CPP server using the provided messages.
 * Handles both JSON and plain text, streaming and non-streaming, and caching.
 *
 * @param messages - Array of chat messages to send.
 * @param options - Configuration options including Llama CPP specifics and meta-options like `json` and `cache`.
 * @returns The full response text from Llama CPP.
 */
export async function generateResponseLlamaCPP(
    { messages, options }: { messages: ChatMessage[]; options: LlamaCPPOptions & { json?: boolean; cache?: boolean; }; }
): Promise<string> {
    const funcStartTime = Date.now();

    try {
        // --- Handle JSON response case ---
        if (options.json) {
            // Ensure grammar forces JSON output if not already set
            const llamaJsonOptions = {
                ...options,
                // Example: Automatically add JSON grammar if schema is present and no grammar provided
                // We don't have schema here, but could add a basic json grammar if json=true
                // grammar: options.grammar || generateJsonGrammar() // Option: enforce grammar for json=true
            };

            const jsonResponse = await generateJSONResponseLlamaCPP<any>({
                messages,
                options: llamaJsonOptions, // Pass adjusted options
                cache: options.cache !== false,
                schema: undefined, // No schema passed directly to this generic function
                // Pass through relevant meta-options if needed by generateJSONResponseLlamaCPP
                logStream: false, // Don't double log stream if json=true
                printPrompt: false,
                requireConfirmation: false,
            });

            const result = await jsonResponse.resultPromise;

            if (result.status === 'success') {
                return JSON.stringify(result.content, null, 2);
            } else {
                console.warn('[generateResponseLlamaCPP] JSON generation failed. Falling back to empty object string.');
                // Attempt to get raw content from stream is complex due to handler structure
                // Returning empty JSON string for consistency on failure.
                return '{}';
            }
        }

        // --- Handle Non-JSON response case ---
        const cacheEnabled = options.cache !== false;
        const requestHash = createRequestHash(messages, options);
        const cacheDir = LLAMA_CPP_RESPONSE_CACHE_DIR;

        // Check cache
        if (cacheEnabled && hasCachedResponse(requestHash, cacheDir)) {
            try {
                const cachedResponse = readCachedResponse<string>(requestHash, cacheDir);
                // console.log('[generateResponseLlamaCPP] Cache hit');
                return cachedResponse;
            } catch (e) {
                console.warn(`[generateResponseLlamaCPP] Failed to read cache: ${e}. Proceeding to generate.`);
            }
        }

        // Prepare request
        // const prompt = formatMessagesForLlamaCPP(messages); // Removed call
        const streamEnabled = options.stream !== false;

        const payload = {
            // prompt, // Removed prompt field
            messages: messages, // Add messages field
            stream: streamEnabled,
            n_predict: options.n_predict ?? -1,
            temperature: options.temperature ?? (streamEnabled ? 0.7 : 0.2), // Different default temp?
            top_p: options.top_p ?? 0.9,
            top_k: options.top_k ?? 40,
            stop: options.stop,
            // Don't include grammar by default for non-JSON
            mirostat: options.mirostat,
            mirostat_tau: options.mirostat_tau,
            mirostat_eta: options.mirostat_eta,
            repeat_penalty: options.repeat_penalty,
            // Filter out meta options not part of Llama CPP API
            ...Object.fromEntries(
                Object.entries(options).filter(([key]) =>
                    !['json', 'cache', 'stream', 'stripThinkTag'].includes(key) && // Standard meta
                    // Also filter out Llama CPP specific options already explicitly set above
                    !['prompt', 'n_predict', 'temperature', 'top_p', 'top_k', 'stop', 'mirostat', 'mirostat_tau', 'mirostat_eta', 'repeat_penalty'].includes(key) &&
                    options[key] !== undefined
                )
            )
        };

        let fullResponse = '';

        // Make API Call
        const response = await fetch(LLAMA_CPP_COMPLETION_ENDPOINT, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errorBody = await response.text();
            throw new Error(`Llama CPP request failed: ${response.status} ${response.statusText} - ${errorBody}`);
        }

        if (!response.body) {
            throw new Error('Llama CPP response body is null');
        }

        // Process response
        if (streamEnabled) {
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            process.stdout.write(CYAN); // Start coloring stream output
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const jsonData = line.substring(6);
                        try {
                            const parsedData = JSON.parse(jsonData);
                            if (parsedData.content) {
                                const contentPiece = parsedData.content;
                                process.stdout.write(contentPiece); // Write stream part
                                fullResponse += contentPiece;
                            }
                        } catch (e) {
                            // Ignore parse errors in stream for non-JSON
                        }
                    }
                }
            }
            // Process remaining buffer for stream
            if (buffer.startsWith('data: ')) {
                const jsonData = buffer.substring(6);
                try {
                    const parsedData = JSON.parse(jsonData);
                    if (parsedData.content) {
                        const contentPiece = parsedData.content;
                        process.stdout.write(contentPiece);
                        fullResponse += contentPiece;
                    }
                } catch (e) { /* ignore */ }
            }
            process.stdout.write(RESET + '\n'); // End coloring and add newline
        } else {
            // Non-streaming: Llama CPP usually returns a single JSON object
            const resultJson = await response.json();
            if (resultJson.content) {
                fullResponse = resultJson.content;
            } else {
                console.warn('[generateResponseLlamaCPP] Non-streaming response did not contain content field.', resultJson);
                fullResponse = JSON.stringify(resultJson); // Fallback to stringifying the whole object
            }
        }

        // --- Post-processing ---
        if (options.stripThinkTag !== false) {
            fullResponse = stripThinkTags(fullResponse);
        }

        // Cache the result
        if (cacheEnabled) {
            try {
                writeResponseCache(requestHash, fullResponse, cacheDir);
            } catch (e) {
                console.warn(`[generateResponseLlamaCPP] Failed to write cache: ${e}`);
            }
        }

        return fullResponse;

    } catch (error) {
        console.error(`${RED}[generateResponseLlamaCPP] Error: ${error}${RESET}`);
        // Optionally log messages and options for easier debugging
        // console.error("Messages:", messages);
        // console.error("Options:", options);
        throw error; // Re-throw the error after logging
    }
}

/**
 * Generates a simple, direct text response from the Llama CPP server 
 * using the /v1/chat/completions endpoint without streaming or complex handling.
 *
 * @param messages - Array of chat messages.
 * @param options - Simplified options for the Llama CPP API call (e.g., temperature).
 * @returns The generated text content as a string.
 */
export async function generateSimpleResponseLlamaCPP(
    { messages, options = {} }: { messages: ChatMessage[]; options?: SimpleLlamaCPPOptions; }
): Promise<string> {
    try {
        // Prepare payload for a non-streaming request
        const payload = {
            messages: messages,
            stream: false, // Explicitly request non-streaming response
            temperature: options.temperature,
            top_p: options.top_p,
            top_k: options.top_k,
            n_predict: options.n_predict,
            stop: options.stop,
            repeat_penalty: options.repeat_penalty,
            // Add other options from SimpleLlamaCPPOptions if the endpoint supports them
        };

        // Make the API call
        const response = await fetch(LLAMA_CPP_COMPLETION_ENDPOINT, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                // Add API key header if required by your server setup
                // 'Authorization': 'Bearer your-api-key',
            },
            body: JSON.stringify(payload),
        });

        // Check for errors
        if (!response.ok) {
            let errorBody = '';
            try {
                errorBody = await response.text();
            } catch { /* Ignore error reading body */ }
            throw new Error(`Llama CPP simple request failed: ${response.status} ${response.statusText} - ${errorBody}`);
        }

        // Parse the JSON response
        const resultData = await response.json();

        // Extract the content (structure based on OpenAI chat completions format)
        if (resultData.choices && resultData.choices.length > 0 && resultData.choices[0].message && resultData.choices[0].message.content) {
            return resultData.choices[0].message.content;
        } else {
            console.warn('[generateSimpleResponseLlamaCPP] Response format unexpected: ', JSON.stringify(resultData));
            throw new Error('Failed to extract content from Llama CPP response.');
        }

    } catch (error) {
        console.error(`${RED}[generateSimpleResponseLlamaCPP] Error: ${error}${RESET}`);
        throw error; // Re-throw the error after logging
    }
}

// Example usage (optional - for testing)
/*
async function testLlamaCPP() {
    console.log('Testing Llama CPP JSON generation...');
    const testSchema = z.object({
        name: z.string(),
        age: z.number(),
        city: z.string()
    });

    const messages: ChatMessage[] = [
        { role: 'system', content: 'You are a helpful assistant. Respond with JSON.' },
        { role: 'user', content: 'Create a JSON object for a person named Alice, age 30, living in Wonderland.' }
    ];

    try {
        const { resultPromise } = await generateJSONResponseLlamaCPP({
            messages,
            options: { temperature: 0.1 }, // Low temp for deterministic output
            schema: testSchema,
            printPrompt: true,
            cache: false, // Disable cache for test
        });

        const result = await resultPromise;

        if (result.status === 'success') {
            console.log('\nLlama CPP JSON Generation Successful:');
            console.log(JSON.stringify(result.content, null, 2));
        } else {
            console.error('\nLlama CPP JSON Generation Failed.');
        }
    } catch (error) {
        console.error('\nError during Llama CPP test:', error);
    }
}

testLlamaCPP();
*/ 