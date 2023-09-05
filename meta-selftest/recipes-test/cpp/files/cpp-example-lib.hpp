/*
* Copyright OpenEmbedded Contributors
*
* SPDX-License-Identifier: MIT
*/

#pragma once

#include <string>

struct CppExample {
    inline static const std::string test_string = "cpp-example-lib Magic: 123456789";

    const std::string& get_string();
    const char* get_json_c_version();
};
