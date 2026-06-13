============================================================================
sanBot Build System


Targets:
make -> Native platform build
make linux -> Linux build
make windows -> Windows build (.exe)
make macos -> macOS build


Author: Sanne Karibo
============================================================================

TARGET := liver
SRC := live.c

UNAME_S := $(shell uname -s)

============================================================================
Platform Detection
============================================================================

ifeq ($(OS),Windows_NT)

PLATFORM = windows

else ifeq ($(UNAME_S),Linux)

PLATFORM = linux

else ifeq ($(UNAME_S),Darwin)

PLATFORM = macos

else

$(error Unsupported platform)

endif

============================================================================
Linux (musl static)
============================================================================

ifeq ($(PLATFORM),linux)

CC = musl-gcc

TARGET_FILE = $(TARGET)

CFLAGS = \
    -O3 \
    -pipe \
    -Wall \
    -Wextra \
    -Iinclude

LDFLAGS = \
    -static \
    -s \
    -lcurl \
    -lcjson \
    -lssl \
    -lcrypto \
    -luuid \
    -lpthread

endif

============================================================================
Windows (MinGW)
============================================================================

ifeq ($(PLATFORM),windows)

CC = x86_64-w64-mingw32-gcc

TARGET_FILE = $(TARGET).exe

CFLAGS = \
    -O3 \
    -Wall \
    -Wextra \
    -Iinclude

LDFLAGS = \
    -static \
    -s \
    -lcurl \
    -lcjson \
    -lssl \
    -lcrypto \
    -lws2_32 \
    -lcrypt32 \
    -lbcrypt

endif

============================================================================
macOS
============================================================================

ifeq ($(PLATFORM),macos)

CC = clang

TARGET_FILE = $(TARGET)

CFLAGS = \
    -O3 \
    -Wall \
    -Wextra \
    -Iinclude

LDFLAGS = \
    -lcurl \
    -lcjson \
    -lssl \
    -lcrypto

endif

============================================================================
Build Rules
============================================================================

.PHONY: all linux windows macos clean verify install-deps uthash

all: $(TARGET_FILE)

$(TARGET_FILE): $(SRC) include/uthash.h
@echo "Building $(TARGET_FILE) for $(PLATFORM)..."
$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)

============================================================================
Explicit Builds
============================================================================

linux:
$(MAKE) PLATFORM=linux

windows:
$(MAKE) PLATFORM=windows OS=Windows_NT

macos:
$(MAKE) PLATFORM=macos

============================================================================
Verify Binary
============================================================================

verify:
@echo
@echo "Binary information:"
file $(TARGET_FILE)
@echo
@echo "Dependencies:"
-ldd $(TARGET_FILE)
@echo

============================================================================
Dependencies
============================================================================

install-deps:
ifeq ($(PLATFORM),linux)
sudo apt update
sudo apt install -y
build-essential
musl-tools
libcurl4-openssl-dev
libcjson-dev
libssl-dev
uuid-dev
wget
endif

ifeq ($(PLATFORM),macos)
brew install curl openssl cjson wget
endif

$(MAKE) uthash
============================================================================
uthash
============================================================================

uthash: include/uthash.h

include/uthash.h:
@echo "Downloading uthash..."
mkdir -p include
wget -q -O include/uthash.h
https://raw.githubusercontent.com/troydhanson/uthash/master/src/uthash.h
@echo "uthash installed."

============================================================================
Cleanup
============================================================================

clean:
rm -f liver liver.exe *.o
rm -rf include
