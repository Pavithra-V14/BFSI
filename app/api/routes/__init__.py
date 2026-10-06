"""The API routes. Routes that hash passwords are plain `def`, so FastAPI runs them in a worker
thread and the slow hashing never blocks the event loop."""
