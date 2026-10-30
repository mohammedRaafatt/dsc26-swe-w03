"""Rawaj Pay transfer API."""
import json
import math
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import Account, IdempotencyRecord, Transfer
from app.settings import Settings


def _problem(status: int, title: str, detail: str | None = None, headers: dict | None = None):
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def _transfer_json(transfer: Transfer) -> dict[str, Any]:
    return {
        "id": transfer.id,
        "from_account_id": transfer.from_account_id,
        "to_account_id": transfer.to_account_id,
        "amount_piastres": transfer.amount_piastres,
        "memo": transfer.memo,
        "status": transfer.status,
    }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    engine = make_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    # customer -> (available tokens, last clock reading)
    app.state.rate_buckets = {}

    def authenticate(request: Request):
        value = request.headers.get("authorization", "")
        parts = value.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            return None
        return settings.api_tokens.get(parts[1])

    def take_rate_token(customer_id: str):
        now = settings.clock()
        capacity = max(0.0, float(settings.rate_limit_capacity))
        refill = max(0.0, float(settings.rate_limit_refill_per_second))
        tokens, last = app.state.rate_buckets.get(customer_id, (capacity, now))
        elapsed = max(0.0, now - last)
        tokens = min(capacity, tokens + elapsed * refill)
        if tokens < 1.0:
            wait = (1.0 - tokens) / refill if refill > 0 else 1.0
            app.state.rate_buckets[customer_id] = (tokens, now)
            return False, max(1, math.ceil(wait))
        app.state.rate_buckets[customer_id] = (tokens - 1.0, now)
        return True, None

    @app.post("/v1/transfers")
    async def create_transfer(request: Request):
        customer_id = authenticate(request)
        if customer_id is None:
            return _problem(401, "Unauthorized", "A valid Bearer token is required.",
                            {"WWW-Authenticate": "Bearer"})

        allowed, retry_after = take_rate_token(customer_id)
        if not allowed:
            return _problem(429, "Too Many Requests", "Transfer rate limit exceeded.",
                            {"Retry-After": str(retry_after)})

        idem_key = request.headers.get("Idempotency-Key")
        if idem_key is None or idem_key == "":
            return _problem(400, "Missing Idempotency-Key", "The Idempotency-Key header is required.")

        try:
            payload = await request.json()
        except Exception:
            return _problem(422, "Unprocessable Content", "The request body must be valid JSON.")

        if not isinstance(payload, dict):
            return _problem(422, "Unprocessable Content", "The request body must be a JSON object.")
        allowed_fields = {"from_account_id", "to_account_id", "amount_piastres", "memo"}
        if set(payload) - allowed_fields:
            return _problem(422, "Unprocessable Content", "Unknown fields are not allowed.")
        required = {"from_account_id", "to_account_id", "amount_piastres"}
        if not required.issubset(payload):
            return _problem(422, "Unprocessable Content", "Required transfer fields are missing.")

        source_id = payload["from_account_id"]
        destination_id = payload["to_account_id"]
        amount = payload["amount_piastres"]
        memo = payload.get("memo")
        if not isinstance(source_id, str) or not isinstance(destination_id, str):
            return _problem(422, "Unprocessable Content", "Account IDs must be strings.")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return _problem(422, "Unprocessable Content", "amount_piastres must be a positive JSON integer.")
        if amount > settings.max_transfer_piastres:
            return _problem(422, "Unprocessable Content", "The transfer exceeds the configured maximum.")
        if memo is not None and (not isinstance(memo, str) or len(memo) > 140):
            return _problem(422, "Unprocessable Content", "memo must be a string of at most 140 characters.")
        if source_id == destination_id:
            return _problem(422, "Unprocessable Content", "Source and destination accounts must differ.")

        # Include the optional field's actual presence/value in the fingerprint.
        request_body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        with Session(app.state.engine) as session:
            with session.begin():
                previous = session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.customer_id == customer_id,
                        IdempotencyRecord.key == idem_key,
                    )
                )
                if previous is not None:
                    if previous.request_body != request_body:
                        return _problem(422, "Unprocessable Content",
                                        "This Idempotency-Key was already used with a different body.")
                    replay_body = json.loads(previous.response_body)
                    response = JSONResponse(
                        replay_body, status_code=previous.response_status,
                        headers={"Idempotent-Replayed": "true"},
                        media_type=("application/problem+json" if previous.response_status >= 400 else "application/json"),
                    )
                    return response

                source = session.get(Account, source_id)
                if source is None or source.owner_id != customer_id:
                    return _problem(404, "Not Found", "The source account was not found.")

                destination = session.get(Account, destination_id)
                if destination is None:
                    return _problem(422, "Unprocessable Content", "The destination account was not found.")

                if source.balance_piastres < amount:
                    problem = {
                        "type": "about:blank",
                        "title": "Insufficient Funds",
                        "status": 409,
                        "detail": "The source account has insufficient funds.",
                    }
                    session.add(IdempotencyRecord(
                        customer_id=customer_id, key=idem_key, request_body=request_body,
                        response_status=409,
                        response_body=json.dumps(problem, separators=(",", ":"), ensure_ascii=False),
                    ))
                    session.flush()
                    # The context commits the 409 idempotency result without changing balances.
                    return JSONResponse(problem, status_code=409, media_type="application/problem+json")

                source.balance_piastres -= amount
                destination.balance_piastres += amount
                transfer = Transfer(
                    id="trf_" + uuid.uuid4().hex[:24],
                    from_account_id=source.id,
                    to_account_id=destination.id,
                    amount_piastres=amount,
                    memo=memo,
                    status="completed",
                )
                session.add(transfer)
                session.flush()
                result = _transfer_json(transfer)
                session.add(IdempotencyRecord(
                    customer_id=customer_id, key=idem_key, request_body=request_body,
                    response_status=201,
                    response_body=json.dumps(result, separators=(",", ":"), ensure_ascii=False),
                ))
                session.flush()
                return JSONResponse(result, status_code=201)

    @app.get("/v1/accounts/{account_id}")
    def get_account(account_id: str, request: Request):
        customer_id = authenticate(request)
        if customer_id is None:
            return _problem(401, "Unauthorized", "A valid Bearer token is required.",
                            {"WWW-Authenticate": "Bearer"})
        with Session(app.state.engine) as session:
            account = session.get(Account, account_id)
            if account is None or account.owner_id != customer_id:
                return _problem(404, "Not Found", "The account was not found.")
            return {
                "id": account.id,
                "balance_piastres": account.balance_piastres,
                "currency": account.currency,
            }

    @app.get("/v1/transfers/{transfer_id}")
    def get_transfer(transfer_id: str, request: Request):
        customer_id = authenticate(request)
        if customer_id is None:
            return _problem(401, "Unauthorized", "A valid Bearer token is required.",
                            {"WWW-Authenticate": "Bearer"})
        with Session(app.state.engine) as session:
            transfer = session.get(Transfer, transfer_id)
            if transfer is None:
                return _problem(404, "Not Found", "The transfer was not found.")
            source = session.get(Account, transfer.from_account_id)
            destination = session.get(Account, transfer.to_account_id)
            if not ((source and source.owner_id == customer_id) or
                    (destination and destination.owner_id == customer_id)):
                return _problem(404, "Not Found", "The transfer was not found.")
            return _transfer_json(transfer)

    return app
