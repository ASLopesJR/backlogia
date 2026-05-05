# app.py
# Entry point for the Backlogia web application

import os
import sys

# Allow running as script or module
if __name__ == "__main__":
    # Add parent directory to path when running as script
    sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    import uvicorn

    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    access_log = os.environ.get("ACCESS_LOG", "false").lower() == "true"
    uvicorn.run(
        "web.main:app",
        host="0.0.0.0",
        port=port,
        reload=debug,
        access_log=access_log,
    )
