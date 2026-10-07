cmake_minimum_required(VERSION 3.22.1)

Project(Winlator)

set(VORTEK_RELEASE_FLAGS "-O3 -DNDEBUG -fno-semantic-interposition -fno-plt -ffunction-sections -fdata-sections")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -O2 -Wno-unused-function -Wimplicit-function-declaration")
set(CMAKE_C_FLAGS_RELEASE "${VORTEK_RELEASE_FLAGS}")
set(CMAKE_C_FLAGS_RELWITHDEBINFO "${VORTEK_RELEASE_FLAGS} -g")
add_link_options("-Wl,-O2,--gc-sections,--as-needed")

include_directories(include)

add_library(winlator SHARED
            src/drawable.c
            src/gpu_image.c
            src/gpu_helper.c
            src/sysvshared_memory.c
            src/xconnector_epoll.c
            src/xinput_stream.c
            src/xoutput_stream.c
            src/arrays.c
            src/ring_buffer.c
            src/wine_registry_editor.c)

target_link_libraries(winlator
                      log
                      android
                      jnigraphics
                      EGL
                      GLESv2
                      GLESv3)