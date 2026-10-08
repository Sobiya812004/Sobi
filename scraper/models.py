from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class Request:
    message_id: str
    client_name: str
    report_date: str
    batch_id: int
    strike_id: str
    client_metadata: str
    google_shopping_url: str
    seller_limit: int
    created_at: str

    @classmethod
    def from_message(cls, body: dict) -> "Request":
        return cls(
            message_id          = body["message_id"],
            client_name         = body["client_name"],
            report_date         = body["report_date"],
            batch_id            = int(body.get("batch_id", 1)),
            strike_id           = body["strike_id"],
            client_metadata     = body["client_metadata"],
            google_shopping_url = body["google_shopping_url"],
            seller_limit        = int(body.get("seller_limit", 25)),
            created_at          = body["created_at"],
        )


@dataclass
class Seller:
    name: str
    price: str
    shipping: str
    prod_url: str
    stock: str = ""
    rating: str = ""
    reviews: str = ""
    condition: str = "New"


@dataclass
class Product:
    message_id: str
    client_name: str
    report_date: str
    batch_id: int
    strike_id: str
    client_metadata: str
    google_shopping_url: str
    seller_limit: int
    status: str
    output: Optional[str]

    @classmethod
    def from_request(cls, req: Request, output: Optional[str], status: str) -> "Product":
        return cls(
            message_id          = req.message_id,
            client_name         = req.client_name,
            report_date         = req.report_date,
            batch_id            = req.batch_id,
            strike_id           = req.strike_id,
            client_metadata     = req.client_metadata,
            google_shopping_url = req.google_shopping_url,
            seller_limit        = req.seller_limit,
            output              = output,
            status              = status,
        )


@dataclass
class Session:
    """A request-method session. Cookies and the oapv template are captured together
    in the same Chrome and are only valid together, so they live in one object and are
    always replaced as a whole (never modified in place).

    validated=False until the first real request made with this session succeeds."""
    requests_session: Any
    oapv_template: str
    session_id: str
    created_at: datetime
    validated: bool = False
