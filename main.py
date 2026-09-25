import uvicorn

from app.api import create_app
from app.service import DexService

service = DexService(ledger_path="ledger.json")
app = create_app(service)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
