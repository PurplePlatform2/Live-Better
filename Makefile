# ============================================================================
# Project
# ============================================================================
TARGET := liver
SRC    := live.c

# User-overridable defaults (use ?= so command line wins)
CC        ?= gcc
CFLAGS    ?= -O3 -Wall -Wextra -Iinclude
LDFLAGS   ?=
TARGET_FILE ?= $(TARGET)

# ============================================================================
# Platform detection
# ============================================================================
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

# ============================================================================
# Linux / gcc defaults (static by default)
# ============================================================================
ifeq ($(PLATFORM),linux)
  TARGET_FILE ?= $(TARGET)
  CFLAGS      += -pipe
  ifneq ($(STATIC),0)
    LDFLAGS   += #-static -s
  endif
  LDFLAGS     += -lcurl -lcjson -lssl -lcrypto -luuid -lpthread
endif

# ============================================================================
# Windows cross-compilation (from Linux)
# ============================================================================
ifeq ($(PLATFORM),windows)
  # Force the cross-compiler (ignores any CC from environment)
  CC           = x86_64-w64-mingw32-gcc
  TARGET_FILE ?= $(TARGET).exe
  CFLAGS      +=
  LDFLAGS     += -static -s \
                 -lcurl -lcjson -lssl -lcrypto \
                 -lws2_32 -lcrypt32 -lbcrypt

  # If we built local cJSON / other libs, add their paths
  MINGW_ROOT   := $(CURDIR)/mingw-root/usr/local
  ifneq ($(wildcard $(MINGW_ROOT)/include/cjson/cJSON.h),)
    CFLAGS     += -I$(MINGW_ROOT)/include
    LDFLAGS    += -L$(MINGW_ROOT)/lib
  endif
endif

# ============================================================================
# macOS
# ============================================================================
ifeq ($(PLATFORM),macos)
  CC          ?= clang
  TARGET_FILE ?= $(TARGET)
  LDFLAGS     += -lcurl -lcjson -lssl -lcrypto
endif

# ============================================================================
# Phony targets
# ============================================================================
.PHONY: all linux windows macos clean verify install-deps \
        install-deps-musl uthash musl musl-init \
        windows-init windows-deps

# ============================================================================
# Main build
# ============================================================================
all: $(TARGET_FILE)

$(TARGET_FILE): $(SRC) include/uthash.h
	@echo "Building $(TARGET_FILE) for $(PLATFORM) with CC=$(CC) (STATIC=$(if $(filter 0,$(STATIC)),off,on))..."
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)

# Platform shortcuts
linux:
	$(MAKE) PLATFORM=linux all

windows: windows-init
	$(MAKE) PLATFORM=windows OS=Windows_NT all

macos:
	$(MAKE) PLATFORM=macos all

# ============================================================================
# Musl build (Linux only) – auto‑installs everything
# ============================================================================
musl: musl-init
	@echo "Building with musl-gcc (fully static)..."
	$(MAKE) all CC=musl-gcc MUSL=1 PLATFORM=linux STATIC=$(or $(STATIC),1)

MUSL_STAMP := musl-root/.stamp

musl-init: $(MUSL_STAMP)

$(MUSL_STAMP):
	@echo "=== Setting up musl toolchain & static libraries ==="
	@# 1. Install musl-gcc (requires sudo once)
	@if ! command -v musl-gcc >/dev/null 2>&1; then \
		echo "Installing musl-tools (sudo) ..."; \
		sudo apt update && sudo apt install -y musl-tools; \
	fi
	@# 2. Prepare rootless Alpine root for static libs
	mkdir -p musl-root
	@# 2a. Download static apk (stable Alpine v3.21)
	@echo "Fetching Alpine apk-tools-static ..."
	@wget -q -O /tmp/apk-tools-static.apk \
		"http://dl-cdn.alpinelinux.org/alpine/v3.21/main/x86_64/apk-tools-static-2.14.4-r0.apk" \
		|| { echo "ERROR: Could not download apk-tools-static. Check your connection."; false; }
	@# 2b. Verify the download is a valid gzip archive
	@if ! gzip -t /tmp/apk-tools-static.apk 2>/dev/null; then \
		echo "ERROR: Downloaded apk file is corrupt."; false; \
	fi
	@tar -xzf /tmp/apk-tools-static.apk -C musl-root
	@rm -f /tmp/apk-tools-static.apk
	@ln -sf /musl-root/sbin/apk.static musl-root/sbin/apk
	@# 2c. Configure Alpine repositories
	mkdir -p musl-root/etc/apk
	@echo "http://dl-cdn.alpinelinux.org/alpine/v3.21/main" > musl-root/etc/apk/repositories
	@echo "http://dl-cdn.alpinelinux.org/alpine/v3.21/community" >> musl-root/etc/apk/repositories
	@# 2d. Install development packages (static libs)
	@echo "Installing static libraries (curl-dev, cjson-dev, openssl-dev, util-linux-dev) ..."
	@musl-root/sbin/apk.static --root musl-root --initdb add \
		curl-dev cjson-dev openssl-dev util-linux-dev
	@touch $(MUSL_STAMP)
	@echo "Musl environment ready (musl-root/)."

