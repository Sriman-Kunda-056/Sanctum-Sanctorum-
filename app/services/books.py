"""Book catalogue operations."""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Book
from app.schemas import BookCreate, BookPage, BookSort, BookUpdate

_SORT_COLUMNS = {
    "title": Book.title,
    "price": Book.price_cents,
}


def _isbn_taken(db: Session, isbn: str, exclude_book_id: Optional[int] = None) -> bool:
    query = select(Book.id).where(Book.isbn == isbn)
    if exclude_book_id is not None:
        query = query.where(Book.id != exclude_book_id)
    return db.scalar(query) is not None


def create_book(db: Session, data: BookCreate) -> Book:
    """Add a book to the catalogue.

    Rules: the (already normalized) ISBN must be unique -> 409 otherwise.
    """
    if _isbn_taken(db, data.isbn):
        raise HTTPException(status_code=409, detail="isbn already exists")
    book = Book(**data.model_dump())
    db.add(book)
    db.commit()
    db.refresh(book)
    return book


def get_book(db: Session, book_id: int) -> Book:
    """Return a book by id, or raise 404."""
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return book


def update_book(db: Session, book_id: int, data: BookUpdate) -> Book:
    """Apply a partial update. Only fields present in the request are changed; 404 if missing.

    ``isbn`` and unknown fields are silently ignored (``BookUpdate`` has no ``isbn`` field, so
    they're already dropped by the schema). Validation happens before any field is mutated, so a
    422 never leaves the book partially updated.
    """
    book = get_book(db, book_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(book, field, value)
    db.commit()
    db.refresh(book)
    return book


def list_books(
    db: Session,
    q: Optional[str] = None,
    restricted: Optional[bool] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    sort: Optional[BookSort] = None,
    limit: int = 20,
    offset: int = 0,
) -> BookPage:
    """Search the catalogue.

    Rules:
    - ``q`` matches title OR author, case-insensitive substring.
    - ``restricted`` filters exactly; ``min_price``/``max_price`` are inclusive.
    - Sorted by ``sort`` (title / price, ``-`` for descending) with ties broken by id;
      default order is id ascending.
    - ``total`` counts all matches before ``limit``/``offset`` are applied.
    """
    query = select(Book)
    if q:
        query = query.where(or_(Book.title.icontains(q, autoescape=True), Book.author.icontains(q, autoescape=True)))
    if restricted is not None:
        query = query.where(Book.restricted == restricted)
    if min_price is not None:
        query = query.where(Book.price_cents >= min_price)
    if max_price is not None:
        query = query.where(Book.price_cents <= max_price)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0

    if sort is None:
        order_by = (Book.id.asc(),)
    else:
        descending = sort.startswith("-")
        column = _SORT_COLUMNS[sort[1:] if descending else sort]
        order_by = (column.desc() if descending else column.asc(), Book.id.asc())

    books = db.scalars(query.order_by(*order_by).limit(limit).offset(offset)).all()

    return BookPage(items=books, total=total, limit=limit, offset=offset)
