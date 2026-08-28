cmake_minimum_required(VERSION 3.19)
project(AgentFlow LANGUAGES CXX)

find_package(Qt6 6.5 REQUIRED COMPONENTS Core Network WebSockets Widgets)

qt_standard_project_setup()

qt_add_executable(AgentFlow
    WIN32 MACOSX_BUNDLE
    backendclient.cpp
    backendclient.h
    backendmanager.cpp
    backendmanager.h
    dispatchmaterialdialog.cpp
    dispatchmaterialdialog.h
    dispatchmaterialdialog.ui
    datahelpdialog.cpp
    datahelpdialog.h
    datahelpdialog.ui
    datatransformationdialog.cpp
    datatransformationdialog.h
    datatransformationdialog.ui
    knowledgeanswerdialog.cpp
    knowledgeanswerdialog.h
    knowledgeanswerdialog.ui
    knowledgedeeptaskdialog.cpp
    knowledgedeeptaskdialog.h
    knowledgedeeptaskdialog.ui
    main.cpp
    mainwindow.cpp
    mainwindow.h
    mainwindow.ui
    modelroutedialog.cpp
    modelroutedialog.h
    modelroutedialog.ui
    presentationstudiodialog.cpp
    presentationstudiodialog.h
    presentationstudiodialog.ui
    taskactivityindicator.cpp
    taskactivityindicator.h
)

# 强制把 icons 文件夹里的 SVG 编译进 exe。
# BASE icons + PREFIX /icons 后，运行时路径就是：
# :/icons/overview.svg
qt_add_resources(AgentFlow "agentflow_icons"
    PREFIX "/icons"
    BASE "icons"
    FILES
        icons/overview.svg
        icons/dispatch.svg
        icons/apps.svg
        icons/workflow.svg
        icons/document.svg
        icons/code.svg
        icons/data.svg
        icons/vision.svg
        icons/video.svg
        icons/knowledge.svg
        icons/plugins.svg
        icons/model.svg
        icons/history.svg
        icons/settings.svg
        icons/report.svg
)

target_link_libraries(AgentFlow
    PRIVATE
        Qt::Core
        Qt::Network
        Qt::WebSockets
        Qt::Widgets
)

if(MSVC)
    target_compile_options(AgentFlow PRIVATE /utf-8)
endif()

# BackendManager 是所有工作台共用的生命周期边界。测试仅启动回环 HTTP 夹具，不读取
# 客户材料、不拉起真实 FastAPI，也不访问模型/网络服务。
include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 6.5 REQUIRED COMPONENTS Test)
    qt_add_executable(BackendManagerTests
        backendmanager_test.cpp
        backendmanager.cpp
        backendmanager.h
    )
    target_link_libraries(BackendManagerTests PRIVATE Qt::Core Qt::Network Qt::Test)
    if(MSVC)
        target_compile_options(BackendManagerTests PRIVATE /utf-8)
    endif()
    add_test(NAME BackendManagerTests COMMAND BackendManagerTests)
    # CTest 从开发终端外启动时不会自动继承 Qt Creator 的 Kit PATH。显式把实际 Qt Core
    # DLL 所在目录前置，避免 Windows 把“缺 DLL”误报成测试失败；发布程序不使用此设置。
    if(WIN32)
        get_target_property(qtCoreDebugLocation Qt6::Core IMPORTED_LOCATION_DEBUG)
        if(NOT qtCoreDebugLocation)
            get_target_property(qtCoreDebugLocation Qt6::Core IMPORTED_LOCATION)
        endif()
        get_filename_component(qtRuntimeDirectory "${qtCoreDebugLocation}" DIRECTORY)
        set_tests_properties(BackendManagerTests PROPERTIES
            ENVIRONMENT "PATH=${qtRuntimeDirectory};$ENV{PATH}")
    endif()
endif()

install(TARGETS AgentFlow
    BUNDLE  DESTINATION .
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)

qt_generate_deploy_app_script(
    TARGET AgentFlow
    OUTPUT_SCRIPT deploy_script
    NO_UNSUPPORTED_PLATFORM_ERROR
)
install(SCRIPT ${deploy_script})
