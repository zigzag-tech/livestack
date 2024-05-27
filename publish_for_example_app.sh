#!/bin/bash

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

# Loop through each folder and execute the commands
for folder in "${folders[@]}"; do
    if [ -d "$folder" ]; then
        echo "Entering directory: $folder"
        cd "$folder" || exit
        # Run the commands
        yarn build
        npm publish --access public
        npx preconstruct dev
        # Go back to the original directory
        cd - || exit
    else
        echo "Directory $folder does not exist."
    fi
done