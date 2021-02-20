import logging
import typing

import requests
import telegram
from sqlalchemy import Column, ForeignKey, UniqueConstraint, VARCHAR, Float
from sqlalchemy import Integer, BigInteger, String, Text, LargeBinary, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base, DeferredReflection
from sqlalchemy.orm import relationship, backref

import utils

if typing.TYPE_CHECKING:
    import worker

log = logging.getLogger(__name__)

# Create a base class to define all the database subclasses
TableDeclarativeBase = declarative_base()


# Define all the database tables using the sqlalchemy declarative base
class User(DeferredReflection, TableDeclarativeBase):
    """A Telegram user who used the bot at least once."""

    # Telegram data
    user_id = Column(BigInteger, primary_key=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String)
    username = Column(String)
    language = Column(String, nullable=False)

    addresses = relationship("Address")

    # Extra table parameters
    __tablename__ = "users"

    def __init__(self, w: "worker.Worker", **kwargs):
        # Initialize the super
        super().__init__(**kwargs)
        # Get the data from telegram
        self.user_id = w.telegram_user.id
        self.first_name = w.telegram_user.first_name
        self.last_name = w.telegram_user.last_name
        self.username = w.telegram_user.username
        if w.telegram_user.language_code:
            self.language = w.telegram_user.language_code
        else:
            self.language = w.cfg["Language"]["default_language"]

    def __str__(self):
        """Describe the user in the best way possible given the available data."""
        if self.username is not None:
            return f"@{self.username}"
        elif self.last_name is not None:
            return f"{self.first_name} {self.last_name}"
        else:
            return self.first_name

    def identifiable_str(self):
        """Describe the user in the best way possible, ensuring a way back to the database record exists."""
        return f"user_{self.user_id} ({str(self)})"

    def mention(self):
        """Mention the user in the best way possible given the available data."""
        if self.username is not None:
            return f"@{self.username}"
        else:
            return f"[{self.first_name}](tg://user?id={self.user_id})"

    @property
    def full_name(self):
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        else:
            return self.first_name


class Category(DeferredReflection, TableDeclarativeBase):
    """Category of product. For example: pizza, beverage, steak, etc."""

    __tablename__ = "categories"

    # Category id
    id = Column(Integer, primary_key=True)
    # Category name
    name = Column(String, unique=True)
    # Is category visible to customers
    is_active = Column(Boolean, default=True)
    # Is category has been deleted
    deleted = Column(Boolean, default=False)
    # Parent category
    parent_id = Column(Integer, ForeignKey("categories.id"))

    parent = relationship("Category", backref=backref("children", uselist=False), remote_side="Category.id")
    products = relationship("Product", backref=backref("category"))


class Size(DeferredReflection, TableDeclarativeBase):
    """Multiple sizes of each product."""

    # Just pkey
    id = Column(Integer, primary_key=True)
    # ID of parent product
    product_id = Column(Integer, ForeignKey("products.id"))
    # Size name
    name = Column(VARCHAR(20))
    # Size cost
    price = Column(Integer)
    # Size has been deleted
    deleted = Column(Boolean, default=False)

    # relationship to parent product
    parent = relationship("Product", backref=backref("children"))

    __tablename__ = "sizes"