# Pass extra paths when MUSL=1
ifdef MUSL
  CFLAGS  += -I$(CURDIR)/musl-root/usr/include
  LDFLAGS += -L$(CURDIR)/musl-root/usr/lib -static -s
endif

# ============================================================================
# Windows cross‑compilation environment – auto‑installs everything
# ============================================================================
WINDOWS_STAMP := mingw-root/.stamp

windows-init: $(WINDOWS_STAMP)

$(WINDOWS_STAMP):
	@echo "=== Setting up Windows cross‑compilation environment ==="
	@# 1. Ensure mingw-w64 toolchain is present
	@if ! command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then \
		echo "ERROR: x86_64-w64-mingw32-gcc not found."; \
		echo "Install it with:"; \
		echo "  sudo apt update && sudo apt install -y mingw-w64"; \
		false; \
	fi
	@# 2. Install pre‑packaged mingw static libs (curl, openssl, zlib)
	@echo "Installing available mingw-w64 libraries..."
	@sudo apt update && sudo apt install -y \
		mingw-w64-x86-64-curl \
		mingw-w64-x86-64-openssl \
		mingw-w64-x86-64-zlib \
		2>/dev/null || echo "Some packages may not exist – will build missing ones locally."
	@# 3. Build cJSON for Windows because no Debian package exists
	@echo "Building cJSON for Windows..."
	mkdir -p mingw-root/usr/local/include/cjson
	mkdir -p mingw-root/usr/local/lib
	@# Download cJSON source (single file)
	@if [ ! -f mingw-root/cJSON.c ]; then \
		wget -q -O mingw-root/cJSON.c \
			https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.15/cJSON.c; \
		wget -q -O mingw-root/cJSON.h \
			https://raw.githubusercontent.com/DaveGamble/cJSON/v1.7.15/cJSON.h; \
	fi
	@# Compile static library with mingw
	@x86_64-w64-mingw32-gcc -c -O2 mingw-root/cJSON.c -o mingw-root/cJSON.o
	@x86_64-w64-mingw32-ar rcs mingw-root/usr/local/lib/libcjson.a mingw-root/cJSON.o
	@cp mingw-root/cJSON.h mingw-root/usr/local/include/cjson/cJSON.h
	@rm -f mingw-root/cJSON.o
	@touch $(WINDOWS_STAMP)
	@echo "Windows environment ready (mingw-root/)."

# ============================================================================
# uthash (single header) – downloaded if missing
# ============================================================================
uthash: include/uthash.h

include/uthash.h:
	@echo "Downloading uthash..."
	mkdir -p include
	wget -q -O include/uthash.h https://raw.githubusercontent.com/troydhanson/uthash/master/src/uthash.h
	@echo "uthash installed."

# ============================================================================
# Dependency helpers (for manual use)
# ============================================================================
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
	@echo "Run 'make musl' – it auto‑installs everything needed."

install-deps-windows:
	@echo "Run 'make windows' – it auto‑installs everything needed."

# ============================================================================
# Cleanup
# ============================================================================
clean:
	rm -f $(TARGET) $(TARGET).exe
	rm -rf include
	rm -rf musl-root
	rm -rf mingw-root

# ============================================================================
# Verification
# ============================================================================
verify:
	@echo "Binary information:"
	file $(TARGET_FILE)
	@echo "Dependencies:"
	-ldd $(TARGET_FILE) 2>/dev/null || echo "Not a dynamically linked ELF (or not Linux)"
