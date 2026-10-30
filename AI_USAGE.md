## Tools

- ChatGPT was used to help interpret the requirements, plan the database models and migration, and review the API implementation.
- Python, FastAPI, SQLAlchemy, Alembic, and pytest were used for implementation and local verification.

## What AI got wrong

- The first implementation approach needed to be checked against the starter repository rather than assuming the project structure.
- The idempotency and rate-limit rules are order-sensitive. In particular, the rate limit must be checked immediately after authentication, and an insufficient-funds response must be saved so a retry replays the original 409.
- I reviewed these points against the task specification and adjusted the implementation accordingly.

## In my own words

A transfer to someone else's account returns 404 instead of 403 so the API does not reveal whether another customer's account exists. Returning the same not-found response for an unknown account and an account the caller cannot access makes account enumeration harder.

If the idempotency record were written in a separate transaction after the transfer, the transfer could commit while the idempotency write fails. The customer might retry because they never received a reliable response, and the system could process the same payment again. Saving the transfer and its idempotency result in the same database transaction makes them succeed or fail together.
