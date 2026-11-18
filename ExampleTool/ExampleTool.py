from slicer.i18n import tr as _
from slicer.ScriptedLoadableModule import ScriptedLoadableModule

from ServerToolsCoreLib.base_widget import ServerToolWidgetBase


class ExampleTool(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Example Tool")
        self.parent.categories = ["Automated Dental Tools Remote.Advanced"]
        self.parent.dependencies = ["ServerToolsCore"]
        self.parent.contributors = ["Jules Grivot Pelisson, University of North Carolina, Chapel Hill"]
        self.parent.helpText = _("""
        Reference client for the tool server's <code>example_tool</code>: it exercises every
        argument shape the schema-driven UI supports (free text, int, float, a single-choice
        dropdown, a multi-choice checkbox group, and an input accepting either a .csv file or a
        whole folder), and returns several result files as one archive.
        Useful to check a server connection end to end without running a real analysis.
        See more information in <a href="https://github.com/DCBIA-OrthoLab/SlicerAutomatedDentalTools">documentation</a>.
        """)
        self.parent.acknowledgementText = _("This module was supported by NIDCR R01 024450.")


class ExampleToolWidget(ServerToolWidgetBase):
    """Thin GUI: everything else (HTTP, async, form generation, styling, lifecycle)
    lives in ServerToolsCoreLib. See ARCHITECTURE.md.

    One line, because this tool's schema already says everything: `input` is a
    file argument accepting ["csv_file", "folder"], so the panel takes either
    (and zips a folder before uploading, having worked out which it was given),
    and `output_kind: "files"` means several result files come back as one
    archive to unpack into a chosen output folder. Declaring any of that here
    would just be repeating the server.
    """

    TOOL_NAME = "example_tool"
