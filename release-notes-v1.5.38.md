# PotatoFlow v1.5.38

This release restores an officially built Windows 10/11 x64 portable package.

- Adds a self-contained `PotatoFlow.exe` bundle with the Web UI on port 5001.
- Bundles verified Windows x64 FFmpeg and ffprobe binaries plus GPLv3 license text.
- Keeps configuration, cookies, databases, recordings, and logs inside the extracted portable directory.
- Validates the packaged executable on GitHub's Windows runner through the literal `/healthz` version before publishing.
- Publishes both the ZIP archive and its SHA-256 checksum with the GitHub Release.

Extract the complete ZIP to a writable directory, then run `start.bat` and open <http://127.0.0.1:5001>.
