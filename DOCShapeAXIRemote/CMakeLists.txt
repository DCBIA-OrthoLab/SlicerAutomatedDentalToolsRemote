#-----------------------------------------------------------------------------
set(MODULE_NAME DOCShapeAXIRemote)

#-----------------------------------------------------------------------------
# DOCShapeAXI_utils/ belonged to the former local module: install_pytorch.py
# built a conda `shapeaxi` environment and, on Windows, a WSL one. The module is
# now a thin client of the server's "DOCShapeAXI" tool and builds its panel from
# GET /tools, so it and the hand-written .ui have both been deleted.
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
if(BUILD_TESTING)

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  # Additional build-time testing
  add_subdirectory(Testing)
endif()
