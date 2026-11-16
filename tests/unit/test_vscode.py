"""The vscode app: its profile, CLI tree, and the Codex panel logic.

No VS Code: the panel is a hand-built list of Nodes in document order, the
shape codex.snapshot produces from the real UIA tree.
"""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from desktoppilot.cli import app
from desktoppilot.core import profiles
from desktoppilot.core.commands import run
from desktoppilot.core.errors import Ambiguous, NotPresent, PilotError
from desktoppilot.linux import procs
from desktoppilot.plugins.vscode import codex, git, palette
from desktoppilot.plugins.vscode import panel as P
from desktoppilot.plugins.vscode.panel import Node

NEW_CHAT_PLACEHOLDER = "Do anything"  # the composer's name on the home screen


def T(name: str, depth: int = 5) -> Node:
    return Node("Text", name, depth=depth)


def B(name: str, depth: int = 5, id: str = "") -> Node:
    return Node("Button", name, id=id, depth=depth)


def composer_nodes(placeholder: str = "Ask for follow-up changes") -> list[Node]:
    return [
        Node("Edit", placeholder, cls="ProseMirror ProseMirror-focused", depth=12),
        B(P.PERMISSIONS[0], 12, "radix-_r_8t_"),
        T("Approve for me", 14),
        B("GPT-6 Sol High", 13, "radix-_r_90_"),
        B(P.SEND[0], 12),
        B(P.WHERE[0], 11, "radix-_r_95_"),
        T("Work locally", 13),
    ]


def turn(prompt: str, reply: list[str] | None) -> list[Node]:
    nodes = [T(P.USER_HEADING[0], 10), T(prompt, 12), B("Copy message", 11)]
    if reply is not None:
        nodes += [T(P.REPLY_HEADING[0], 10), *[T(t, 12) for t in reply], B(P.COPY[0], 11)]
    return nodes


# ------------------------------------------------------------ app and CLI


def test_vscode_extends_code():
    profile = profiles.load("vscode")
    assert profile.extends == "code"
    assert profile.launch == "adopt"
    assert profile.depth == 25
    assert "--force-renderer-accessibility" in profile.args


def test_the_codex_group_is_on_the_cli():
    result = CliRunner().invoke(app, ["vscode", "codex", "--help"])
    assert result.exit_code == 0
    for command in ("open", "model", "send", "wait", "read"):
        assert command in result.output


def test_the_git_group_is_on_the_cli():
    result = CliRunner().invoke(app, ["vscode", "git", "checkout", "--help"])
    assert result.exit_code == 0
    for option in ("--remote", "--window", "--timeout"):
        assert option in result.output


def test_model_needs_all_three_options_or_none(monkeypatch):
    monkeypatch.setattr(codex, "set_model", lambda *a, **k: pytest.fail("must not set"))
    result = CliRunner().invoke(app, ["vscode", "codex", "model", "--name", "GPT-6 Sol"])
    assert result.exit_code == 1
    assert "together" in json.loads(result.output)["message"]


def test_send_takes_text_or_file_not_both(tmp_path):
    prompt = tmp_path / "p.txt"
    prompt.write_text("x", encoding="utf-8")
    result = CliRunner().invoke(app, ["vscode", "codex", "send", "hi", "--file", str(prompt)])
    assert result.exit_code == 1


def test_an_error_with_details_hands_them_over(capsys):
    def fail():
        raise codex.ModelMismatch("wrong", {"mismatches": [{"field": "fast"}]})

    with pytest.raises(typer.Exit):
        run(fail)
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "ModelMismatch"
    assert payload["details"] == {"mismatches": [{"field": "fast"}]}


# --------------------------------------------------------------- picker


@pytest.mark.parametrize(
    "label, model, effort",
    [
        ("GPT-6 Sol High", "GPT-6 Sol", "high"),
        ("GPT-6 Sol Extra High", "GPT-6 Sol", "xhigh"),
        ("GPT-6 Astra Medium", "GPT-6 Astra", "medium"),
        ("GPT-5.5  Ultra", "GPT-5.5", "ultra"),
        ("GPT-5.5", "GPT-5.5", None),
    ],
)
def test_parse_label(label, model, effort):
    parsed = P.parse_label(label)
    assert (parsed["model"], parsed["effort"]) == (model, effort)


@pytest.mark.parametrize(
    "given, key",
    [("high", "high"), ("High", "high"), ("xhigh", "xhigh"), ("Extra High", "xhigh")],
)
def test_effort_key(given, key):
    assert P.effort_key(given) == key


def test_unknown_effort_is_refused():
    with pytest.raises(PilotError, match="unknown effort"):
        P.effort_key("turbo")


