"""Session list, resume, delete-my-chats and thumbs feedback (Phase 10): ownership, paging limits
and the review row a thumbs-down makes."""

import json

import pytest

from tests.api_chat_fixtures import QUESTION, ChatEnv

MISSING = "00000000-0000-4000-8000-000000000000"
NOT_FOUND = {"error": {"code": "not_found", "message": "Not found."}}


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path)
    yield made
    made.close()


def start(env, token, text=QUESTION):
    body = env.chat(token, text).json()
    return body["session_id"]


def messages_of(env, session_id):
    return env.rows(
        "SELECT id, role FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
    )


# ---- the list ----------
def test_the_list_shows_only_my_sessions_newest_first(env):
    _, mine = env.user_token("asha_rao")
    _, other = env.user_token("ravi_k")
    first = start(env, mine, QUESTION + " one")
    second = start(env, mine, QUESTION + " two")
    start(env, other, QUESTION + " theirs")
    # (the rig's clock is fixed, so make the two sessions differ in activity explicitly)
    env.execute(
        "UPDATE sessions SET last_active_at = '2026-10-09T09:00:00+00:00' WHERE id = ?", (first,)
    )
    env.execute(
        "UPDATE sessions SET last_active_at = '2026-10-09T10:00:00+00:00' WHERE id = ?", (second,)
    )
    body = env.client.get("/sessions", headers=env.bearer(mine)).json()
    assert body["total"] == 2 and [s["id"] for s in body["sessions"]] == [second, first]
    assert set(body["sessions"][0]) == {"id", "title", "created_at", "last_active_at", "status"}
    assert body["limit"] == 20 and body["offset"] == 0


def test_a_list_has_a_default_and_a_maximum_page_size_and_larger_is_refused(env):
    _, token = env.user_token("asha_rao")
    for number in range(3):
        start(env, token, f"{QUESTION} number {number}")
    headers = env.bearer(token)
    assert env.client.get("/sessions?limit=2", headers=headers).json()["sessions"].__len__() == 2
    assert (
        len(env.client.get("/sessions?limit=2&offset=2", headers=headers).json()["sessions"]) == 1
    )
    for query in ("limit=101", "limit=0", "limit=-1", "offset=-1", "offset=100001", "limit=abc"):
        response = env.client.get(f"/sessions?{query}", headers=headers)
        assert response.status_code == 422, query
        assert response.json()["error"]["code"] == "invalid_request"


# ---- resume ----------
def test_resume_returns_the_whole_transcript_with_citations_and_flags(env):
    _, token = env.user_token("asha_rao")
    session_id = start(env, token)
    env.chat(token, QUESTION + " again", session_id=session_id)
    body = env.client.get(f"/sessions/{session_id}", headers=env.bearer(token)).json()
    assert body["id"] == session_id and body["total"] == 4 and body["status"] == "active"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "assistant"]
    answer = body["messages"][1]
    assert answer["citations"] and answer["citations"][0]["doc_id"] == "doc-a"
    assert answer["grounded"] is True and answer["source_removed"] is False
    assert answer["notice"] is None and "30 days" in answer["text"]


def test_resume_returns_the_grounded_flag_false_for_an_answer_stored_as_not_verified(env):
    _, token = env.user_token("asha_rao")
    session_id = start(env, token)
    env.execute("UPDATE messages SET grounded = 0 WHERE role = 'assistant'")
    body = env.client.get(f"/sessions/{session_id}", headers=env.bearer(token)).json()
    answer = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert answer["grounded"] is False  # the frontend shows its "not fully verified" notice


def test_resume_marks_a_greeting_as_a_fixed_reply_and_an_answer_as_an_answer(env):
    _, token = env.user_token("asha_rao")
    greeted = start(env, token, "Hello!")
    asked = start(env, token)
    kind = {}
    for name, session_id in (("greeting", greeted), ("question", asked)):
        body = env.client.get(f"/sessions/{session_id}", headers=env.bearer(token)).json()
        roles = {m["role"]: m for m in body["messages"]}
        kind[name] = roles["assistant"]["kind"]
        assert roles["user"]["kind"] is None  # the marker is for assistant messages only
    assert kind == {"greeting": "fixed_reply", "question": "answer"}


