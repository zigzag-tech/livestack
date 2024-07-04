#!/bin/bash

# npx lerna version

# List of folders
folders=(
    "vault-interface"
    "vault-client"
    "shared"
    "core"
    "client"
    "gateway"
    "lab-common"
    "lab-server"
    "summarizer"
    "transcribe"
    "translate-server"
)

# Make sure "main" and "exports" are using prod settings
yarn use-prod

# Loop through each folder and execute the commands
for folder in "${folders[@]}"; do
    if [ -d "$folder" ]; then
        echo "Entering directory: $folder"
        cd "$folder" || exit
        # Run the commands
        yarn build
        # Go back to the original directory
        cd - || exit
    else
        echo "Directory $folder does not exist."
    fi
done