def test_effort_step_direction():
    assert P.effort_step("medium", "high") == "{Right}"
    assert P.effort_step("ultra", "high") == "{Left}"


def test_slider_position():
    assert P.slider_position("GPT-6 Sol Standard, 2 of 5.") == (2, 5)
    assert P.slider_position("Use Left and Right arrow keys") is None


OFFERED = [
    "Default Recommended set of models",
    "GPT-6 Sol",
    "GPT-6 Astra",
    "GPT-5.6 Sol",
    "GPT-5.5",
]


def test_an_exact_model_name_wins():
    assert P.match_model("gpt-6 sol", OFFERED) == "GPT-6 Sol"


def test_a_word_names_the_one_model_that_has_it():
    assert P.match_model("Astra", OFFERED) == "GPT-6 Astra"


def test_a_word_two_models_share_is_ambiguous():
    with pytest.raises(Ambiguous, match="GPT-5.6 Sol"):
        P.match_model("Sol", OFFERED)


def test_a_missing_model_lists_what_is_offered():
    with pytest.raises(P.ModelNotFound, match="GPT-6 Astra"):
        P.match_model("Terra", OFFERED)


def test_mismatches_flags_each_differing_field():
    wanted = {"model": "GPT-6 Sol", "effort": "high", "fast": False}
    got = {"model": "GPT-6 Sol", "effort": "medium", "fast": True}
    assert P.mismatches(wanted, got) == [
        {"field": "effort", "wanted": "high", "got": "medium"},
        {"field": "fast", "wanted": False, "got": True},
    ]


def test_an_unreadable_field_is_a_mismatch():
    assert P.mismatches({"fast": False}, {"fast": None}) == [
        {"field": "fast", "wanted": False, "got": None}
    ]


def test_a_matching_read_back_has_no_mismatch():
    same = {"model": "GPT-6 Sol", "effort": "high", "fast": False}
    assert P.mismatches(same, dict(same)) == []


@pytest.mark.parametrize(
    "name, toggle, state",
    [
        (P.FAST[0], 0, False),
        (P.STANDARD[0], 1, True),
        (P.FAST[0], 1, None),  # the name and the state disagree
        (P.FAST[0], None, None),
    ],
)
def test_fast_state(name, toggle, state):
    assert P.fast_state([Node("CheckBox", name, toggle=toggle)]) is state


def test_no_fast_checkbox_is_unreadable():
    assert P.fast_state([T("More usage")]) is None


def test_the_picker_is_the_other_radix_button():
    assert P.picker(composer_nodes()).name == "GPT-6 Sol High"


def test_permissions_and_where_read_their_labels():
    nodes = composer_nodes()
    assert P.permissions(nodes) == "Approve for me"
    assert P.where(nodes) == "Work locally"


# ---------------------------------------------------------------- panel


def test_home_screen_is_a_new_chat():
    assert P.is_new_chat(composer_nodes(NEW_CHAT_PLACEHOLDER))
    assert not P.is_new_chat(turn("hi", ["OK"]) + composer_nodes())


def test_the_tour_has_no_composer():
    assert P.in_onboarding([T("Codex in your IDE"), B("Back"), B("Next")])
    assert not P.in_onboarding(composer_nodes() + [B("Back"), B("Next")])


def test_codex_tab_names():
    assert P.is_codex_tab("Codex")
    assert P.is_codex_tab("Codex, Editor Group 2")
    assert not P.is_codex_tab("Respond to greeting, preview, Editor Group 2")
    assert not P.is_codex_tab("Code VS Code plugin")


def test_last_reply_is_after_the_last_heading():
    nodes = turn("one", ["first"]) + turn("two", ["second", "second"]) + composer_nodes()
    reply = P.last_reply(nodes)
    assert reply["texts"] == ["second"]
    assert reply["copy"] is nodes[-len(composer_nodes()) - 1]


def test_no_reply_yet():
    assert P.last_reply(turn("hi", None) + composer_nodes()) is None


@pytest.mark.parametrize(
    "text, asks",
    [
        ("Done.", False),
        ("Should I also fix the tests?", True),
        ("Findings below.\n\n**Want me to apply them?**", True),
        ("", False),
    ],
)
def test_asks_question(text, asks):
    assert P.asks_question(text) is asks


def test_stop_means_generating():
    nodes = turn("hi", None) + [B(P.STOP[0])]
    assert P.turn_state(nodes)["state"] == "generating"


def test_a_turn_with_no_reply_and_no_stop_is_still_starting():
    assert P.turn_state(turn("hi", None) + composer_nodes())["state"] == "generating"


