import winston from "winston";

const loggers: { [key: string]: winston.Logger } = {};

export function getLogger(workerName: string, logColor?: string) {
  if (!loggers[workerName]) {
    loggers[workerName] = winston.createLogger({
      level: "info",
      format: winston.format.combine(
        winston.format.label({
          label: "[LOGGER]",
        }),
        winston.format.timestamp({
          format: "YY-MM-DD HH:mm:ss",
        }),
        winston.format.printf(
          ({ level, message, timestamp }) =>
            `[${workerName}] [${timestamp}] [${level.toUpperCase()}]: ${message}`
        )
      ),
      defaultMeta: { service: workerName },
      transports: [
        new winston.transports.Console(),
        new winston.transports.File({ filename: `logs/${workerName}.log` }),
      ],
    });
  }
  return loggers[workerName]!;
}
