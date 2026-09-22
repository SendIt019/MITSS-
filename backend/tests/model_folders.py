"""Model folders for tests that talk to a stub server on 127.0.0.1.

A local endpoint gets its model sent as the absolute path of a folder under
MITSS_MODELS_DIR, and that folder is checked before any request. Tests point
MITSS_MODELS_DIR at a throwaway directory holding the names they use.
"""

from __future__ import annotations

import json
import os
import tempfile

NAMES = ("local-model", "my-model", "default-model", "m", "team-7b",
         "team-70b", "team-7b-q4", "works", "broken", "plain")


def use_fake_models_dir(test_case, names=NAMES) -> str:
    """Create a folder with a config.json per name; undone after the test."""
    tmp = tempfile.TemporaryDirectory()
    for name in names:
        os.makedirs(os.path.join(tmp.name, name))
        with open(os.path.join(tmp.name, name, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"model_type": "stub"}, handle)
    previous = os.environ.get("MITSS_MODELS_DIR")
    os.environ["MITSS_MODELS_DIR"] = tmp.name

    def restore():
        if previous is None:
            os.environ.pop("MITSS_MODELS_DIR", None)
        else:
            os.environ["MITSS_MODELS_DIR"] = previous
        tmp.cleanup()

    test_case.addCleanup(restore)
    return tmp.name