def test_an_approval_card_is_a_question_even_while_stop_shows():
    nodes = turn("run it", None) + [
        T("Allow ChatGPT to run this command?"),
        B("Deny"),
        B(P.STOP[0]),
    ]
    state = P.turn_state(nodes)
    assert state["state"] == "question"
    assert state["reason"] == "approval"


def test_a_reply_that_asks_is_a_question():
    state = P.turn_state(turn("review", ["Two findings.", "Should I fix them?"]))
    assert state == {"state": "question", "reason": "reply_asks", "prompt": "Should I fix them?"}


def test_a_usage_limit_ends_the_turn_as_failed():
    nodes = (
        turn("hi", None)
        + [T("You've hit your usage limit. Contact your workspace owner for more access.")]
        + [B("Request credits")]
        + composer_nodes()
    )
    state = P.turn_state(nodes)
    assert state["state"] == "failed"
    assert "usage limit" in state["message"]


def test_an_answered_turn_is_done():
    assert P.turn_state(turn("hi", ["OK"]) + composer_nodes()) == {"state": "done"}


def test_palette_row_may_carry_recently_used():
    assert palette._is_row("Codex: New Codex Agent, recently used", "Codex: New Codex Agent")
    assert not palette._is_row("Codex: New Codex Agent Panel", "Codex: New Codex Agent")
    assert not palette._is_row(None, "Codex: New Codex Agent")


