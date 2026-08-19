"""软降级改写 think budget 的纯函数测试。"""

from aitube_pllm.core.queue import apply_think_budget

CAPS = {"high": -1, "medium": 256, "low": 0}


def test_high_leaves_client_settings():
    body = {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_token_budget": 4096,
    }
    assert apply_think_budget(body, "high", caps=CAPS) == "unchanged"
    assert body["thinking_token_budget"] == 4096
    assert body["chat_template_kwargs"]["enable_thinking"] is True


def test_medium_caps_unbounded_thinking():
    body = {"chat_template_kwargs": {"enable_thinking": True}}
    assert apply_think_budget(body, "medium", caps=CAPS) == "cap:256"
    assert body["thinking_token_budget"] == 256
    assert body["chat_template_kwargs"]["enable_thinking"] is True


def test_medium_never_raises_smaller_client_budget():
    body = {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_token_budget": 64,
    }
    apply_think_budget(body, "medium", caps=CAPS)
    assert body["thinking_token_budget"] == 64


def test_medium_rewrites_broken_thinking_object():
    body = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    apply_think_budget(body, "medium", caps=CAPS)
    assert "thinking" not in body
    assert body["thinking_token_budget"] == 256
    assert body["chat_template_kwargs"]["enable_thinking"] is True


def test_medium_does_not_enable_thinking_when_off():
    body = {"messages": []}
    apply_think_budget(body, "medium", caps=CAPS)
    assert body["thinking_token_budget"] == 256
    assert body.get("chat_template_kwargs") in (None, {})


def test_low_disables_thinking():
    body = {
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_token_budget": 256,
        "enable_thinking": True,
    }
    assert apply_think_budget(body, "low", caps=CAPS) == "disable"
    assert body["enable_thinking"] is False
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert "thinking_token_budget" not in body