def test_the_fixed_reply_marker_is_stored_when_the_message_is_written(env):
    _, token = env.user_token("asha_rao")
    start(env, token, "Hello!")
    start(env, token)
    rows = env.rows("SELECT fixed_reply FROM messages WHERE role = 'assistant' ORDER BY created_at")
    assert [bool(r[0]) for r in rows] == [True, False]


def test_resume_pages_are_limited(env):
    _, token = env.user_token("asha_rao")
    session_id = start(env, token)
    for query, status in (("limit=201", 422), ("limit=1", 200), ("offset=-1", 422)):
        response = env.client.get(f"/sessions/{session_id}?{query}", headers=env.bearer(token))
        assert response.status_code == status, query
    one = env.client.get(f"/sessions/{session_id}?limit=1", headers=env.bearer(token)).json()
    assert len(one["messages"]) == 1 and one["total"] == 2 and one["limit"] == 1


@pytest.mark.parametrize("session_id", [MISSING, "not-a-uuid", "x" * 64, "%2e%2e"])
def test_a_missing_or_malformed_id_is_the_same_404(env, session_id):
    _, token = env.user_token("asha_rao")
    headers = env.bearer(token)
    for response in (
        env.client.get(f"/sessions/{session_id}", headers=headers),
        env.client.delete(f"/sessions/{session_id}", headers=headers),
    ):
        assert response.status_code == 404 and response.json() == NOT_FOUND


def test_someone_elses_session_is_the_same_404_for_every_route_and_stays_intact(env):
    _, owner = env.user_token("asha_rao")
    _, other = env.user_token("ravi_k")
    session_id = start(env, owner)
    before = messages_of(env, session_id)
    headers = env.bearer(other)
    message_id = before[1][0]
    responses = [
        env.client.get(f"/sessions/{session_id}", headers=headers),
        env.client.delete(f"/sessions/{session_id}", headers=headers),
        env.client.post(
            f"/messages/{message_id}/feedback", json={"rating": "down"}, headers=headers
        ),
    ]
    for response in responses:
        assert response.status_code == 404 and response.json() == NOT_FOUND
    assert messages_of(env, session_id) == before
    assert env.rows("SELECT COUNT(*) FROM feedback_entries") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM review_queue") == [(0,)]


def test_the_id_format_is_checked_before_any_lookup(env, monkeypatch):
    from app.memory import store

    def forbidden(*args, **kwargs):
        raise AssertionError("a malformed id must never reach the database")

    monkeypatch.setattr(store, "transcript", forbidden)
    monkeypatch.setattr(store, "delete_own_session", forbidden)
    _, token = env.user_token("asha_rao")
    headers = env.bearer(token)
    assert env.client.get("/sessions/not-a-uuid", headers=headers).status_code == 404
    assert env.client.delete("/sessions/not-a-uuid", headers=headers).status_code == 404


# ---- delete my chats ----------
def test_deleting_my_session_removes_it_with_its_messages_and_feedback(env):
    _, token = env.user_token("asha_rao")
    session_id = start(env, token)
    message_id = messages_of(env, session_id)[1][0]
    env.client.post(
        f"/messages/{message_id}/feedback", json={"rating": "down"}, headers=env.bearer(token)
    )
    assert (
        env.client.delete(f"/sessions/{session_id}", headers=env.bearer(token)).status_code == 204
    )
    assert messages_of(env, session_id) == []
    assert env.rows("SELECT COUNT(*) FROM feedback_entries") == [(0,)]
    assert env.client.get(f"/sessions/{session_id}", headers=env.bearer(token)).status_code == 404