class _FakePanel:
    def __init__(self, frames):
        self.frames = list(frames)
        self.title_ = "ChatGPT"

    def nodes(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def title(self):
        return self.title_


def test_until_composer_names_an_empty_document(monkeypatch):
    monkeypatch.setattr(codex.time, "sleep", lambda s: None)
    pan = _FakePanel([[Node("Document", "ChatGPT"), Node("Document", "")]])
    with pytest.raises(codex.Timeout, match="empty document"):
        codex._until_composer(pan, 0.05)


def test_until_composer_fails_fast_when_codex_could_not_start():
    pan = _FakePanel([[Node("Document", P.BROKEN[0]), T(P.BROKEN[0])]])
    with pytest.raises(codex.CodexFailed, match="could not start"):
        codex._until_composer(pan, 60)


def test_until_composer_returns_once_the_composer_shows():
    pan = _FakePanel([composer_nodes(NEW_CHAT_PLACEHOLDER)])
    nodes, pages = codex._until_composer(pan, 5)
    assert P.composer(nodes) is not None and pages == 0


# ------------------------------------------------------------ palette keys


class _FakePalette:
    """The focused row after each key: {Down} moves through `rows` (wrapping),
    Esc and Enter end it. `focus_lost_after` drops focus after that many keys."""

    def __init__(self, rows, focus_lost_after=None):
        self.rows, self.i, self.keys, self.closed = list(rows), 0, [], False
        self.focus_lost_after = focus_lost_after

    def row(self):
        if self.closed:
            return None
        if self.focus_lost_after is not None and len(self.keys) >= self.focus_lost_after:
            return None
        return self.rows[self.i]

    def send(self, keys, **_):
        self.keys.append(keys)
        if keys == "{Ctrl}{Shift}p":
            self.closed, self.i = False, 0
        elif keys == "{Down}":
            self.i = (self.i + 1) % len(self.rows)
        elif keys in ("{Esc}", "{Enter}"):
            self.closed = True


@pytest.fixture
def fake_palette(monkeypatch):
    def make(rows, **kw):
        fake = _FakePalette(rows, **kw)
        monkeypatch.setattr(palette, "_focused_row", fake.row)
        monkeypatch.setattr(palette.auto, "SendKeys", fake.send)
        monkeypatch.setattr(palette, "bring_to_front", lambda w: None)
        monkeypatch.setattr(palette, "SETTLE", 0.0)
        monkeypatch.setattr(palette, "STEP_WAIT", 0.0)
        return fake

    return make


CHECKOUT = "Git: Checkout to..."


def test_run_presses_enter_on_a_first_row_that_matches(fake_palette):
    fake = fake_palette([CHECKOUT + ", recently used", "Git: Checkout to (Detached)..."])
    assert palette.run(None, CHECKOUT) == {"ran": CHECKOUT}
    assert "{Down}" not in fake.keys
    assert fake.keys[-1] == "{Enter}"


def test_run_steps_down_past_a_longer_title(fake_palette):
    fake = fake_palette(["Git: Checkout to (Detached)...", "Git: Checkout to..., recently used"])
    palette.run(None, CHECKOUT)
    assert fake.keys[-2:] == ["{Down}", "{Enter}"]


def test_run_without_the_row_closes_the_palette(fake_palette):
    fake = fake_palette(["Git: Checkout to (Detached)...", "Git: Clone"])
    with pytest.raises(PilotError, match=r"offers 'Git: Checkout to \(Detached"):
        palette.run(None, CHECKOUT)
    assert fake.keys[-1] == "{Esc}"
    assert "{Enter}" not in fake.keys


def test_run_gives_up_after_max_steps(fake_palette):
    fake = fake_palette([f"Git: row {i}" for i in range(20)])
    with pytest.raises(PilotError, match="closed it"):
        palette.run(None, CHECKOUT, max_steps=3)
    assert fake.keys.count("{Down}") == 3


def test_no_key_once_focus_leaves_the_palette(fake_palette):
    # Ctrl+Shift+P, the typed title, then focus goes (e.g. to a chat composer)
    fake = fake_palette(["Git: Checkout to (Detached)...", CHECKOUT], focus_lost_after=2)
    with pytest.raises(PilotError, match="sent nothing more"):
        palette.run(None, CHECKOUT, timeout=0.2)
    assert fake.keys[2:] == []


def test_type_filter_refuses_without_palette_focus(fake_palette):
    fake = fake_palette(["x"], focus_lost_after=0)
    with pytest.raises(PilotError, match="not in the palette"):
        palette.type_filter("main")
    assert fake.keys == []


def test_pick_collects_the_rows_it_saw(fake_palette):
    fake_palette(["Create new branch...", "git-branch feat-2, 1 day ago", "git-branch feat, now"])
    seen = []
    row = palette.pick(lambda r: git.branch_row_matches(r, "feat", False), "feat", seen=seen)
    assert row == "git-branch feat, now"
    assert seen == ["Create new branch...", "git-branch feat-2, 1 day ago", row]


# -------------------------------------------------------------------- git

LOCAL = (
    "git-branch user/me/feature, 2 weeks ago, Jane Doe circle-small-filled "
    "7a68f1f circle-small-filled Keep only the comments, branches"
)
REMOTE = "cloud origin/user/me/feature, 2 weeks ago, Jane Doe, remote branches"


@pytest.mark.parametrize(
    "row, branch, remote, hit",
    [
        (LOCAL, "user/me/feature", False, True),
        (LOCAL, "user/me/feature", True, True),
        (REMOTE, "user/me/feature", False, False),
        (REMOTE, "user/me/feature", True, True),
        ("cloud upstream/feat, now, remote branches", "feat", True, True),
        ("git-branch feat-2, 1 day ago, branches", "feat", False, False),
        ("cloud origin/feat-2, 1 day ago", "feat", True, False),
        ("git-branch feat, 1 day ago, branches", "feat-2", False, False),
        ("git-branch feat, recently used", "feat", False, True),
        ("Create new branch...", "feat", True, False),
        ("tag feat, now", "feat", True, False),
        (None, "feat", True, False),
    ],
)
def test_branch_row_matches(row, branch, remote, hit):
    assert git.branch_row_matches(row, branch, remote) is hit


def _windows(monkeypatch, titles):
    from desktoppilot.core import apps

    listed = [{"hwnd": i + 1, "title": f"{t} - Visual Studio Code"} for i, t in enumerate(titles)]
    monkeypatch.setattr(apps, "profile_of", lambda ctx: profiles.load("vscode"))
    monkeypatch.setattr(apps, "require_running", lambda profile: [1])
    monkeypatch.setattr(apps, "windows", lambda profile, main_only=False: listed)
    monkeypatch.setattr(git.auto, "ControlFromHandle", lambda hwnd: hwnd)
    monkeypatch.setattr(
        git.palette.auto, "SendKeys", lambda *a, **k: pytest.fail("must not send keys")
    )


def test_several_windows_need_window(monkeypatch):
    _windows(monkeypatch, ["Claude Code - MyRepo", "Pull in main - OtherRepo"])
    with pytest.raises(Ambiguous, match="pass --window") as exc:
        git.checkout(None, "main")
    assert "MyRepo" in str(exc.value) and "OtherRepo" in str(exc.value)


def test_one_window_is_used_without_window(monkeypatch):
    _windows(monkeypatch, ["OtherRepo"])
    assert git.pick_window(None, None) == 1


def test_window_picks_the_one_title_that_matches(monkeypatch):
    _windows(monkeypatch, ["Claude Code - MyRepo", "Pull in main - MyRepoOther"])
    assert git.pick_window(None, "other") == 2
    with pytest.raises(Ambiguous):
        git.pick_window(None, "MyRepo")
    with pytest.raises(PilotError, match="no VS Code window"):
        git.pick_window(None, "nothere")


def test_a_remote_only_row():
    assert git.is_remote_row(REMOTE, "user/me/feature")
    assert not git.is_remote_row(LOCAL, "user/me/feature")


@pytest.mark.parametrize(
    "name, branch",
    [
        (
            "OtherRepo (Git) - user/me/other-feature*+!, Checkout Branch/Tag...",
            "user/me/other-feature",
        ),
        ("OtherRepo (Git) - main, Checkout Branch/Tag...", "main"),
        ("feat, Checkout Branch/Tag...", "feat"),
        ("OtherRepo (Git) - Synchronize Changes", None),
        ("", None),
    ],
)
def test_status_branch(name, branch):
    assert git.status_branch(name) == branch


# --------------------------------------------------- turn scope and strings


def test_a_question_text_in_an_old_turn_is_history():
    nodes = (
        turn("why?", ["It failed to connect to the database."])
        + [T("Allow ChatGPT to run this command?"), B("Deny")]
        + turn("thanks", ["Done."])
        + composer_nodes()
    )
    assert P.turn_state(nodes) == {"state": "done"}


def test_the_prompts_own_words_are_not_a_question():
    nodes = turn("Why does it say 'failed to connect to'", ["Because the port is closed."])
    assert P.turn_state(nodes + composer_nodes()) == {"state": "done"}


def test_a_question_text_in_the_newest_turn_matches_any_case():
    nodes = turn("connect", None) + [T("Allow ChatGPT TO CONNECT TO the server")]
    assert P.turn_state(nodes)["reason"] == "approval"


def test_every_effort_label_can_be_ordered():
    assert set(P.EFFORT_ORDER) == set(P.EFFORT_LABELS)
    assert P.effort_step("ultra", "persistent") == "{Right}"


def test_an_unknown_effort_tail_reads_as_no_effort():
    assert P.parse_label("GPT-6 Sol Standard")["effort"] is None


def test_a_label_model_may_abbreviate_the_radio():
    offered = ["Default Recommended set of models", "GPT-6 Sol", "GPT-5.6 Sol"]
    assert P.label_model_as_offered("Default", offered) == offered[0]
    assert P.label_model_as_offered("GPT-6 Sol", offered) == "GPT-6 Sol"
    assert P.label_model_as_offered("Sol", offered) is None
    assert P.label_model_as_offered(None, offered) is None


def test_display_strings_are_tuples_and_sets_are_frozen():
    for name in ("SEND", "STOP", "COPY", "USER_HEADING", "REPLY_HEADING", "NEXT", "CODEX_TAB"):
        assert isinstance(getattr(P, name), tuple)
    assert all(isinstance(v, tuple) for v in P.EFFORT_LABELS.values())
    for name in ("OTHER_DROPDOWNS", "QUESTION_BUTTONS", "FAILURE_BUTTONS"):
        assert isinstance(getattr(P, name), frozenset)
    assert isinstance(git.CHECKOUT_COMMAND, tuple) and isinstance(git.STATUS_SUFFIX, tuple)
    assert isinstance(codex.NEW_PANEL_COMMAND, tuple) and isinstance(codex.FRAME_NAME, tuple)


def test_a_localized_text_beside_the_english_one_matches(monkeypatch):
    monkeypatch.setattr(P, "SEND", ("Send", "送信"))
    monkeypatch.setattr(P, "CODEX_TAB", ("Codex", "コーデックス"))
    assert P.named([B("送信")], P.SEND) and P.is_codex_tab("コーデックス, Editor Group 2")


def test_named_takes_a_bare_string_whole_not_by_substring():
    assert P.named([B("Send"), B("Sen")], "Send") == [B("Send")]


def test_palette_run_tries_each_accepted_title(fake_palette):
    fake = fake_palette(["Git: Kasse...", "Git: Checkout to..."])
    assert palette.run(None, ("Git: Auschecken...", CHECKOUT)) == {"ran": CHECKOUT}
    assert fake.keys[-1] == "{Enter}"


def test_the_codex_commands_take_window():
    for command in ("open", "model", "send", "wait", "read"):
        result = CliRunner().invoke(app, ["vscode", "codex", command, "--help"])
        assert "--window" in result.output, command
    send_help = CliRunner().invoke(app, ["vscode", "codex", "send", "--help"]).output
    assert "--replace-draft" in send_help
    assert "--settle-polls" in CliRunner().invoke(app, ["vscode", "codex", "wait", "--help"]).output


# ------------------------------------------------------------ git compare

MAIN_ROW = "main, 11↓ 0↑ ⇄ origin/main git-commit f19354e • yesterday"
COUNTS = ["Behind 0 commits", "Ahead 3 commits", "2 files changed, 5 additions (+), 1 deletion (-)"]


def test_the_compare_command_is_on_the_cli():
    result = CliRunner().invoke(app, ["vscode", "git", "compare", "--help"])
    assert result.exit_code == 0
    for option in ("--base", "--window", "--timeout"):
        assert option in result.output


@pytest.mark.parametrize(
    "row, ref, hit",
    [
        (MAIN_ROW, "main", True),
        ("origin/main, git-commit f19354e • yesterday, Remote Branches", "main", False),
        ("origin/feature/merge-to-main, Remote Branches", "main", False),
        ("main-2, 1↓, Branches", "main", False),
        ("main", "main", False),
        (None, "main", False),
    ],
)
def test_ref_row_matches_only_the_exact_ref(row, ref, hit):
    assert git.ref_row_matches(row, ref) is hit


@pytest.mark.parametrize(
    "name, hit",
    [
        ("Comparing HEAD with main MyRepo, has actions", True),
        ("Comparing HEAD with main", True),
        ("Comparing HEAD with main-2 MyRepo, has actions", False),
        ("Comparing HEAD with origin/main MyRepo, has actions", False),
        ("Comparing user/x/feat with main MyRepo, has actions", False),
        ("Behind 0 commits, has actions", False),
        (None, False),
    ],
)
def test_compare_node_matches(name, hit):
    assert git.compare_node_matches(name, "HEAD", "main") is hit


def test_compare_counts_reads_singular_and_plural_rows():
    rows = [
        "Behind 0 commits, has actions",
        "Ahead 1 commit, has actions",
        "12 files changed, 256 additions (+), 1 deletion (-), has actions",
        "MyApp.Core/ElementLoader",
    ]
    assert git.compare_counts(rows) == {
        "behind": 0,
        "ahead": 1,
        "files_changed": 12,
        "additions": 256,
        "deletions": 1,
    }


def test_compare_counts_not_shown_are_none():
    assert git.compare_counts(["Behind 2 commits"])["ahead"] is None


class _Row:
    def __init__(self, name, cls="monaco-list-row"):
        self.Name, self.ClassName = name, cls


@pytest.fixture
def fake_compare(monkeypatch):
    """compare() with no VS Code: `views` are the node lists each walk of the
    view returns (the last repeats); the returned list records palette steps."""

    def make(views, *, picked="main, 0↓ 0↑", children=COUNTS):
        walks = list(views)
        steps = []
        monkeypatch.setattr(git, "pick_window", lambda ctx, window: _Row("repo - VS Code"))
        monkeypatch.setattr(git, "_view", lambda window: object())
        monkeypatch.setattr(
            git, "_nodes", lambda view, base: walks.pop(0) if len(walks) > 1 else walks[0]
        )
        monkeypatch.setattr(git.palette, "run", lambda *a, **k: steps.append("run"))
        monkeypatch.setattr(git, "_wait_picker", lambda timeout: steps.append("picker"))
        monkeypatch.setattr(git.palette, "type_filter", lambda text: steps.append(f"type {text}"))

        def pick(match, what, **k):
            steps.append("pick")
            if not match(picked):
                raise palette.NoRow("no row")
            return picked

        monkeypatch.setattr(git.palette, "pick", pick)
        monkeypatch.setattr(git, "_expand", lambda node: None)
        monkeypatch.setattr(git, "_children", lambda node: children)
        return steps

    return make


def test_compare_a_new_node(fake_compare):
    node = _Row("Comparing HEAD with main repo, has actions")
    steps = fake_compare([[], [node]])
    out = git.compare(None, "main", timeout=1)
    assert steps == ["run", "picker", "type main", "pick"]
    assert out["node"] == "Comparing HEAD with main repo"
    assert out["existed_before"] is False
    assert (out["ahead"], out["behind"], out["files_changed"]) == (3, 0, 2)
    assert (out["additions"], out["deletions"]) == (5, 1)


def test_compare_reuses_the_node_gitlens_selects(fake_compare):
    old = _Row("Comparing HEAD with main repo, has actions")
    chosen = _Row("Comparing HEAD with main repo, has actions", "monaco-list-row selected focused")
    fake_compare([[old], [chosen]])
    assert git.compare(None, "main", timeout=1)["existed_before"] is True


def test_compare_with_the_view_hidden_before_does_not_know(fake_compare, monkeypatch):
    fake_compare([[_Row("Comparing HEAD with main repo", "monaco-list-row selected")]])
    views = iter([None])
    monkeypatch.setattr(git, "_view", lambda window: next(views, object()))
    assert git.compare(None, "main", timeout=1)["existed_before"] is None


def test_compare_without_a_node_fails(fake_compare):
    fake_compare([[]])
    with pytest.raises(PilotError, match="no 'Comparing HEAD with main' node"):
        git.compare(None, "main", timeout=0.1)


def test_compare_counts_the_view_never_shows_are_null(fake_compare):
    selected = _Row("Comparing HEAD with main", "monaco-list-row selected")
    fake_compare([[selected]], children=["Behind 0 commits"])
    out = git.compare(None, "main", timeout=0.1)
    assert out["behind"] == 0 and out["ahead"] is None


def test_compare_a_base_only_a_remote_matches_is_not_present(fake_compare):
    steps = fake_compare([[]], picked="origin/main, Remote Branches")
    with pytest.raises(NotPresent, match="no ref 'main'"):
        git.compare(None, "main", timeout=0.1)
    assert steps[-1] == "pick"


def test_compare_without_gitlens_is_not_present(fake_compare, monkeypatch):
    fake_compare([[]])

    def not_offered(*a, **k):
        raise palette.NotOffered("the palette offers 'Git: Compare'")

    monkeypatch.setattr(git.palette, "run", not_offered)
    monkeypatch.setattr(git, "_wait_picker", lambda t: pytest.fail("must not go on"))
    with pytest.raises(NotPresent, match="GitLens"):
        git.compare(None, "main", timeout=0.1)


@pytest.mark.parametrize("base", ["", "{Enter}", "a b"])
def test_compare_refuses_a_base_that_is_not_a_ref(base, monkeypatch):
    monkeypatch.setattr(git, "pick_window", lambda *a: pytest.fail("must not look"))
    with pytest.raises(PilotError, match="not a ref name"):
        git.compare(None, base)


def test_compare_several_windows_need_window(monkeypatch):
    _windows(monkeypatch, ["Claude Code - MyRepo", "Pull in main - OtherRepo"])
    with pytest.raises(Ambiguous, match="pass --window"):
        git.compare(None, "main")


def test_wait_picker_closes_and_types_nothing_when_the_box_never_asks(monkeypatch):
    closed = []
    monkeypatch.setattr(palette, "prompt", lambda: "Type the name of a command to run.")
    monkeypatch.setattr(palette, "_close", lambda: closed.append(True))
    with pytest.raises(PilotError, match="typed nothing"):
        git._wait_picker(0.1)
    assert closed == [True]


def test_wait_picker_returns_once_the_box_asks_for_a_reference(monkeypatch):
    asks = git.REF_PROMPT[0] + " (or enter a revision prefixed with #) - Compare HEAD with"
    monkeypatch.setattr(palette, "prompt", lambda: asks)
    git._wait_picker(1)


def test_compare_strings_are_tuples():
    for name in ("COMPARE_COMMAND", "REF_PROMPT", "VIEW_NAME", "NODE_PREFIX", "ROW_ACTIONS"):
        assert isinstance(getattr(git, name), tuple)


# ------------------------------------------------------------ Linux tree

from desktoppilot.plugins.vscode import common, locate, picker, snapshot  # noqa: E402


def test_model_items_are_radios_or_checkable_menu_items():
    nodes = [
        Node("MenuItem", "Select model"),
        Node("RadioButton", "GPT-6 Sol", selected=True),
        Node("MenuItem", "6 Luna", selected=False),
        Node("MenuItem", "Power"),
    ]
    assert [n.name for n in P.model_items(nodes)] == ["GPT-6 Sol", "6 Luna"]


class _Pattern:
    def __init__(self, **state):
        self.__dict__.update(state)


class _Item:
    """A control with only the patterns given."""

    def __init__(self, kind, name, **patterns):
        self.ControlTypeName, self.Name = kind, name
        self.AutomationId = self.ClassName = ""
        self.patterns = patterns

    def __getattr__(self, getter):
        if getter.startswith("Get") and getter.endswith("Pattern"):
            return lambda: self.patterns.get(getter)
        raise AttributeError(getter)


def test_a_radio_menu_item_reads_checked_through_selection_or_toggle():
    selected = _Item("MenuItemControl", "6 Luna", GetSelectionItemPattern=_Pattern(IsSelected=True))
    toggled = _Item("MenuItemControl", "5.6 Luna", GetTogglePattern=_Pattern(ToggleState=0))
    plain = _Item("MenuItemControl", "Power")
    assert snapshot._node(selected, 3).selected is True
    assert snapshot._node(toggled, 3).selected is False
    assert snapshot._node(plain, 3).selected is None


def test_choose_takes_a_closed_list_as_the_answer(monkeypatch):
    class Pan:
        def __init__(self, walks):
            self.walks = iter(walks)

        def nodes(self):
            return next(self.walks)

    def landed_then_raised(pan, node):
        raise PilotError("the action did nothing: timed out waiting for the item")

    monkeypatch.setattr(codex, "_click", landed_then_raised)
    radio = Node("MenuItem", "5.6 Luna", selected=False)
    closed = [Node("Button", "Send")]
    picker._choose(Pan([closed, closed]), radio)
    with pytest.raises(PilotError, match="did nothing"):
        picker._choose(Pan([[radio]]), radio)


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Element:
    def __init__(self, cls, parent=None, rect=(0, 0, 0, 0)):
        self.ClassName, self.parent, self.BoundingRectangle = cls, parent, _Rect(*rect)

    def GetParentControl(self):
        return self.parent


def _frame(*classes, rect=(0, 0, 0, 0)):
    parent = None
    for cls in reversed(classes):
        parent = _Element(cls, parent)
    return _Element("frame", parent, rect)


def test_a_frame_under_a_pane_body_is_the_sidebar():
    assert locate._in_sidebar(_frame("document", "pane-body", "split-view"))
    assert not locate._in_sidebar(_frame("document", "editor-instance"))


def test_an_overlaid_frame_is_the_sidebar_when_it_covers_a_view_body():
    view = _frame("document", "webview-overlay-content outer-right", rect=(1600, 400, 1900, 1300))
    editor = _frame("document", "webview-overlay-content outer-bottom", rect=(900, 400, 1600, 1300))
    bodies = lambda: [(1600, 400, 1900, 1300)]  # noqa: E731
    assert locate._in_sidebar(view, bodies)
    assert not locate._in_sidebar(editor, bodies)
    assert not locate._in_sidebar(view)


def test_contains_is_right_and_bottom_exclusive():
    assert locate.contains((0, 0, 10, 10), (0, 9))
    assert not locate.contains((0, 0, 10, 10), (10, 5))


@pytest.mark.parametrize(
    ("windows", "attr", "value"),
    [(True, "AutomationId", "RootWebArea"), (False, "LocalizedControlType", "document web")],
)
def test_the_frame_document_is_named_per_platform(monkeypatch, windows, attr, value):
    monkeypatch.setattr(locate.native, "WINDOWS", windows)
    control = _Item("DocumentControl", "ChatGPT")
    control.LocalizedControlType = "document frame"
    assert not locate.is_web_root(control)
    setattr(control, attr, value)
    assert locate.is_web_root(control)


def test_bring_to_front_refuses_a_window_that_stays_behind(monkeypatch):
    class Window:
        NativeWindowHandle = 7

        def SetActive(self, waitTime=0):
            return True

    monkeypatch.setattr(palette.win32gui, "GetForegroundWindow", lambda: 9)
    monkeypatch.setattr(palette, "FRONT_TIMEOUT", 0.0)
    with pytest.raises(PilotError, match="sent nothing"):
        palette.bring_to_front(Window())


def test_send_keys_if_passes_the_pacing_on(monkeypatch):
    calls = []
    monkeypatch.setattr(common.auto, "SendKeys", lambda keys, **kw: calls.append(kw))
    common.send_keys_if(lambda: True, "x", "no", interval=0.01)
    assert calls == [{"interval": 0.01}]


def test_the_composer_text_is_its_value():
    edit = _Item("EditControl", "Do anything", GetValuePattern=_Pattern(Value="Reply  with\nOK"))
    assert codex._composer_text(Node("Edit", "Do anything", control=edit)) == "Reply with OK"


def test_notices_read_a_native_modal_from_core_dialogs(monkeypatch):
    from desktoppilot.core import apps

    monkeypatch.setattr(git.ui, "search", lambda *a, **k: [])
    monkeypatch.setattr(apps, "profile_of", lambda ctx: profiles.load("vscode"))
    monkeypatch.setattr(apps, "all_pids", lambda profile: [5])
    monkeypatch.setattr(
        git.dialogs,
        "dialog_windows",
        lambda pids, classes: [{"hwnd": 3, "title": "Visual Studio Code", "text": "Stash?"}],
    )
    monkeypatch.setattr(git.auto, "ControlFromHandle", lambda hwnd: None)
    assert git.notices(None, None) == ["Visual Studio Code", "Stash?"]


def test_the_cdp_group_is_on_the_cli():
    result = CliRunner().invoke(app, ["vscode", "cdp", "--help"])
    assert result.exit_code == 0
    for command in ("targets", "eval", "type", "set-value"):
        assert command in result.output


def test_code_starts_electron_as_an_app_even_from_an_extension_host():
    assert "ELECTRON_RUN_AS_NODE" not in profiles.load("code").env
    env = procs.app_env({"HOME": "/home/me", "ELECTRON_RUN_AS_NODE": "1"})
    assert "ELECTRON_RUN_AS_NODE" not in env