class Product(DeferredReflection, TableDeclarativeBase):
    """A purchasable product."""

    # Product id
    id = Column(Integer, primary_key=True)
    # Product name
    name = Column(String)
    # Product description
    description = Column(Text)
    # Product price, if null product is not for sale
    price = Column(Integer)
    # Image data
    image = Column(LargeBinary)
    # Product has been deleted
    deleted = Column(Boolean, nullable=False)
    # Multiple sizes of product
    # children = relationship("Size", backref=backref("parent"))
    category_id = Column(Integer, ForeignKey("categories.id"))

    # category = relationship("Category", backref=backref("products"))

    # Extra table parameters
    __tablename__ = "products"

    # No __init__ is needed, the default one is sufficient

    def text(self, w: "worker.Worker", *, style: str = "full", cart_qty: int = None, size_id: int = None,
             session = None):
        """Return the product details formatted with Telegram HTML. The image is omitted."""
        if size_id is not None:
            size = session.query(Size).filter_by(id=size_id, deleted=False).one()
            size_name = " " + str(size.name)
            size_price = float(size.price)
            price = str(w.Price(size_price))
        else:
            size_name = ""
            size_price = ""
            sizes = session.query(Size).filter_by(deleted=False, product_id=self.id).all()
            if len(sizes) != 0:
                price = ""
            else:
                price = str(w.Price(self.price))
            # price = str(w.Price(self.price))
        if style == "short":
            return f"{cart_qty}x {utils.telegram_html_escape(self.name + size_name)} - " \
                   f"{price * cart_qty}"
        elif style == "full":
            if cart_qty is not None:
                cart = w.loc.get("in_cart_format_string", quantity=cart_qty)
            else:
                cart = ''
            return w.loc.get("product_format_string", name=str(
                utils.telegram_html_escape(self.name) + size_name
            ),
                             description=utils.telegram_html_escape(self.description),
                             price=price,
                             cart=cart)
        else:
            raise ValueError("style is not an accepted value")

    def __repr__(self):
        return f"<Product {self.name}>"

    def send_as_message(self, w: "worker.Worker", chat_id: int, session: dict = None) -> dict:
        """Send a message containing the product data."""
        if self.image is None:
            r = requests.get(f"https://api.telegram.org/bot{w.cfg['Telegram']['token']}/sendMessage",
                             params={"chat_id": chat_id,
                                     "text": self.text(w, session=session),
                                     "parse_mode": "HTML"})
        else:
            r = requests.post(f"https://api.telegram.org/bot{w.cfg['Telegram']['token']}/sendPhoto",
                              files={"photo": self.image},
                              params={"chat_id": chat_id,
                                      "caption": self.text(w, session=session),
                                      "parse_mode": "HTML"})
        return r.json()

    def set_image(self, file: telegram.File):
        """Download an image from Telegram and store it in the image column.
        This is a slow blocking function. Try to avoid calling it directly, use a thread if possible."""
        # Download the photo through a get request
        r = requests.get(file.file_path)
        # Store the photo in the database record
        self.image = r.content


class Transaction(DeferredReflection, TableDeclarativeBase):
    """A greed wallet transaction.
    Wallet credit ISN'T calculated from these, but they can be used to recalculate it."""
    # TODO: split this into multiple tables

    # The internal transaction ID
    transaction_id = Column(Integer, primary_key=True)
    # The user whose credit is affected by this transaction
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    user = relationship("User", backref=backref("transactions"))
    # The value of this transaction. Can be both negative and positive.
    value = Column(Integer, nullable=False)
    # Refunded status: if True, ignore the value of this transaction when recalculating
    refunded = Column(Boolean, default=False)
    # Extra notes on the transaction
    notes = Column(Text)

    # Order ID
    order_id = Column(Integer, ForeignKey("orders.order_id"))
    order = relationship("Order")

    # Extra table parameters
    __tablename__ = "transactions"
    # __table_args__ = (UniqueConstraint("provider", "provider_charge_id"),)

    def text(self, w: "worker.Worker"):
        string = f"<b>T{self.transaction_id}</b> | {str(self.user)} | {w.Price(self.value)}"
        if self.refunded:
            string += f" | {w.loc.get('emoji_refunded')}"
        if self.notes:
            string += f" | {self.notes}"
        return string

    def __repr__(self):
        return f"<Transaction {self.transaction_id} for User {self.user_id}>"


class Admin(DeferredReflection, TableDeclarativeBase):
    """A greed administrator with his permissions."""

    # The telegram id
    user_id = Column(BigInteger, ForeignKey("users.user_id"), primary_key=True)
    user = relationship("User")
    # Permissions
    edit_products = Column(Boolean, default=False)
    display_on_help = Column(Boolean, default=False)
    is_owner = Column(Boolean, default=False)
    # Live mode enabled
    live_mode = Column(Boolean, default=False)

    # Extra table parameters
    __tablename__ = "admins"

    def __repr__(self):
        return f"<Admin {self.user_id}>"


