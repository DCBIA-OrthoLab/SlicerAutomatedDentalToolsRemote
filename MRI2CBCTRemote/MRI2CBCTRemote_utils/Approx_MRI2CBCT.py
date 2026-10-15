from .Method import Method
from .utils_CBCT import GetDictPatients, GetPatients
import os, sys

import glob
import json
import vtk
import numpy as np

from glob import iglob
import slicer
import time
import qt
import platform
import re

import sys
import logging

# ===== Logging Configuration =====
logger = logging.getLogger("MRI2CBCT_Approx")
logger.setLevel(logging.INFO)
logger.propagate = False
if logger.handlers:
    logger.handlers.clear()
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(name)s - %(levelname)s - (%(filename)s:%(lineno)d) - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

class Approximation_MRI2CBCT(Method):
    def __init__(self, widget):
        super().__init__(widget)
        documentsLocation = qt.QStandardPaths.DocumentsLocation
        documents = qt.QStandardPaths.writableLocation(documentsLocation)
        self._lastOutputFolder = None
        self._keepResultInScene = False

    def getGPUUsage(self):
        if platform.system() == "Darwin":
            return 1
        else:
            return 5

    def NumberScan(self, scan_folder_t1: str, scan_folder_t2: str):
        return len(GetDictPatients(scan_folder_t1, scan_folder_t2))

    def getModelUrl(self):
        return {
            "MeanCBCT": "xxx",
            "ROI": "xxx"
        }

    def TestScan(self, scan_folder: str):
        if os.path.isfile(scan_folder):
            return True, ""
        extensions = ['.nii', '.nii.gz']
        found_files = self.search(scan_folder, extensions)
        if any(found_files[ext] for ext in extensions):
            return True, ""
        else:
            return False, "No files to run has been found in the input folder"

    def TestProcess(self, **kwargs) -> str:
        out = ""
        ok = True

        if kwargs["cbct_folder"] == "":
            out += "Please select an input folder for CBCT scans\n"
            ok = False

        if kwargs["mri_folder"] == "":
            out += "Please select an input folder for MRI scans\n"
            ok = False

        if kwargs["output_folder"] == "":
            out += "Please select an output folder\n"
            ok = False

        if out == "":
            out = None

        return ok,out

    def Process(self, **kwargs):
        list_process=[]

        tmp_folder = slicer.util.tempDirectory()

        # MRI2CBCT_APPROX writes its per-patient point files under
        # "first_approximation"; finalizeApproximation() needs this same path
        # once the CLI is done, so remember it now.
        self._lastOutputFolder = os.path.join(kwargs["output_folder"], "first_approximation")
        # When the input came from the scene (single case) rather than a
        # batch folder, leave the resulting approximated MRI loaded so the
        # user sees it immediately instead of having to load it from disk.
        self._keepResultInScene = bool(kwargs.get("use_scene_volumes", False))

        MRI2CBCT_APPROX = slicer.modules.mri2cbct_approx
        parameter_mri2cbct_approx = {
            "cbct_folder": kwargs["cbct_folder"],
            "mri_folder": kwargs["mri_folder"],
            "output_folder": kwargs["output_folder"],
            "model_folder": kwargs["model_folder"],
            "tmp_folder": tmp_folder,
        }

        list_process.append(
            {
                "Process": MRI2CBCT_APPROX,
                "Parameter": parameter_mri2cbct_approx,
                "Module": "MRI2CBCT approximation",
            }
        )

        return list_process

    def finalizeApproximation(self):
        """
        Reads the per-patient condyle/MRI-center point JSON files written by
        MRI2CBCT_APPROX, uses Slicer's own FiducialRegistration module to
        compute the translation that matches them, combines it with the
        header-based rotation correction kept from the JSON, and writes the
        final registered MRI volume + transform - the same outputs the old
        torchreg-based approximation used to produce.
        """
        output_folder = self._lastOutputFolder
        if not output_folder:
            logger.warning("No output folder recorded for the approximation finalize step")
            return

        points_folder = os.path.join(output_folder, "points")
        point_files = sorted(glob.glob(os.path.join(points_folder, "*_approx_points.json")))
        if not point_files:
            logger.warning(f"No approximation point files found in {points_folder}")
            return

        for point_file in point_files:
            with open(point_file, "r") as f:
                data = json.load(f)

            patient_id = data["patient_id"]
            mri_path = data["mri_path"]
            rotation = np.array(data["rotation_ras"])
            cbct_point = data["cbct_point_ras"]
            mri_point_rotated = data["mri_point_rotated_ras"]

            mriNode = None
            transformNode = None
            try:
                translation = self._matchPointsWithFiducialRegistration(cbct_point, mri_point_rotated)

                matrix = vtk.vtkMatrix4x4()
                matrix.Identity()
                for i in range(3):
                    for j in range(3):
                        matrix.SetElement(i, j, float(rotation[i, j]))
                    matrix.SetElement(i, 3, float(translation[i]))

                transformNode = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLLinearTransformNode", f"{patient_id}_ApproxTransform")
                transformNode.SetMatrixTransformToParent(matrix)

                mriNode = slicer.util.loadVolume(mri_path)
                mriNode.SetAndObserveTransformNodeID(transformNode.GetID())
                slicer.vtkSlicerTransformLogic().hardenTransform(mriNode)

                mri_basename = os.path.basename(mri_path)
                for ext in (".nii.gz", ".nii"):
                    if mri_basename.endswith(ext):
                        mri_basename = mri_basename[: -len(ext)]
                        break
                out_volume_path = os.path.join(output_folder, f"{mri_basename}_approximate.nii.gz")
                slicer.util.saveNode(mriNode, out_volume_path)

                out_transform_path = os.path.join(output_folder, f"{patient_id}_MRI_approximate.tfm")
                slicer.util.saveNode(transformNode, out_transform_path)

                if self._keepResultInScene:
                    mriNode.SetName(f"{mri_basename}_approximate")
                    mriNode = None  # don't remove it below, leave it loaded for the user

                logger.info(f"{patient_id}: approximation finalized -> {out_volume_path}")
            except Exception as e:
                logger.error(f"Failed to finalize approximation for {patient_id}: {e}")
            finally:
                if mriNode is not None:
                    slicer.mrmlScene.RemoveNode(mriNode)
                if transformNode is not None:
                    slicer.mrmlScene.RemoveNode(transformNode)

    def _matchPointsWithFiducialRegistration(self, fixedPoint, movingPoint):
        """Use Slicer's own FiducialRegistration CLI module to compute the
        translation that matches a single fixed/moving point pair."""
        fixedFid = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "ApproxFixedPoint")
        movingFid = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "ApproxMovingPoint")
        regTransform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "ApproxFidRegTransform")
        try:
            fixedFid.AddControlPoint(vtk.vtkVector3d(*fixedPoint))
            movingFid.AddControlPoint(vtk.vtkVector3d(*movingPoint))

            params = {
                "fixedLandmarks": fixedFid,
                "movingLandmarks": movingFid,
                "transformType": "Translation",
                "saveTransform": regTransform,
            }
            slicer.cli.runSync(slicer.modules.fiducialregistration, None, params)

            matrix = vtk.vtkMatrix4x4()
            regTransform.GetMatrixTransformToParent(matrix)
            return np.array([matrix.GetElement(i, 3) for i in range(3)])
        finally:
            slicer.mrmlScene.RemoveNode(fixedFid)
            slicer.mrmlScene.RemoveNode(movingFid)
            slicer.mrmlScene.RemoveNode(regTransform)
