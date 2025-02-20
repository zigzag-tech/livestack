import { RedisMemoryServer } from 'redis-memory-server';

async function ensureRedisMemoryServer() {
    const redisServer = await RedisMemoryServer.create();
    const host = await redisServer.getHost();
    const port = await redisServer.getPort();
    // override environment variables
    process.env.REDIS_HOST = host;
    process.env.REDIS_PORT = port.toString();
    console.info("Temporary Redis server created at", host, port);
    return {
        host,
        port,
    }
}


export const REDIS_MEMORY_SERVER_P = ensureRedisMemoryServer();
