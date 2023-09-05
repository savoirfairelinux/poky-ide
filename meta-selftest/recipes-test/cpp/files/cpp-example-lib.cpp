/*
* Copyright OpenEmbedded Contributors
*
* SPDX-License-Identifier: MIT
*/

#include <string>
#include <json-c/json.h>
#include "cpp-example-lib.hpp"

const std::string& CppExample::get_string() {
    return test_string;
}

const char* CppExample::get_json_c_version() {
    return json_c_version();
}
