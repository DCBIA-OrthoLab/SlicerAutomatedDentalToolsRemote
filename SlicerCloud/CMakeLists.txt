#-----------------------------------------------------------------------------
set(MODULE_NAME SlicerCloud)

#-----------------------------------------------------------------------------
# SlicerCloudLib is a plain Python package, not a Slicer module in its own
# right. Listing it as extra SCRIPTS of this module lands it next to
# SlicerCloud.py and makes `from SlicerCloudLib import ...` work — the same
# arrangement ServerToolsCore uses for ServerToolsCoreLib, see ARCHITECTURE.md.
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  SlicerCloudLib/__init__.py
  SlicerCloudLib/backends.py
  SlicerCloudLib/deploy.py
  SlicerCloudLib/local_cert.py
  SlicerCloudLib/state.py
  )

# No icon: it uses Slicer's default one.
set(MODULE_PYTHON_RESOURCES
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
  add_subdirectory(Testing)
endif()
