"""Who is asking, and which brands they may see (milestone 4).

A Principal comes from the verified login token (chatbot/auth.py) -- or, with AUTH_MODE=off
(tests, the laptop, the load tests), an anonymous principal that sees every brand, which is
the behaviour before this milestone.

Brand access is enforced in code on every query, never left to the model:
  - the SQL guard wraps each view that has a `brand` column in a subquery filtered to the
    caller's brands, so even SQL that ignores the brand (or a prompt-injected "all brands")
    can only read allowed rows                                  -- the enforcement
  - SQL naming another brand is refused with a 403              -- a clear answer
  - the model is only shown the caller's brands as known values -- less to leak
  - cache, stale-answer and idempotency keys include the caller's scope, so one brand's
    cached answer can never be served to another
"""
import hashlib
from dataclasses import dataclass

from chatbot import catalog

ALL_BRANDS_GROUP = "all-brands"
GROUP_PREFIX = "brand-"


class Forbidden(PermissionError):
    """The request needs data the caller may not see -> 403."""


@dataclass(frozen=True)
class Principal:
    subject: str                         # stable user id (token `sub`), "anonymous" with auth off
    username: str = ""
    brands: frozenset[str] | None = None   # None = every brand

    @property
    def scope(self) -> str:
        """Short, stable id of what this principal can see: part of every cache key."""
        if self.brands is None:
            return "all"
        return hashlib.sha256("|".join(sorted(self.brands)).encode()).hexdigest()[:16]


ANONYMOUS = Principal(subject="anonymous", username="anonymous", brands=None)


def client_brands() -> dict[str, str]:
    return dict(catalog.load()["access"]["client_brands"])


def brand_column() -> str:
    return catalog.load()["access"]["brand_column"]


def brands_for_groups(groups: list[str]) -> frozenset[str] | None:
    """Token groups -> visible brand names (None = all). Unknown groups are ignored."""
    names = {g.strip("/") for g in groups}
    if ALL_BRANDS_GROUP in names:
        return None
    mapping = client_brands()
    return frozenset(mapping[g[len(GROUP_PREFIX):]] for g in names
                     if g.startswith(GROUP_PREFIX) and g[len(GROUP_PREFIX):] in mapping)


def brand_views() -> set[str]:
    """Views that carry the brand column, so must be confined."""
    col = brand_column()
    return {name.lower() for name, v in catalog.load()["views"].items() if col in v["columns"]}


def filter_known_values(known: dict[str, list], principal: Principal) -> dict[str, list]:
    """Hide other brands from the prompt."""
    if principal.brands is None:
        return known
    col = brand_column()
    return {k: ([v for v in vals if v in principal.brands] if k.endswith(f".{col}") else vals)
            for k, vals in known.items()}
