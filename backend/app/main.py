from fastapi import FastAPI

app = FastAPI(title="365 WeCom Archive")


@app.get("/health")
def health():
    return {"status": "ok"}