class Address(DeferredReflection, TableDeclarativeBase):
    """Save all addresses to use in future"""
    __tablename__ = "addresses"

    id = Column(Integer, primary_key=True)
    text = Column(String)
    longitude = Column(Float)
    latitude = Column(Float)
    deleted = Column(Boolean)
    user_id = Column(Integer, ForeignKey("users.user_id"))

    user = relationship("User")


class Order(DeferredReflection, TableDeclarativeBase):
    """An order which has been placed by an user.
    It may include multiple products, available in the OrderItem table."""

    # The unique order id
    order_id = Column(Integer, primary_key=True)
    # The user who placed the order
    user_id = Column(BigInteger, ForeignKey("users.user_id"))
    user = relationship("User")
    # Date of creation
    creation_date = Column(DateTime, nullable=False)
    # Date of delivery
    delivery_date = Column(DateTime)
    # Date of refund: if null, product hasn't been refunded
    refund_date = Column(DateTime)
    # Refund reason: if null, product hasn't been refunded
    refund_reason = Column(Text)
    # List of items in the order
    items: typing.List["OrderItem"] = relationship("OrderItem")
    # Extra details specified by the purchasing user
    notes = Column(Text)
    # Linked transaction
    transaction = relationship("Transaction", uselist=False)
    address_id = Column(Integer, ForeignKey("addresses.id"))
    address = relationship("Address")
    is_pickup = Column(Boolean, default=False)
    phone = Column(String)

    # Extra table parameters
    __tablename__ = "orders"

    def __repr__(self):
        return f"<Order {self.order_id} placed by User {self.user_id}>"

    def text(self, w: "worker.Worker", session, user=False):
        joined_self = session.query(Order).filter_by(order_id=self.order_id).join(Transaction).one()
        items = ""
        for item in self.items:
            items += item.text(w) + "\n"
        if self.delivery_date is not None:
            status_emoji = w.loc.get("emoji_completed")
            status_text = w.loc.get("text_completed")
        elif self.refund_date is not None:
            status_emoji = w.loc.get("emoji_refunded")
            status_text = w.loc.get("text_refunded")
        else:
            status_emoji = w.loc.get("emoji_not_processed")
            status_text = w.loc.get("text_not_processed")
        if user and w.cfg["Appearance"]["full_order_info"] == "no":
            return w.loc.get("user_order_format_string",
                             status_emoji=status_emoji,
                             status_text=status_text,
                             items=items,
                             notes=self.notes,
                             value=str(w.Price(-joined_self.transaction.value))) + \
                   (w.loc.get("refund_reason", reason=self.refund_reason) if self.refund_date is not None else "")
        else:
            return status_emoji + " " + \
                   w.loc.get("order_number", id=self.order_id) + "\n" + \
                   w.loc.get("order_format_string",
                             user=self.user.mention(),
                             date=self.creation_date.isoformat(),
                             items=items,
                             notes=self.notes if self.notes is not None else "",
                             value=str(w.Price(-joined_self.transaction.value))) + \
                   (w.loc.get("refund_reason", reason=self.refund_reason) if self.refund_date is not None else "")


class OrderItem(DeferredReflection, TableDeclarativeBase):
    """A product that has been purchased as part of an order."""

    # The unique item id
    item_id = Column(Integer, primary_key=True)
    # The product that is being ordered
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product = relationship("Product")
    # The order in which this item is being purchased
    order_id = Column(Integer, ForeignKey("orders.order_id"), nullable=False)
    size_id = Column(Integer, ForeignKey("sizes.id"))

    # Extra table parameters
    __tablename__ = "orderitems"

    def text(self, w: "worker.Worker"):
        return f"{self.product.name} - {str(w.Price(self.product.price))}"

    def __repr__(self):
        return f"<OrderItem {self.item_id}>"
