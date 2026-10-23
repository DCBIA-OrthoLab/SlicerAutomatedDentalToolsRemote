cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(AutomatedDentalToolsRemote)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/DCBIA-OrthoLab/SlicerAutomatedDentalToolsRemote/blob/main/README.md")
set(EXTENSION_CATEGORY "Orthodontics")
set(EXTENSION_CONTRIBUTORS "Maxime Gillot (University of Michigan), Baptiste Baquero (UoM), Juan Carlos Prieto (University of North Carolina), Nathan Hutin (UoM), Luc Anchling (UoM), Jeanne Claret (UoM), Gaelle Leroux(UoM) Lucie Dole (University of North Carolina)")
set(EXTENSION_DESCRIPTION "Cloud client for Automated Dental Tools: each module is a thin panel over a remote inference server, so a laptop that cannot run a segmentation can still ask for one. A ported module installs nothing into Slicer's interpreter and downloads no model. CNE is not yet ported and still installs its own dependencies and downloads its own models. MRI2CBCT is temporarily not shipped until it is ported to the server.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/DCBIA-OrthoLab/SlicerAutomatedDentalToolsRemote/main/SlicerAutomaticTools.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/DCBIA-OrthoLab/SlicerAutomatedDentalToolsRemote/main/ADT-exemple.png")
# No extension dependencies: every computation runs on the remote tool server,
# so this client needs neither SlicerConda, nor SlicerDentalModelSeg, nor NNUNet.
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(AMASSSRemote)
add_subdirectory(ALIRemote)
add_subdirectory(ASORemote)
add_subdirectory(AREGRemote)
add_subdirectory(AutoMatrixRemote)
add_subdirectory(AutoCrop3DRemote)
add_subdirectory(FlexRegRemote)
# MRI2CBCTRemote and MRI2CBCT_CLIRemote are kept in the tree but not built:
# they stay out until MRI2CBCT is ported to the tool server.
add_subdirectory(BATCHDENTALSEGRemote)
add_subdirectory(DOCShapeAXIRemote)
add_subdirectory(CLICRemote)
add_subdirectory(CNERemote)
add_subdirectory(CNE_CLIRemote)
add_subdirectory(Medical_Data_Anonymizer_ModuleRemote)
add_subdirectory(VFACERemote)
add_subdirectory(GreedyRegRemote)
add_subdirectory(AgentRemote)
add_subdirectory(Agent_CLIRemote)
add_subdirectory(ServerToolsCore)
add_subdirectory(ServerToolsSettings)
add_subdirectory(SlicerCloud)
add_subdirectory(SurgMovPredRemote)
add_subdirectory(Crown_Seg)
add_subdirectory(VISU)
add_subdirectory(ExampleTool)
# The CLI modules of the former local extension used to sit here, unbuilt. They
# have been deleted: nothing shipped called them, nothing imported them, and an
# unbuilt directory is weight in the clone rather than a module anyone could
# reach. The sequencing they existed for is the server's now.
#
# The names survive as STRINGS -- the server's tools are called ALI_CBCT, ASO
# and AREG_CBCT -- in AREG/AREG_Method/ and VFACE/VFACE_utils/, which is a tool
# being named over the wire and not a directory being looked for.
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
