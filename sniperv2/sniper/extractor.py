"""Czyste funkcje wyciągające dane z JSON-ów API Vinted (bez I/O - łatwe do testowania).

Struktury zgodne z api.docx:
  * /api/v2/items/{id}/details/sidebar  -> tytuł, cena, zdjęcia, plugins[item_status|description|user_info_header|...]
  * /api/v2/items/{id}/shipping_details -> {"shipping_details": {"price": {...}, "free_shipping": ..., ...}}
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .config import BASE_URL


@dataclass
class Seller:
    id: int | None
    name: str | None
    country: str | None
    country_code: str | None
    feedback_count: int | None
    feedback_reputation: float | None   # 0.0 - 1.0, tak jak zwraca API
    stars: float | None                 # reputacja przeliczona na skalę 0-5
    business: bool | None


@dataclass
class Shipping:
    price: float | None
    currency: str | None
    free_shipping: bool
    pickup_only: bool
    multiple_options: bool
    discount: object = None


@dataclass
class Offer:
    id: int
    url: str
    title: str
    price: float | None
    currency: str
    description: str
    photo_urls: list[str]
    seller: Seller
    shipping: Shipping | None
    total_price: float | None
    brand: str | None = None
    condition: str | None = None
    detected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        """Słownik gotowy do serializacji i wysyłki do modelu AI."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        """Odwrotność to_dict() - np. oferta wczytana z logs/offers.jsonl."""
        data = dict(data)
        seller = data.get("seller") or {}
        data["seller"] = Seller(**{name: seller.get(name) for name in Seller.__dataclass_fields__})
        if data.get("shipping") is not None:
            data["shipping"] = Shipping(**data["shipping"])
        return cls(**data)


def _decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _money(value):
    amount = _decimal(value)
    return float(amount) if amount is not None else None


def get_plugin(sidebar, name):
    """Zwraca `data` pluginu o danej nazwie z listy plugins (albo {})."""
    for plugin in sidebar.get("plugins") or []:
        if plugin.get("name") == name:
            return plugin.get("data") or {}
    return {}


def unwrap_sidebar(payload):
    """Niektóre odpowiedzi opakowują dane w klucz 'item'."""
    return payload.get("item", payload) if isinstance(payload, dict) else {}


def closing_action(sidebar):
    """Wartość item_closing_action z pluginu item_status (None = oferta aktywna)."""
    return get_plugin(sidebar, "item_status").get("item_closing_action")


def inactive_reason(sidebar):
    """Zwraca powód odrzucenia ('sold', 'reserved', ...) albo None, gdy oferta jest aktywna."""
    status = get_plugin(sidebar, "item_status")
    action = status.get("item_closing_action")
    if action is not None:
        return str(action)            # "sold" i inne akcje zamknięcia
    if status.get("is_closed") or sidebar.get("is_closed"):
        return "closed"
    if status.get("is_reserved") or sidebar.get("is_reserved"):
        return "reserved"
    if status.get("is_hidden") or sidebar.get("is_hidden"):
        return "hidden"
    if status.get("is_draft"):
        return "draft"
    return None


def extract_photo_urls(sidebar, catalog_item=None):
    """Czysta lista full_size_url (kolejność jak w ogłoszeniu, bez duplikatów)."""
    photos = sidebar.get("photos") or (catalog_item or {}).get("photos") or []
    if not photos and catalog_item and catalog_item.get("photo"):
        photos = [catalog_item["photo"]]
    photos = sorted(photos, key=lambda p: p.get("image_no") or 0)
    urls = []
    for photo in photos:
        url = photo.get("full_size_url") or photo.get("url")
        if url and url not in urls:
            urls.append(url)
    return urls


def extract_shipping(payload):
    if not payload:
        return None
    details = payload.get("shipping_details") or {}
    if not details:
        return None
    price = details.get("price") or {}
    free = bool(details.get("free_shipping"))
    amount = _money(price.get("amount"))
    return Shipping(
        price=0.0 if free and amount is None else amount,
        currency=price.get("currency_code"),
        free_shipping=free,
        pickup_only=bool(details.get("pickup_only")),
        multiple_options=bool(details.get("multiple_shipping_options_available")),
        discount=details.get("discount"),
    )


def extract_seller(sidebar, catalog_item=None, user_profile=None):
    header = get_plugin(sidebar, "user_info_header")
    cat_user = (catalog_item or {}).get("user") or {}
    profile = (user_profile or {}).get("user", user_profile or {})

    reputation = header.get("feedback_reputation", profile.get("feedback_reputation"))
    reputation = float(reputation) if reputation is not None else None

    return Seller(
        id=header.get("seller_id") or sidebar.get("seller_id") or cat_user.get("id") or profile.get("id"),
        name=header.get("name") or cat_user.get("login") or profile.get("login"),
        country=cat_user.get("country_title") or profile.get("country_title") or profile.get("country_title_local"),
        country_code=cat_user.get("country_iso_code") or cat_user.get("country_code")
        or profile.get("country_iso_code") or profile.get("country_code"),
        feedback_count=header.get("feedback_count", profile.get("feedback_count")),
        feedback_reputation=reputation,
        stars=round(reputation * 5, 2) if reputation is not None else None,
        business=header.get("business", sidebar.get("business")),
    )


def item_url(item_id, catalog_item=None):
    url = (catalog_item or {}).get("url")
    if url:
        return url if url.startswith("http") else BASE_URL + url
    return f"{BASE_URL}/items/{item_id}"


def _attribute(sidebar, code):
    for attr in get_plugin(sidebar, "attributes").get("attributes") or []:
        if attr.get("code") == code:
            return (attr.get("data") or {}).get("value")
    return None


def build_offer(item_id, sidebar, shipping_payload=None, catalog_item=None, user_profile=None):
    """Skleja Offer z odpowiedzi sidebar + shipping_details (+ opcjonalnie pozycji katalogu i profilu)."""
    catalog_item = catalog_item or {}
    price_obj = sidebar.get("price") or catalog_item.get("price") or {}
    if not isinstance(price_obj, dict):  # starsze odpowiedzi katalogu: "price": "69.99"
        price_obj = {"amount": price_obj, "currency_code": catalog_item.get("currency")}
    price = _decimal(price_obj.get("amount"))
    currency = price_obj.get("currency_code") or sidebar.get("currency") or "PLN"

    shipping = extract_shipping(shipping_payload)
    total = None
    if price is not None and shipping and shipping.price is not None:
        total = float(price + Decimal(str(shipping.price)))

    brand = (sidebar.get("brand_dto") or {}).get("title") or catalog_item.get("brand_title")

    return Offer(
        id=int(item_id),
        url=item_url(item_id, catalog_item),
        title=sidebar.get("title") or catalog_item.get("title") or "",
        price=float(price) if price is not None else None,
        currency=currency,
        description=get_plugin(sidebar, "description").get("description") or "",
        photo_urls=extract_photo_urls(sidebar, catalog_item),
        seller=extract_seller(sidebar, catalog_item, user_profile),
        shipping=shipping,
        total_price=total,
        brand=brand,
        condition=_attribute(sidebar, "status") or catalog_item.get("status"),
    )
