# Turtle Tracker project instructions

- Communicate with the user in German, but keep all source code, configuration, documentation, API names, and code comments in English.
- Use Python 3.11+ with FastAPI, OpenCV, SQLite, and Pydantic settings.
- Keep the application under `src/turtle_tracker` and tests under `tests`.
- Preserve the REST contracts documented in `docs/api.md`.
- Prefer dependency injection through `create_app()` so API tests can use temporary SQLite databases.
- Keep camera ingestion compatible with JPEG payloads from ESP32-CAM devices.
- Maintain `.github/workflows/publish-image.yml` so every push to `main` builds and publishes `ghcr.io/danix42/x42-iot-esp32-camandradar-turtletracker:latest`.
- When pushing application, Dockerfile, Compose, or workflow changes to GitHub, verify that the corresponding "Publish container image" GitHub Actions run succeeds and report the image tag for LXC deployment.
