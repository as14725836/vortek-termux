cmake_minimum_required(VERSION 3.22.1)

project(Vortek C)
message("Building ${PROJECT_NAME}")

set(CMAKE_VERBOSE_MAKEFILE on)
set(WARNS "-Wall -Wno-discarded-qualifiers")
# Release 优化：
#  -O3 + NDEBUG：vulkan_calls.c / vortek_serializer.h 里 assert 极多，Release 下关掉可省掉热路径分支
#  -fno-semantic-interposition / -fno-plt：允许同一 .so 内的调用被直接绑定/内联（ICD 不需要被外部插桩）
#  -ffunction-sections/--gc-sections：裁掉未被引用的序列化代码，减小 .so 与重定位开销
option(VORTEK_ENABLE_LTO "Enable LTO for the ICD (实测收益有限, 构建时间约 x2, 默认关)" OFF)
set(VORTEK_RELEASE_FLAGS "-O3 -DNDEBUG -fno-semantic-interposition -fno-plt -ffunction-sections -fdata-sections")
set(VORTEK_LINK_FLAGS "-Wl,-O2,--gc-sections,--as-needed")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -O2 ${WARNS} -DVK_USE_PLATFORM_XLIB_KHR -DVK_USE_PLATFORM_XCB_KHR")
set(CMAKE_C_FLAGS_RELEASE "${VORTEK_RELEASE_FLAGS}")
set(CMAKE_C_FLAGS_RELWITHDEBINFO "${VORTEK_RELEASE_FLAGS} -g")
if(VORTEK_ENABLE_LTO)
    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
endif()
# vulkan_xcb.h / vulkan_xlib.h 不在 NDK 与上游 core headers 里，仓库自带一份
set(VORTEK_VULKAN_INCLUDE_DIR "" CACHE PATH "额外的 Vulkan headers 目录（留空=用系统/NDK 自带）")
if(VORTEK_VULKAN_INCLUDE_DIR)
    include_directories(${VORTEK_VULKAN_INCLUDE_DIR})
endif()
include_directories(include third_party/vulkan-platform)

add_library(vulkan_vortek SHARED 
            src/main.c
            src/vulkan_calls.c
            src/vk_object.c
            src/vk_object_pool.c
            src/arrays.c
            src/descriptor_update_template.c
            src/ring_buffer.c)
target_compile_definitions(vulkan_vortek PRIVATE VT_CLIENT)
target_link_options(vulkan_vortek PRIVATE ${VORTEK_LINK_FLAGS})
# println() 在 __ANDROID__ 下展开成 __android_log_print，需要 liblog
if(ANDROID)
    target_link_libraries(vulkan_vortek PRIVATE log)
else()
    find_library(VORTEK_LOG_LIB log)
    if(VORTEK_LOG_LIB)
        target_link_libraries(vulkan_vortek PRIVATE ${VORTEK_LOG_LIB})
    endif()
endif()
if(ANDROID)
    # 客户端用 __android_log_print，Android 上必须显式链 liblog
    target_link_libraries(vulkan_vortek PRIVATE log)
endif()

install(TARGETS vulkan_vortek LIBRARY DESTINATION lib)
install(FILES vortek_icd.aarch64.json DESTINATION share/vulkan/icd.d)

# 可重定位版本：library_path 用相对 manifest 自身的路径（icd.d -> vulkan -> share -> 前缀根 -> lib/）
# 已用真实 Vulkan loader 验证：loader 会把相对路径按 manifest 所在目录解析
# （tools/icd_load_probe.py relative）。这样解压到任意目录都能直接用。
set(VORTEK_ICD_ABS_PATH "/data/data/com.termux/files/usr/glibc/lib/libvulkan_vortek.so")
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/vortek_icd.aarch64.json" _vortek_icd_json)
string(REPLACE "${VORTEK_ICD_ABS_PATH}" "../../../lib/libvulkan_vortek.so"
       _vortek_icd_json_rel "${_vortek_icd_json}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/vortek_icd.relocatable.aarch64.json" "${_vortek_icd_json_rel}")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/vortek_icd.relocatable.aarch64.json"
        DESTINATION share/vulkan/icd.d)
