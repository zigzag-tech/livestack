import chalk from "ansis";

interface Logger {
  info: (message: string, ...args: any[]) => void;
  warn: (message: string, ...args: any[]) => void;
  error: (message: string, ...args: any[]) => void;
}

const loggers: { [key: string]: Logger } = {};

function formatTimestamp(): string {
  const now = new Date();
  return now.toLocaleString('en-GB', {
    year: '2-digit',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  });
}

function createLogMessage(workerName: string, level: string, message: string): string {
  const timestamp = formatTimestamp();
  const levelUpper = level.toUpperCase();
  let coloredMessage = message;
  let coloredLevel = levelUpper;

  switch (levelUpper) {
    case "INFO":
      coloredMessage = chalk.green(message);
      coloredLevel = chalk.black.bgGreenBright.bold(levelUpper);
      break;
    case "WARN":
      coloredMessage = chalk.yellow(message);
      coloredLevel = chalk.black.bgYellowBright.bold(levelUpper);
      break;
    case "ERROR":
      coloredMessage = chalk.red(message);
      coloredLevel = chalk.black.bgRedBright.bold(levelUpper);
      break;
  }

  return `[${chalk.black.bgBlue.bold(workerName)}] [${chalk.black.bgWhiteBright(timestamp)}] [${coloredLevel}]: ${coloredMessage}`;
}

export function getLogger(workerName: string, logColor?: string): Logger {
  if (!loggers[workerName]) {
    loggers[workerName] = {
      info: (message: string, ...args: any[]) => {
        console.log(createLogMessage(workerName, 'info', message), ...args);
      },
      warn: (message: string, ...args: any[]) => {
        console.warn(createLogMessage(workerName, 'warn', message), ...args);
      },
      error: (message: string, ...args: any[]) => {
        console.error(createLogMessage(workerName, 'error', message), ...args);
      }
    };
  }
  return loggers[workerName]!;
}
