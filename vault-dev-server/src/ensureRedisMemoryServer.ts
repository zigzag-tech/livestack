import { RedisMemoryServer } from 'redis-memory-server';

async function ensureRedisMemoryServer() {
    const redisServer = new RedisMemoryServer();
    const host = await redisServer.getHost();
    const port = await redisServer.getPort();
    return {
        host,
        port,
    }
}


export const REDIS_MEMORY_SERVER_P = ensureRedisMemoryServer();
