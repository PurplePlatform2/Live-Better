
# ============================================================================
# sanBot Makefile
# Builds:
#   Linux   -> liver
#   Windows -> liver.exe
# ============================================================================

# ---------------------------------------------------------------------------
# Target selection
#
# Examples:
#   make
#   make TARGET_OS=windows
# ---------------------------------------------------------------------------
TARGET_OS ?= linux

# ---------------------------------------------------------------------------
# Compiler setup
# ---------------------------------------------------------------------------
ifeq ($(TARGET_OS),windows)

CC       = x86_64-w64-mingw32-gcc
TARGET   = liver.exe

CFLAGS   = -Wall -O2 -Iinclude

LDFLAGS  = \
	-static \
	-lcurl \
	-lssl \
	-lcrypto \
	-lws2_32 \
	-lcrypt32 \
	-lbcrypt

else

CC       = gcc
TARGET   = liver

CFLAGS   = \
	-Wall \
	-O2 \
	-pthread \
	$(shell pkg-config --cflags libcurl libcjson 2>/dev/null) \
	-Iinclude

LDFLAGS  = \
	$(shell pkg-config --libs libcurl libcjson 2>/dev/null) \
	-lssl \
	-lcrypto \
	-luuid \
	-lpthread

endif

SRC = live.c

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
.PHONY: all clean install-deps install-mingw uthash linux windows

all: $(TARGET)

linux:
	$(MAKE) TARGET_OS=linux

windows:
	$(MAKE) TARGET_OS=windows

$(TARGET): $(SRC) include/uthash.h
	@echo "Building $(TARGET)..."
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

# ---------------------------------------------------------------------------
# Linux dependencies
# ---------------------------------------------------------------------------
install-deps:
	sudo apt-get update
	sudo apt-get install -y \
		build-essential \
		libcurl4-openssl-dev \
		libcjson-dev \
		uuid-dev \
		libssl-dev \
		wget
	$(MAKE) uthash

# ---------------------------------------------------------------------------
# Windows cross compiler
# ---------------------------------------------------------------------------
install-mingw:
	sudo apt-get update
	sudo apt-get install -y \
		mingw-w64 \
		gcc-mingw-w64-x86-64

# ---------------------------------------------------------------------------
# uthash
# ---------------------------------------------------------------------------
uthash: include/uthash.h

include/uthash.h:
	@echo "Downloading uthash..."
	mkdir -p include
	wget -q -O include/uthash.h \
	"https://raw.githubusercontent.com/troydhanson/uthash/master/src/uthash.h"
	@echo "uthash installed."

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
clean:
	rm -f liver liver.exe *.o
	rm -rf include
