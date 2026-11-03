"""Run the app (page and API) locally: python run_backend.py, then open http://127.0.0.1:8000."""

import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:create_app", factory=True, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
