from app.models.base import Base, BaseWithUuidId
from app.models.brand import Brand
from app.models.brand_event import BrandEvent, BrandEventType
from app.models.brand_follow import BrandFollow
from app.models.consent_log import ConsentLog, ConsentMethod, ConsentType
from app.models.oauth import OAuth
from app.models.product import Product, ProductCategory
from app.models.product_event import ProductEvent, ProductEventType
from app.models.search_event import SearchEvent
from app.models.user import User
from app.models.user_product_affinity import UserProductAffinity
from app.models.wishlist import Wishlist

__all__ = [
    "Base",
    "BaseWithUuidId",
    "Brand",
    "BrandEvent",
    "BrandEventType",
    "BrandFollow",
    "ConsentLog",
    "ConsentMethod",
    "ConsentType",
    "OAuth",
    "Product",
    "ProductCategory",
    "ProductEvent",
    "ProductEventType",
    "SearchEvent",
    "User",
    "UserProductAffinity",
    "Wishlist",
]
