from pathlib import Path

from repofit.evidence import _clean, _frontmatter, _memory_items, redact
from repofit.model import MemoryDoc


def test_redact_secrets_and_contacts():
    text = ("ssh -p 2201 root@box.example.org Xk7pQ2mZ9wLr and key sk-ant-abcdefghijklmnopqrstuvwx "  # leak-audit: ok
            "call 13812345678 or mail someone@example.com")  # leak-audit: ok
    out = redact(text)
    assert "Xk7pQ2mZ9wLr" not in out
    assert "sk-ant-" not in out
    assert "13812345678" not in out  # leak-audit: ok
    assert "someone@example.com" not in out


def test_redact_keeps_ordinary_identifiers():
    text = "Qwen2.5-3B-Instruct on RTX5090, commit 8097f4f4, v1.4.1"
    assert redact(text) == text


def test_clean_strips_agent_wrappers():
    wrapped = ("# Files mentioned by the user: ## a.pdf: D:/x/a.pdf ## My request for Codex: "
               "turn this into a technical report")
    assert _clean(wrapped, 200) == "turn this into a technical report"
    assert _clean('<pasted_content id="1"> long log </pasted_content> looks good now', 200) == "looks good now"
    assert _clean("[Image: original 2550x2640]", 200) is None
    assert _clean("Traceback (most recent call last):\n  File x", 200) is None


def test_frontmatter_reads_nested_type():
    meta, body = _frontmatter("---\nname: x\ndescription: likes plain words\nmetadata:\n  type: user\n---\n\nbody")
    assert meta["type"] == "user" and meta["description"] == "likes plain words"
    assert body.strip() == "body"


def _doc(tmp_path: Path, name: str, text: str, agent: str = "claude-code", kind: str = "memory",
         scope: str = "project") -> MemoryDoc:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return MemoryDoc(agent, path, kind, scope)


def test_memory_selection_prefers_people_and_skips_templates(tmp_path):
    docs = [
        _doc(tmp_path, "project_x.md", "---\ndescription: project notes\ntype: project\n---\n" + "p" * 900),
        _doc(tmp_path, "user_role.md", "---\ndescription: researcher who directs agents\ntype: user\n---\nmore"),
        _doc(tmp_path, "SOUL.md", "_Fill this in during your first conversation._", agent="workbuddy", kind="persona"),
        _doc(tmp_path, "AGENTS.md", "repo layout", agent="project", kind="instructions"),
    ]
    items = _memory_items(docs, budget=10_000)
    texts = [i.text for i in items]
    assert texts[0].startswith("researcher who directs agents")
    assert not any("Fill this in" in t for t in texts)
    assert texts[-1] == "repo layout"
