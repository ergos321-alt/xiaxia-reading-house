web: gunicorn --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT --workers 1 --timeout 120 mcp_app:app
