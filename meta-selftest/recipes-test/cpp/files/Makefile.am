noinst_LIBRARIES = libautoexample.a
libautoexample_a_SOURCES = cpp-example-lib.cpp

bin_PROGRAMS = autoexample testautoexample

autoexample_SOURCES = cpp-example.cpp
autoexample_LDADD = libautoexample.a

testautoexample_SOURCES = test-cpp-example.cpp
testautoexample_LDADD = libautoexample.a
TESTS=testautoexample

AM_LDFLAGS = -ljson-c
