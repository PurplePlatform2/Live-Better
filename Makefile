# Project
TARGET   := liver
SRC      := live.c

# User-overridable defaults (use ?=)
CC        ?= gcc
CFLAGS    ?= -O3 -Wall -Wextra -Iinclude
LDFLAGS   ?=
TARGET_FILE ?= $(TARGET)

# Platform detection
UNAME_S := $(shell uname -s)
ifeq ($(OS),Windows_NT)
  PLATFORM := windows
else ifeq ($(UNAME_S),Linux)
  PLATFORM := linux
else ifeq ($(UNAME_S),Darwin)
  PLATFORM := macos
else
  $(error Unsupported platform)
endif

# --- Linux / gcc defaults (static by default) ---------------------------------
ifeq ($(PLATFORM),linux)
  TARGET_FILE ?= $(TARGET)
  CFLAGS      += -pipe

  # Static unless explicitly turned off
  ifneq ($(STATIC),0)
    LDFLAGS   += -static -s
  endif

  # Libraries (dynamic or static depending on STATIC)
  LDFLAGS     += -lcurl -lcjson -lssl -lcrypto -luuid -lpthread
endif

# --- Windows (cross from Linux) ------------------------------------------------
ifeq ($(PLATFORM),windows)
  CC          ?= x86_64-w64-mingw32-gcc
  TARGET_FILE ?= $(TARGET).exe
  LDFLAGS     += -static -s \
                 -lcurl -lcjson -lssl -lcrypto \
                 -lws2_32 -lcrypt32 -lbcrypt
endif

# --- macOS --------------------------------------------------------------------
ifeq ($(PLATFORM),macos)
  CC          ?= clang
  TARGET_FILE ?= $(TARGET)
  LDFLAGS     += -lcurl -lcjson -lssl -lcrypto
endif

# --- Phony targets ------------------------------------------------------------
.PHONY: all linux windows macos clean verify install-deps install-deps-musl uthash musl musl-init musl-deps

# --- Build --------------------------------------------------------------------
all: $(TARGET_FILE)

$(TARGET_FILE): $(SRC) include/uthash.h
	@echo "Building $(TARGET_FILE) for $(PLATFORM) with CC=$(CC) (STATIC=$(if $(filter 0,$(STATIC)),off,on))..."
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)

# Platform convenience
linux:
	$(MAKE) PLATFORM=linux all

windows:
	$(MAKE) PLATFORM=windows OS=Windows_NT all

macos:
	$(MAKE) PLATFORM=macos all

# --- Musl build (Linux only) --------------------------------------------------
# 'make musl' will set up the musl environment if needed and then build.
musl: musl-init
	@echo "Building with musl-gcc (full static)..."
	$(MAKE) all CC=musl-gcc MUSL=1 PLATFORM=linux STATIC=$(or $(STATIC),1)

# Musl environment initialisation (runs only once, tracked by stamp)
MUSL_STAMP := musl-root/.stamp

musl-init: $(MUSL_STAMP)

$(MUSL_STAMP):
	@echo "=== Setting up musl toolchain & static libraries ==="
	@# 1. Install musl-gcc (requires sudo)
	@if ! command -v musl-gcc >/dev/null 2>&1; then \
		echo "Installing musl-tools (sudo) ..."; \
		sudo apt update && sudo apt install -y musl-tools; \
	fi
	@# 2. Prepare a rootless Alpine root for static musl libraries
	mkdir -p musl-root
	@# 2a. Download static apk tool (no root required)
	@if [ ! -f musl-root/sbin/apk.static ]; then \
		echo "Fetching Alpine apk-tools-static ..."; \
		wget -q -O /tmp/apk-tools-static.apk \
			http://dl-cdn.alpinelinux.org/alpine/v3.19/main/x86_64/apk-tools-static-2.14.0-r2.apk; \
		tar -xzf /tmp/apk-tools-static.apk -C musl-root; \
		rm -f /tmp/apk-tools-static.apk; \
		ln -sf /musl-root/sbin/apk.static musl-root/sbin/apk; \
	fi
	@# 2b. Configure Alpine repositories
	@echo "http://dl-cdn.alpinelinux.org/alpine/v3.19/main" > musl-root/etc/apk/repositories
	@echo "http://dl-cdn.alpinelinux.org/alpine/v3.19/community" >> musl-root/etc/apk/repositories
	@# 2c. Install development packages with static libs
	@echo "Installing packages into musl-root (curl-dev, cjson-dev, openssl-dev, util-linux-dev) ..."
	@musl-root/sbin/apk.static --root musl-root --initdb add \
		curl-dev cjson-dev openssl-dev util-linux-dev
	@# 2d. Ensure static libraries are in the standard musl search path
	@# (musl-gcc uses its own sysroot, we'll point via CPATH/LIBRARY_PATH)
	@touch $(MUSL_STAMP)
	@echo "Musl environment ready (musl-root/)."

# Override build flags when MUSL=1
ifdef MUSL
  # Add musl-root to include/lib search
  CFLAGS  += -I$(CURDIR)/musl-root/usr/include
  LDFLAGS += -L$(CURDIR)/musl-root/usr/lib -static -s
  # UUID comes from util-linux in musl-root
  # Curl, cjson, ssl, crypto, uuid, pthread are all now inside musl-root/usr/lib
endif

# --- Uthash -------------------------------------------------------------------
uthash: include/uthash.h

include/uthash.h:
	@echo "Downloading uthash..."
	mkdir -p include
	wget -q -O include/uthash.h https://raw.githubusercontent.com/troydhanson/uthash/master/src/uthash.h
	@echo "uthash installed."

# --- Dependency install helpers (for glibc / manual) --------------------------
install-deps:
ifeq ($(PLATFORM),linux)
	sudo apt update
	sudo apt install -y build-essential libcurl4-openssl-dev \
	                    libcjson-dev libssl-dev uuid-dev wget
endif
ifeq ($(PLATFORM),macos)
	brew install curl openssl cjson wget
endif

install-deps-musl:
	@echo "Run 'make musl' to auto-install everything."
	@echo "Or manually: sudo apt install musl-tools, then set up static musl libs."

# --- Cleanup ------------------------------------------------------------------
clean:
	rm -f $(TARGET) $(TARGET).exe
	rm -rf include
	rm -rf musl-root   # remove entire musl root (can be rebuilt on next 'make musl')

# --- Verification -------------------------------------------------------------
verify:
	@echo "Binary information:"
	file $(TARGET_FILE)
	@echo "Dependencies:"
	-ldd $(TARGET_FILE) 2>/dev/null || echo "Not an ELF binary or fully static"
