"""Unit tests for the CNE module's client behaviour - run outside Slicer, with
`qt`/`ctk`/`slicer` stubbed (ServerToolsCore/Testing/Python/qt_stubs.py).

What is tested here is what the generic ServerToolsCore tests cannot cover,
because it depends on CNE's own schema: that a notes folder and a note type
alone are a complete request (the note type picks the model on the server, as
the old radio buttons did), that the notes picker offers notes and not scans,
and that the module no longer installs or downloads anything.

`CNERemote.py` itself is deliberately NOT imported: it subclasses
ScriptedLoadableModule and ServerToolWidgetBase, which need a real Slicer. Its
declarations are read out of the source with `ast` instead.

Usage:
    python3 -m unittest CNERemote/Testing/Python/test_cne_client.py
"""

import ast
import os
import sys
import unittest

_HERE = os.path.abspath(os.path.dirname(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_CORE = os.path.join(_REPO_ROOT, "ServerToolsCore")
sys.path.insert(0, os.path.join(_CORE, "Testing", "Python"))
sys.path.insert(0, _CORE)

import qt_stubs

qt, ctk = qt_stubs.install()

from ServerToolsCoreLib import formgen
from ServerToolsCoreLib.client import ToolServerClient, file_extensions_for

# The server's GET /tools payload for CNE, taken from a running server and
# brought up to the tool's current signature: `model` optional (the note type
# picks the bundle) and `notes` publishing the extensions its readers read.
# Keys the server always sends as null for this tool are left out.
CNE_SCHEMA = {
    "name": "CNE",
    "arguments": {
        "notes": {
            "type": "path",
            "types": [
                "path"
            ],
            "required": True,
            "description": "One clinical note (.txt, .pdf or .docx), or a folder of them for a cohort. Folders are searched recursively and the extension is matched case-insensitively, so notes filed per patient and a `NOTE.PDF` off a Windows share are both found.",
            "server_selectable": "testfile",
            "choices": None,
            "initial": None,
            "extensions": {
                "path": [
                    ".docx",
                    ".pdf",
                    ".txt"
                ]
            },
            "label": "Clinical notes",
            "section": "Notes",
            "visible_when": None,
            "options_when": None,
            "hidden": False,
            "ui": None,
            "groups": None
        },
        "notes_type": {
            "type": "choice",
            "types": [
                "choice"
            ],
            "required": True,
            "description": "Which fine-tune is being run, \"TMJ\" or \"Ortho\". It selects the system prompt, the context window and, unless a model is named below, the model itself -- they are two different models.",
            "server_selectable": None,
            "choices": {
                "TMJ": True,
                "Ortho": False
            },
            "initial": None,
            "extensions": None,
            "label": "Note type",
            "section": "Notes",
            "visible_when": None,
            "options_when": None,
            "hidden": False,
            "ui": None,
            "groups": None
        },
        "model": {
            "type": "str",
            "types": [
                "str"
            ],
            "required": False,
            "description": "The quantised GGUF model, named from what this server hosts. Left empty, the bundle named after the note type is used, which is the one fine-tuned for it. A hosted bundle folder holding exactly one .gguf resolves to it; one holding several is refused rather than resolved by sort order.",
            "server_selectable": "model",
            "choices": None,
            "initial": None,
            "extensions": None,
            "label": "Model",
            "section": "Notes",
            "visible_when": None,
            "options_when": None,
            "hidden": False,
            "ui": None,
            "groups": None
        },
        "max_tokens": {
            "type": "int",
            "types": [
                "int"
            ],
            "required": False,
            "description": "The longest answer the model may give per note. An answer cut off at this limit is reported as a failure and never written: truncated JSON parses as nothing, and upstream wrote the fragment as if it were the extraction.",
            "server_selectable": None,
            "choices": None,
            "initial": 2048,
            "extensions": None,
            "label": "Longest answer (tokens)",
            "section": "Generation",
            "visible_when": None,
            "options_when": None,
            "hidden": False,
            "ui": None,
            "groups": None
        },
        "temperature": {
            "type": "float",
            "types": [
                "float"
            ],
            "required": False,
            "description": "Sampling temperature. 0 makes the same note extract to the same answer twice, which is what lets a result be checked.",
            "server_selectable": None,
            "choices": None,
            "initial": 0.0,
            "extensions": None,
            "label": "Temperature",
            "section": "Generation",
            "visible_when": None,
            "options_when": None,
            "hidden": False,
            "ui": None,
            "groups": None
        },
        "context_tokens": {
            "type": "int",
            "types": [
                "int"
            ],
            "required": False,
            "description": "The context window, in tokens. 0 means the note type's own default (TMJ 6144, Ortho 2048).",
            "server_selectable": None,
            "choices": None,
            "initial": 0,
            "extensions": None,
            "label": "Context window (tokens, 0 = auto)",
            "section": "Generation",
            "visible_when": None,
            "options_when": None,
            "hidden": False,
            "ui": None,
            "groups": None
        },
        "seed": {
            "type": "int",
            "types": [
                "int"
            ],
            "required": False,
            "description": "The sampler's seed, so a run above temperature 0 is still reproducible.",
            "server_selectable": None,
            "choices": None,
            "initial": 0,
            "extensions": None,
            "label": "Seed",
            "section": "Generation",
            "visible_when": None,
            "options_when": None,
            "hidden": True,
            "ui": None,
            "groups": None
        },
        "device": {
            "type": "choice",
            "types": [
                "choice"
            ],
            "required": False,
            "description": "\"cpu\" or \"cuda\". \"cuda\" offloads every layer of the model to the card, which is measured at 8-13x faster per note; it needs the CUDA build of llama.cpp, which this tool pins. Where that build is not the one installed, the request is reported as ignored -- `gpu_offload` in the report is what actually happened -- rather than passing silently as it used to.",
            "server_selectable": None,
            "choices": {
                "cpu": True,
                "cuda": False
            },
            "initial": None,
            "extensions": None,
            "label": "Device",
            "section": "Generation",
            "visible_when": None,
            "options_when": None,
            "hidden": True,
            "ui": None,
            "groups": None
        }
    },
    "output_kind": "files"
}


def _argument(name: str) -> dict:
    return CNE_SCHEMA["arguments"][name]


def _module_source() -> str:
    with open(os.path.join(_REPO_ROOT, "CNERemote", "CNERemote.py"), encoding="utf-8") as handle:
        return handle.read()


def _class_attribute(name: str):
    tree = ast.parse(_module_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CNERemoteWidget":
            for statement in node.body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            return ast.literal_eval(statement.value)
    raise AssertionError(f"CNERemoteWidget declares no {name}")


class TestToolName(unittest.TestCase):
    def test_it_names_the_tool_the_server_serves(self):
        self.assertEqual(_class_attribute("TOOL_NAME"), CNE_SCHEMA["name"])

    def test_it_reads_the_report_the_tool_writes(self):
        self.assertEqual(_class_attribute("RUN_REPORT"), "CNE_report.json")


class TestOneRequest(unittest.TestCase):
    def test_notes_and_a_note_type_alone_satisfy_the_schema(self):
        """The old panel asked for a folder and TMJ/Ortho, nothing else."""
        ToolServerClient._validate_against_schema(
            CNE_SCHEMA, {"notes_type": "TMJ"}, {"notes": "/tmp/notes.zip"})

    def test_missing_notes_are_caught_before_the_round_trip(self):
        from ServerToolsCoreLib.errors import ServerToolError

        with self.assertRaises(ServerToolError):
            ToolServerClient._validate_against_schema(CNE_SCHEMA, {"notes_type": "TMJ"}, {})

    def test_the_two_note_types_are_the_ones_upstream_offered(self):
        self.assertEqual(list(_argument("notes_type")["choices"]), ["TMJ", "Ortho"])

    def test_the_model_is_a_hosted_name_and_may_be_left_automatic(self):
        model = _argument("model")
        self.assertEqual(model["server_selectable"], "model")
        self.assertFalse(model["required"])

    def test_the_result_is_an_archive_to_unpack(self):
        self.assertEqual(formgen.result_kind_for(CNE_SCHEMA["output_kind"]), "save_as")

    def test_the_module_lets_the_schema_decide_the_result_kind(self):
        self.assertNotIn("RESULT_KIND = ", _module_source())


class TestNotesPicker(unittest.TestCase):
    def test_the_notes_take_a_file_or_a_folder(self):
        modes = formgen.file_input_modes(CNE_SCHEMA["arguments"])
        self.assertEqual(modes["notes"], "file_or_folder")

    def test_the_dialog_offers_the_formats_the_old_picker_did(self):
        self.assertEqual(sorted(file_extensions_for(_argument("notes"))), [".docx", ".pdf", ".txt"])

    def test_no_scene_node_is_offered_as_a_note(self):
        self.assertEqual(formgen.scene_kinds_for(_argument("notes"), "notes"), ())

    def test_the_module_declares_no_file_inputs(self):
        self.assertNotIn("FILE_INPUTS = ", _module_source())


class TestNothingRunsHere(unittest.TestCase):
    """The reason for the port: no pip, no model download, no local CLI."""

    def test_the_module_installs_and_downloads_nothing(self):
        source = _module_source()
        for forbidden in ("pip_install", "urlretrieve", "huggingface.co", "slicer.cli.run"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_module_carries_no_stylesheet(self):
        self.assertNotIn("setStyleSheet", _module_source())

    def test_the_local_cli_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(_REPO_ROOT, "CNE_CLIRemote")))
        with open(os.path.join(_REPO_ROOT, "CMakeLists.txt"), encoding="utf-8") as handle:
            self.assertNotIn("CNE_CLIRemote", handle.read())

    def test_nothing_is_loaded_into_the_scene(self):
        """An extraction is text: there is no `_LOADABLE`, so no load box."""
        with self.assertRaises(AssertionError):
            _class_attribute("_LOADABLE")


if __name__ == "__main__":
    unittest.main()
