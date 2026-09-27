from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    login_identifier: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class UserAuthCredential(Base):
    __tablename__ = "user_auth_credential"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, unique=True)
    login_identifier: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    user: Mapped[AppUser] = relationship()


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    user_agent: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    user: Mapped[AppUser] = relationship()


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int] = mapped_column(default=0, server_default="0")
    level: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class Brand(Base):
    __tablename__ = "brand"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    logo_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AttributeKey(Base):
    __tablename__ = "attribute_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_spec: Mapped[bool] = mapped_column(default=False, server_default="false")
    is_filter: Mapped[bool] = mapped_column(default=False, server_default="false")


class AttributeValue(Base):
    __tablename__ = "attribute_value"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attr_key_id: Mapped[int] = mapped_column(ForeignKey("attribute_key.id"), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)

    attr_key: Mapped[AttributeKey] = relationship()


class Spu(Base):
    __tablename__ = "spu"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), nullable=False)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brand.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    sub_title: Mapped[str | None] = mapped_column(String(255))
    detail_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[int] = mapped_column(default=0, server_default="0")
    sales_count: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    brand: Mapped[Brand] = relationship()
    category: Mapped[Category] = relationship()
    skus: Mapped[list["Sku"]] = relationship(back_populates="spu")


class Sku(Base):
    __tablename__ = "sku"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    spu_id: Mapped[int] = mapped_column(ForeignKey("spu.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    stock: Mapped[int] = mapped_column(default=0, server_default="0")
    sales_count: Mapped[int] = mapped_column(default=0, server_default="0")
    specs_json: Mapped[dict | None] = mapped_column(JSONB)
    image_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    spu: Mapped[Spu] = relationship(back_populates="skus")


class GoodsAttributeRelation(Base):
    __tablename__ = "goods_attribute_relation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    spu_id: Mapped[int] = mapped_column(ForeignKey("spu.id"), nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    attr_key_id: Mapped[int] = mapped_column(ForeignKey("attribute_key.id"), nullable=False)
    attr_value_id: Mapped[int] = mapped_column(ForeignKey("attribute_value.id"), nullable=False)


class OrderInfo(Base):
    __tablename__ = "order_info"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    pay_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    freight_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default="0"
    )
    pay_type: Mapped[int] = mapped_column(default=0, server_default="0")
    status: Mapped[int] = mapped_column(default=1, server_default="1")
    receiver_name: Mapped[str] = mapped_column(String(64), nullable=False)
    receiver_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    receiver_address: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    pay_at: Mapped[datetime | None]
    delivery_at: Mapped[datetime | None]

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    logistics: Mapped["OrderLogistics | None"] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order_info.id"), nullable=False)
    spu_id: Mapped[int] = mapped_column(ForeignKey("spu.id"), nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("sku.id"), nullable=False)
    sku_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku_specs: Mapped[dict | None] = mapped_column(JSONB)
    sku_image: Mapped[str | None] = mapped_column(String(512))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)

    order: Mapped[OrderInfo] = relationship(back_populates="items")


class OrderLogistics(Base):
    __tablename__ = "order_logistics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order_info.id"), nullable=False, unique=True)
    express_company: Mapped[str | None] = mapped_column(String(64))
    express_code: Mapped[str | None] = mapped_column(String(32))
    logistic_no: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[int] = mapped_column(default=0, server_default="0")
    trace_json: Mapped[dict | list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    order: Mapped[OrderInfo] = relationship(back_populates="logistics")


Index("idx_attribute_key_category_id", AttributeKey.category_id)
Index("idx_attribute_value_key_id", AttributeValue.attr_key_id)
Index("idx_spu_category_brand", Spu.category_id, Spu.brand_id)
Index("idx_sku_spu_id", Sku.spu_id)
Index("idx_goods_attribute_spu_sku", GoodsAttributeRelation.spu_id, GoodsAttributeRelation.sku_id)
Index("idx_goods_attribute_value", GoodsAttributeRelation.attr_value_id)
Index("idx_user_auth_login_identifier", UserAuthCredential.login_identifier)
Index("idx_user_session_user_id", UserSession.user_id)
Index("idx_user_session_status_expires", UserSession.status, UserSession.expires_at)
Index("idx_order_info_user_id", OrderInfo.user_id)
Index("idx_order_info_created_at", OrderInfo.created_at)
Index("idx_order_item_order_id", OrderItem.order_id)
Index("idx_order_logistics_logistic_no", OrderLogistics.logistic_no)
