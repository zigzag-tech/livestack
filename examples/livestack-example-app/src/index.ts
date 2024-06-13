#!/usr/bin/env node

import path from "path";
import fs from "fs";
import { execSync } from "child_process";
import { fileURLToPath } from "node:url";
import { Command } from "commander";

const templates = [
  "typescript-speech-app",
  "typescript-backend-only",
  "typescript-setup-only",
  "typescript-live-counter",
];
const templateDefault = "typescript-setup-only";

const main = async () => {
  let projectDir: string | null = null;
  let template: string = templateDefault;

  const program = new Command();
  program
    .command("create-livestack")
    .argument("<project-directory>")
    .usage("<project-directory> [options]")
    .option(
      "--template [path-to-template]",
      `specify a template for the created project: ${templates.join(", ")}`,
      templateDefault
    )
    .action((projectDirName, options) => {
      projectDir = projectDirName;

      if (!templates.includes(options.template)) {
        console.error(
          `Invalid template. Using default template: ${templateDefault}`
        );
      } else {
        template = options.template;
      }
    })
    .parse(process.argv);

  if (!projectDir) {
    console.error("Project directory is required");
    return;
  }
  
  const destination = path.join(process.cwd(), projectDir);
  const templateDir = path.resolve(
    fileURLToPath(import.meta.url),
    "..",
    "template",
    template
  );

  fs.cpSync(templateDir, destination, { recursive: true });

  console.log(`Project created at ${destination} using template ${template}`);

  try {
    execSync("git init --initial-branch=main", { cwd: destination, stdio: "inherit" });
    console.log("Initialized a new git repository.");
  } catch (err) {
    console.error("Error initializing git repository:", err);
    return;
  }

  try {
    execSync("npm install", { cwd: destination, stdio: "inherit" });
    console.log("Installed npm dependencies.");
  } catch (err) {
    console.error("Error installing npm dependencies:", err);
    return;
  }
};

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
