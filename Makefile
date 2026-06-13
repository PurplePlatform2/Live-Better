TARGET := liver
SRC := live.c

UNAME_S := $(shell uname -s)

ifeq ($(OS),Windows_NT)
PLATFORM = windows
else ifeq ($(UNAME_S),Linux)
PLATFORM = linux
else ifeq ($(UNAME_S),Darwin)
PLATFORM = macos
else
$(error Unsupported platform)
endif

ifeq ($(PLATFORM),linux)
CC = musl-gcc
TARGET_FILE = $(TARGET)

CFLAGS = -O3 -pipe -Wall -Wextra -Iinclude

LDFLAGS = -static -s \
    -lcurl -lcjson -lssl -lcrypto -luuid -lpthread
endif

ifeq ($(PLATFORM),windows)
CC = x86_64-w64-mingw32-gcc
TARGET_FILE = $(TARGET).exe

CFLAGS = -O3 -Wall -Wextra -Iinclude

LDFLAGS = -static -s \
    -lcurl -lcjson -lssl -lcrypto \
    -lws2_32 -lcrypt32 -lbcrypt
endif

ifeq ($(PLATFORM),macos)
CC = clang
TARGET_FILE = $(TARGET)

CFLAGS = -O3 -Wall -Wextra -Iinclude

LDFLAGS = -lcurl -lcjson -lssl -lcrypto
endif

.PHONY: all linux windows macos clean verify install-deps uthash

all: $(TARGET_FILE)

$(TARGET_FILE): $(SRC) include/uthash.h
	@echo "Building $(TARGET_FILE) for $(PLATFORM)..."
	$(CC) $(CFLAGS) $< -o $@ $(LDFLAGS)

linux:
	$(MAKE) PLATFORM=linux

windows:
	$(MAKE) PLATFORM=windows OS=Windows_NT

macos:
	$(MAKE) PLATFORM=macos

verify:
	@echo "Binary information:"
	file $(TARGET_FILE)
	@echo "Dependencies:"
	-ldd $(TARGET_FILE)

install-deps:
ifeq ($(PLATFORM),linux)
	sudo apt update
	sudo apt install -y build-essential musl-tools libcurl4-openssl-dev libcjson-dev libssl-dev uuid-dev wget
endif

ifeq ($(PLATFORM),macos)
	brew install curl openssl cjson wget
endif

uthash: include/uthash.h

include/uthash.h:
	@echo "Downloading uthash..."
	mkdir -p include
	wget -q -O include/uthash.h https://raw.githubusercontent.com/troydhanson/uthash/master/src/uthash.h
	@echo "uthash installed."

clean:
	rm -f liver liver.exe *.o
	rm -rf include
