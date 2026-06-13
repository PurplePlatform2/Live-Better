# Makefile for betway_gt_bot (live.c -> liver)
# Handles dependency installation and compilation.

# ---------------------------------------------------------------------------
# Compiler and flags
# ---------------------------------------------------------------------------
CC       = gcc
CFLAGS   = -Wall -O2 -pthread $(shell pkg-config --cflags libcurl cjson 2>/dev/null) -Iinclude
LDFLAGS  = $(shell pkg-config --libs   libcurl cjson 2>/dev/null) -lssl -lcrypto -luuid -lpthread

TARGET   = liver
SRC      = live.c

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
.PHONY: all clean install-deps uthash

all: $(TARGET)

$(TARGET): $(SRC) include/uthash.h
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

# ---------------------------------------------------------------------------
# Dependencies installation (Debian / Ubuntu)
# ---------------------------------------------------------------------------
install-deps:
	@echo "Installing system packages (requires sudo)..."
	sudo apt-get update
	sudo apt-get install -y libcurl4-openssl-dev libcjson-dev uuid-dev libssl-dev
	$(MAKE) uthash

# ---------------------------------------------------------------------------
# uthash (header-only) local download if missing
# ---------------------------------------------------------------------------
uthash: include/uthash.h

include/uthash.h:
	@echo "Downloading uthash..."
	mkdir -p include
	wget -q -O include/uthash.h \
	  "https://raw.githubusercontent.com/troydhanson/uthash/master/src/uthash.h"
	@echo "uthash installed locally."

# ---------------------------------------------------------------------------
# Clean up
# ---------------------------------------------------------------------------
clean:
	rm -f $(TARGET) *.o
	rm -rf include
