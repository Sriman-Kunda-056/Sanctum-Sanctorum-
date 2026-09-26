"""Order operations: placing, paying and cancelling purchases."""
from datetime import datetime
from typing import Dict, List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Book, Member, MemberTier, Order, OrderItem, OrderStatus
from app.schemas import OrderCreate
from app.services.members import ensure_can_access_restricted, get_member

# Percentage discount granted by each membership tier.
TIER_DISCOUNT_PERCENT: Dict[str, int] = {
    MemberTier.APPRENTICE.value: 0,
    MemberTier.ADEPT.value: 5,
    MemberTier.MASTER.value: 10,
    MemberTier.SUPREME.value: 15,
}

# Extra discount when the total quantity across all items reaches the threshold.
BULK_QUANTITY_THRESHOLD = 10
BULK_DISCOUNT_PERCENT = 5


def _lock_books(db: Session, book_ids: List[int]) -> Dict[int, Book]:
    """Load books under a row lock (``SELECT ... FOR UPDATE``; a no-op on SQLite).

    The lock is held until commit/rollback, so two orders racing for the last copy are
    serialized instead of both passing the stock check. Rows are locked in id order so
    concurrent multi-book orders cannot deadlock each other.
    """
    books = db.scalars(select(Book).where(Book.id.in_(book_ids)).order_by(Book.id).with_for_update())
    return {book.id: book for book in books}


def calculate_discount_percent(member: Member, total_quantity: int) -> int:
    """Tier discount, plus the bulk discount when total quantity >= threshold."""
    percent = TIER_DISCOUNT_PERCENT[member.tier]
    if total_quantity >= BULK_QUANTITY_THRESHOLD:
        percent += BULK_DISCOUNT_PERCENT
    return percent


def create_order(db: Session, data: OrderCreate, now: datetime) -> Order:
    """Place a pending order and reserve stock.

    Checks, in order (422 for empty items / bad quantity / duplicate books is done by the schema):
    1. 404 member not found; 404 any book not found
    2. 403 any book restricted and member tier below master
    3. 409 any book has insufficient stock (all-or-nothing: nothing is changed)
    Then stock is decremented for every item and prices are snapshotted.
    Pricing: discount_cents = subtotal * percent // 100; total = subtotal - discount.
    """
    member = get_member(db, data.member_id)

    books = _lock_books(db, [item.book_id for item in data.items])
    for item in data.items:
        if item.book_id not in books:
            raise HTTPException(status_code=404, detail=f"Book {item.book_id} not found")

    if any(book.restricted for book in books.values()):
        ensure_can_access_restricted(member)

    for item in data.items:
        book = books[item.book_id]
        if book.stock < item.quantity:
            raise HTTPException(status_code=409, detail=f"Insufficient stock for book {item.book_id}")

    total_quantity = sum(item.quantity for item in data.items)
    discount_percent = calculate_discount_percent(member, total_quantity)

    order_items = []
    subtotal_cents = 0
    for item in data.items:
        book = books[item.book_id]
        book.stock -= item.quantity
        subtotal_cents += book.price_cents * item.quantity
        order_items.append(
            OrderItem(book_id=item.book_id, quantity=item.quantity, unit_price_cents=book.price_cents)
        )

    discount_cents = subtotal_cents * discount_percent // 100
    order = Order(
        member_id=member.id,
        status=OrderStatus.PENDING.value,
        subtotal_cents=subtotal_cents,
        discount_percent=discount_percent,
        discount_cents=discount_cents,
        total_cents=subtotal_cents - discount_cents,
        created_at=now,
        items=order_items,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int, lock: bool = False) -> Order:
    """Return an order by id, or raise 404. ``lock`` takes a row lock for status transitions."""
    order = db.get(Order, order_id, with_for_update=lock)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def pay_order(db: Session, order_id: int) -> Order:
    """Mark a pending order as paid. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id, lock=True)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot pay an order that is {order.status}")
    order.status = OrderStatus.PAID.value
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a pending order and restore the reserved stock. 404 if missing; 409 if not pending."""
    order = get_order(db, order_id, lock=True)
    if order.status != OrderStatus.PENDING.value:
        raise HTTPException(status_code=409, detail=f"Cannot cancel an order that is {order.status}")
    books = _lock_books(db, [item.book_id for item in order.items])
    for item in order.items:
        books[item.book_id].stock += item.quantity
    order.status = OrderStatus.CANCELLED.value
    db.commit()
    db.refresh(order)
    return order
