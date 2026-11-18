import os

# Never depend on (or talk to) whatever model the developer configured in their shell: these tests use the
# deterministic playbook engine, and the model-loop tests install scripted models and switch the mode themselves.
os.environ["AGENT_MODE"] = "playbook"
os.environ["AGENT_PREFETCH"] = "1"
for _name in ("LLM_PROVIDER", "AGENT_MODEL", "OLLAMA_MODEL"):
    os.environ.pop(_name, None)

import json
import os
import unittest
from unittest.mock import patch

import httpx

from classifier import classify
from model_client import classify_with_model


class Response:
    def __init__(self, category):
        self.body = {"message": {"role": "assistant", "content": json.dumps({"category": category})}}

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class ModelClientTests(unittest.TestCase):
    @patch("model_client.httpx.Client.post")
    def test_requests_structured_label(self, post):
        post.return_value = Response("vpn")
        self.assertEqual(classify_with_model("remote access tunnel keeps dropping"), "vpn")
        payload = post.call_args.kwargs["json"]
        self.assertIn("enum", payload["format"]["properties"]["category"])
        self.assertNotIn("tools", payload)

    @patch.dict(os.environ, {}, clear=True)
    @patch("model_client.httpx.Client.post")
    def test_omits_unspecified_thinking_option(self, post):
        post.return_value = Response("unknown")
        classify_with_model("something odd happened today")
        self.assertIs(post.call_args.kwargs["json"]["think"], False)  # qwen3 default model: reasoning off

    @patch("model_client.classify_with_model", side_effect=httpx.ConnectError("down"))
    def test_model_outage_falls_back_to_unknown(self, _):
        self.assertEqual(classify("the thing on my desk is weird"), ("unknown", "none"))

    @patch("model_client.classify_with_model", return_value="hardware")
    def test_model_used_only_when_rules_fail(self, model):
        self.assertEqual(classify("My vpn is not working"), ("vpn", "rules"))
        model.assert_not_called()
        self.assertEqual(classify("the monitor on my desk is weird"), ("hardware", "model"))

    @patch("model_client.classify_with_model", return_value="delete_everything")
    def test_unknown_model_label_rejected(self, _):
        self.assertEqual(classify("the thing on my desk is weird"), ("unknown", "none"))
