"""Edge cases beyond the provided suite: normalization, atomicity and boundary behaviour."""


def hyphenate(isbn: str) -> str:
    return f"{isbn[:3]}-{isbn[3:4]}-{isbn[4:9]}-{isbn[9:12]}-{isbn[12:]}"


class TestBookEdgeCases:
    def test_duplicate_isbn_is_detected_after_normalization(self, client, make_book):
        book = make_book()
        payload = {
            "title": "Another",
            "author": "Someone",
            "isbn": hyphenate(book["isbn"]),
            "price_cents": 100,
            "stock": 1,
        }
        assert client.post("/books", json=payload).status_code == 409

    def test_invalid_isbn_checksum_returns_422(self, client, make_book):
        isbn = make_book()["isbn"]
        wrong_check_digit = isbn[:-1] + str((int(isbn[-1]) + 1) % 10)
        payload = {"title": "T", "author": "A", "isbn": wrong_check_digit, "price_cents": 1, "stock": 1}
        assert client.post("/books", json=payload).status_code == 422

    def test_patch_null_is_rejected_and_book_is_unchanged(self, client, make_book):
        book = make_book(title="Keep Me")
        response = client.patch(f"/books/{book['id']}", json={"price_cents": 5, "title": None})
        assert response.status_code == 422
        assert client.get(f"/books/{book['id']}").json() == book

    def test_patch_ignores_isbn_and_unknown_fields(self, client, make_book):
        book = make_book()
        response = client.patch(f"/books/{book['id']}", json={"isbn": "0000000000000", "colour": "red", "stock": 3})
        assert response.status_code == 200
        assert response.json() == {**book, "stock": 3}

    def test_sorting_by_id_is_rejected(self, client):
        assert client.get("/books", params={"sort": "id"}).status_code == 422

    def test_total_ignores_pagination(self, client, make_book):
        for _ in range(3):
            make_book()
        body = client.get("/books", params={"limit": 1, "offset": 1}).json()
        assert (len(body["items"]), body["total"]) == (1, 3)


class TestMemberEdgeCases:
    def test_duplicate_email_differing_only_in_case_returns_409(self, client, make_member):
        make_member(email="Reader@Example.com")
        payload = {"name": "Other", "email": " reader@example.COM "}
        assert client.post("/members", json=payload).status_code == 409


class TestOrderAtomicity:
    def test_forbidden_item_leaves_stock_of_allowed_items_untouched(self, client, make_member, make_book):
        open_book = make_book(stock=5)
        restricted = make_book(restricted=True, stock=5)
        member = make_member(tier="adept")
        body = {
            "member_id": member["id"],
            "items": [{"book_id": open_book["id"], "quantity": 2}, {"book_id": restricted["id"], "quantity": 1}],
        }
        assert client.post("/orders", json=body).status_code == 403
        assert client.get(f"/books/{open_book['id']}").json()["stock"] == 5

    def test_price_change_after_order_does_not_change_pay_total(self, client, make_member, make_book):
        book = make_book(price_cents=1000)
        order = client.post(
            "/orders", json={"member_id": make_member()["id"], "items": [{"book_id": book["id"], "quantity": 1}]}
        ).json()
        client.patch(f"/books/{book['id']}", json={"price_cents": 9999})
        assert client.post(f"/orders/{order['id']}/pay").json()["total_cents"] == 1000


class TestLoanEdgeCases:
    def test_free_book_never_accrues_a_late_fee(self, client, clock, make_member, make_book):
        loan = client.post(
            "/loans", json={"member_id": make_member()["id"], "book_id": make_book(price_cents=0)["id"]}
        ).json()
        clock.advance(days=30)
        assert client.post(f"/loans/{loan['id']}/return").json()["late_fee_cents"] == 0

    def test_late_fee_uses_price_at_time_of_return(self, client, clock, make_member, make_book):
        book = make_book(price_cents=10_000)
        loan = client.post("/loans", json={"member_id": make_member()["id"], "book_id": book["id"]}).json()
        client.patch(f"/books/{book['id']}", json={"price_cents": 30})
        clock.advance(days=24)  # 10 days late -> 250 uncapped, capped at the current price
        assert client.post(f"/loans/{loan['id']}/return").json()["late_fee_cents"] == 30

    def test_overdue_loan_of_one_member_does_not_block_another(self, client, clock, make_member, make_book):
        first, second = make_member(tier="supreme"), make_member(tier="supreme")
        client.post("/loans", json={"member_id": first["id"], "book_id": make_book()["id"]})
        clock.advance(days=14, seconds=1)
        response = client.post("/loans", json={"member_id": second["id"], "book_id": make_book()["id"]})
        assert response.status_code == 201
        stats = client.get(f"/members/{first['id']}/stats").json()
        assert (stats["active_loans"], stats["overdue_loans"]) == (1, 1)