def test_delete_all_removes_only_my_sessions(env):
    _, mine = env.user_token("asha_rao")
    _, other = env.user_token("ravi_k")
    for number in range(3):
        start(env, mine, f"{QUESTION} number {number}")
    theirs = start(env, other)
    response = env.client.delete("/sessions", headers=env.bearer(mine))
    assert response.status_code == 200 and response.json() == {"deleted": 3}
    assert env.client.get("/sessions", headers=env.bearer(mine)).json()["total"] == 0
    assert len(messages_of(env, theirs)) == 2  # untouched


# ---- feedback ----------
def rating_rows(env):
    return env.rows("SELECT rating FROM feedback_entries")


def test_a_thumbs_up_is_stored_once_and_the_other_thumb_changes_it(env):
    _, token = env.user_token("asha_rao")
    message_id = messages_of(env, start(env, token))[1][0]
    url, headers = f"/messages/{message_id}/feedback", env.bearer(token)
    assert env.client.post(url, json={"rating": "up"}, headers=headers).status_code == 200
    assert env.client.post(url, json={"rating": "up"}, headers=headers).status_code == 200
    assert rating_rows(env) == [("up",)]
    assert env.client.post(url, json={"rating": "down"}, headers=headers).status_code == 200
    assert rating_rows(env) == [("down",)]  # changed, not added


def test_a_thumbs_down_makes_one_review_item_with_the_masked_question(env):
    _, token = env.user_token("asha_rao")
    session_id = start(env, token)
    message_id = messages_of(env, session_id)[1][0]
    url, headers = f"/messages/{message_id}/feedback", env.bearer(token)
    for _ in range(3):  # three times: still one item
        env.client.post(url, json={"rating": "down"}, headers=headers)
    rows = env.rows("SELECT kind, status, session_id, message_id, details FROM review_queue")
    assert len(rows) == 1
    kind, status, item_session, item_message, details = rows[0]
    assert (kind, status, item_session, item_message) == (
        "thumbs_down",
        "open",
        session_id,
        message_id,
    )
    parsed = json.loads(details)
    assert parsed == {"reason": "thumbs_down", "question": QUESTION}


def test_a_thumbs_up_makes_no_review_item(env):
    _, token = env.user_token("asha_rao")
    message_id = messages_of(env, start(env, token))[1][0]
    env.client.post(
        f"/messages/{message_id}/feedback", json={"rating": "up"}, headers=env.bearer(token)
    )
    assert env.rows("SELECT COUNT(*) FROM review_queue") == [(0,)]


def test_feedback_only_on_an_assistant_message_and_only_with_a_known_rating(env):
    _, token = env.user_token("asha_rao")
    rows = messages_of(env, start(env, token))
    user_message, answer = rows[0][0], rows[1][0]
    headers = env.bearer(token)
    assert (
        env.client.post(
            f"/messages/{user_message}/feedback", json={"rating": "up"}, headers=headers
        ).status_code
        == 404
    )
    for body in ({"rating": "meh"}, {}, {"rating": "up", "note": "x"}):
        response = env.client.post(f"/messages/{answer}/feedback", json=body, headers=headers)
        assert response.status_code == 422, body
    for bad in (MISSING, "not-a-uuid"):
        response = env.client.post(
            f"/messages/{bad}/feedback", json={"rating": "up"}, headers=headers
        )
        assert response.status_code == 404 and response.json() == NOT_FOUND


def test_feedback_on_an_answer_hidden_by_shadow_mode_is_404_for_its_user(env):
    env.set_stage_in_database("shadow")
    _, token = env.user_token("asha_rao")
    message_id = messages_of(env, start(env, token))[1][0]
    response = env.client.post(
        f"/messages/{message_id}/feedback", json={"rating": "down"}, headers=env.bearer(token)
    )
    assert response.status_code == 404 and response.json() == NOT_FOUND
    assert env.rows("SELECT COUNT(*) FROM feedback_entries") == [(0,)]
