const fs = require('fs');
const path = require('path');

const mode = process.argv[2]; // Get the mode (dev or prod) from command line arguments

if (!mode || (mode !== 'dev' && mode !== 'prod')) {
    console.error('Please specify a valid mode: dev or prod');
    process.exit(1);
}

// Define the new "main" value based on the mode
const mainValue = mode === 'dev' ? 'src/index.ts' : 'dist/index.js';

const submoduleFolders = ['core', 'shared', 'vault-interface', 'vault-client'];

for (const folder of submoduleFolders) {
    const packageJsonPath = path.resolve(__dirname, folder, 'package.json');
    const packageJsonContent = fs.readFileSync(packageJsonPath, 'utf8');
    const packageJson = JSON.parse(packageJsonContent);
    packageJson.main = mainValue;
    fs.writeFileSync(packageJsonPath, JSON.stringify(packageJson, null, 2) + '\n', 'utf8');
    console.log(`Updated "main" field for ${folder} to "${mainValue}" for ${mode} mode.`);
}
