This folder stores the full UxPlay runtime used by the app.

Required structure:
- tools/uxplay/bin/uxplay.exe
- tools/uxplay/lib/... (GStreamer runtime/plugins)

The build script embeds this entire folder recursively into the final onefile .exe.
