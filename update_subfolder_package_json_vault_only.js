const fs = require("fs");
const path = require("path");

const mode = process.argv[2]; // Get the mode (dev or prod) from command line arguments

if (!mode || (mode !== "dev" && mode !== "prod")) {
  console.error("Please specify a valid mode: dev or prod");
  process.exit(1);
}

function writePackageJson(pkgFolder, folder, indexFile) {
  const folderDir = path.join(__dirname, pkgFolder, folder);
  const packageJsonPath = path.join(folderDir, "package.json");
  const packageJsonContent = JSON.stringify(
    { main: `../src/${folder}/${indexFile}` },
    null,
    2
  );
  if (!fs.existsSync(folderDir)) {
    fs.mkdirSync(folderDir, { recursive: true });
  }
  fs.writeFileSync(packageJsonPath, packageJsonContent, "utf8");
  console.log(`package.json written to ${packageJsonPath}`);
}

// Define the new "main" value based on the mode
const mainValue = mode === "dev" ? "src/index.ts" : "dist/index.js";

const exportsValue =
  mode === "dev"
    ? {
        "./client": "./src/client/index.ts",
        "./server": "./src/server/index.ts",
        "./package.json": "./package.json",
      }
    : {
        "./client": {
          default: "./client/index.js",
          types: "./client/index.d.ts",
        },
        "./server": {
          import: "./server/index.js",
          require: "./server/index.js",
          defualt: "./server/index.js",
          types: "./server/index.d.ts",
        },
        "./package.json": "./package.json",
      };

const subFolders = ["vault-client", "vault-interface"];

const multiEntryPointsSubfolders = [];

for (const folder of subFolders) {
  const packageJsonPath = path.resolve(__dirname, folder, "package.json");
  const packageJsonContent = fs.readFileSync(packageJsonPath, "utf8");
  const packageJson = JSON.parse(packageJsonContent);

  // Update the "main" field in the package.json file
  packageJson.main = mainValue;
  fs.writeFileSync(
    packageJsonPath,
    JSON.stringify(packageJson, null, 2) + "\n",
    "utf8"
  );
  console.log(
    `Updated "main" field for ${folder} to "${mainValue}" for ${mode} mode.`
  );
}

for (const folder of multiEntryPointsSubfolders) {
  const packageJsonPath = path.resolve(__dirname, folder, "package.json");
  const packageJsonContent = fs.readFileSync(packageJsonPath, "utf8");
  const packageJson = JSON.parse(packageJsonContent);

  // Update the "exports" field in the package.json file
  const static = {
    "./raw-pcm-processor.js": "./static/raw-pcm-processor.js",
    "./fft.js": "./static/fft.js",
    "./vad-audio-worklet.js": "./static/vad-audio-worklet.js",
  };
  if (folder == "transcribe") {
    packageJson.exports = { ...exportsValue, ...static };
  } else {
    packageJson.exports = exportsValue;
  }

  fs.writeFileSync(
    packageJsonPath,
    JSON.stringify(packageJson, null, 2) + "\n",
    "utf8"
  );
  console.log(`Updated "exports" field for ${folder} for ${mode} mode.`);

  if (mode === "dev") {
    writePackageJson(folder, "client", "index.ts");
    writePackageJson(folder, "server", "index.ts");
  }
}
