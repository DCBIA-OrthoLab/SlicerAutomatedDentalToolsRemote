#-----------------------------------------------------------------------------
set(MODULE_NAME AutoMatrixRemote)

#-----------------------------------------------------------------------------
# AutoMatrix_Method/ and Resources/UI/AutoMatrix.ui belonged to the former local
# module: they built the AutoMatrix CLI's parameters, counted the scans for the
# progress bar, and laid out the hand-written panel with its Mirror download
# button. The module is now a thin client of the server's "AutoMatrix" tool and
# builds its panel from GET /tools, so both have been deleted.
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
)

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
)

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
)

#-----------------------------------------------------------------------------
if (BUILD_TESTING)

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  # Additional build-time testing
  add_subdirectory(Testing)
endif ()
