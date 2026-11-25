from tests.e2e import materialize_doc_feature_questions as materialize


def test_html_body_is_reduced_to_readable_sections() -> None:
    body = (
        "<html><head><meta name='x' content='y'></head><body>"
        "<h2>Resource Limits</h2><p>Set max connections and keepalive limits.</p>"
        "<script>ignored()</script></body></html>"
    )
    text = materialize._html_to_text(body)
    assert "meta name" not in text
    assert "ignored" not in text
    section = materialize._section(text, "Resource Limits")
    assert "max connections" in section


def test_markdown_section_stops_at_next_heading() -> None:
    body = "# Intro\nintro text\n\n## Examples\nfirst example\n\n## Returns\nreturn value\n"
    assert "first example" in materialize._section(body, "Examples")
    assert "return value" not in materialize._section(body, "Examples")


def test_question_type_prefers_explicit_evidence() -> None:
    assert (
        materialize._question_type("example", False, ["demo.Client"], "", {})
        == "example"
    )
    assert (
        materialize._question_type("narrative", False, [], "", {"param_names": {}})
        == "parameter"
    )
    assert (
        materialize._question_type("narrative", True, [], "drift", {}, True) == "drift"
    )
    assert materialize._question_type("narrative", False, [], "", {}) == "no_answer"
