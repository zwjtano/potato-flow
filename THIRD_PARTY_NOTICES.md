# Third-party notices

This repository combines and modifies two upstream projects:

## biliup

- Project: <https://github.com/biliup/biliup>
- Base revision: `adf6a1c03be9f777a76c8c501038c27f3d90a097`
- License: MIT
- License text: `upstream-biliup/LICENSE`

Local changes restrict the built-in live recorder plugins and WebUI choices to Bilibili Live and Douyu, and integrate the recorder with the unified Y2A application.

## Y2A-Auto

- Project: <https://github.com/fqscfqj/Y2A-Auto>
- Base revision: `4419498d365414f5cef6842c78d75f43b7172292`
- License: GNU General Public License v3.0
- License text: `y2a-auto/LICENSE`

Local changes make Bilibili the only upload destination, preserve YouTube download and monitoring features, add the unified live-recorder console, and connect recorded media and danmaku to the automated upload pipeline.
