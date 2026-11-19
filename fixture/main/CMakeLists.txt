# The fixture's one input is fixture.env at the project root: KEY=value
# lines, read here into a generated header. No file, or a required key
# left empty, stops the build with the reason.
set(FIXTURE_ENV "${CMAKE_CURRENT_LIST_DIR}/../fixture.env")
if(NOT EXISTS "${FIXTURE_ENV}")
    message(FATAL_ERROR
        "fixture.env not found at ${FIXTURE_ENV}: copy fixture.env.example to "
        "fixture.env and fill in WIFI_SSID, WIFI_PSK and API_KEY (./fixture.sh env does it)")
endif()

set(FIXTURE_WIFI_SSID "")
set(FIXTURE_WIFI_PSK "")
set(FIXTURE_API_KEY "")
set(FIXTURE_HOSTNAME "")

# Read the file whole and split on newlines ourselves: file(STRINGS)
# would also split on semicolons, which a passphrase may carry.
file(READ "${FIXTURE_ENV}" _env_text)
string(REPLACE ";" "\\;" _env_text "${_env_text}")
string(REGEX REPLACE "\r?\n" ";" _env_lines "${_env_text}")
foreach(_line IN LISTS _env_lines)
    if(_line MATCHES "^[ \t]*#" OR _line MATCHES "^[ \t]*$")
        continue()
    endif()
    if(_line MATCHES "^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
        set(_key "${CMAKE_MATCH_1}")
        set(_val "${CMAKE_MATCH_2}")
        string(STRIP "${_val}" _val)
        string(REGEX REPLACE "^\"(.*)\"$" "\\1" _val "${_val}")
        string(REGEX REPLACE "^'(.*)'$" "\\1" _val "${_val}")
        set(FIXTURE_${_key} "${_val}")
    else()
        message(FATAL_ERROR "fixture.env: cannot read line: ${_line}")
    endif()
endforeach()

foreach(_key WIFI_SSID API_KEY)
    if("${FIXTURE_${_key}}" STREQUAL "")
        message(FATAL_ERROR "fixture.env: ${_key} is empty")
    endif()
endforeach()
if("${FIXTURE_HOSTNAME}" STREQUAL "")
    set(FIXTURE_HOSTNAME "forgefixture")
endif()
string(LENGTH "${FIXTURE_HOSTNAME}" _hostlen)
if(NOT FIXTURE_HOSTNAME MATCHES "^[a-z0-9][a-z0-9-]*$" OR FIXTURE_HOSTNAME MATCHES "-$" OR _hostlen GREATER 32)
    message(FATAL_ERROR "fixture.env: HOSTNAME must be a DNS label (lowercase letters, digits, hyphens; 32 at most)")
endif()

# Into C string literals.
foreach(_key WIFI_SSID WIFI_PSK API_KEY HOSTNAME)
    string(REPLACE "\\" "\\\\" FIXTURE_${_key} "${FIXTURE_${_key}}")
    string(REPLACE "\"" "\\\"" FIXTURE_${_key} "${FIXTURE_${_key}}")
endforeach()

set(_gen_dir "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${_gen_dir}")
configure_file("${CMAKE_CURRENT_LIST_DIR}/fixture_config.h.in" "${_gen_dir}/fixture_config.h" @ONLY)

idf_component_register(
    SRCS "main.c" "wifi.c" "relays.c" "api.c" "policy.c" "auth.c" "sha256.c"
    INCLUDE_DIRS "." "${_gen_dir}"
    REQUIRES nvs_flash esp_wifi esp_netif esp_event esp_http_server json esp_timer driver
)
