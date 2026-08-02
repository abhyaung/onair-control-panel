.PHONY: all build app run doctor clean cert help

APP     := OnAir.app
# Signing identity. An UNSIGNED app cannot hold Accessibility for the processes
# it launches — macOS refuses to let it take TCC responsibility, so every write
# the agent attempted silently changed nothing while the code and the grants
# were both fine. A stable self-signed cert also means the grant survives
# rebuilds; ad-hoc signing does not, because its identity is the binary hash.
#   make cert   creates the identity if you do not have one
SIGN_ID := onair-dev
BUNDLE  := build/$(APP)
PY      := python3

help:
	@echo "onair"
	@echo "  make build   compile the media-key helper and the menu-bar app"
	@echo "  make app     package build/OnAir.app (drag to /Applications)"
	@echo "  make run     run the agent in this terminal"
	@echo "  make doctor  check permissions and dependencies"
	@echo "  make cert    create the self-signed signing identity (once)"
	@echo "  make clean   remove build artefacts"

all: build

# The media-key helper is compiled, never committed: a prebuilt binary
# downloaded from a release is quarantined by Gatekeeper, while one built here
# is not. Same reason the Swift app is built locally.
build: agent/bin/mediakey build/OnAir

agent/bin/mediakey: agent/bin/mediakey.m
	clang -O2 -framework Foundation -framework AppKit -o $@ $<

build/OnAir: macapp/OnAir.swift
	@mkdir -p build
	swiftc -O -o $@ $<

app: build
	@rm -rf $(BUNDLE)
	@mkdir -p $(BUNDLE)/Contents/MacOS $(BUNDLE)/Contents/Resources
	@cp build/OnAir $(BUNDLE)/Contents/MacOS/OnAir
	@cp -R agent $(BUNDLE)/Contents/Resources/
	@cp -R ../panel $(BUNDLE)/Contents/Resources/panel
	@cp ../protocol/config.default.json $(BUNDLE)/Contents/Resources/config.json
	@find $(BUNDLE)/Contents/Resources -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	@printf '%s\n' \
	  '<?xml version="1.0" encoding="UTF-8"?>' \
	  '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
	  '<plist version="1.0"><dict>' \
	  '  <key>CFBundleName</key><string>onair</string>' \
	  '  <key>CFBundleDisplayName</key><string>onair</string>' \
	  '  <key>CFBundleIdentifier</key><string>dev.onair.panel</string>' \
	  '  <key>CFBundleExecutable</key><string>OnAir</string>' \
	  '  <key>CFBundlePackageType</key><string>APPL</string>' \
	  '  <key>CFBundleShortVersionString</key><string>0.1.0</string>' \
	  '  <key>LSMinimumSystemVersion</key><string>13.0</string>' \
	  '  <key>LSUIElement</key><true/>' \
	  '  <key>NSAppleEventsUsageDescription</key>' \
	  '  <string>onair controls your meeting mic and camera through your browser.</string>' \
	  '  <key>NSLocalNetworkUsageDescription</key>' \
	  '  <string>onair serves the control panel to your iPad on your local network.</string>' \
	  '</dict></plist>' > $(BUNDLE)/Contents/Info.plist
	@security find-certificate -c "$(SIGN_ID)" >/dev/null 2>&1 || { \
	  echo "ERROR: no '$(SIGN_ID)' signing identity. Run: make cert"; \
	  echo "Unsigned builds cannot hold Accessibility for the agent."; \
	  exit 1; }
	@# Nested code first, then the bundle. NOT --deep: it is deprecated and
	@# produced a broken signature here ("code has no resources but signature
	@# indicates they must be present"), which silently fell back to ad-hoc.
	@codesign --force --sign "$(SIGN_ID)" $(BUNDLE)/Contents/Resources/agent/bin/mediakey
	@codesign --force --sign "$(SIGN_ID)" $(BUNDLE)/Contents/MacOS/OnAir
	@codesign --force --sign "$(SIGN_ID)" $(BUNDLE)
	@# Verify rather than assume. A failed signing that prints nothing is how
	@# this went unnoticed the first time.
	@codesign --verify --strict $(BUNDLE) || { echo "ERROR: signature invalid"; exit 1; }
	@codesign -dvv $(BUNDLE) 2>&1 | grep -q "Authority=$(SIGN_ID)" || { \
	  echo "ERROR: bundle is not signed by $(SIGN_ID)"; exit 1; }
	@echo "signed by $(SIGN_ID) and verified — Accessibility grant survives rebuilds"
	@echo "built $(BUNDLE)"

cert:
	@if security find-certificate -c "$(SIGN_ID)" >/dev/null 2>&1; then \
	  echo "identity '$(SIGN_ID)' already present"; \
	else \
	  scripts/make-cert.sh "$(SIGN_ID)"; \
	fi

run:
	$(PY) -m agent.server

doctor:
	@$(PY) -m agent.doctor

clean:
	rm -rf build agent/bin/mediakey
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
