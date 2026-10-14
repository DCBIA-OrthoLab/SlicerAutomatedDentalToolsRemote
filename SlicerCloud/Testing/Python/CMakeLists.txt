#-----------------------------------------------------------------------------
# test_deploy.py imports SlicerCloudLib.deploy only, which pulls in neither
# slicer nor qt (it is subprocess plus the standard library, and the stdlib-only
# ServerToolsCoreLib.tls it shares with the client). test_panel.py
# does need them, but only as the stand-ins in ServerToolsCore's qt_stubs.py.
# Both are therefore plain Python3 ctests rather than slicer_add_python_unittest
# — no need to pay for a full Slicer interpreter launch. Same rationale as
# ServerToolsCore/Testing/Python, see ARCHITECTURE.md, "Debatable decisions".
find_package(Python3 COMPONENTS Interpreter REQUIRED)

add_test(
  NAME SlicerCloud_test_deploy
  COMMAND ${Python3_EXECUTABLE} -m unittest test_deploy
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

add_test(
  NAME SlicerCloud_test_panel
  COMMAND ${Python3_EXECUTABLE} -m unittest test_panel
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )
