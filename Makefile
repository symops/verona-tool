BINARY := verona-tool
PKG := ./cmd/verona-tool
CROSS_CC := aarch64-linux-gnu-gcc

# The set of shared libraries the target runtime (the rescue/recovery
# environment on both Monarch and Duo units) actually ships.
ALLOWED_LIBS := ld-linux-aarch64.so.1 libblkid.so.1 libc.so.6 libcap.so.2 libfdisk.so.1 libm.so.6 libmount.so.1 libreadline.so.8 libresolv.so.2 libsmartcols.so.1 libtinfow.so.6 libudev.so.1 libuuid.so.1

.PHONY: build build-arm64 verify-deps strip test clean

# Default build target: a normal dynamically-linked glibc binary for
# arm64, cross-compiled with the aarch64 C toolchain. In practice this
# only pulls in libc.so.6 (+ld-linux-aarch64.so.1 as the interpreter) --
# both are in the target's library set, so dynamic linking is fine. GPT
# and partition-table parsing stays pure-Go raw struct decoding (see
# pkg/verona/gpt.go); this never links against libfdisk/libblkid/libmount
# /libuuid or any other util-linux library, even though they happen to be
# present on the target.
build: build-arm64

build-arm64:
	CGO_ENABLED=1 CC=$(CROSS_CC) GOOS=linux GOARCH=arm64 go build -ldflags '-linkmode=external' -o $(BINARY)-arm64 $(PKG)

# Build for the host, for local testing against image files.
build-host:
	CGO_ENABLED=0 go build -o $(BINARY) $(PKG)

# Fail the build if the resulting arm64 binary needs any shared library
# outside the target runtime's known set.
verify-deps: build-arm64
	@needed=$$(readelf -d $(BINARY)-arm64 | awk -F'[][]' '/NEEDED/{print $$2}'); \
	for lib in $$needed; do \
		found=0; \
		for ok in $(ALLOWED_LIBS); do [ "$$lib" = "$$ok" ] && found=1; done; \
		if [ "$$found" = "0" ]; then echo "FAIL: $(BINARY)-arm64 needs $$lib, not in the target's library set"; exit 1; fi; \
	done; \
	echo "OK: $(BINARY)-arm64 needs only: $$needed"

strip: verify-deps
	aarch64-linux-gnu-strip $(BINARY)-arm64
	@ls -la $(BINARY)-arm64

test:
	go test ./...

clean:
	rm -f $(BINARY) $(BINARY)-arm64
