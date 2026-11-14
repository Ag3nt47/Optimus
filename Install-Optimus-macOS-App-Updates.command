#!/bin/bash
set -u

app_candidates=(
  "$HOME/Applications/Optimus.app/Contents/MacOS/Optimus"
  "/Applications/Optimus.app/Contents/MacOS/Optimus"
  "$(cd "$(dirname "$0")" && pwd)/Optimus.app/Contents/MacOS/Optimus"
)

optimus_binary=""
for candidate in "${app_candidates[@]}"; do
  if [[ -x "$candidate" ]]; then
    optimus_binary="$candidate"
    break
  fi
done

if [[ -z "$optimus_binary" ]]; then
  echo "Optimus.app was not found in ~/Applications, /Applications, or beside this setup file."
  echo "Install Optimus first, then run this setup again."
  read -r -p "Press Return to close..." _
  exit 2
fi

echo "Installing Optimus's optional Mac App Store update provider..."
echo "The official mas-cli 7.0.0 package and extracted executable are checked against pinned SHA-256 hashes."
"$optimus_binary" --install-macos-app-store-provider
result=$?

if [[ $result -eq 0 ]]; then
  echo "Mac App Store app updates are ready in Optimus."
else
  echo "Setup did not complete. Review the error above; no unverified provider was enabled."
fi
read -r -p "Press Return to close..." _
exit "$result"
