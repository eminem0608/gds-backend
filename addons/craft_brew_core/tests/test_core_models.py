from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCoreModels(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.user = self.env["res.users"].create(
            {
                "name": "API Test User",
                "login": "api-test-user",
                "company_id": self.company.id,
                "company_ids": [(6, 0, [self.company.id])],
            }
        )

    def test_session_tokens_are_hashed_and_revocable(self):
        session, access_token, refresh_token = self.env["brew.api.session"].issue(
            self.user, self.company, {"name": "test"}
        )
        self.assertNotEqual(session.access_token_hash, access_token)
        self.assertNotEqual(session.refresh_token_hash, refresh_token)
        self.assertEqual(
            self.env["brew.api.session"].authenticate_access_token(access_token),
            session,
        )
        session.action_revoke("test")
        self.assertFalse(self.env["brew.api.session"].authenticate_access_token(access_token))

    def test_idempotency_replays_same_payload(self):
        model = self.env["brew.idempotency.record"]
        record, replay = model.begin(
            self.company, self.user, "POST", "/test", "same-key", {"a": 1}
        )
        self.assertFalse(replay)
        record.complete(201, {"id": "1"})
        repeated, replay = model.begin(
            self.company, self.user, "POST", "/test", "same-key", {"a": 1}
        )
        self.assertTrue(replay)
        self.assertEqual(repeated.stored_response(), (201, {"id": "1"}))

    def test_outbox_dispatch_marks_event_published(self):
        event = self.env["brew.outbox.event"].enqueue(
            "test.created", "test.aggregate", "1", {"id": 1}, self.company
        )
        self.assertEqual(self.env["brew.outbox.event"]._cron_dispatch(), 1)
        self.assertEqual(event.state, "published")
        self.assertEqual(event.attempts, 1)
