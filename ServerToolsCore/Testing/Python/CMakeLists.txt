#-----------------------------------------------------------------------------
# test_client.py only imports ServerToolsCoreLib.client (requests + stdlib, no
# slicer/qt), so it is registered as a plain Python3 ctest rather than through
# slicer_add_python_unittest - no need to pay for a full Slicer interpreter
# launch to test an HTTP client. See ARCHITECTURE.md, "Debatable decisions".
find_package(Python3 COMPONENTS Interpreter REQUIRED)

add_test(
  NAME ServerToolsCore_test_client
  COMMAND ${Python3_EXECUTABLE} -m unittest test_client
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_transfer.py drives the real chunked upload / ranged download against a
# stdlib ThreadingHTTPServer on a loopback socket, wrapped in TLS since the
# client speaks https:// only (tls_fixture.py), no mocks, because what that
# module does is concurrency and byte offsets. Same reasoning as above: requests
# + stdlib only, so no Slicer interpreter is needed. It binds an ephemeral
# 127.0.0.1 port and never reaches the network.
add_test(
  NAME ServerToolsCore_test_transfer
  COMMAND ${Python3_EXECUTABLE} -m unittest test_transfer
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_tls.py: https:// only, and the loopback certificate pin, partly against a
# real TLS server on 127.0.0.1 with a throwaway certificate (tls_fixture.py,
# which shells out to `openssl`). Same reasoning: requests + stdlib only.
add_test(
  NAME ServerToolsCore_test_tls
  COMMAND ${Python3_EXECUTABLE} -m unittest test_tls
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_formgen.py does need qt/ctk/slicer, but only as the small stand-ins in
# qt_stubs.py: it checks which widgets a schema produces and what they read
# back as, which is pure Python once the widget classes are stubbed. Same
# rationale as above - no Slicer interpreter launch needed.
add_test(
  NAME ServerToolsCore_test_formgen
  COMMAND ${Python3_EXECUTABLE} -m unittest test_formgen
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_hosted_test_files.py: what happens when a user picks one of a tool's
# server-hosted test files -- the download, the cache, the unpacking and what
# reaches the scene. Same stubs plus a handful of slicer.util functions, and a
# BackgroundJob stand-in running on a real thread, since "this must not block
# the Slicer window" is the property under test.
add_test(
  NAME ServerToolsCore_test_hosted_test_files
  COMMAND ${Python3_EXECUTABLE} -m unittest test_hosted_test_files
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_joystick.py: the 2D pad's value/geometry logic under the same stubs.
# Painting aside, the pad is plain arithmetic.
add_test(
  NAME ServerToolsCore_test_joystick
  COMMAND ${Python3_EXECUTABLE} -m unittest test_joystick
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_run_progress.py: the run's progress stream and its cancellation. Like
# test_transfer.py it runs against a real ThreadingHTTPServer on a loopback
# socket rather than a mock, because what it has to show is a SECOND connection
# held open while the first one is blocked inside the POST -- a barrier neither
# request can pass alone. requests + stdlib only, no Slicer interpreter.
add_test(
  NAME ServerToolsCore_test_run_progress
  COMMAND ${Python3_EXECUTABLE} -m unittest test_run_progress
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_runs.py and test_worker.py were written before this file listed them and
# so had never run in CI: the run queue, the two timers, the cancel token and
# the panel's half of the progress stream. Both use the same stand-ins as
# test_formgen.py and need no Slicer interpreter either.
add_test(
  NAME ServerToolsCore_test_runs
  COMMAND ${Python3_EXECUTABLE} -m unittest test_runs
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

add_test(
  NAME ServerToolsCore_test_worker
  COMMAND ${Python3_EXECUTABLE} -m unittest test_worker
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_scoped_hosted_files.py: an argument drawing from one subfolder of the
# tool's hosted files instead of all of them. Stand-ins only, and the subject
# is the CLIENT half -- which list a picker reads, and what it does when the
# server sent no scoped section at all.
add_test(
  NAME ServerToolsCore_test_scoped_hosted_files
  COMMAND ${Python3_EXECUTABLE} -m unittest test_scoped_hosted_files
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_schema_refresh.py: the panel noticing that the server's schema changed
# under it. A form built at setup() outlives the tool it was built from, and
# the rule -- re-read on every enter(), rebuild only on a difference -- is not
# visible anywhere in the panel, so only a test can hold it. Stand-ins only:
# no Slicer interpreter, no server.
add_test(
  NAME ServerToolsCore_test_schema_refresh
  COMMAND ${Python3_EXECUTABLE} -m unittest test_schema_refresh
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_quality_control.py: a run that stops for a reader to correct it, and is
# then carried on. Both halves -- the client recognising the record and posting
# the corrections back, and the panel's loop around a stub reviewer -- run
# under the same stand-ins as test_runs.py, so no Slicer interpreter and no
# server are needed.
add_test(
  NAME ServerToolsCore_test_quality_control
  COMMAND ${Python3_EXECUTABLE} -m unittest test_quality_control
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )

# test_digest.py: which files of a reviewed checkpoint travel back. Pure
# stdlib against real files on a real disk, and it imports no qt/slicer stub
# at all -- digest.py must stay free of both, and this failing to import is
# what says it did not.
add_test(
  NAME ServerToolsCore_test_digest
  COMMAND ${Python3_EXECUTABLE} -m unittest test_digest
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  )
