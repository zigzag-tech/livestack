#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# loop throught all packages where package.json has a build script, and run npm run build
# Find only first-level package.json files with build scripts
for package in $(find . -maxdepth 2 -name "package.json" -type f -exec grep -l '"build":' {} \;); do
    echo "Building $package"
    npm run build --prefix $(dirname $package)
done
