
#slicer_add_python_unittest(SCRIPT ${MODULE_NAME}ModuleTest.py)

#-----------------------------------------------------------------------------
# test_ali_client.py drives ServerToolsCoreLib with ALI's own GET /tools schema
# as a fixture, against the qt/ctk/slicer stand-ins in
# ServerToolsCore/Testing/Python/qt_stubs.py. Pure Python once the widgets are
# stubbed, so it is registered as a plain Python3 ctest rather than through
# slicer_add_python_unittest - no Slicer interpreter launch needed. Same
# rationale as ServerToolsCore/Testing/Python/CMakeLists.txt.
find_package(Python3 COMPONENTS Interpreter REQUIRED)

add_test(
  NAME ALI_test_ali_client
  COMMAND ${Python3_EXECUTABLE} -m unittest test_ali_client
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )
