import winston, { format } from "winston";
import chalk from "ansis";

const { combine, timestamp, label, printf, simple, splat } = format;

const loggers: { [key: string]: winston.Logger } = {};

export function getLogger(workerName: string, logColor?: string) {
  const consoleFormat = printf(({ level, message, label, timestamp }) => {
    var levelUpper = level.toUpperCase();
    switch (levelUpper) {
      case "INFO":
        message = chalk.green(message);
        level = chalk.black.bgGreenBright.bold(level);
        break;

      case "WARN":
        message = chalk.yellow(message);
        level = chalk.black.bgYellowBright.bold(level);
        break;

      case "ERROR":
        message = chalk.red(message);
        level = chalk.black.bgRedBright.bold(level);
        break;

      default:
        break;
    }
    return `[${chalk.black.bgBlue.bold(
      workerName
    )}] [${chalk.black.bgWhiteBright(
      timestamp
    )}] [${level.toUpperCase()}]: ${message}`;
  });

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
        consoleFormat
      ),
      defaultMeta: { service: workerName },
      transports: [
        new winston.transports.Console(),
        // new winston.transports.File({ filename: `logs/${workerName}.log` }),
      ],
    });
  }
  return loggers[workerName]!;
}
